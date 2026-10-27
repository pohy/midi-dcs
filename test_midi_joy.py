import ctypes
import io
import json
import math
import socket
import tempfile
import wave
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
    label,
    led_on,
    switch_state,
)
from midi import MidiEvent, describe, midi_event
from midi_joy import Config, dispatch, load_config
from overlay import OverlayConfig, place
from sound import SoundConfig
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
    "ANTI_COLL_LIGHT_KNB": {"identifier": "ANTI_COLL_LIGHT_KNB", "description": "ANTI-COLL Knob", "inputs": [
        {"interface": "fixed_step"}, {"interface": "set_state", "max_value": 7}],
        "positions": ["OFF", "1", "2", "3", "4", "A", "B", "C"]},
    "FORM_LIGHT_KNB": {"identifier": "FORM_LIGHT_KNB", "inputs": [
        {"interface": "set_state", "max_value": 65535}, {"interface": "variable_step", "max_value": 65535}]},
    "ALT_BARO_SET_KNB": {"identifier": "ALT_BARO_SET_KNB", "inputs": [{"interface": "variable_step"}]},
    "AP_PITCH_SW": {"identifier": "AP_PITCH_SW", "description": "Autopilot PITCH Switch", "inputs": [
        {"interface": "set_state", "max_value": 2}], "outputs": [
        {"address": 17408, "mask": 768, "max_value": 2, "shift_by": 8, "type": "integer"}],
        "positions": ["ATT HOLD", "A/P OFF", "ALT HOLD"]},
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
    assert describe(note) == "ch1 note:1 press"
    assert describe(mido.Message("control_change", channel=15, control=62, value=5)) == "ch16 cc:62 = 5"


def test_load_config() -> None:
    c = _load("[midi]\nport = MIDI Mix\n[axes]\nX = 19  ; fader 1\nrz = 23\n[buttons]\n1 = note:1\n12 = CC:64\n")
    assert (c.midi.port, c.midi.channel, c.vjoy.device, c.vjoy.smooth) == ("MIDI Mix", None, 1, 0.05)
    assert c.midi.hold_s == 0.5
    assert c.vjoy.axes == {"x": 19, "rz": 23}
    assert c.vjoy.buttons == {1: ("note", 1), 12: ("cc", 64)}
    assert c.dcsbios == DcsConfig({}, {})
    assert c.sound == SoundConfig((), ())  # silent without [sound]
    assert c.overlay is None  # off without [overlay]
    assert _load("[midi]\n").vjoy.axes == {}  # sections optional; main() rejects an empty mapping
    _rejects("[axes]\nfoo = 1\n", "[axes]\nz = 128\n", "[axes]\nz = abc\n", "[midi]\nchannel = 17\n",
             "[buttons]\n1 = foo 3\n", "[buttons]\n1 = note\n", "[buttons]\n0 = note:1\n",
             "[buttons]\n129 = note:1\n", "[buttons]\n1 = note 1\n", "[buttons]\n1 = note:1 hold\n", "[buttons]\n1 = note:128\n", "[buttons]\nx = note:1\n",
             "[midi]\nhold_s = 0\n")


def test_load_config_dcsbios() -> None:
    c = _load("[dcsbios:F-16C_50]\nanti_coll_light_knb = cc:17\nFORM_LIGHT_KNB = cc:18\nap_pitch_sw = note:19\n"
              "laser_arm_sw = note:18\n")
    assert c.dcsbios.knobs == {
        "ANTI_COLL_LIGHT_KNB": (17, 7, "ANTI-COLL Knob", ("OFF", "1", "2", "3", "4", "A", "B", "C")),
        "FORM_LIGHT_KNB": (18, 65535, "FORM_LIGHT_KNB", ())}  # no description: identifier
    assert c.dcsbios.switches == {
        "AP_PITCH_SW": (19, 2, 17408, 768, 8, "Autopilot PITCH Switch", ("ATT HOLD", "A/P OFF", "ALT HOLD"), None,
                        None),
        "LASER_ARM_SW": (18, 1, 17448, 8, 3, "LASER_ARM_SW", (), None, None)}
    assert _load("[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 hold\n").dcsbios.switches["AP_PITCH_SW"].off == 1
    assert _load("[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 HOLD OFF:0\n").dcsbios.switches["AP_PITCH_SW"].off == 0
    hold_first = _load("[dcsbios:F-16C_50]\nAP_PITCH_SW = off:0 hold note:19\n").dcsbios.switches["AP_PITCH_SW"]
    assert (hold_first.note, hold_first.off) == (19, 0)
    assert _load("[dcsbios:F-16C_50]\nLASER_ARM_SW = DARK:1 note:18\n").dcsbios.switches["LASER_ARM_SW"].dark == 1
    hold_dark = _load("[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 dark:2 hold off:0\n").dcsbios.switches["AP_PITCH_SW"]
    assert (hold_dark.off, hold_dark.dark) == (0, 2)
    _rejects("[dcsbios:F-16C_50]\nFOO = cc:1\n",  # unknown control
             "[dcsbios:F-16C_50]\nANTI_COLL_LIGHT_KNB = note:1\n",  # 8 positions, notes cycle up to 5
             "[dcsbios:F-16C_50]\nALT_BARO_SET_KNB = cc:1\n",  # no set_state input
             "[dcsbios:F-16C_50]\nLASER_ARM_SW = note:18 hold\n",  # hold needs 3 positions
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = cc:19 hold\n",  # and a note
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 hold off:3\n",  # off position out of range
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 hold off:x\n",
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 hold off:\n",
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 off:0\n",  # off without hold
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 hold off:0 off:1\n",
             "[dcsbios:F-16C_50]\nLASER_ARM_SW = note:18 dark:2\n",  # dark position out of range
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 dark:\n",
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 dark\n",
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 dark:0 dark:1\n",
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = cc:19 dark:0\n",  # dark needs a note
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 hold:0\n",  # old syntax
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 hold 0\n",  # old syntax
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 foo\n",  # unknown option
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 hold hold\n",
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note 19\n",
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = hold\n",  # no source
             "[dcsbios:F-16C_50]\nAP_PITCH_SW = note:19 cc:19\n",  # two sources
             "[dcsbios:F-16C_50]\nAP_PITCH_SW =\n",
             "[dcsbios:A-10C]\nFOO = cc:1\n",  # no json
             "[dcsbios:F-16C_50]\n[dcsbios:A-10C]\n")  # two aircraft


def test_label() -> None:
    assert label(DcsControl(17, 7, "ANTI-COLL Knob", tuple("O1234ABC")), 6) == "ANTI-COLL Knob  B"
    assert label(DcsSwitch(19, 2, 0, 0, 0, "PITCH", ()), 1) == "PITCH  1/2"  # no position names
    assert label(DcsSwitch(19, 2, 0, 0, 0, "PITCH", ("UP", "DOWN")), 1) == "PITCH  1/2"  # names don't fit
    assert label(DcsControl(18, 65535, "FORM", ()), 32767) == "FORM  49%"  # dial


def test_load_config_overlay() -> None:
    assert _load("[overlay]\n").overlay == OverlayConfig("N", 0, 0, 1.5)
    c = _load("[overlay]\nposition = sw\noffset_x = 20\noffset_y = -20\ntimeout_s = 3\n").overlay
    assert c == OverlayConfig("SW", 20, -20, 3.0)
    _rejects("[overlay]\nposition = top\n", "[overlay]\ntimeout_s = 0\n", "[overlay]\noffset_x = 1.5\n")
    screen, box = (1920, 1080), (200, 50)
    assert place(OverlayConfig("N", 0, 60, 1), screen, box) == (860, 60)
    assert place(OverlayConfig("SW", 20, -20, 1), screen, box) == (20, 1010)
    assert place(OverlayConfig("SE", 0, 0, 1), screen, box) == (1720, 1030)
    assert place(OverlayConfig("C", 0, 0, 1), screen, box) == (860, 515)


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
    clicks: list[None] = []
    shown: list[str] = []
    d = DcsBios({"ANTI_COLL_LIGHT_KNB": DcsControl(17, 7, "ANTI-COLL", ()),
                 "FORM_LIGHT_KNB": DcsControl(19, 65535, "FORM", ())},
                lambda: clicks.append(None), shown.append)
    d.close()
    sock = _FakeSock()
    d.sock = cast(socket.socket, sock)
    for value in (0, 5, 16, 16):  # 0 and 5 share zone 0
        d.on_midi(MidiEvent(("cc", 17), value, False))
    d.on_midi(MidiEvent(("cc", 18), 127, True))  # unmapped
    assert sock.sent == [b"ANTI_COLL_LIGHT_KNB 0\n", b"ANTI_COLL_LIGHT_KNB 1\n"]
    assert len(clicks) == 2  # one per position
    d.on_midi(MidiEvent(("cc", 19), 64, False))
    assert len(sock.sent) == 3 and len(clicks) == 2  # dials don't click
    assert shown == ["ANTI-COLL  0/7", "ANTI-COLL  1/7", "FORM  50%"]  # one per command sent


def _wav(path: Path, samples: list[int], sampwidth: int = 2) -> None:
    with wave.open(str(path), "wb") as w:
        w.setparams((1, sampwidth, 44100, 0, "NONE", "not compressed"))
        w.writeframes(b"".join(s.to_bytes(sampwidth, "little", signed=True) for s in samples))


def test_load_config_sound() -> None:
    with tempfile.TemporaryDirectory() as d:
        _wav(Path(d, "a.wav"), [1000, -32768, 3])
        _wav(Path(d, "b.wav"), [0])
        c = _load(f"[sound]\nknob = {Path(d, 'a.wav')}\nknob_volume = 0.5\n")
        with wave.open(io.BytesIO(c.sound.knob[0])) as w:
            assert (w.getnchannels(), w.getframerate()) == (1, 44100)
            assert w.readframes(3) == b"".join(s.to_bytes(2, "little", signed=True) for s in (500, -16384, 1))
        assert c.sound.switch == ()
        assert len(_load(f"[sound]\nswitch = {d}\n").sound.switch) == 2  # folder: every wav
        _wav(Path(d, "24bit.wav"), [0x123456, -0x800000], sampwidth=3)
        with wave.open(io.BytesIO(_load(f"[sound]\nswitch = {Path(d, '24bit.wav')}\n").sound.switch[0])) as w:
            assert w.getsampwidth() == 2  # top two bytes kept
            assert w.readframes(2) == b"".join(s.to_bytes(2, "little", signed=True) for s in (0x1234, -0x8000))
        _wav(Path(d, "8bit.wav"), [5], sampwidth=1)
        _rejects(f"[sound]\nknob = {Path(d, '8bit.wav')}\n", f"[sound]\nknob = {Path(d, 'none.wav')}\n",
                 f"[sound]\nswitch = {Path(d, 'a.wav')}\nswitch_volume = 1.5\n",
                 "[sound]\nknob = .\n")  # folder without wavs


def test_export() -> None:
    mem = bytearray(0x10000)
    pitch = DcsSwitch(19, 2, 0x4400, 0x300, 8, "PITCH", ())
    roll = DcsSwitch(21, 2, 0x4400, 0xC00, 10, "ROLL", ())
    # sync, then two blocks: pitch up + roll down at 0x4400, something else at 0x4402
    apply_export(mem, b"\x55\x55\x55\x55" + bytes.fromhex("0044 0200 0002") + bytes.fromhex("0244 0200 ffff"))
    assert (switch_state(mem, pitch), switch_state(mem, roll)) == (2, 0)
    apply_export(mem, bytes.fromhex("0044 0200 0001"))  # later packet of the same frame, no sync
    assert switch_state(mem, pitch) == 1
    apply_export(mem, bytes.fromhex("feff 0400 00000000"))  # runs past 64K: dropped, mem not resized
    apply_export(mem, bytes.fromhex("0044 0400 0002"))  # truncated: dropped
    assert len(mem) == 0x10000 and switch_state(mem, pitch) == 1


def test_led_on() -> None:
    def frames(state: int | None, max_value: int, off: int | None = None, dark: int | None = None) -> str:
        sw = DcsSwitch(1, max_value, 0, 0, 0, "", (), off, dark)  # 1 s, a char per 125 ms
        return "".join("1" if led_on(state, sw, (i + 0.5) * 0.125) else "0" for i in range(8))
    assert [frames(s, 2) for s in (2, 1, 0)] == ["11111111", "00000000", "11110000"]  # up solid, mid dark, down blinks
    assert [frames(s, 2, off=1) for s in (2, 1, 0)] == ["11111111", "00000000", "11110000"]  # hold, mid off: same
    assert [frames(s, 2, off=0) for s in (2, 1, 0)] == ["11111111", "11110000", "00000000"]  # hold, bottom off dark
    assert [frames(s, 2, dark=2) for s in (2, 1, 0)] == ["00000000", "11111111", "11110000"]  # top dark
    assert [frames(s, 2, off=0, dark=1) for s in (2, 1, 0)] == ["11111111", "00000000", "11110000"]  # beats off
    assert [frames(s, 1) for s in (1, 0)] == ["11111111", "00000000"]  # 2-position: on / off
    assert [frames(s, 1, dark=1) for s in (1, 0)] == ["00000000", "11111111"]  # inverted
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


def _switches(clicks: list[None] | None = None,
              **switches: DcsSwitch) -> tuple[DcsSwitches, _FakeExport, _FakeLeds, _FakeSock, list[str]]:
    export, leds, sock, shown, played = _FakeExport(), _FakeLeds(), _FakeSock(), list[str](), clicks
    d = DcsSwitches(switches, cast(mido.ports.BaseOutput, leds), 0, cast(socket.socket, export), 0.5, shown.append,
                    lambda: played.append(None) if played is not None else None)
    d.sock.close()
    d.sock = cast(socket.socket, sock)
    return d, export, leds, sock, shown


F16 = bytes.fromhex("0000 0800") + b"F-16C_50"  # export block setting the aircraft name


def test_dcsbios_switches() -> None:
    clicks: list[None] = []
    d, export, leds, sock, shown = _switches(clicks,
                                             AP_PITCH_SW=DcsSwitch(19, 2, 0x4400, 0x300, 8, "PITCH", ("D", "M", "U")))
    press = MidiEvent(("note", 19), 127, True)
    d.tick(0.001)
    assert leds.sent == [(19, 0)]  # stale LED cleared before any DCS data
    d.on_midi(press)  # unknown state counts as mid
    d.on_midi(MidiEvent(("note", 19), 0, False))  # release ignored
    d.tick(0.001)
    export.packets.append(F16 + bytes.fromhex("0044 0200 0002"))  # DCS: F-16, up
    d.tick(0.001)
    for pos in "01", "00", "01":  # DCS: mid, down, mid
        d.on_midi(press)
        d.tick(0.001)
        export.packets.append(bytes.fromhex("0044 0200 00" + pos))
        d.tick(0.001)
    d.on_midi(press)
    d.tick(0.001)
    d.tick(1)  # no answer, e.g. autopilot refused to engage
    export.packets.append(bytes.fromhex("0044 0200 0002"))  # moved up in DCS, long after the press
    d.tick(0.001)
    # physical order, never down -> up or up -> down in one command
    assert sock.sent == [b"AP_PITCH_SW 2\n", b"AP_PITCH_SW 1\n", b"AP_PITCH_SW 0\n", b"AP_PITCH_SW 1\n",
                         b"AP_PITCH_SW 2\n"]
    assert len(clicks) == 5  # one per press, also the refused one
    # what DCS reported after each press, the unchanged mid on the refused one; the late move didn't show
    assert shown == ["DCS-BIOS connected  F-16C_50", "PITCH  U", "PITCH  M", "PITCH  D", "PITCH  M", "PITCH  M"]
    assert leds.sent == [(19, 0), (19, 127), (19, 0), (19, 127), (19, 0), (19, 127)]  # down = slow blink, on
    d.close()
    assert leds.sent[-1] == (19, 0)


def test_dcsbios_two_position_switch() -> None:
    d, export, leds, sock, shown = _switches(LASER_ARM_SW=DcsSwitch(18, 1, 0x4428, 0x8, 3, "LASER", ()))
    press = MidiEvent(("note", 18), 127, True)
    d.on_midi(press)  # unknown counts as off
    d.tick(0.001)
    export.packets.append(bytes.fromhex("2844 0200 0800"))  # DCS: armed, aircraft name not seen yet
    d.tick(1.5)
    assert leds.sent == [(18, 0)] and shown == []  # position still unknown when the watch ends
    export.packets.append(F16)  # name arrives
    d.tick(0.001)
    d.on_midi(press)
    d.tick(0.001)
    assert sock.sent == [b"LASER_ARM_SW 1\n", b"LASER_ARM_SW 0\n"]
    assert leds.sent == [(18, 0), (18, 127)]
    d.tick(1.5)  # DCS didn't disarm
    export.packets.append(bytes.fromhex("0000 0600") + b"NONE\0\0")  # spectating / dead
    d.tick(0.001)
    export.packets.append(F16)  # back in the cockpit, still armed
    d.tick(0.001)
    export.packets.append(bytes.fromhex("0000 0200 0000"))  # mission end: name cleared
    d.tick(0.001)
    d.on_midi(press)
    d.tick(0.001)
    assert leds.sent == [(18, 0), (18, 127), (18, 0), (18, 127), (18, 0)]
    assert sock.sent[-1] == b"LASER_ARM_SW 1\n"  # position unknown again, first press goes to the top
    on, off = "DCS-BIOS connected  F-16C_50", "DCS-BIOS disconnected"
    assert shown == [on, "LASER  1/1", off, on, off]


def test_dcsbios_hold_switch() -> None:
    clicks: list[None] = []
    d, export, leds, sock, shown = _switches(
        clicks, AP_PITCH_SW=DcsSwitch(19, 2, 0x4400, 0x300, 8, "PITCH", ("D", "M", "U"), off=1))
    press, release = MidiEvent(("note", 19), 127, True), MidiEvent(("note", 19), 0, False)

    def dcs(pos: int) -> None:
        export.packets.append(bytes.fromhex(f"0044 0200 000{pos}"))
        d.tick(0.001)

    def tap() -> None:
        d.on_midi(press)
        d.on_midi(release)
        d.tick(0.001)

    export.packets.append(F16)
    dcs(2)  # up
    tap()  # short: down, through the middle
    d.tick(0.001)
    assert sock.sent == [b"AP_PITCH_SW 1\n"]  # next step waits for DCS
    dcs(1)
    dcs(0)
    tap()  # short: up again
    dcs(1)
    dcs(2)
    assert sock.sent[1:] == [b"AP_PITCH_SW 0\n", b"AP_PITCH_SW 1\n", b"AP_PITCH_SW 2\n"]
    d.on_midi(press)
    d.tick(0.001)  # press seen
    d.tick(0.4)
    assert len(sock.sent) == 4  # not long yet
    d.tick(0.2)  # long: off, while still held
    d.on_midi(release)
    d.tick(0.001)
    dcs(1)
    assert sock.sent[4:] == [b"AP_PITCH_SW 1\n"] and leds.sent[-1] == (19, 0)  # LED dark in off
    d.on_midi(press)
    d.tick(0.001)
    d.tick(0.6)  # long when already off: nothing sent, position shows
    d.on_midi(release)
    tap()  # short from off: up
    dcs(2)
    assert sock.sent[5:] == [b"AP_PITCH_SW 2\n"]
    tap()  # short: DCS refuses even the first step
    d.tick(0.3)  # not reported: sent again
    d.tick(1)  # watch ends, travel given up
    dcs(1)  # moved with the mouse later: travel doesn't resume
    assert sock.sent[6:] == [b"AP_PITCH_SW 1\n", b"AP_PITCH_SW 1\n"]
    assert len(clicks) == 5  # one per move, none for the long press on off
    assert shown == ["DCS-BIOS connected  F-16C_50", "PITCH  M", "PITCH  D", "PITCH  M", "PITCH  U", "PITCH  M",
                     "PITCH  M", "PITCH  U", "PITCH  U"]
    tap()  # from off: up
    tap()  # down again before DCS took the top: waits for it, then passes the middle
    dcs(2)
    dcs(1)
    tap()  # up again before DCS took the bottom
    dcs(0)
    dcs(1)
    dcs(2)
    assert sock.sent[8:] == [b"AP_PITCH_SW 2\n", b"AP_PITCH_SW 1\n", b"AP_PITCH_SW 0\n", b"AP_PITCH_SW 1\n",
                             b"AP_PITCH_SW 2\n"]
    assert not d.travel
    tap()  # down
    dcs(1)
    d.tick(0.3)  # DCS dropped the bottom: sent again
    dcs(0)
    tap()  # up
    dcs(1)  # DCS drops the top
    d.tick(0.05)  # under STEP_S, still in flight; not on it, float error decides that side
    tap()  # down again while the top is still awaited
    d.tick(0.2)  # top never reported: down from the middle DCS shows
    dcs(0)
    assert sock.sent[13:] == [b"AP_PITCH_SW 1\n", b"AP_PITCH_SW 0\n", b"AP_PITCH_SW 0\n", b"AP_PITCH_SW 1\n",
                              b"AP_PITCH_SW 2\n", b"AP_PITCH_SW 0\n"]
    assert not d.travel


def test_dcsbios_hold_switch_off_at_bottom() -> None:
    d, export, leds, sock, _ = _switches(ANTI_SKID_SW=DcsSwitch(22, 2, 0x4400, 0x300, 8, "SKID", (), off=0))
    press, release = MidiEvent(("note", 22), 127, True), MidiEvent(("note", 22), 0, False)

    def dcs(pos: int) -> None:
        export.packets.append(bytes.fromhex(f"0044 0200 000{pos}"))
        d.tick(0.001)

    export.packets.append(F16)
    dcs(0)  # off
    assert leds.sent == [(22, 0)]
    d.on_midi(press)
    d.on_midi(release)
    d.tick(0.001)  # short from off: up, through the middle
    dcs(1)
    dcs(2)
    d.on_midi(press)
    d.on_midi(release)
    d.tick(0.001)  # short: the other non-off position, the middle
    dcs(1)
    d.on_midi(press)
    d.tick(0.001)
    d.tick(0.6)  # long: off
    dcs(0)
    assert sock.sent == [b"ANTI_SKID_SW 1\n", b"ANTI_SKID_SW 2\n", b"ANTI_SKID_SW 1\n", b"ANTI_SKID_SW 0\n"]
    assert leds.sent[-1] == (22, 0)


def test_dcsbios_export_timeout() -> None:
    d, export, leds, _, shown = _switches(LASER_ARM_SW=DcsSwitch(18, 1, 0x4428, 0x8, 3, "LASER", ()))
    export.packets.append(F16 + bytes.fromhex("2844 0200 0800"))  # DCS: F-16, armed
    d.tick(0.001)
    d.tick(2.9)
    assert leds.sent == [(18, 127)] and len(shown) == 1  # quiet, but not long enough
    d.tick(0.2)  # 3 s without export: DCS crashed, no mission end frame
    assert leds.sent == [(18, 127), (18, 0)] and d.state["LASER_ARM_SW"] is None
    d.tick(5)
    export.packets.append(F16)  # DCS back, switch data not resent yet
    d.tick(0.001)
    assert d.state["LASER_ARM_SW"] == 0  # stale armed state forgotten
    assert shown == ["DCS-BIOS connected  F-16C_50", "DCS-BIOS disconnected", "DCS-BIOS connected  F-16C_50"]


if __name__ == "__main__":
    test_cc_to_axis()
    test_cc_to_state()
    test_smooth()
    test_midi_event_and_describe()
    test_load_config()
    test_load_config_dcsbios()
    test_label()
    test_load_config_overlay()
    test_dispatch()
    test_vjoy_sends_changes_only()
    test_dcsbios_sends_once_per_zone()
    test_load_config_sound()
    test_export()
    test_led_on()
    test_dcsbios_switches()
    test_dcsbios_two_position_switch()
    test_dcsbios_hold_switch()
    test_dcsbios_hold_switch_off_at_bottom()
    test_dcsbios_export_timeout()
    print("ok")
