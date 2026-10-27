"""DCS-BIOS: [dcsbios:AIRCRAFT] section. CCs drive set_state commands over UDP; notes cycle switches of up
to 5 positions, with the switch state read back from the export stream onto the button LED."""
import json
import socket
import struct
from collections import deque
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TypedDict

import mido

from midi import MidiEvent, parse_source

# ponytail: default Saved Games path, add a config key if DCS lives elsewhere
JSON_DIR = Path.home() / "Saved Games/DCS/Scripts/DCS-BIOS/doc/json"
ADDR = ("127.0.0.1", 7778)  # DCS-BIOS UDP import port
EXPORT = ("239.255.50.10", 5010)  # DCS-BIOS multicast export, udp_config in BIOSConfig.lua
SYNC = b"\x55\x55\x55\x55"  # starts the first packet of every export frame
ACFT_NAME = slice(0, 24)  # MetadataStart _ACFT_NAME, null-terminated
NO_AIRCRAFT = (b"", b"NONE")  # "" sent once at mission end, NONE while spectating or dead
EXPORT_TIMEOUT_S = 3.0  # export silent this long = DCS gone
# overlay shows switch changes this long after a press, or the unchanged position if DCS didn't move it
# ponytail: guessed round trip + switch animation, raise it if slow switches show the old position
WATCH_S = 1.0
# hold switch step DCS doesn't report this long = dropped, sent again until WATCH_S ends the travel
# ponytail: guessed, a normal answer takes ~2 export frames; raise it if low frame rates resend every step
STEP_S = 0.1
LED_STEP_S = 0.125  # time per pattern character
# LED per switch position, counted down from the top: solid, dark, slow blink, fast blink, double flash.
# Top on and the one below dark gives ON/OFF on 2-position switches and a dark OFF in the middle of 3-position ones.
PATTERNS = ("11111111", "00000000", "11110000", "10101010", "10100000")
# set_state above this is a dial (65535): no knob clicks, those would come on every CC step, and the
# overlay shows percent. Switches have 2+ CC steps per position up to here
SWITCH_MAX_VALUE = 63


class _Input(TypedDict, total=False):  # py3.10 has no NotRequired; only max_value is really optional
    interface: str
    max_value: int


class _Output(TypedDict):  # mask and shift_by only on type "integer"
    type: str
    address: int
    mask: int
    shift_by: int


class _Control(TypedDict, total=False):  # only description and positions are really optional
    identifier: str
    description: str
    positions: list[str]  # names per set_state value, on some controls only
    inputs: list[_Input]
    outputs: list[_Output]


class DcsControl(NamedTuple):
    cc: int
    max_value: int  # set_state takes 0..max_value
    name: str  # json description, for the overlay
    positions: tuple[str, ...]  # position names, empty if the json has none


class DcsSwitch(NamedTuple):
    """Switch with positions 0 (bottom) ..max_value (top) stepped by a note, position read from the export."""
    note: int
    max_value: int
    address: int
    mask: int
    shift_by: int
    name: str
    positions: tuple[str, ...]
    off: int | None = None  # 'hold' switch: position a long press goes to, None = cycles on every press
    dark: int | None = None  # position with the dark LED, None = off on hold switches, else second from the top


@dataclass(frozen=True)
class DcsConfig:
    knobs: dict[str, DcsControl]
    switches: dict[str, DcsSwitch]


def parse(cfg: ConfigParser, json_dir: Path) -> DcsConfig:
    """[dcsbios:AIRCRAFT] IDENTIFIER = cc:N | note:N [hold [off:OFF]] [dark:N] in any order, checked against the
    aircraft json."""
    # ponytail: one aircraft; several sections need aircraft detection from the DCS-BIOS export stream
    sections = [s for s in cfg.sections() if s.startswith("dcsbios:")]
    if not sections:
        return DcsConfig({}, {})
    if len(sections) > 1:
        raise ValueError(f"only one [dcsbios:AIRCRAFT] section supported, got {', '.join(sections)}")
    section = sections[0]
    controls = _load_controls(json_dir, section)
    knobs: dict[str, DcsControl] = {}
    switches: dict[str, DcsSwitch] = {}
    for ident, src in cfg.items(section):
        where = f"[{section}] {ident}"
        c = controls.get(ident.upper())
        if c is None:
            raise ValueError(f"[{section}] unknown control '{ident}', look it up in Bort")
        # arg[:value] in any order: one source, the rest options
        args = src.split()
        sources = [a for a in args if a.lower().startswith(("note:", "cc:"))]
        opts = [a for a in args if a not in sources]
        if len(sources) != 1:
            raise ValueError(f"{where} needs one 'note:N' or 'cc:N', got '{src}'")
        kind, num = parse_source(sources[0], where)
        name, positions = c.get("description", c["identifier"]), tuple(c.get("positions", ()))
        max_value = next((i.get("max_value") for i in c["inputs"] if i.get("interface") == "set_state"), None)
        if max_value is None:
            raise ValueError(f"{where} has no set_state input")
        pairs = [o.lower().partition(":") for o in opts]
        named = {k: v for k, _, v in pairs}
        if len(named) != len(pairs) or named.keys() - {"hold", "off", "dark"} or (named and kind == "cc"):
            raise ValueError(f"{where}: options are 'hold', 'off:OFF' and 'dark:N' on 'note:N' switches, "
                             f"got '{' '.join(opts)}'")
        off = dark = None
        if "hold" in named or "off" in named:
            off_src = named.get("off", "1")  # middle
            if named.get("hold") != "" or max_value != 2 or off_src not in ("0", "1", "2"):
                raise ValueError(f"{where}: 'hold' and 'off:OFF' (0 bottom to 2, default 1) need a 3-position "
                                 f"switch, got '{' '.join(opts)}'")
            off = int(off_src)
        if "dark" in named:
            if named["dark"] not in [str(p) for p in range(max_value + 1)]:
                raise ValueError(f"{where}: 'dark:N' takes a position from 0 (bottom) to {max_value}, "
                                 f"got 'dark:{named['dark']}'")
            dark = int(named["dark"])
        if kind == "cc":
            knobs[c["identifier"]] = DcsControl(num, max_value, name, positions)
            continue
        out = next((o for o in c.get("outputs", []) if o["type"] == "integer"), None)
        if max_value >= len(PATTERNS) or out is None:
            raise ValueError(f"{where}: 'note:N' cycles switches of up to {len(PATTERNS)} positions, use 'cc:N'")
        switches[c["identifier"]] = DcsSwitch(num, max_value, out["address"], out["mask"], out["shift_by"],
                                              name, positions, off, dark)
    return DcsConfig(knobs, switches)


def _load_controls(json_dir: Path, section: str) -> dict[str, _Control]:
    """Aircraft json -> {IDENTIFIER: control}, upper-cased because configparser lower-cases keys."""
    aircraft = section.partition(":")[2]
    try:
        doc: dict[str, dict[str, _Control]] = json.loads((json_dir / f"{aircraft}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"[{section}] no DCS-BIOS json for '{aircraft}' in {json_dir}")
    return {c["identifier"].upper(): c for cat in doc.values() for c in cat.values()}


def summary(conf: DcsConfig) -> str:
    return " ".join([f"{ident}=cc:{c.cc}" for ident, c in conf.knobs.items()]
                    + [f"{ident}=note:{s.note}" for ident, s in conf.switches.items()])


def cc_to_state(value: int, max_value: int) -> int:
    """MIDI 0..127 -> DCS-BIOS set_state 0..max_value in equal zones; 127 hits max_value exactly."""
    return min(value * (max_value + 1) // 127, max_value)


def label(c: DcsControl | DcsSwitch, state: int) -> str:
    """Overlay text: control name and position name, or position number, or percent on dials."""
    if len(c.positions) == c.max_value + 1:
        value = c.positions[state]
    elif c.max_value > SWITCH_MAX_VALUE:
        value = f"{state * 100 // c.max_value}%"
    else:
        value = f"{state}/{c.max_value}"
    return f"{c.name}  {value}"


def apply_export(mem: bytearray, pkt: bytes) -> None:
    """Write an export packet's [address u16][length u16][data] blocks into the 64K memory image."""
    i = len(SYNC) if pkt.startswith(SYNC) else 0
    while i + 4 <= len(pkt):
        addr, n = struct.unpack_from("<HH", pkt, i)
        data = pkt[i + 4:i + 4 + n]
        if len(data) != n or addr + n > len(mem):
            return  # malformed, drop the rest instead of resizing mem
        mem[addr:addr + n] = data
        i += 4 + n


def aircraft_name(mem: bytearray) -> bytes:
    return bytes(mem[ACFT_NAME]).partition(b"\0")[0]


def switch_state(mem: bytearray, sw: DcsSwitch) -> int:
    return (int.from_bytes(mem[sw.address:sw.address + 2], "little") & sw.mask) >> sw.shift_by


def led_on(state: int | None, sw: DcsSwitch, clock: float) -> bool:
    """LED pattern for the position at clock; dark while not yet known or out of range (another aircraft).
    The dark position (default: a hold switch's off) takes the dark pattern, wherever it sits."""
    if state is None or state > sw.max_value:
        return False
    order = list(range(sw.max_value, -1, -1))  # top first
    dark = sw.off if sw.dark is None else sw.dark
    if dark is not None:
        order.remove(dark)
        order.insert(1, dark)
    pattern = PATTERNS[order.index(state)]
    return pattern[int(clock / LED_STEP_S) % len(pattern)] == "1"


def listen() -> socket.socket:
    """Join the export multicast group, non-blocking so tick() can drain it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Bort and other panels listen on the same port
    sock.bind(("", EXPORT[1]))
    # ponytail: default interface; join on 127.0.0.1 instead if several NICs and no data arrives
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(EXPORT[0]) + socket.inet_aton("0.0.0.0"))
    sock.setblocking(False)
    return sock


class DcsBios:
    """Plain UDP, so commands go out straight from the MIDI thread."""

    def __init__(self, controls: dict[str, DcsControl], click: Callable[[], None] = lambda: None,
                 show: Callable[[str], None] = lambda text: None) -> None:
        self.controls = controls
        self.click = click  # on every new position of a switch, not of a dial
        self.show = show  # overlay text on every command sent
        self.sent: dict[str, int] = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def on_midi(self, ev: MidiEvent) -> None:
        for ident, c in self.controls.items():
            if ev.src != ("cc", c.cc):
                continue
            state = cc_to_state(ev.value, c.max_value)
            # ponytail: dedup per zone, so after moving the switch with the mouse
            # the knob only resyncs once it enters another zone
            if self.sent.get(ident) == state:
                continue
            self.sent[ident] = state
            self.sock.sendto(f"{ident} {state}\n".encode(), ADDR)
            # both also on the first move after start, even if DCS already sits in that position
            self.show(label(c, state))
            if c.max_value <= SWITCH_MAX_VALUE:
                self.click()

    def tick(self, dt: float) -> None:
        """Commands go out from on_midi, nothing to do per tick."""

    def close(self) -> None:
        self.sock.close()


class DcsSwitches:
    """Presses queue up on the MIDI thread; commands, export, LEDs and overlay run in tick() on the main thread."""

    def __init__(self, switches: dict[str, DcsSwitch], leds: mido.ports.BaseOutput, channel: int,
                 export: socket.socket, hold_s: float, show: Callable[[str], None] = lambda text: None,
                 click: Callable[[], None] = lambda: None) -> None:
        self.switches = switches
        self.hold_s = hold_s  # long press on hold switches
        self.show = show  # overlay text on (dis)connects and on positions DCS reports right after a press
        self.click = click  # on every press, also when DCS then refuses the move
        self.leds = leds
        self.channel = channel  # 0-based, LED note_on goes out on it
        self.export = export
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.mem = bytearray(0x10000)
        self.state: dict[str, int | None] = dict.fromkeys(switches)  # None until the first export packet
        self.led_sent: dict[str, bool | None] = dict.fromkeys(switches)
        self.up = dict.fromkeys(switches, True)  # travel direction of the next press
        self.notes: deque[tuple[str, bool]] = deque()  # (ident, pressed), thread-safe append/popleft
        self.held: dict[str, float] = {}  # hold switches down, clock at the press
        # hold switch moving: (target, last position sent, clock when sent)
        self.travel: dict[str, tuple[int, int, float]] = {}
        self.watch: dict[str, float] = {}  # clock until which DCS's answer to a press shows
        self.answered: set[str] = set()  # watched switches that changed since the press
        self.clock = 0.0
        self.quiet = 0.0  # seconds since the last export packet
        self.aircraft = b""  # name while in a cockpit, b"" = disconnected

    def on_midi(self, ev: MidiEvent) -> None:
        for ident, sw in self.switches.items():
            # releases only matter on hold switches
            if ev.src == ("note", sw.note) and (ev.pressed or sw.off is not None):
                self.notes.append((ident, ev.pressed))

    def _press(self, ident: str) -> None:
        """One position along the travel, turning around at the ends (3-position: mid, up, mid, down, mid).
        Never jumps over a position: DCS moves some switches one notch per command (F-16 AP_PITCH_SW)."""
        # stepped from what DCS reports, so a switch the sim dropped (autopilot disengage) goes on from there
        # ponytail: a second press before DCS echoes the first (~50 ms) steps from the stale state and
        # resends the same position; track the last sent state with a timeout if fast double presses get lost
        sw, state = self.switches[ident], self.state[ident]
        # not yet known counts as the dark position below the top, so the first press goes to the top
        pos = sw.max_value - 1 if state is None else state
        if pos in (0, sw.max_value):
            self.up[ident] = pos == 0
        step = pos + 1 if self.up[ident] else pos - 1
        self.sock.sendto(f"{ident} {step}\n".encode(), ADDR)
        self.click()
        self.watch[ident] = self.clock + WATCH_S
        self.answered.discard(ident)

    def _toggle(self, ident: str, long: bool) -> None:
        """Hold switch: long press to off, short press to the upper of the other two positions, or the lower
        one when already there. Aims from the target of a travel still under way."""
        sw, state = self.switches[ident], self.state[ident]
        assert sw.off is not None
        lower, upper = [p for p in range(sw.max_value + 1) if p != sw.off]
        pos = self.travel[ident][0] if ident in self.travel else state
        target = sw.off if long else lower if pos == upper else upper
        if target == pos:
            self.show(label(sw, target))  # long press, already off
            return
        self.click()
        self.watch[ident] = self.clock + WATCH_S
        self.answered.discard(ident)
        if state is None:
            self.sock.sendto(f"{ident} {target}\n".encode(), ADDR)  # DCS not reporting, nothing to step along
            return
        # a step still in flight is waited for, stepping on from the stale state would end the travel early
        # or skip the middle
        _, sent, since = self.travel.get(ident, (target, state, self.clock))
        self.travel[ident] = (target, sent, since)
        self._travel(ident)

    def _travel(self, ident: str) -> None:
        """One position per command like a real toggle passing the middle, the next once DCS reports the last.
        A step not reported within STEP_S counts as dropped and goes again from the reported position."""
        target, sent, since = self.travel[ident]
        state = self.state[ident]
        if state is not None and state != sent and self.clock - since < STEP_S:
            return  # in flight
        if state is None or state == target:
            del self.travel[ident]
            return
        step = state + 1 if target > state else state - 1
        self.sock.sendto(f"{ident} {step}\n".encode(), ADDR)
        self.travel[ident] = (target, step, self.clock)

    def tick(self, dt: float) -> None:
        self.clock += dt
        fresh = False
        while True:
            try:
                pkt = self.export.recv(0x10000)
            except BlockingIOError:
                break
            apply_export(self.mem, pkt)
            fresh = True
        if fresh:
            self.quiet = 0.0
        else:
            self.quiet += dt
            if self.quiet >= EXPORT_TIMEOUT_S and aircraft_name(self.mem):
                self.mem[:] = bytes(len(self.mem))  # DCS closed or crashed without the mission end frame
                fresh = True
        if fresh:
            # no cockpit: positions unknown, LEDs dark. Also dark when joining mid-mission until
            # DCS-BIOS resends the unchanged name
            # ponytail: no check that the aircraft matches the section, another module shows
            # whatever lives at these addresses; needs the module's aircraft list from its Lua file
            name = aircraft_name(self.mem)
            live = name not in NO_AIRCRAFT
            aircraft = name if live else b""
            if aircraft != self.aircraft:
                self.aircraft = aircraft
                self.show(f"DCS-BIOS connected  {aircraft.decode(errors='replace')}" if aircraft
                          else "DCS-BIOS disconnected")
            for ident, sw in self.switches.items():
                state = switch_state(self.mem, sw) if live else None
                # every change while watched, so a switch that springs back (autopilot refused) shows both
                if state is not None and state != self.state[ident] and ident in self.watch:
                    self.answered.add(ident)
                    self.show(label(sw, state))
                self.state[ident] = state
        while self.notes:
            ident, pressed = self.notes.popleft()
            if self.switches[ident].off is None:
                self._press(ident)
            elif pressed:
                self.held[ident] = self.clock
            elif self.held.pop(ident, None) is not None:  # released before hold_s
                self._toggle(ident, long=False)
        for ident in [i for i, since in self.held.items() if self.clock - since >= self.hold_s]:
            del self.held[ident]  # fires while still held, the release is ignored
            self._toggle(ident, long=True)
        for ident in [i for i, until in self.watch.items() if self.clock >= until]:
            del self.watch[ident]
            # DCS refused a step: stop, or a later move with the mouse would resume the travel
            self.travel.pop(ident, None)
            state = self.state[ident]
            if ident not in self.answered and state is not None:
                self.show(label(self.switches[ident], state))  # DCS didn't move it
        for ident in list(self.travel):
            self._travel(ident)
        for ident, sw in self.switches.items():
            on = led_on(self.state[ident], sw, self.clock)
            if on == self.led_sent[ident]:
                continue
            self.led_sent[ident] = on
            self._led(sw.note, on)

    def _led(self, note: int, on: bool) -> None:
        self.leds.send(mido.Message("note_on", channel=self.channel, note=note, velocity=127 if on else 0))

    def close(self) -> None:
        for sw in self.switches.values():
            self._led(sw.note, False)
        self.leds.close()
        self.export.close()
        self.sock.close()
