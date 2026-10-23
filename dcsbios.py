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


@dataclass(frozen=True)
class DcsConfig:
    knobs: dict[str, DcsControl]
    switches: dict[str, DcsSwitch]


def parse(cfg: ConfigParser, json_dir: Path) -> DcsConfig:
    """[dcsbios:AIRCRAFT] IDENTIFIER = cc N | note N, checked against the aircraft json."""
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
        kind, num = parse_source(src, where)
        name, positions = c.get("description", c["identifier"]), tuple(c.get("positions", ()))
        max_value = next((i.get("max_value") for i in c["inputs"] if i.get("interface") == "set_state"), None)
        if max_value is None:
            raise ValueError(f"{where} has no set_state input")
        if kind == "cc":
            knobs[c["identifier"]] = DcsControl(num, max_value, name, positions)
            continue
        out = next((o for o in c.get("outputs", []) if o["type"] == "integer"), None)
        if max_value >= len(PATTERNS) or out is None:
            raise ValueError(f"{where}: 'note N' cycles switches of up to {len(PATTERNS)} positions, use 'cc N'")
        switches[c["identifier"]] = DcsSwitch(num, max_value, out["address"], out["mask"], out["shift_by"],
                                              name, positions)
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
    return " ".join([f"{ident}=cc{c.cc}" for ident, c in conf.knobs.items()]
                    + [f"{ident}=note{s.note}" for ident, s in conf.switches.items()])


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


def led_on(state: int | None, max_value: int, clock: float) -> bool:
    """LED pattern for the position at clock; dark while not yet known or out of range (another aircraft)."""
    if state is None or state > max_value:
        return False
    pattern = PATTERNS[max_value - state]
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
                 export: socket.socket, show: Callable[[str], None] = lambda text: None) -> None:
        self.switches = switches
        self.show = show  # overlay text on (dis)connects and on positions DCS reports right after a press
        self.leds = leds
        self.channel = channel  # 0-based, LED note_on goes out on it
        self.export = export
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.mem = bytearray(0x10000)
        self.state: dict[str, int | None] = dict.fromkeys(switches)  # None until the first export packet
        self.led_sent: dict[str, bool | None] = dict.fromkeys(switches)
        self.up = dict.fromkeys(switches, True)  # travel direction of the next press
        self.presses: deque[str] = deque()  # thread-safe append/popleft
        self.watch: dict[str, float] = {}  # clock until which DCS's answer to a press shows
        self.answered: set[str] = set()  # watched switches that changed since the press
        self.clock = 0.0
        self.quiet = 0.0  # seconds since the last export packet
        self.aircraft = b""  # name while in a cockpit, b"" = disconnected

    def on_midi(self, ev: MidiEvent) -> None:
        for ident, sw in self.switches.items():
            if ev.src == ("note", sw.note) and ev.pressed:
                self.presses.append(ident)

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
        self.watch[ident] = self.clock + WATCH_S
        self.answered.discard(ident)

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
        while self.presses:
            self._press(self.presses.popleft())
        for ident in [i for i, until in self.watch.items() if self.clock >= until]:
            del self.watch[ident]
            state = self.state[ident]
            if ident not in self.answered and state is not None:
                self.show(label(self.switches[ident], state))  # DCS didn't move it
        for ident, sw in self.switches.items():
            on = led_on(self.state[ident], sw.max_value, self.clock)
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
