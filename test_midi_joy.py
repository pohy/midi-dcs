import math
import tempfile
from pathlib import Path

import mido

from midi_joy import button_event, cc_to_axis, describe, load_config, smooth


def test_cc_to_axis():
    assert cc_to_axis(0) == 1
    assert cc_to_axis(127) == 0x8000


def test_smooth():
    assert smooth(0.0, 100, 0.01, 0) == 100.0  # tau 0 = off
    assert math.isclose(smooth(0.0, 100, 0.05, 0.05), 100 * (1 - math.exp(-1)))  # 63.2% after one tau
    # poll-rate independent: two 10 ms steps == one 20 ms step
    assert math.isclose(smooth(smooth(0.0, 100, 0.01, 0.05), 100, 0.01, 0.05), smooth(0.0, 100, 0.02, 0.05))


def _load(text):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "c.ini")
        path.write_text(text)
        return load_config(path)


def test_button_event_and_describe():
    note = mido.Message("note_on", channel=0, note=1, velocity=127)
    assert button_event(note) == (("note", 1), True)
    assert button_event(note.copy(velocity=0)) == (("note", 1), False)  # note_on vel 0 = release
    assert button_event(mido.Message("note_off", note=1)) == (("note", 1), False)
    assert button_event(mido.Message("control_change", control=64, value=64)) == (("cc", 64), True)
    assert button_event(mido.Message("control_change", control=64, value=63)) == (("cc", 64), False)
    assert button_event(mido.Message("pitchwheel")) is None
    assert describe(note) == "ch1 note 1 press"
    assert describe(mido.Message("control_change", channel=15, control=62, value=5)) == "ch16 cc 62 = 5"


def test_load_config():
    c = _load("[midi]\nport = MIDI Mix\n[axes]\nX = 19  ; fader 1\nrz = 23\n[buttons]\n1 = note 1\n12 = CC 64\n")
    assert (c.port, c.channel, c.device, c.smooth) == ("MIDI Mix", None, 1, 0.05)
    assert c.axes == {"x": 19, "rz": 23}
    assert c.buttons == {1: ("note", 1), 12: ("cc", 64)}
    assert _load("[midi]\n").axes == {}  # sections optional; main() rejects an empty mapping
    for bad in ("[axes]\nfoo = 1\n", "[axes]\nz = 128\n", "[axes]\nz = abc\n", "[midi]\nchannel = 17\n",
                "[buttons]\n1 = foo 3\n", "[buttons]\n1 = note\n", "[buttons]\n0 = note 1\n",
                "[buttons]\n129 = note 1\n", "[buttons]\n1 = note 128\n", "[buttons]\nx = note 1\n"):
        try:
            _load(bad)
        except SystemExit:
            continue
        raise AssertionError(f"accepted bad config: {bad!r}")


if __name__ == "__main__":
    test_cc_to_axis()
    test_smooth()
    test_button_event_and_describe()
    test_load_config()
    print("ok")
