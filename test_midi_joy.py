import json
import math
import tempfile
from pathlib import Path

import mido

from midi_joy import (
    button_event,
    cc_to_axis,
    cc_to_state,
    describe,
    load_config,
    smooth,
)


def test_cc_to_axis():
    assert cc_to_axis(0) == 1
    assert cc_to_axis(127) == 0x8000


def test_cc_to_state():
    assert [cc_to_state(v, 7) for v in (0, 15, 16, 111, 112, 127)] == [0, 0, 1, 6, 7, 7]
    zones = [cc_to_state(v, 4) for v in range(128)]
    assert sorted(set(zones)) == [0, 1, 2, 3, 4]
    assert max(map(zones.count, set(zones))) - min(map(zones.count, set(zones))) <= 1  # equal zones
    assert cc_to_state(0, 65535) == 0 and cc_to_state(127, 65535) == 65535  # dials reach full range


def test_smooth():
    assert smooth(0.0, 100, 0.01, 0) == 100.0  # tau 0 = off
    assert math.isclose(smooth(0.0, 100, 0.05, 0.05), 100 * (1 - math.exp(-1)))  # 63.2% after one tau
    # poll-rate independent: two 10 ms steps == one 20 ms step
    assert math.isclose(smooth(smooth(0.0, 100, 0.01, 0.05), 100, 0.01, 0.05), smooth(0.0, 100, 0.02, 0.05))


# trimmed copy of DCS-BIOS F-16C_50.json entries
FAKE_JSON = {"External Lights": {
    "ANTI_COLL_LIGHT_KNB": {"identifier": "ANTI_COLL_LIGHT_KNB", "inputs": [
        {"interface": "fixed_step"}, {"interface": "set_state", "max_value": 7}]},
    "FORM_LIGHT_KNB": {"identifier": "FORM_LIGHT_KNB", "inputs": [
        {"interface": "set_state", "max_value": 65535}, {"interface": "variable_step", "max_value": 65535}]},
    "ALT_BARO_SET_KNB": {"identifier": "ALT_BARO_SET_KNB", "inputs": [{"interface": "variable_step"}]},
}}


def _load(text):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "c.ini")
        path.write_text(text)
        Path(d, "F-16C_50.json").write_text(json.dumps(FAKE_JSON))
        return load_config(path, json_dir=d)


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
    test_cc_to_state()
    test_smooth()
    test_button_event_and_describe()
    test_load_config()
    print("ok")
