"""vJoy output: [vjoy], [vjoy.axes."DEVICE"] and [vjoy.buttons."DEVICE"] tables, smoothed axes and buttons on one
vJoy device."""
import ctypes
import math
from dataclasses import dataclass

from midi import MidiEvent, Source, Table, get, in_range, parse_source, toml, typed

# ponytail: hardcoded default install path, add a config key if vJoy lives elsewhere
VJOY_DLL = r"C:\Program Files\vJoy\x64\vJoyInterface.dll"
AXES: dict[str, int] = {"x": 0x30, "y": 0x31, "z": 0x32, "rx": 0x33, "ry": 0x34, "rz": 0x35, "sl0": 0x36, "sl1": 0x37}  # HID usages
VJD_STAT_FREE = 1
VJD_STATUS: dict[int, str] = {0: "owned by this process", 2: "busy (another feeder owns it)", 3: "missing (not configured)"}


@dataclass(frozen=True)
class VJoyConfig:
    device: int
    smooth: float  # time constant in seconds, 0 = off
    axes: dict[str, Source]  # axis name -> cc
    buttons: dict[int, Source]  # button 1-128 -> note/cc


def parse(cfg: Table) -> VJoyConfig:
    v = get(cfg, "vjoy", dict, {}, "")
    axes: dict[str, Source] = {}
    for device, table in get(v, "axes", dict, {}, "[vjoy]").items():
        section = f"[vjoy.axes.{toml(device)}]"
        for name, cc in typed(table, dict, section).items():
            axis, where = name.lower(), f"{section} {name}"
            if axis not in AXES:
                raise ValueError(f"{section} unknown axis '{name}', valid: {' '.join(AXES)}")
            if axis in axes:
                raise ValueError(f"{where}: already mapped in [vjoy.axes.{toml(axes[axis][0])}]")
            axes[axis] = (device, "cc", in_range(typed(cc, int, where), 0, 127, where))
    buttons: dict[int, Source] = {}
    for device, table in get(v, "buttons", dict, {}, "[vjoy]").items():
        section = f"[vjoy.buttons.{toml(device)}]"
        for btn, src in typed(table, dict, section).items():
            if not btn.isdecimal() or not 1 <= int(btn) <= 128:
                raise ValueError(f"{section} button must be 1-128, got '{btn}'")
            if int(btn) in buttons:
                raise ValueError(f"{section} {btn}: already mapped in [vjoy.buttons.{toml(buttons[int(btn)][0])}]")
            buttons[int(btn)] = parse_source(src, device, f"{section} {btn}")
    return VJoyConfig(
        device=get(v, "device", int, 1, "[vjoy]"),
        smooth=get(v, "smooth", float, 0.05, "[vjoy]"),
        axes=axes,
        buttons=buttons,
    )


def summary(conf: VJoyConfig, device: str) -> str:
    """What device drives, for the startup line."""
    return " ".join([f"{a}=cc:{n}" for a, (d, _, n) in conf.axes.items() if d == device]
                    + [f"btn{b}={k}:{n}" for b, (d, k, n) in conf.buttons.items() if d == device])


def cc_to_axis(value: int) -> int:
    """MIDI 0..127 -> vJoy 1..32768."""
    return 1 + value * 0x7FFF // 127


def smooth(prev: float, raw: float, dt: float, tau: float) -> float:
    """One-pole low-pass. alpha derived from dt so behaviour is poll-rate independent."""
    if tau <= 0:
        return float(raw)
    alpha = 1.0 - math.exp(-dt / tau)
    return prev + alpha * (raw - prev)


def acquire(conf: VJoyConfig) -> ctypes.CDLL:
    """Load the driver, check the device has every mapped axis and button, take ownership."""
    try:
        dll = ctypes.CDLL(VJOY_DLL)
    except FileNotFoundError:
        raise SystemExit(f"vJoy not installed ({VJOY_DLL})")
    if not dll.vJoyEnabled():
        raise SystemExit("vJoy driver not enabled")
    status: int = dll.GetVJDStatus(conf.device)
    if status != VJD_STAT_FREE:
        raise SystemExit(f"vJoy device {conf.device} {VJD_STATUS.get(status, f'unavailable (status {status})')}")
    for name in conf.axes:
        if not dll.GetVJDAxisExist(conf.device, AXES[name]):
            raise SystemExit(f"vJoy device {conf.device} has no {name} axis, enable it in Configure vJoy")
    count: int = dll.GetVJDButtonNumber(conf.device)
    if conf.buttons and max(conf.buttons) > count:
        raise SystemExit(f"vJoy device {conf.device} has only {count} buttons, add more in Configure vJoy")
    if not dll.AcquireVJD(conf.device):
        raise SystemExit(f"failed to acquire vJoy device {conf.device}")
    return dll


class VJoy:
    """on_midi only stores the latest targets; tick() is the only caller into the driver."""

    def __init__(self, conf: VJoyConfig, dll: ctypes.CDLL) -> None:
        self.conf = conf
        self.dll = dll
        self.axis_target: dict[str, int | None] = dict.fromkeys(conf.axes)
        self.axis_pos: dict[str, float | None] = dict.fromkeys(conf.axes)
        self.axis_sent: dict[str, int | None] = dict.fromkeys(conf.axes)
        self.btn_target: dict[int, bool | None] = dict.fromkeys(conf.buttons)
        self.btn_sent: dict[int, bool | None] = dict.fromkeys(conf.buttons)

    def on_midi(self, ev: MidiEvent) -> None:
        for name, src in self.conf.axes.items():
            if ev.src == src:
                self.axis_target[name] = cc_to_axis(ev.value)
        for btn, src in self.conf.buttons.items():
            if ev.src == src:
                self.btn_target[btn] = ev.pressed

    def tick(self, dt: float) -> None:
        for name, target in self.axis_target.items():
            if target is None:
                continue
            prev = self.axis_pos[name]
            # first value jumps straight to the slider, no sweep from an unknown start
            pos = target if prev is None else smooth(prev, target, dt, self.conf.smooth)
            self.axis_pos[name] = pos
            if round(pos) == self.axis_sent[name]:
                continue
            self.axis_sent[name] = round(pos)
            self.dll.SetAxis(round(pos), self.conf.device, AXES[name])
        # ponytail: state sampled per tick, a press+release inside one tick (~1.4 ms) is lost;
        # queue events if a controller ever sends taps that short
        for btn, pressed in self.btn_target.items():
            if pressed == self.btn_sent[btn]:
                continue
            self.btn_sent[btn] = pressed
            self.dll.SetBtn(pressed, self.conf.device, btn)

    def close(self) -> None:
        for btn in self.conf.buttons:
            self.dll.SetBtn(0, self.conf.device, btn)  # vJoy keeps state after exit, don't leave buttons held
        self.dll.RelinquishVJD(self.conf.device)
