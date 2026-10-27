"""Map MIDI CC sliders and notes to vJoy axes/buttons and DCS-BIOS controls, configured in an ini file."""
import argparse
import configparser
import ctypes
import sys
import time
import traceback
from collections.abc import Callable
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
    """on_midi runs on the rtmidi callback thread, tick and close on the main thread."""

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
    """Read the ini file and let each module parse its own sections; exit with a readable message on any problem."""
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
    try:
        if not cfg.read(path, encoding="utf-8"):
            raise SystemExit(f"config not found: {path}")
        return Config(midi=midi.parse(cfg), vjoy=vjoy.parse(cfg), dcsbios=dcsbios.parse(cfg, json_dir),
                      sound=sound.parse(cfg, path.parent), overlay=overlay.parse(cfg))
    except (configparser.Error, ValueError) as e:
        raise SystemExit(f"{path}: {e}")


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
        if conf.midi.port is None:
            raise SystemExit("[midi] port needed for switch LEDs, the default MIDI output is usually a synth")
        leds = mido.open_output(midi.find_port(conf.midi.port, mido.get_output_names()))
        channel = (conf.midi.channel or 1) - 1
        switch = sound.Clicker(conf.sound.switch).play if conf.sound.switch else lambda: None
        outputs.append(dcsbios.DcsSwitches(conf.dcsbios.switches, leds, channel, dcsbios.listen(), conf.midi.hold_s,
                                           show, switch))
    return outputs


def dispatch(channel: int | None, outputs: list[Output]) -> Callable[[mido.Message], None]:
    """MIDI callback: drop unmappable messages and other channels, hand the rest to every output."""
    def callback(msg: mido.Message) -> None:
        ev = midi.midi_event(msg)
        if ev is None:
            return
        if channel is not None and msg.channel != channel - 1:
            return
        for out in outputs:
            out.on_midi(ev)
    return callback


def run(port: str | None, callback: Callable[[mido.Message], None], outputs: list[Output], mapping: str) -> None:
    """Listen on the MIDI port and tick outputs until Ctrl+C, then close them."""
    # py3.10 sleep() has ~15.6 ms granularity without this; reset by Windows on process exit
    ctypes.windll.winmm.timeBeginPeriod(1)
    try:
        with mido.open_input(midi.find_port(port, mido.get_input_names()), callback=callback) as midi_in:
            print(f"listening on '{midi_in.name}' ({mapping}), Ctrl+C to quit")
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
    # frozen (PyInstaller): __file__ is in the temp unpack dir, the ini sits next to the exe
    here = Path(sys.executable if FROZEN else __file__)
    p.add_argument("--config", type=Path, default=here.with_name("midi_joy.ini"),
                   help="config file (default: midi_joy.ini next to this script or exe)")
    p.add_argument("--list", action="store_true", help="list MIDI input ports and exit")
    p.add_argument("--monitor", action="store_true",
                   help="print incoming MIDI from the configured port in config syntax, no vJoy or DCS-BIOS")
    args = p.parse_args()

    if args.list:
        print("\n".join(mido.get_input_names()) or "no MIDI input ports")
        return
    conf = load_config(args.config)
    mapping = " ".join(filter(None, [vjoy.summary(conf.vjoy), dcsbios.summary(conf.dcsbios)]))
    if args.monitor:
        run(conf.midi.port, lambda msg: print(midi.describe(msg)), [], mapping)
        return
    outputs = open_outputs(conf)
    if not outputs:
        raise SystemExit(f"{args.config}: nothing mapped in [vjoy:axes], [vjoy:buttons] or [dcsbios:AIRCRAFT]")
    run(conf.midi.port, dispatch(conf.midi.channel, outputs), outputs, mapping)


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
