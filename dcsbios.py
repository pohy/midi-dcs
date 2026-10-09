"""DCS-BIOS output: [dcsbios:AIRCRAFT] section, MIDI CCs drive set_state commands over UDP."""
import json
import socket
from configparser import ConfigParser
from pathlib import Path
from typing import NamedTuple, TypedDict

from midi import MidiEvent, parse_source

# ponytail: default Saved Games path, add a config key if DCS lives elsewhere
JSON_DIR = Path.home() / "Saved Games/DCS/Scripts/DCS-BIOS/doc/json"
ADDR = ("127.0.0.1", 7778)  # DCS-BIOS UDP import port


class _Input(TypedDict, total=False):  # py3.10 has no NotRequired; only max_value is really optional
    interface: str
    max_value: int


class _Control(TypedDict):
    identifier: str
    inputs: list[_Input]


class DcsControl(NamedTuple):
    cc: int
    max_value: int  # set_state takes 0..max_value


def parse(cfg: ConfigParser, json_dir: Path) -> dict[str, DcsControl]:
    """[dcsbios:AIRCRAFT] IDENTIFIER = cc N -> {IDENTIFIER: DcsControl}, checked against the aircraft json."""
    # ponytail: one aircraft; several sections need aircraft detection from the DCS-BIOS export stream
    sections = [s for s in cfg.sections() if s.startswith("dcsbios:")]
    if not sections:
        return {}
    if len(sections) > 1:
        raise ValueError(f"only one [dcsbios:AIRCRAFT] section supported, got {', '.join(sections)}")
    section = sections[0]
    controls = _load_controls(json_dir, section)
    mapping: dict[str, DcsControl] = {}
    for ident, src in cfg.items(section):
        c = controls.get(ident.upper())
        if c is None:
            raise ValueError(f"[{section}] unknown control '{ident}', look it up in Bort")
        kind, cc = parse_source(src, f"[{section}] {ident}")
        if kind != "cc":
            raise ValueError(f"[{section}] {ident}: only 'cc N' supported, got '{src}'")
        max_value = next((i.get("max_value") for i in c["inputs"] if i.get("interface") == "set_state"), None)
        if max_value is None:
            raise ValueError(f"[{section}] {ident} has no set_state input, can't follow a cc")
        mapping[c["identifier"]] = DcsControl(cc, max_value)
    return mapping


def _load_controls(json_dir: Path, section: str) -> dict[str, _Control]:
    """Aircraft json -> {IDENTIFIER: control}, upper-cased because configparser lower-cases keys."""
    aircraft = section.partition(":")[2]
    try:
        doc: dict[str, dict[str, _Control]] = json.loads((json_dir / f"{aircraft}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"[{section}] no DCS-BIOS json for '{aircraft}' in {json_dir}")
    return {c["identifier"].upper(): c for cat in doc.values() for c in cat.values()}


def summary(controls: dict[str, DcsControl]) -> str:
    return " ".join(f"{ident}=cc{c.cc}" for ident, c in controls.items())


def cc_to_state(value: int, max_value: int) -> int:
    """MIDI 0..127 -> DCS-BIOS set_state 0..max_value in equal zones; 127 hits max_value exactly."""
    return min(value * (max_value + 1) // 127, max_value)


class DcsBios:
    """Plain UDP, so commands go out straight from the MIDI thread."""

    def __init__(self, controls: dict[str, DcsControl]) -> None:
        self.controls = controls
        self.sent: dict[str, int] = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def on_midi(self, ev: MidiEvent) -> None:
        for ident, (cc, max_value) in self.controls.items():
            if ev.src != ("cc", cc):
                continue
            state = cc_to_state(ev.value, max_value)
            # ponytail: dedup per zone, so after moving the switch with the mouse
            # the knob only resyncs once it enters another zone
            if self.sent.get(ident) == state:
                continue
            self.sent[ident] = state
            self.sock.sendto(f"{ident} {state}\n".encode(), ADDR)

    def tick(self, dt: float) -> None:
        """Commands go out from on_midi, nothing to do per tick."""

    def close(self) -> None:
        self.sock.close()
