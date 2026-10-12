import ctypes
import json
import math
import socket
import tempfile
from pathlib import Path
from typing import cast

import mido

from dcsbios import (
    DcsBios,
    DcsConfig,
    DcsControl,
    DcsSwitch,
    DcsSwitches,
    apply_export,
    cc_to_state,
    led_on,
    switch_state,
)
from midi import MidiEvent, describe, midi_event
from midi_joy import Config, dispatch, load_config
from vjoy import VJoy, VJoyConfig, cc_to_axis, smooth


def test_cc_to_axis() -> None:
    assert cc_to_axis(0) == 1
    assert cc_to_axis(127) == 0x8000


def test_cc_to_state() -> None:
    assert [cc_to_state(v, 7) for v in (0, 15, 16, 111, 112, 127)] == [0, 0, 1, 6, 7, 7]
    zones = [cc_to_state(v, 4) for v in range(128)]
    assert sorted(set(zones)) == [0, 1, 2, 3, 4]
    assert max(map(zones.count, set(zones))) - min(map(zones.count, set(zones))) <= 1  # equal zones
    assert cc_to_state(0, 65535) == 0 and cc_to_state(127, 65535) == 65535  # dials reach full range


def test_smooth() -> None:
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
    "AP_PITCH_SW": {"identifier": "AP_PITCH_SW", "inputs": [{"interface": "set_state", "max_value": 2}], "outputs": [
        {"address": 17408, "mask": 768, "max_value": 2, "shift_by": 8, "type": "integer"}]},
    "LASER_ARM_SW": {"identifier": "LASER_ARM_SW", "inputs": [{"interface": "set_state", "max_value": 1}], "outputs": [
        {"address": 17448, "mask": 8, "max_value": 1, "shift_by": 3, "type": "integer"}]},
}}


def _load(text: str) -> Config:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "c.ini")
        path.write_text(text)
        Path(d, "F-16C_50.json").write_text(json.dumps(FAKE_JSON))
        return load_config(path, json_dir=Path(d))


def _rejects(*bad: str) -> None:
    for text in bad:
        try:
            _load(text)
        except SystemExit:
            continue
        raise AssertionError(f"accepted bad config: {text!r}")


def test_midi_event_and_describe() -> None:
    note = mido.Message("note_on", channel=0, note=1, velocity=127)
    assert midi_event(note) == (("note", 1), 127, True)
    assert midi_event(note.copy(velocity=0)) == (("note", 1), 0, False)  # note_on vel 0 = release
    assert midi_event(mido.Message("note_off", note=1, velocity=0)) == (("note", 1), 0, False)
    assert midi_event(mido.Message("control_change", control=64, value=64)) == (("cc", 64), 64, True)
    assert midi_event(mido.Message("control_change", control=64, value=63)) == (("cc", 64), 63, False)
    assert midi_event(mido.Message("pitchwheel")) is None
    assert describe(note) == "ch1 note 1 press"
    assert describe(mido.Message("control_change", channel=15, control=62, value=5)) == "ch16 cc 62 = 5"


def test_load_config() -> None:
    c = _load("[midi]\nport = MIDI Mix\n[axes]\nX = 19  ; fader 1\nrz = 23\n[buttons]\n1 = note 1\n12 = CC 64\n")
    assert (c.midi.port, c.midi.channel, c.vjoy.device, c.vjoy.smooth) == ("MIDI Mix", None, 1, 0.05)
    assert c.vjoy.axes == {"x": 19, "rz": 23}
    assert c.vjoy.buttons == {1: ("note", 1), 12: ("cc", 64)}
    assert c.dcsbios == DcsConfig({}, {})
    assert _load("[midi]\n").vjoy.axes == {}  # sections optional; main() rejects an empty mapping
    _rejects("[axes]\nfoo = 1\n", "[axes]\nz = 128\n", "[axes]\nz = abc\n", "[midi]\nchannel = 17\n",
             "[buttons]\n1 = foo 3\n", "[buttons]\n1 = note\n", "[buttons]\n0 = note 1\n",
             "[buttons]\n129 = note 1\n", "[buttons]\n1 = note 128\n", "[buttons]\nx = note 1\n")


def test_load_config_dcsbios() -> None:
    c = _load("[dcsbios:F-16C_50]\nanti_coll_light_knb = cc 17\nFORM_LIGHT_KNB = cc 18\nap_pitch_sw = note 19\n"
              "laser_arm_sw = note 18\n")
    assert c.dcsbios.knobs == {"ANTI_COLL_LIGHT_KNB": (17, 7), "FORM_LIGHT_KNB": (18, 65535)}
    assert c.dcsbios.switches == {"AP_PITCH_SW": (19, 2, 17408, 768, 8), "LASER_ARM_SW": (18, 1, 17448, 8, 3)}
    _rejects("[dcsbios:F-16C_50]\nFOO = cc 1\n",  # unknown control
             "[dcsbios:F-16C_50]\nANTI_COLL_LIGHT_KNB = note 1\n",  # 8 positions, notes cycle up to 5
             "[dcsbios:F-16C_50]\nALT_BARO_SET_KNB = cc 1\n",  # no set_state input
             "[dcsbios:A-10C]\nFOO = cc 1\n",  # no json
             "[dcsbios:F-16C_50]\n[dcsbios:A-10C]\n")  # two aircraft


class _Recorder:
    def __init__(self) -> None:
        self.events: list[MidiEvent] = []

    def on_midi(self, ev: MidiEvent) -> None:
        self.events.append(ev)

    def tick(self, dt: float) -> None: ...

    def close(self) -> None: ...


def test_dispatch() -> None:
    out = _Recorder()
    callback = dispatch(2, [out])
    callback(mido.Message("note_on", channel=1, note=1, velocity=127))
    callback(mido.Message("note_on", channel=0, note=2, velocity=127))  # other channel
    callback(mido.Message("pitchwheel", channel=1))  # not mappable
    assert out.events == [(("note", 1), 127, True)]


class _FakeDll:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def SetAxis(self, value: int, device: int, axis: int) -> None:
        self.calls.append((f"axis{axis:#x}", value, device))

    def SetBtn(self, on: int, device: int, btn: int) -> None:
        self.calls.append((f"btn{btn}", int(on), device))

    def RelinquishVJD(self, device: int) -> None:
        self.calls.append(("relinquish", 0, device))


def test_vjoy_sends_changes_only() -> None:
    dll = _FakeDll()
    v = VJoy(VJoyConfig(device=2, smooth=0, axes={"z": 62}, buttons={1: ("note", 22)}), cast(ctypes.CDLL, dll))
    v.tick(0.001)
    assert dll.calls == []  # nothing received yet, nothing sent
    v.on_midi(MidiEvent(("cc", 62), 127, True))
    v.on_midi(MidiEvent(("note", 22), 127, True))
    v.on_midi(MidiEvent(("cc", 63), 0, False))  # unmapped
    v.tick(0.001)
    v.tick(0.001)  # unchanged, not resent
    assert dll.calls == [("axis0x32", 0x8000, 2), ("btn1", 1, 2)]
    v.close()
    assert dll.calls[2:] == [("btn1", 0, 2), ("relinquish", 0, 2)]  # buttons released on exit


class _FakeSock:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append(data)

    def close(self) -> None: ...


def test_dcsbios_sends_once_per_zone() -> None:
    d = DcsBios({"ANTI_COLL_LIGHT_KNB": DcsControl(17, 7)})
    d.close()
    sock = _FakeSock()
    d.sock = cast(socket.socket, sock)
    for value in (0, 5, 16, 16):  # 0 and 5 share zone 0
        d.on_midi(MidiEvent(("cc", 17), value, False))
    d.on_midi(MidiEvent(("cc", 18), 127, True))  # unmapped
    assert sock.sent == [b"ANTI_COLL_LIGHT_KNB 0\n", b"ANTI_COLL_LIGHT_KNB 1\n"]


def test_export() -> None:
    mem = bytearray(0x10000)
    pitch = DcsSwitch(19, 2, 0x4400, 0x300, 8)
    roll = DcsSwitch(21, 2, 0x4400, 0xC00, 10)
    # sync, then two blocks: pitch up + roll down at 0x4400, something else at 0x4402
    apply_export(mem, b"\x55\x55\x55\x55" + bytes.fromhex("0044 0200 0002") + bytes.fromhex("0244 0200 ffff"))
    assert (switch_state(mem, pitch), switch_state(mem, roll)) == (2, 0)
    apply_export(mem, bytes.fromhex("0044 0200 0001"))  # later packet of the same frame, no sync
    assert switch_state(mem, pitch) == 1
    apply_export(mem, bytes.fromhex("feff 0400 00000000"))  # runs past 64K: dropped, mem not resized
    apply_export(mem, bytes.fromhex("0044 0400 0002"))  # truncated: dropped
    assert len(mem) == 0x10000 and switch_state(mem, pitch) == 1


def test_led_on() -> None:
    def frames(state: int | None, max_value: int) -> str:  # LED over one second, a character per 125 ms
        return "".join("1" if led_on(state, max_value, (i + 0.5) * 0.125) else "0" for i in range(8))
    assert [frames(s, 2) for s in (2, 1, 0)] == ["11111111", "00000000", "11110000"]  # up solid, mid dark, down blinks
    assert [frames(s, 1) for s in (1, 0)] == ["11111111", "00000000"]  # 2-position: on / off
    assert len({frames(s, 4) for s in range(5)}) == 5  # 5 positions tell apart
    assert frames(None, 2) == frames(3, 2) == "00000000"  # unknown / out of range dark


class _FakeExport:
    def __init__(self) -> None:
        self.packets: list[bytes] = []

    def recv(self, size: int) -> bytes:
        if not self.packets:
            raise BlockingIOError
        return self.packets.pop(0)

    def close(self) -> None: ...


class _FakeLeds:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []

    def send(self, msg: mido.Message) -> None:
        self.sent.append((msg.note, msg.velocity))

    def close(self) -> None: ...


def _switches(**switches: DcsSwitch) -> tuple[DcsSwitches, _FakeExport, _FakeLeds, _FakeSock]:
    export, leds, sock = _FakeExport(), _FakeLeds(), _FakeSock()
    d = DcsSwitches(switches, cast(mido.ports.BaseOutput, leds), 0, cast(socket.socket, export))
    d.sock.close()
    d.sock = cast(socket.socket, sock)
    return d, export, leds, sock


F16 = bytes.fromhex("0000 0800") + b"F-16C_50"  # export block setting the aircraft name


def test_dcsbios_switches() -> None:
    d, export, leds, sock = _switches(AP_PITCH_SW=DcsSwitch(19, 2, 0x4400, 0x300, 8))
    press = MidiEvent(("note", 19), 127, True)
    d.tick(0.001)
    assert leds.sent == [(19, 0)]  # stale LED cleared before any DCS data
    d.on_midi(press)  # unknown state counts as mid
    d.on_midi(MidiEvent(("note", 19), 0, False))  # release ignored
    export.packets.append(F16 + bytes.fromhex("0044 0200 0002"))  # DCS: F-16, up
    d.tick(0.001)
    d.on_midi(press)
    export.packets.append(bytes.fromhex("0044 0200 0000"))  # DCS: down
    d.tick(0.001)
    d.tick(0.5)  # blink off
    d.on_midi(press)
    export.packets.append(bytes.fromhex("0044 0200 0001"))  # DCS: mid, e.g. autopilot dropped out
    d.tick(0.001)
    d.on_midi(press)
    assert sock.sent == [b"AP_PITCH_SW 2\n", b"AP_PITCH_SW 0\n", b"AP_PITCH_SW 1\n", b"AP_PITCH_SW 2\n"]
    assert leds.sent == [(19, 0), (19, 127), (19, 0)]  # up on; down blinks on (unchanged), then off; mid off
    d.close()
    assert leds.sent[-1] == (19, 0)


def test_dcsbios_two_position_switch() -> None:
    d, export, leds, sock = _switches(LASER_ARM_SW=DcsSwitch(18, 1, 0x4428, 0x8, 3))
    press = MidiEvent(("note", 18), 127, True)
    d.on_midi(press)  # unknown counts as off
    export.packets.append(bytes.fromhex("2844 0200 0800"))  # DCS: armed, aircraft name not seen yet
    d.tick(0.001)
    assert leds.sent == [(18, 0)]
    export.packets.append(F16)  # name arrives
    d.tick(0.001)
    d.on_midi(press)
    assert sock.sent == [b"LASER_ARM_SW 1\n", b"LASER_ARM_SW 0\n"]
    assert leds.sent == [(18, 0), (18, 127)]
    export.packets.append(bytes.fromhex("0000 0600") + b"NONE\0\0")  # spectating / dead
    d.tick(0.001)
    export.packets.append(F16)  # back in the cockpit, still armed
    d.tick(0.001)
    export.packets.append(bytes.fromhex("0000 0200 0000"))  # mission end: name cleared
    d.tick(0.001)
    d.on_midi(press)
    assert leds.sent == [(18, 0), (18, 127), (18, 0), (18, 127), (18, 0)]
    assert sock.sent[-1] == b"LASER_ARM_SW 1\n"  # position unknown again, first press goes to the top


if __name__ == "__main__":
    test_cc_to_axis()
    test_cc_to_state()
    test_smooth()
    test_midi_event_and_describe()
    test_load_config()
    test_load_config_dcsbios()
    test_dispatch()
    test_vjoy_sends_changes_only()
    test_dcsbios_sends_once_per_zone()
    test_export()
    test_led_on()
    test_dcsbios_switches()
    test_dcsbios_two_position_switch()
    print("ok")
