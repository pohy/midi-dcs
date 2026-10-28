"""Map MIDI CC sliders and notes to vJoy axes/buttons and DCS-BIOS controls, configured in a toml file."""
import argparse
import ctypes
import sys
import time
import tomllib
import traceback
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import mido

import dcsbios
import midi
import overlay
import sound
import vjoy

TICK_S = 0.001  # output loop period
FROZEN = getattr(sys, "frozen", False)  # running as the PyInstaller exe


class Output(Protocol):
    """on_midi runs on the rtmidi callback threads, one per device, tick and close on the main thread.
    A mapping takes one device, so two threads never update the same one."""

    def on_midi(self, ev: midi.MidiEvent) -> None: ...

    def tick(self, dt: float) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Config:
    midi: midi.MidiConfig
    vjoy: vjoy.VJoyConfig
    dcsbios: dcsbios.DcsConfig
    sound: sound.SoundConfig
    overlay: overlay.OverlayConfig | None


def load_config(path: Path, json_dir: Path = dcsbios.JSON_DIR) -> Config:
    """Read the toml file and let each module parse its own tables; exit with a readable message on any problem."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"config not found: {path}")
    try:
        cfg = tomllib.loads(text)
        return Config(midi=midi.parse(cfg), vjoy=vjoy.parse(cfg), dcsbios=dcsbios.parse(cfg, json_dir),
                      sound=sound.parse(cfg, path.parent), overlay=overlay.parse(cfg))
    except ValueError as e:  # tomllib.TOMLDecodeError is one
        raise SystemExit(f"{path}: {e}")


def devices(conf: Config) -> dict[str, midi.Device]:
    """Every device with a [midi."DEVICE"] table or something mapped; without a table, the port of that name."""
    srcs = [*conf.vjoy.axes.values(), *conf.vjoy.buttons.values(),
            *(c.src for c in conf.dcsbios.knobs.values()), *(s.src for s in conf.dcsbios.switches.values())]
    return conf.midi.devices | {d: conf.midi.devices.get(d, midi.Device(d, None)) for d, _, _ in srcs}


def by_port(devs: dict[str, midi.Device], names: list[str]) -> dict[str | None, dict[str, midi.Device]]:
    """Devices grouped by the port name they match, so each port opens once."""
    ports: dict[str | None, dict[str, midi.Device]] = {}
    for d, dev in devs.items():
        ports.setdefault(midi.find_port(dev.port, names), {})[d] = dev
    return ports


def summary(conf: Config, devs: Iterable[str]) -> str:
    """What the devices map, for the startup line."""
    return " ".join(filter(None, [s for d in devs for s in (vjoy.summary(conf.vjoy, d),
                                                              dcsbios.summary(conf.dcsbios, d))]))


def open_outputs(conf: Config) -> list[Output]:
    outputs: list[Output] = []
    if conf.vjoy.axes or conf.vjoy.buttons:
        outputs.append(vjoy.VJoy(conf.vjoy, vjoy.acquire(conf.vjoy)))
    show: Callable[[str], None] = lambda text: None
    if conf.overlay and (conf.dcsbios.knobs or conf.dcsbios.switches):
        box = overlay.Overlay(conf.overlay)
        outputs.append(box)
        show = box.show
    if conf.dcsbios.knobs:
        knob = sound.Clicker(conf.sound.knob).play if conf.sound.knob else lambda: None
        outputs.append(dcsbios.DcsBios(conf.dcsbios.knobs, knob, show))
    if conf.dcsbios.switches:
        # LEDs on the device the note comes from, on its channel; devices on one port share its output
        used = {s.src[0] for s in conf.dcsbios.switches.values()}
        leds: dict[str, tuple[mido.ports.BaseOutput, int]] = {}
        for port, devs in by_port({d: dev for d, dev in devices(conf).items() if d in used and dev.leds},
                                  mido.get_output_names()).items():
            out = mido.open_output(port)
            leds |= {d: (out, (dev.channel or 1) - 1) for d, dev in devs.items()}
        switch = sound.Clicker(conf.sound.switch).play if conf.sound.switch else lambda: None
        outputs.append(dcsbios.DcsSwitches(conf.dcsbios.switches, leds, dcsbios.listen(), conf.midi.hold_s,
                                           show, switch))
    return outputs


def dispatch(devs: dict[str, midi.Device], outputs: list[Output]) -> Callable[[mido.Message], None]:
    """MIDI callback of one port: drop unmappable messages, hand the rest to every output as coming from each
    device whose channel it's on."""
    def callback(msg: mido.Message) -> None:
        for device, dev in devs.items():
            ev = midi.midi_event(msg, device)
            if ev is None:
                return
            if dev.channel is not None and msg.channel != dev.channel - 1:
                continue
            for out in outputs:
                out.on_midi(ev)
    return callback


def monitor(devs: dict[str, midi.Device]) -> Callable[[mido.Message], None]:
    """--monitor callback of one port: every message in config syntax, after the devices whose channel it's on."""
    def callback(msg: mido.Message) -> None:
        names = [midi.toml(d) for d, dev in devs.items()
                 if midi.midi_event(msg, d) is not None and (dev.channel is None or msg.channel == dev.channel - 1)]
        print(" ".join([*names, midi.describe(msg)]))
    return callback


def run(inputs: dict[str | None, tuple[Callable[[mido.Message], None], str]], outputs: list[Output]) -> None:
    """Listen on every MIDI port and tick outputs until Ctrl+C, then close them.
    inputs: port name (None = system default) -> (callback, what it maps for the startup line)."""
    # py3.10 sleep() has ~15.6 ms granularity without this; reset by Windows on process exit
    ctypes.windll.winmm.timeBeginPeriod(1)
    try:
        with ExitStack() as ports:
            for port, (callback, mapping) in inputs.items():
                midi_in = ports.enter_context(mido.open_input(port, callback=callback))
                print(f"listening on '{midi_in.name}' ({mapping})")
            print("Ctrl+C to quit")
            last = time.perf_counter()
            while True:
                # blocking port iteration ignores Ctrl+C on Windows; callback + sleep doesn't
                time.sleep(TICK_S)
                now = time.perf_counter()
                dt, last = now - last, now
                for out in outputs:
                    out.tick(dt)
    except KeyboardInterrupt:
        pass
    finally:
        for out in outputs:
            out.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    # frozen (PyInstaller): __file__ is in the temp unpack dir, the config sits next to the exe
    here = Path(sys.executable if FROZEN else __file__)
    p.add_argument("--config", type=Path, default=here.with_name("midi_joy.toml"),
                   help="config file (default: midi_joy.toml next to this script or exe)")
    p.add_argument("--list", action="store_true", help="list MIDI input ports and exit")
    p.add_argument("--monitor", action="store_true",
                   help="print incoming MIDI from every configured device, or the system default port without any "
                        "(channel, note/cc number), no vJoy or DCS-BIOS")
    args = p.parse_args()

    if args.list:
        print("\n".join(mido.get_input_names()) or "no MIDI input ports")
        return
    conf = load_config(args.config)
    ports = by_port(devices(conf), mido.get_input_names())
    if args.monitor:
        ports = ports or {None: {}}  # no devices: the system default port
        run({p: (monitor(devs), summary(conf, devs)) for p, devs in ports.items()}, [])
        return
    outputs = open_outputs(conf)
    if not outputs:
        raise SystemExit(f'{args.config}: nothing mapped in [vjoy.axes."DEVICE"], [vjoy.buttons."DEVICE"] or '
                         f'[dcsbios.AIRCRAFT."DEVICE"]')
    run({p: (dispatch(devs, outputs), summary(conf, devs)) for p, devs in ports.items()}, outputs)


def main_paused() -> None:
    """main() for the exe: a double-clicked console closes on exit, so wait for Enter after an error."""
    # ponytail: also pauses when the exe fails in a terminal; GetConsoleProcessList can tell the two apart
    try:
        main()
        return
    except SystemExit as e:
        if not e.code:
            raise
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
    except Exception:  # noqa: BLE001 - any crash, shown before the window closes
        traceback.print_exc()
    input("Press Enter to exit")
    raise SystemExit(1)


if __name__ == "__main__":
    main_paused() if FROZEN else main()
