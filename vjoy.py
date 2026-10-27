"""vJoy output: [vjoy], [vjoy:axes] and [vjoy:buttons] sections, smoothed axes and buttons on one device."""
import ctypes
import math
from configparser import ConfigParser
from dataclasses import dataclass

from midi import MidiEvent, Source, in_range, parse_source

# ponytail: hardcoded default install path, add a config key if vJoy lives elsewhere
VJOY_DLL = r"C:\Program Files\vJoy\x64\vJoyInterface.dll"
AXES: dict[str, int] = {"x": 0x30, "y": 0x31, "z": 0x32, "rx": 0x33, "ry": 0x34, "rz": 0x35, "sl0": 0x36, "sl1": 0x37}  # HID usages
VJD_STAT_FREE = 1
VJD_STATUS: dict[int, str] = {0: "owned by this process", 2: "busy (another feeder owns it)", 3: "missing (not configured)"}


@dataclass(frozen=True)
class VJoyConfig:
    device: int
    smooth: float  # time constant in seconds, 0 = off
    axes: dict[str, int]  # axis name -> cc number
    buttons: dict[int, Source]  # button 1-128 -> note/cc


def parse(cfg: ConfigParser) -> VJoyConfig:
    axes: dict[str, int] = {}
    for name, cc in cfg.items("vjoy:axes") if cfg.has_section("vjoy:axes") else []:
        if name not in AXES:
            raise ValueError(f"unknown axis '{name}', valid: {' '.join(AXES)}")
        axes[name] = in_range(int(cc), 0, 127, f"[vjoy:axes] {name}")
    buttons: dict[int, Source] = {}
    for btn, src in cfg.items("vjoy:buttons") if cfg.has_section("vjoy:buttons") else []:
        buttons[in_range(int(btn), 1, 128, "[vjoy:buttons] button")] = parse_source(src, f"[vjoy:buttons] {btn}")
    return VJoyConfig(
        device=cfg.getint("vjoy", "device", fallback=1),
        smooth=cfg.getfloat("vjoy", "smooth", fallback=0.05),
        axes=axes,
        buttons=buttons,
    )


def summary(conf: VJoyConfig) -> str:
    return " ".join([f"{n}=cc:{c}" for n, c in conf.axes.items()]
                    + [f"btn{b}={k}:{n}" for b, (k, n) in conf.buttons.items()])


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
    dll = ctypes.CDLL(VJOY_DLL)
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
        for name, cc in self.conf.axes.items():
            if ev.src == ("cc", cc):
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
