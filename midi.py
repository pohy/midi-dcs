"""MIDI input: [midi] and [midi."DEVICE"] tables, message decoding, monitor lines, port lookup; config value checks
for all tables. A device is a name for a port, or for some channel of one."""
import json
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, cast

import mido

type Kind = Literal["note", "cc"]
# (device, kind, number): ("MIDI Mix", "note", 22) from { note = 22 } in [dcsbios.F-16C_50."MIDI Mix"]
type Source = tuple[str, Kind, int]
type Table = dict[str, Any]  # parsed toml table
TYPE_NAMES: dict[type, str] = {str: "a string", int: "an integer", float: "a number", bool: "true or false",
                               dict: "a table"}


class MidiEvent(NamedTuple):
    src: Source
    value: int  # cc value or note velocity, 0..127
    pressed: bool  # note on with velocity > 0, or cc >= 64


class Device(NamedTuple):
    """What a device name in the mapping tables listens to; several devices can share a port on other channels."""
    port: str  # port name or part of it, default: the device name
    channel: int | None  # 1-16, None = any


@dataclass(frozen=True)
class MidiConfig:
    devices: dict[str, Device]  # [midi."DEVICE"] tables
    hold_s: float  # held at least this long = long press, on DCS-BIOS hold switches


def parse(cfg: Table) -> MidiConfig:
    m = get(cfg, "midi", dict, {}, "")
    hold_s = get(m, "hold_s", float, 0.5, "[midi]")
    if hold_s <= 0:
        raise ValueError(f"[midi] hold_s must be above 0, got {hold_s}")
    devices: dict[str, Device] = {}
    for device, table in m.items():
        if device == "hold_s":
            continue
        where = f"[midi.{toml(device)}]"
        channel = get(typed(table, dict, where), "channel", int, None, where)
        devices[device] = Device(get(table, "port", str, device, where),
                                 None if channel is None else in_range(channel, 1, 16, f"{where} channel"))
    return MidiConfig(devices, hold_s)


def typed[T](value: Any, kind: type[T], where: str) -> T:
    """value checked against kind; an integer is fine for a number, true/false never counts as an integer."""
    if kind is float and type(value) is int:
        value = float(value)
    if type(value) is not kind:
        raise ValueError(f"{where} must be {TYPE_NAMES[kind]}, got {toml(value)}")
    return value  # narrowed to T by the type() check


def toml(value: Any) -> str:
    """value spelled as in the config, for error messages."""
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml(v)}" for k, v in value.items()) + " }" if value else "{}"
    return json.dumps(value)  # true/false, "strings", numbers


def get[T, D](table: Table, key: str, kind: type[T], default: D, where: str) -> T | D:
    """table[key] checked against kind, default when missing; where = the table, e.g. '[midi]', '' at the top."""
    return default if key not in table else typed(table[key], kind, f"{where} {key}".lstrip())


def in_range(value: int, lo: int, hi: int, what: str) -> int:
    if not lo <= value <= hi:
        raise ValueError(f"{what} must be {lo}-{hi}, got {value}")
    return value


def parse_source(src: Any, device: str, where: str, options: tuple[str, ...] = ()) -> Source:
    """{ note = N } / { cc = N }, plus the caller's option keys -> (device, "note"|"cc", N)."""
    table = typed(src, dict, where)
    unknown = table.keys() - {"note", "cc", *options}
    if unknown:
        raise ValueError(f"{where}: unknown {', '.join(sorted(unknown))}, valid: {' '.join(('note', 'cc') + options)}")
    kinds = [k for k in ("note", "cc") if k in table]
    if len(kinds) != 1:
        raise ValueError(f"{where} needs one of note = N or cc = N, got {toml(table)}")
    kind = cast(Kind, kinds[0])
    return device, kind, in_range(typed(table[kind], int, f"{where} {kind}"), 0, 127, f"{where} {kind}")


def midi_event(msg: mido.Message, device: str) -> MidiEvent | None:
    """Messages that can drive a mapping, None for the rest."""
    if msg.type == "control_change":
        return MidiEvent((device, "cc", msg.control), msg.value, msg.value >= 64)
    if msg.type in ("note_on", "note_off"):
        return MidiEvent((device, "note", msg.note), msg.velocity, msg.type == "note_on" and msg.velocity > 0)
    return None


def describe(msg: mido.Message) -> str:
    """Monitor line in config syntax, 1-based channel."""
    ev = midi_event(msg, "")
    if ev is None:
        return str(msg)
    _, kind, num = ev.src
    state = f"= {ev.value}" if kind == "cc" else ("press" if ev.pressed else "release")
    return f"ch{msg.channel + 1} {kind}:{num} {state}"


def find_port(port: str, names: list[str]) -> str:
    """rtmidi appends a shifting index to port names ("MIDI Mix 3"), so match substrings."""
    return next((n for n in names if port in n), port)
