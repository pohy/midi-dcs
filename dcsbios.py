"""DCS-BIOS: [dcsbios.AIRCRAFT."DEVICE"] tables. CCs drive set_state commands over UDP; notes cycle switches of up
to 5 positions, with the switch state read back from the export stream onto the button LED of the same device."""
import json
import socket
import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TypedDict

import mido

from midi import MidiEvent, Source, Table, get, parse_source, toml, typed

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


class _Control(TypedDict, total=False):  # only description, positions and api_variant are really optional
    identifier: str
    api_variant: str  # "momentary_last_position" on push buttons
    description: str
    positions: list[str]  # names per set_state value, on some controls only
    inputs: list[_Input]
    outputs: list[_Output]


class DcsControl(NamedTuple):
    src: Source  # cc
    max_value: int  # set_state takes 0..max_value
    name: str  # json description, for the overlay
    positions: tuple[str, ...]  # position names, empty if the json has none


class DcsSwitch(NamedTuple):
    """Switch with positions 0 (bottom) ..max_value (top) stepped by a note, position read from the export."""
    src: Source  # note, also the LED
    max_value: int
    address: int
    mask: int
    shift_by: int
    name: str
    positions: tuple[str, ...]
    off: int | None = None  # 'hold' switch: position a long press goes to, None = cycles on every press
    dark: int | None = None  # position with the dark LED, None = off on hold switches, else second from the top
    momentary: bool = False  # push button: last position while the note is held, False = steps on every press
    down: Source | None = None  # rocker: note holding the bottom, src holds the top, both back to the middle


@dataclass(frozen=True)
class DcsConfig:
    knobs: dict[str, DcsControl]
    switches: dict[str, DcsSwitch]


def parse(cfg: Table, json_dir: Path) -> DcsConfig:
    """[dcsbios.AIRCRAFT."DEVICE"] IDENTIFIER = { cc = N } | { note = N, hold = true, off = OFF, dark = N,
    latch = true } (options optional) | { up = N, down = N }, checked against the aircraft json."""
    # ponytail: one aircraft; several tables need aircraft detection from the DCS-BIOS export stream
    tables = get(cfg, "dcsbios", dict, {}, "")
    if not tables:
        return DcsConfig({}, {})
    if len(tables) > 1:
        raise ValueError(f"only one [dcsbios.AIRCRAFT] table supported, got {', '.join(tables)}")
    [(aircraft, devices)] = tables.items()
    controls = _load_controls(json_dir, aircraft, f"[dcsbios.{aircraft}]")
    knobs: dict[str, DcsControl] = {}
    switches: dict[str, DcsSwitch] = {}
    for device, mappings in typed(devices, dict, f"[dcsbios.{aircraft}]").items():
        section = f"[dcsbios.{aircraft}.{toml(device)}]"
        for ident, src in typed(mappings, dict, section).items():
            where = f"{section} {ident}"
            c = controls.get(ident.upper())
            if c is None:
                raise ValueError(f"{section} unknown control '{ident}', look it up in Bort")
            mapped = knobs.get(c["identifier"]) or switches.get(c["identifier"])
            if mapped:
                raise ValueError(f"{where}: already mapped in [dcsbios.{aircraft}.{toml(mapped.src[0])}]")
            rocker = bool(typed(src, dict, where).keys() & {"up", "down"})
            down: Source | None = None
            if rocker:
                if src.keys() != {"up", "down"}:
                    raise ValueError(f"{where}: a rocker takes up = N and down = N only, got {toml(src)}")
                source = parse_source({"note": src["up"]}, device, f"{where} up")
                down = parse_source({"note": src["down"]}, device, f"{where} down")
            else:
                source = parse_source(src, device, where, ("hold", "off", "dark", "latch"))
            kind = source[1]
            name, positions = c.get("description", c["identifier"]), tuple(c.get("positions", ()))
            max_value = next((i.get("max_value") for i in c["inputs"] if i.get("interface") == "set_state"), None)
            if max_value is None:
                raise ValueError(f"{where} has no set_state input")
            if kind == "cc" and len(src) > 1:
                raise ValueError(f"{where}: hold, off, dark and latch are for note switches, got {toml(src)}")
            hold = get(src, "hold", bool, False, where)
            off = get(src, "off", int, None, where)
            dark = get(src, "dark", int, None, where)
            latch = get(src, "latch", bool, False, where)
            push = c.get("api_variant") == "momentary_last_position"
            if off is not None and not hold:
                raise ValueError(f"{where}: off needs hold = true")
            if latch and not push:
                raise ValueError(f"{where}: latch is for push buttons (momentary in the DCS-BIOS json)")
            if rocker and max_value != 2:
                raise ValueError(f"{where}: up and down need a 3-position switch")
            if hold:
                off = 1 if off is None else off  # middle
                if max_value != 2 or off not in (0, 1, 2):
                    raise ValueError(f"{where}: hold and off (0 bottom to 2, default 1) need a 3-position switch, "
                                     f"got {toml(src)}")
            if dark is not None and not 0 <= dark <= max_value:
                raise ValueError(f"{where}: dark takes a position from 0 (bottom) to {max_value}, got {dark}")
            if kind == "cc":
                knobs[c["identifier"]] = DcsControl(source, max_value, name, positions)
                continue
            out = next((o for o in c.get("outputs", []) if o["type"] == "integer"), None)
            if max_value >= len(PATTERNS) or out is None:
                raise ValueError(f"{where}: note cycles switches of up to {len(PATTERNS)} positions, use cc")
            switches[c["identifier"]] = DcsSwitch(source, max_value, out["address"], out["mask"], out["shift_by"],
                                                  name, positions, off, dark, rocker or push and not latch, down)
    return DcsConfig(knobs, switches)


def _load_controls(json_dir: Path, aircraft: str, section: str) -> dict[str, _Control]:
    """Aircraft json -> {IDENTIFIER: control}, upper-cased so the config can spell identifiers in any case."""
    try:
        doc: dict[str, dict[str, _Control]] = json.loads((json_dir / f"{aircraft}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"{section} no DCS-BIOS json for '{aircraft}' in {json_dir}")
    return {c["identifier"].upper(): c for cat in doc.values() for c in cat.values()}


def summary(conf: DcsConfig, device: str | None) -> str:
    """What device drives, for the startup line."""
    return " ".join([f"{ident}=cc:{c.src[2]}" for ident, c in conf.knobs.items() if c.src[0] == device]
                    + [f"{ident}=note:{s.src[2]}" + (f"/{s.down[2]}" if s.down else "")
                       for ident, s in conf.switches.items() if s.src[0] == device])


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


def button_leds(state: int | None, sw: DcsSwitch, clock: float) -> list[tuple[Source, bool]]:
    """(note, LED on) for each button of the switch; a rocker side is lit while DCS holds the switch there."""
    if sw.down is None:
        return [(sw.src, led_on(state, sw, clock))]
    return [(sw.src, state == sw.max_value), (sw.down, state == 0)]


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
    """Plain UDP, so commands go out straight from the MIDI threads."""

    def __init__(self, controls: dict[str, DcsControl], click: Callable[[], None] = lambda: None,
                 show: Callable[[str], None] = lambda text: None) -> None:
        self.controls = controls
        self.click = click  # on every new position of a switch, not of a dial
        self.show = show  # overlay text on every command sent
        self.sent: dict[str, int] = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def on_midi(self, ev: MidiEvent) -> None:
        for ident, c in self.controls.items():
            if ev.src != c.src:
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
    """Presses queue up on the MIDI threads; commands, export, LEDs and overlay run in tick() on the main thread."""

    def __init__(self, switches: dict[str, DcsSwitch], leds: dict[str, tuple[mido.ports.BaseOutput, int]],
                 export: socket.socket, hold_s: float, show: Callable[[str], None] = lambda text: None,
                 click: Callable[[], None] = lambda: None) -> None:
        self.switches = switches
        self.hold_s = hold_s  # long press on hold switches
        self.show = show  # overlay text on (dis)connects and on positions DCS reports right after a press
        self.click = click  # on every press, also when DCS then refuses the move
        self.leds = leds  # device -> (MIDI output, 0-based channel the LED note_on goes out on)
        self.export = export
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.mem = bytearray(0x10000)
        self.state: dict[str, int | None] = dict.fromkeys(switches)  # None until the first export packet
        self.led_sent: dict[Source, bool] = {}
        self.up = dict.fromkeys(switches, True)  # travel direction of the next press
        # (ident, pressed, rocker down note), thread-safe append/popleft
        self.notes: deque[tuple[str, bool, bool]] = deque()
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
            # releases only matter on hold switches, push buttons and rockers
            if ev.src == sw.src and (ev.pressed or sw.off is not None or sw.momentary):
                self.notes.append((ident, ev.pressed, False))
            elif ev.src == sw.down:
                self.notes.append((ident, ev.pressed, True))

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

    def _push(self, ident: str, pressed: bool, down: bool) -> None:
        """Push button or rocker: held in the cockpit while the note is, the top (rocker down note: the bottom),
        back to the position below the top on release. No overlay, the LED shows it."""
        # ponytail: a tap shorter than a DCS frame sends press and release in one frame and some buttons
        # miss it; hold the release until DCS reports the press if short taps get lost
        # ponytail: any rocker release centers, also with the other side still held
        sw = self.switches[ident]
        position = (0 if down else sw.max_value) if pressed else sw.max_value - 1
        self.sock.sendto(f"{ident} {position}\n".encode(), ADDR)
        if pressed:
            self.click()

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
            # ponytail: no check that the aircraft matches the config table, another module shows
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
            ident, pressed, down = self.notes.popleft()
            if self.switches[ident].momentary:
                self._push(ident, pressed, down)
            elif self.switches[ident].off is None:
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
            for src, on in button_leds(self.state[ident], sw, self.clock):
                if on == self.led_sent.get(src):
                    continue
                self.led_sent[src] = on
                self._led(src, on)

    def _led(self, src: Source, on: bool) -> None:
        device, _, note = src
        out, channel = self.leds[device]
        out.send(mido.Message("note_on", channel=channel, note=note, velocity=127 if on else 0))

    def close(self) -> None:
        for src in self.led_sent:
            self._led(src, False)
        for out, _ in self.leds.values():
            out.close()
        self.export.close()
        self.sock.close()
