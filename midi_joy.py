"""Map MIDI CC sliders and notes to vJoy axes/buttons and DCS-BIOS controls, configured in an ini file."""
import argparse
import configparser
import ctypes
import json
import math
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import mido

# ponytail: hardcoded default install paths, add config keys if vJoy or Saved Games live elsewhere
VJOY_DLL = r"C:\Program Files\vJoy\x64\vJoyInterface.dll"
DCSBIOS_JSON = Path.home() / "Saved Games/DCS/Scripts/DCS-BIOS/doc/json"
DCSBIOS_ADDR = ("127.0.0.1", 7778)  # DCS-BIOS UDP import port
AXES = {"x": 0x30, "y": 0x31, "z": 0x32, "rx": 0x33, "ry": 0x34, "rz": 0x35, "sl0": 0x36, "sl1": 0x37}  # HID usages
VJD_STATUS = {0: "owned by this process", 2: "busy (another feeder owns it)", 3: "missing (not configured)"}
TICK_S = 0.001  # output loop period


def cc_to_axis(value):
    """MIDI 0..127 -> vJoy 1..32768."""
    return 1 + value * 0x7FFF // 127


def cc_to_state(value, max_value):
    """MIDI 0..127 -> DCS-BIOS set_state 0..max_value in equal zones; 127 hits max_value exactly."""
    return min(value * (max_value + 1) // 127, max_value)


def smooth(prev, raw, dt, tau):
    """One-pole low-pass. alpha derived from dt so behaviour is poll-rate independent."""
    if tau <= 0:
        return float(raw)
    alpha = 1.0 - math.exp(-dt / tau)
    return prev + alpha * (raw - prev)


def button_event(msg):
    """(("note"|"cc", number), pressed) for messages that can drive a button, else None."""
    if msg.type == "control_change":
        return ("cc", msg.control), msg.value >= 64
    if msg.type in ("note_on", "note_off"):
        return ("note", msg.note), msg.type == "note_on" and msg.velocity > 0
    return None


def describe(msg):
    """Monitor line in config syntax, 1-based channel."""
    ev = button_event(msg)
    if ev is None:
        return str(msg)
    (kind, num), pressed = ev
    state = f"= {msg.value}" if kind == "cc" else ("press" if pressed else "release")
    return f"ch{msg.channel + 1} {kind} {num} {state}"


def in_range(value, lo, hi, what):
    if value is not None and not lo <= value <= hi:
        raise SystemExit(f"{what} must be {lo}-{hi}, got {value}")
    return value


def parse_source(src, where):
    """'note N' / 'cc N' -> ("note"|"cc", N)."""
    kind, _, num = src.lower().partition(" ")
    if kind not in ("note", "cc") or not num.strip().isdigit():
        raise SystemExit(f"{where} must be 'note N' or 'cc N', got '{src}'")
    return kind, in_range(int(num), 0, 127, where)


def load_dcsbios(cfg, section, json_dir):
    """[dcsbios:AIRCRAFT] IDENTIFIER = cc N -> {IDENTIFIER: (cc, max_value)}, checked against the aircraft json."""
    aircraft = section.partition(":")[2]
    try:
        doc = json.loads((Path(json_dir) / f"{aircraft}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"[{section}] no DCS-BIOS json for '{aircraft}' in {json_dir}")
    controls = {c["identifier"].upper(): c for cat in doc.values() for c in cat.values()}
    mapping = {}
    for ident, src in cfg.items(section):
        c = controls.get(ident.upper())
        if c is None:
            raise SystemExit(f"[{section}] unknown control '{ident}', look it up in Bort")
        kind, cc = parse_source(src, f"[{section}] {ident}")
        if kind != "cc":
            raise SystemExit(f"[{section}] {ident}: only 'cc N' supported, got '{src}'")
        max_value = next((i["max_value"] for i in c["inputs"] if i["interface"] == "set_state"), None)
        if max_value is None:
            raise SystemExit(f"[{section}] {ident} has no set_state input, can't follow a cc")
        mapping[c["identifier"]] = (cc, max_value)
    return mapping


def load_config(path, json_dir=DCSBIOS_JSON):
    """Read the ini file, exit with a readable message on any problem."""
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
    try:
        if not cfg.read(path, encoding="utf-8"):
            raise SystemExit(f"config not found: {path}")
        axes = {}
        for name, cc in cfg.items("axes") if cfg.has_section("axes") else []:
            if name not in AXES:
                raise SystemExit(f"{path}: unknown axis '{name}', valid: {' '.join(AXES)}")
            axes[name] = in_range(int(cc), 0, 127, f"[axes] {name}")
        buttons = {}
        for btn, src in cfg.items("buttons") if cfg.has_section("buttons") else []:
            buttons[in_range(int(btn), 1, 128, "[buttons] button")] = parse_source(src, f"[buttons] {btn}")
        # ponytail: one aircraft; several sections need aircraft detection from the DCS-BIOS export stream
        dcs = [s for s in cfg.sections() if s.startswith("dcsbios:")]
        if len(dcs) > 1:
            raise SystemExit(f"{path}: only one [dcsbios:AIRCRAFT] section supported, got {', '.join(dcs)}")
        return SimpleNamespace(
            port=cfg.get("midi", "port", fallback=None) or None,
            channel=in_range(cfg.getint("midi", "channel", fallback=None), 1, 16, "[midi] channel"),
            device=cfg.getint("vjoy", "device", fallback=1),
            smooth=cfg.getfloat("vjoy", "smooth", fallback=0.05),
            axes=axes,
            buttons=buttons,
            dcsbios=load_dcsbios(cfg, dcs[0], json_dir) if dcs else {},
        )
    except (configparser.Error, ValueError) as e:
        raise SystemExit(f"{path}: {e}")


def open_vjoy(device, axis_names, buttons):
    vj = ctypes.CDLL(VJOY_DLL)
    if not vj.vJoyEnabled():
        raise SystemExit("vJoy driver not enabled")
    status = vj.GetVJDStatus(device)
    if status != 1:  # VJD_STAT_FREE
        raise SystemExit(f"vJoy device {device} {VJD_STATUS.get(status, f'unavailable (status {status})')}")
    for name in axis_names:
        if not vj.GetVJDAxisExist(device, AXES[name]):
            raise SystemExit(f"vJoy device {device} has no {name} axis, enable it in Configure vJoy")
    count = vj.GetVJDButtonNumber(device)
    if buttons and max(buttons) > count:
        raise SystemExit(f"vJoy device {device} has only {count} buttons, add more in Configure vJoy")
    if not vj.AcquireVJD(device):
        raise SystemExit(f"failed to acquire vJoy device {device}")
    return vj


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=Path(__file__).with_name("midi_joy.ini"),
                   help="config file (default: midi_joy.ini next to this script)")
    p.add_argument("--list", action="store_true", help="list MIDI input ports and exit")
    p.add_argument("--monitor", action="store_true",
                   help="print incoming MIDI from the configured port in config syntax, no vJoy or DCS-BIOS")
    args = p.parse_args()

    if args.list:
        print("\n".join(mido.get_input_names()) or "no MIDI input ports")
        return
    conf = load_config(args.config)
    if not args.monitor and not conf.axes and not conf.buttons and not conf.dcsbios:
        raise SystemExit(f"{args.config}: nothing mapped in [axes], [buttons] or [dcsbios:AIRCRAFT]")

    vj = None
    # latest state per output, written by the MIDI callback thread; only the main loop talks to vJoy
    target = dict.fromkeys(conf.axes)
    btn_target = dict.fromkeys(conf.buttons)
    dcs_sent = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.monitor:
        def callback(msg):
            print(describe(msg))
    else:
        if conf.axes or conf.buttons:
            vj = open_vjoy(conf.device, conf.axes, conf.buttons)

        def callback(msg):
            ev = button_event(msg)
            if ev is None or (conf.channel is not None and msg.channel != conf.channel - 1):
                return
            src, pressed = ev
            for name, cc in conf.axes.items():
                if src == ("cc", cc):
                    target[name] = cc_to_axis(msg.value)
            for btn, s in conf.buttons.items():
                if src == s:
                    btn_target[btn] = pressed
            # DCS-BIOS is plain UDP, safe to send straight from here
            for ident, (cc, max_value) in conf.dcsbios.items():
                if src == ("cc", cc):
                    state = cc_to_state(msg.value, max_value)
                    # ponytail: dedup per zone, so after moving the switch with the mouse
                    # the knob only resyncs once it enters another zone
                    if dcs_sent.get(ident) != state:
                        dcs_sent[ident] = state
                        sock.sendto(f"{ident} {state}\n".encode(), DCSBIOS_ADDR)

    # rtmidi appends a shifting index to port names ("MIDI Mix 3"), so match substrings
    port_name = next((n for n in mido.get_input_names() if conf.port and conf.port in n), conf.port)
    # py3.10 sleep() has ~15.6 ms granularity without this; reset by Windows on process exit
    ctypes.windll.winmm.timeBeginPeriod(1)
    try:
        with mido.open_input(port_name, callback=callback) as port:
            mapping = " ".join([f"{n}=cc{c}" for n, c in conf.axes.items()]
                               + [f"btn{b}={k}{n}" for b, (k, n) in conf.buttons.items()]
                               + [f"{i}=cc{c}" for i, (c, _) in conf.dcsbios.items()])
            print(f"listening on '{port.name}' ({mapping}), Ctrl+C to quit")
            pos = dict.fromkeys(conf.axes)
            sent = dict.fromkeys(conf.axes)
            btn_sent = dict.fromkeys(conf.buttons)
            last = time.perf_counter()
            while True:
                # blocking port iteration ignores Ctrl+C on Windows; callback + sleep doesn't
                time.sleep(TICK_S)
                now = time.perf_counter()
                dt, last = now - last, now
                for name in conf.axes:
                    t = target[name]
                    if t is None:
                        continue
                    # first value jumps straight to the slider, no sweep from an unknown start
                    pos[name] = t if pos[name] is None else smooth(pos[name], t, dt, conf.smooth)
                    if round(pos[name]) != sent[name]:
                        sent[name] = round(pos[name])
                        vj.SetAxis(sent[name], conf.device, AXES[name])
                # ponytail: state sampled per tick, a press+release inside one tick (~1.4 ms) is lost;
                # queue events if a controller ever sends taps that short
                for btn in conf.buttons:
                    t = btn_target[btn]
                    if t != btn_sent[btn]:
                        btn_sent[btn] = t
                        vj.SetBtn(t, conf.device, btn)
    except KeyboardInterrupt:
        pass
    finally:
        if vj:
            for btn in conf.buttons:
                vj.SetBtn(0, conf.device, btn)  # vJoy keeps state after exit, don't leave buttons held
            vj.RelinquishVJD(conf.device)


if __name__ == "__main__":
    main()
