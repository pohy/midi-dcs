"""MIDI input: [midi] section, message decoding, monitor lines, port lookup."""
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Literal, NamedTuple, TypeAlias, cast

import mido

Kind: TypeAlias = Literal["note", "cc"]
Source: TypeAlias = tuple[Kind, int]  # ("note", 22) / ("cc", 62), as written in the config


class MidiEvent(NamedTuple):
    src: Source
    value: int  # cc value or note velocity, 0..127
    pressed: bool  # note on with velocity > 0, or cc >= 64


@dataclass(frozen=True)
class MidiConfig:
    port: str | None  # port name or part of it, None = system default
    channel: int | None  # 1-16, None = any
    hold_s: float  # held at least this long = long press, on DCS-BIOS hold switches


def parse(cfg: ConfigParser) -> MidiConfig:
    channel = cfg.getint("midi", "channel", fallback=None)
    hold_s = cfg.getfloat("midi", "hold_s", fallback=0.5)
    if hold_s <= 0:
        raise ValueError(f"[midi] hold_s must be above 0, got {hold_s}")
    return MidiConfig(
        port=cfg.get("midi", "port", fallback=None) or None,
        channel=None if channel is None else in_range(channel, 1, 16, "[midi] channel"),
        hold_s=hold_s,
    )


def in_range(value: int, lo: int, hi: int, what: str) -> int:
    if not lo <= value <= hi:
        raise ValueError(f"{what} must be {lo}-{hi}, got {value}")
    return value


def parse_source(src: str, where: str) -> Source:
    """'note N' / 'cc N' -> ("note"|"cc", N)."""
    kind, _, num = src.lower().partition(" ")
    if kind not in ("note", "cc") or not num.strip().isdigit():
        raise ValueError(f"{where} must be 'note N' or 'cc N', got '{src}'")
    return cast(Kind, kind), in_range(int(num), 0, 127, where)


def midi_event(msg: mido.Message) -> MidiEvent | None:
    """Messages that can drive a mapping, None for the rest."""
    if msg.type == "control_change":
        return MidiEvent(("cc", msg.control), msg.value, msg.value >= 64)
    if msg.type in ("note_on", "note_off"):
        return MidiEvent(("note", msg.note), msg.velocity, msg.type == "note_on" and msg.velocity > 0)
    return None


def describe(msg: mido.Message) -> str:
    """Monitor line in config syntax, 1-based channel."""
    ev = midi_event(msg)
    if ev is None:
        return str(msg)
    kind, num = ev.src
    state = f"= {ev.value}" if kind == "cc" else ("press" if ev.pressed else "release")
    return f"ch{msg.channel + 1} {kind} {num} {state}"


def find_port(port: str | None, names: list[str]) -> str | None:
    """rtmidi appends a shifting index to port names ("MIDI Mix 3"), so match substrings."""
    if port is None:
        return None
    return next((n for n in names if port in n), port)
