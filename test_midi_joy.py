import builtins
import ctypes
import io
import json
import math
import socket
import tempfile
import wave
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import mido

import midi_joy
import vjoy
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
from midi import Device, MidiEvent, Source, describe, midi_event
from midi_joy import Config, dispatch, load_config
from overlay import OverlayConfig, place
from sound import SoundConfig
from vjoy import VJoy, VJoyConfig, cc_to_axis, smooth

MIX = "MIDI Mix"


def note(n: int) -> Source:
    return MIX, "note", n


def cc(n: int) -> Source:
    return MIX, "cc", n


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
    "MASTER_CAUTION": {"identifier": "MASTER_CAUTION", "api_variant": "momentary_last_position", "inputs": [
        {"interface": "set_state", "max_value": 1}], "outputs": [
        {"address": 17408, "mask": 32768, "max_value": 1, "shift_by": 15, "type": "integer"}]},
    "MFD_L_BRT_SW": {"identifier": "MFD_L_BRT_SW", "description": "MFD Left BRT Rocker Switch", "inputs": [
        {"interface": "set_state", "max_value": 2}], "outputs": [
        {"address": 17506, "mask": 12, "max_value": 2, "shift_by": 2, "type": "integer"}]},
}}


def _load(text: str) -> Config:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d, "c.toml")
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
    on = mido.Message("note_on", channel=0, note=1, velocity=127)
    assert midi_event(on, MIX) == (note(1), 127, True)
    assert midi_event(on.copy(velocity=0), MIX) == (note(1), 0, False)  # note_on vel 0 = release
    assert midi_event(mido.Message("note_off", note=1, velocity=0), MIX) == (note(1), 0, False)
    assert midi_event(mido.Message("control_change", control=64, value=64), MIX) == (cc(64), 64, True)
    assert midi_event(mido.Message("control_change", control=64, value=63), MIX) == (cc(64), 63, False)
    assert midi_event(mido.Message("pitchwheel"), MIX) is None
    assert describe(on) == "ch1 note:1 press"
    assert describe(mido.Message("control_change", channel=15, control=62, value=5)) == "ch16 cc:62 = 5"


def test_load_config() -> None:
    c = _load('[midi."MIDI Mix"]\nchannel = 2\n[midi.left]\nport = "LPProMK3"\nchannel = 1\nleds = false\n'
              '[vjoy.axes."MIDI Mix"]\nX = 19  # fader 1\n[vjoy.axes.Pedals]\nrz = 23\n'
              '[vjoy.buttons."MIDI Mix"]\n1 = { note = 1 }\n12 = { cc = 64 }\n[vjoy.buttons.left]\n2 = { note = 65 }\n')
    assert (c.midi.devices, c.midi.hold_s, c.vjoy.device, c.vjoy.smooth) == (
        {MIX: Device(MIX, 2), "left": Device("LPProMK3", 1, False)}, 0.5, 1, 0.05)  # port defaults to the device name
    assert c.vjoy.axes == {"x": cc(19), "rz": ("Pedals", "cc", 23)}
    assert c.vjoy.buttons == {1: note(1), 12: cc(64), 2: ("left", "note", 65)}
    assert midi_joy.devices(c) == {MIX: Device(MIX, 2), "left": Device("LPProMK3", 1, False),
                                   "Pedals": Device("Pedals", None)}  # without [midi."DEVICE"]: any channel
    assert midi_joy.summary(c, [MIX, "left"]) == "x=cc:19 btn1=note:1 btn12=cc:64 btn2=note:65"
    # devices sharing a port open it once, matched by the port's real name
    devs = {"left": Device("LPProMK3", 1), "right": Device("LPPro", 2), MIX: Device(MIX, None)}
    assert midi_joy.by_port(devs, ["MIDI Mix 0", "LPProMK3 MIDI 1"]) == {
        "LPProMK3 MIDI 1": {"left": devs["left"], "right": devs["right"]}, "MIDI Mix 0": {MIX: devs[MIX]}}
    assert c.dcsbios == DcsConfig({}, {})
    assert c.sound == SoundConfig((), ())  # silent without [sound]
    assert c.overlay is None  # off without [overlay]
    assert _load("[midi]\n").vjoy.axes == {}  # tables optional; main() rejects an empty mapping
    assert _load("[midi]\nhold_s = 1\n").midi.hold_s == 1.0  # an integer for a number
    ax, bt = "[vjoy.axes.M]\n", "[vjoy.buttons.M]\n"
    _rejects(ax + "foo = 1\n", ax + "z = 128\n", ax + 'z = "62"\n', "[midi.M]\nchannel = 17\n",
             "[midi.M]\nchannel = true\n",  # true/false isn't an integer
             "[midi.M]\nleds = 0\n", "[midi.M]\nport = 1\n",
             '[midi]\nport = "MIDI Mix"\n',  # old syntax, devices are tables
             "[vjoy.axes]\nz = 62\n", "[vjoy.buttons]\n1 = { note = 1 }\n",  # no device
             ax + "z = 1\n[vjoy.axes.N]\nZ = 2\n",  # one axis from two devices
             bt + "1 = { note = 1 }\n[vjoy.buttons.N]\n1 = { cc = 1 }\n",
             '[vjoy]\nsmooth = "0.1"\n', "midi = 1\n",  # not a table
             bt + '1 = "note:1"\n',  # old syntax
             bt + "1 = 1\n", bt + "1 = {}\n", bt + "1 = { note = 1, cc = 1 }\n",
             bt + "0 = { note = 1 }\n", bt + "129 = { note = 1 }\n",
             bt + "x = { note = 1 }\n", bt + "1 = { note = 1, hold = true }\n",
             bt + "1 = { note = 128 }\n", bt + '1 = { note = "1" }\n',
             "[midi]\nhold_s = 0\n",
             "[midi\n", "[midi.M]\nchannel = 1\nchannel = 2\n")  # toml syntax: unclosed table, duplicate key


def test_load_config_dcsbios() -> None:
    f16 = '[dcsbios.F-16C_50."MIDI Mix"]\n'
    c = _load(f16 + "anti_coll_light_knb = { cc = 17 }\nFORM_LIGHT_KNB = { cc = 18 }\nap_pitch_sw = { note = 19 }\n"
              "laser_arm_sw = { note = 18 }\n")
    assert c.dcsbios.knobs == {
        "ANTI_COLL_LIGHT_KNB": (cc(17), 7, "ANTI-COLL Knob", ("OFF", "1", "2", "3", "4", "A", "B", "C")),
        "FORM_LIGHT_KNB": (cc(18), 65535, "FORM_LIGHT_KNB", ())}  # no description: identifier
    assert c.dcsbios.switches == {
        "AP_PITCH_SW": (note(19), 2, 17408, 768, 8, "Autopilot PITCH Switch", ("ATT HOLD", "A/P OFF", "ALT HOLD"),
                        None, None, False, None),
        "LASER_ARM_SW": (note(18), 1, 17448, 8, 3, "LASER_ARM_SW", (), None, None, False, None)}
    two = _load(f16 + "LASER_ARM_SW = { note = 18 }\n[dcsbios.F-16C_50.Pedals]\nAP_PITCH_SW = { note = 18 }\n")
    assert [s.src for s in two.dcsbios.switches.values()] == [note(18), ("Pedals", "note", 18)]
    def pitch(src: str) -> DcsSwitch:
        return _load(f"{f16}AP_PITCH_SW = {src}\n").dcsbios.switches["AP_PITCH_SW"]
    assert pitch("{ note = 19, hold = true }").off == 1
    assert pitch("{ note = 19, hold = true, off = 0 }").off == 0
    assert pitch("{ note = 19, hold = false }").off is None
    assert _load(f16 + "LASER_ARM_SW = { note = 18, dark = 1 }\n").dcsbios.switches["LASER_ARM_SW"].dark == 1
    hold_dark = pitch("{ note = 19, dark = 2, hold = true, off = 0 }")
    assert (hold_dark.off, hold_dark.dark) == (0, 2)

    def caution(src: str) -> bool:
        return _load(f"{f16}MASTER_CAUTION = {src}\n").dcsbios.switches["MASTER_CAUTION"].momentary
    assert caution("{ note = 25 }") and caution("{ note = 25, latch = false }")  # push button: momentary
    assert not caution("{ note = 25, latch = true }")
    rocker = _load(f16 + "MFD_L_BRT_SW = { up = 31, down = 32 }\n").dcsbios.switches["MFD_L_BRT_SW"]
    assert (rocker.src, rocker.down, rocker.max_value, rocker.momentary) == (note(31), note(32), 2, True)
    _rejects(f16 + "FOO = { cc = 1 }\n",  # unknown control
             f16 + "ANTI_COLL_LIGHT_KNB = { note = 1 }\n",  # 8 positions, notes cycle up to 5
             f16 + "ALT_BARO_SET_KNB = { cc = 1 }\n",  # no set_state input
             f16 + "LASER_ARM_SW = { note = 18, hold = true }\n",  # hold needs 3 positions
             f16 + "AP_PITCH_SW = { cc = 19, hold = true }\n",  # and a note
             f16 + "AP_PITCH_SW = { note = 19, hold = true, off = 3 }\n",  # off position out of range
             f16 + 'AP_PITCH_SW = { note = 19, hold = true, off = "0" }\n',
             f16 + "AP_PITCH_SW = { note = 19, off = 0 }\n",  # off without hold
             f16 + "AP_PITCH_SW = { note = 19, hold = false, off = 0 }\n",
             f16 + "LASER_ARM_SW = { note = 18, dark = 2 }\n",  # dark position out of range
             f16 + "AP_PITCH_SW = { note = 19, dark = -1 }\n",
             f16 + "AP_PITCH_SW = { note = 19, dark = true }\n",
             f16 + "AP_PITCH_SW = { cc = 19, dark = 0 }\n",  # dark needs a note
             f16 + "AP_PITCH_SW = { note = 19, hold = 1 }\n",  # hold is true/false
             f16 + "LASER_ARM_SW = { note = 18, latch = true }\n",  # latch needs a push button
             f16 + "MASTER_CAUTION = { cc = 25, latch = true }\n",  # and a note
             f16 + "MASTER_CAUTION = { note = 25, latch = 1 }\n",
             f16 + "MFD_L_BRT_SW = { up = 31 }\n",  # rocker needs both
             f16 + "MFD_L_BRT_SW = { up = 31, down = 32, hold = true }\n",  # and nothing else
             f16 + "MFD_L_BRT_SW = { up = 31, down = 32, note = 33 }\n",
             f16 + "MFD_L_BRT_SW = { up = 31, down = 128 }\n",
             f16 + 'MFD_L_BRT_SW = { up = 31, down = "32" }\n',
             f16 + "LASER_ARM_SW = { up = 31, down = 32 }\n",  # needs 3 positions
             f16 + "AP_PITCH_SW = { note = 19, foo = true }\n",  # unknown option
             f16 + "AP_PITCH_SW = { note = 19, HOLD = true }\n",  # keys are lower case
             f16 + 'AP_PITCH_SW = "note:19 hold"\n',  # old syntax
             f16 + "AP_PITCH_SW = { hold = true }\n",  # no source
             f16 + "AP_PITCH_SW = { note = 19, cc = 19 }\n",  # two sources
             f16 + "AP_PITCH_SW = {}\n",
             "[dcsbios.A-10C.M]\nFOO = { cc = 1 }\n",  # no json
             "[dcsbios.F-16C_50]\nAP_PITCH_SW = { note = 19 }\n",  # no device
             "[dcsbios.F-16C_50]\nM = 1\n",
             f16 + "AP_PITCH_SW = { note = 19 }\n[dcsbios.F-16C_50.N]\nap_pitch_sw = { cc = 1 }\n",  # two devices
             "[dcsbios]\nF-16C_50 = 1\n",  # aircraft not a table
             f16 + "[dcsbios.A-10C]\n")  # two aircraft


def test_label() -> None:
    assert label(DcsControl(cc(17), 7, "ANTI-COLL Knob", tuple("O1234ABC")), 6) == "ANTI-COLL Knob  B"
    assert label(DcsSwitch(note(19), 2, 0, 0, 0, "PITCH", ()), 1) == "PITCH  1/2"  # no position names
    assert label(DcsSwitch(note(19), 2, 0, 0, 0, "PITCH", ("UP", "DOWN")), 1) == "PITCH  1/2"  # names don't fit
    assert label(DcsControl(cc(18), 65535, "FORM", ()), 32767) == "FORM  49%"  # dial


def test_load_config_overlay() -> None:
    assert _load("[overlay]\n").overlay == OverlayConfig("N", 0, 0, 1.5)
    c = _load('[overlay]\nposition = "sw"\noffset_x = 20\noffset_y = -20\ntimeout_s = 3\n').overlay
    assert c == OverlayConfig("SW", 20, -20, 3.0)
    _rejects('[overlay]\nposition = "top"\n', "[overlay]\ntimeout_s = 0\n", "[overlay]\noffset_x = 1.5\n")
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
    callback = dispatch({MIX: Device(MIX, 2)}, [out])
    callback(mido.Message("note_on", channel=1, note=1, velocity=127))
    callback(mido.Message("note_on", channel=0, note=2, velocity=127))  # other channel
    callback(mido.Message("pitchwheel", channel=1))  # not mappable
    assert out.events == [(note(1), 127, True)]
    out = _Recorder()
    lp = {"left": Device("LP", 1), "right": Device("LP", 2)}  # one port, a device per channel
    callback = dispatch(lp, [out])
    callback(mido.Message("note_on", channel=1, note=65, velocity=127))
    callback(mido.Message("note_on", channel=2, note=65, velocity=127))  # no device on ch3
    callback(mido.Message("sysex", data=[1]))  # no channel at all
    assert out.events == [(("right", "note", 65), 127, True)]
    with redirect_stdout(io.StringIO()) as printed:
        show = midi_joy.monitor(lp | {"any": Device("LP", None)})
        show(mido.Message("note_on", channel=1, note=65, velocity=127))
        show(mido.Message("note_on", channel=2, note=65, velocity=127))
        show(mido.Message("sysex", data=[1]))
    assert printed.getvalue().splitlines() == ['"right" "any" ch2 note:65 press', '"any" ch3 note:65 press',
                                               "sysex data=(1) time=0"]


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
    v = VJoy(VJoyConfig(device=2, smooth=0, axes={"z": cc(62)}, buttons={1: note(22)}), cast(ctypes.CDLL, dll))
    v.tick(0.001)
    assert dll.calls == []  # nothing received yet, nothing sent
    v.on_midi(MidiEvent(cc(62), 127, True))
    v.on_midi(MidiEvent(("Pedals", "cc", 62), 0, False))  # same cc, other device
    v.on_midi(MidiEvent(note(22), 127, True))
    v.on_midi(MidiEvent(cc(63), 0, False))  # unmapped
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
    d = DcsBios({"ANTI_COLL_LIGHT_KNB": DcsControl(cc(17), 7, "ANTI-COLL", ()),
                 "FORM_LIGHT_KNB": DcsControl(cc(19), 65535, "FORM", ())},
                lambda: clicks.append(None), shown.append)
    d.close()
    sock = _FakeSock()
    d.sock = cast(socket.socket, sock)
    for value in (0, 5, 16, 16):  # 0 and 5 share zone 0
        d.on_midi(MidiEvent(cc(17), value, False))
    d.on_midi(MidiEvent(cc(18), 127, True))  # unmapped
    assert sock.sent == [b"ANTI_COLL_LIGHT_KNB 0\n", b"ANTI_COLL_LIGHT_KNB 1\n"]
    assert len(clicks) == 2  # one per position
    d.on_midi(MidiEvent(cc(19), 64, False))
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
        # absolute paths for the test, in literal 'strings' so backslashes aren't escapes
        c = _load(f"[sound]\nknob = '{Path(d, 'a.wav')}'\nknob_volume = 0.5\n")
        with wave.open(io.BytesIO(c.sound.knob[0])) as w:
            assert (w.getnchannels(), w.getframerate()) == (1, 44100)
            assert w.readframes(3) == b"".join(s.to_bytes(2, "little", signed=True) for s in (500, -16384, 1))
        assert c.sound.switch == ()
        assert len(_load(f"[sound]\nswitch = '{d}'\n").sound.switch) == 2  # folder: every wav
        _wav(Path(d, "24bit.wav"), [0x123456, -0x800000], sampwidth=3)
        with wave.open(io.BytesIO(_load(f"[sound]\nswitch = '{Path(d, '24bit.wav')}'\n").sound.switch[0])) as w:
            assert w.getsampwidth() == 2  # top two bytes kept
            assert w.readframes(2) == b"".join(s.to_bytes(2, "little", signed=True) for s in (0x1234, -0x8000))
        _wav(Path(d, "8bit.wav"), [5], sampwidth=1)
        _rejects(f"[sound]\nknob = '{Path(d, '8bit.wav')}'\n", f"[sound]\nknob = '{Path(d, 'none.wav')}'\n",
                 f"[sound]\nswitch = '{Path(d, 'a.wav')}'\nswitch_volume = 1.5\n",
                 "[sound]\nknob = '.'\n")  # folder without wavs


def test_export() -> None:
    mem = bytearray(0x10000)
    pitch = DcsSwitch(note(19), 2, 0x4400, 0x300, 8, "PITCH", ())
    roll = DcsSwitch(note(21), 2, 0x4400, 0xC00, 10, "ROLL", ())
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
        sw = DcsSwitch(note(1), max_value, 0, 0, 0, "", (), off, dark)  # 1 s, a char per 125 ms
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
        self.channel = -1

    def send(self, msg: mido.Message) -> None:
        self.sent.append((msg.note, msg.velocity))
        self.channel = msg.channel

    def close(self) -> None: ...


def _switches(clicks: list[None] | None = None,
              **switches: DcsSwitch) -> tuple[DcsSwitches, _FakeExport, _FakeLeds, _FakeSock, list[str]]:
    export, leds, sock, shown, played = _FakeExport(), _FakeLeds(), _FakeSock(), list[str](), clicks
    d = DcsSwitches(switches, {MIX: (cast(mido.ports.BaseOutput, leds), 0)}, cast(socket.socket, export), 0.5,
                    shown.append, lambda: played.append(None) if played is not None else None)
    d.sock.close()
    d.sock = cast(socket.socket, sock)
    return d, export, leds, sock, shown


F16 = bytes.fromhex("0000 0800") + b"F-16C_50"  # export block setting the aircraft name


def test_dcsbios_switches() -> None:
    clicks: list[None] = []
    d, export, leds, sock, shown = _switches(clicks,
                                             AP_PITCH_SW=DcsSwitch(note(19), 2, 0x4400, 0x300, 8, "PITCH", ("D", "M", "U")))
    press = MidiEvent(note(19), 127, True)
    d.tick(0.001)
    assert leds.sent == [(19, 0)]  # stale LED cleared before any DCS data
    d.on_midi(press)  # unknown state counts as mid
    d.on_midi(MidiEvent(note(19), 0, False))  # release ignored
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
    d, export, leds, sock, shown = _switches(LASER_ARM_SW=DcsSwitch(note(18), 1, 0x4428, 0x8, 3, "LASER", ()))
    press = MidiEvent(note(18), 127, True)
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


def test_dcsbios_push_button() -> None:
    clicks: list[None] = []
    d, export, leds, sock, shown = _switches(
        clicks, MASTER_CAUTION=DcsSwitch(note(25), 1, 0x4400, 0x8000, 15, "CAUTION", (), momentary=True))
    export.packets.append(F16)
    d.tick(0.001)
    d.on_midi(MidiEvent(note(25), 127, True))
    d.tick(0.001)
    export.packets.append(bytes.fromhex("0044 0200 0080"))  # DCS: pressed
    d.tick(0.001)
    d.on_midi(MidiEvent(note(25), 0, False))
    d.tick(0.001)
    export.packets.append(bytes.fromhex("0044 0200 0000"))  # DCS: released
    d.tick(0.001)
    assert sock.sent == [b"MASTER_CAUTION 1\n", b"MASTER_CAUTION 0\n"]  # held down while the note is
    assert len(clicks) == 1  # on the press only
    assert leds.sent == [(25, 0), (25, 127), (25, 0)]  # lit while DCS reports it pressed
    assert shown == ["DCS-BIOS connected  F-16C_50"]  # no overlay, the LED shows it


def test_dcsbios_rocker() -> None:
    clicks: list[None] = []
    d, export, leds, sock, shown = _switches(
        clicks, MFD_L_BRT_SW=DcsSwitch(note(31), 2, 0x4462, 0xC, 2, "BRT", (), momentary=True, down=note(32)))
    up, down = note(31), note(32)

    def dcs(pos: int) -> None:
        export.packets.append(bytes.fromhex(f"6244 0200 0{pos * 4:x}00"))
        d.tick(0.001)

    export.packets.append(F16)
    dcs(1)
    assert leds.sent == [(31, 0), (32, 0)]  # centered: both dark
    d.on_midi(MidiEvent(up, 127, True))
    d.tick(0.001)
    dcs(2)  # DCS: held up
    d.on_midi(MidiEvent(up, 0, False))
    d.tick(0.001)
    dcs(1)
    d.on_midi(MidiEvent(down, 127, True))
    d.tick(0.001)
    dcs(0)  # DCS: held down
    d.on_midi(MidiEvent(up, 127, True))  # other side while down is still held: last press wins
    d.on_midi(MidiEvent(up, 0, False))  # any release centers
    d.tick(0.001)
    assert sock.sent == [b"MFD_L_BRT_SW 2\n", b"MFD_L_BRT_SW 1\n", b"MFD_L_BRT_SW 0\n", b"MFD_L_BRT_SW 2\n",
                         b"MFD_L_BRT_SW 1\n"]
    assert len(clicks) == 3  # one per press
    assert leds.sent == [(31, 0), (32, 0), (31, 127), (31, 0), (32, 127)]  # each side lit while DCS holds it
    assert shown == ["DCS-BIOS connected  F-16C_50"]  # no overlay, the LEDs show it
    d.close()
    assert leds.sent[-2:] == [(31, 0), (32, 0)]


def test_dcsbios_hold_switch() -> None:
    clicks: list[None] = []
    d, export, leds, sock, shown = _switches(
        clicks, AP_PITCH_SW=DcsSwitch(note(19), 2, 0x4400, 0x300, 8, "PITCH", ("D", "M", "U"), off=1))
    press, release = MidiEvent(note(19), 127, True), MidiEvent(note(19), 0, False)

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
    d, export, leds, sock, _ = _switches(ANTI_SKID_SW=DcsSwitch(note(22), 2, 0x4400, 0x300, 8, "SKID", (), off=0))
    press, release = MidiEvent(note(22), 127, True), MidiEvent(note(22), 0, False)

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


def test_dcsbios_switches_per_device() -> None:
    mix, pedals, sock = _FakeLeds(), _FakeLeds(), _FakeSock()
    d = DcsSwitches({"LASER_ARM_SW": DcsSwitch(note(18), 1, 0x4428, 0x8, 3, "LASER", ()),
                     "AP_PITCH_SW": DcsSwitch(("Pedals", "note", 18), 2, 0x4400, 0x300, 8, "PITCH", ()),
                     "RF_SW": DcsSwitch(("Dark", "note", 18), 2, 0x4402, 0x300, 8, "RF", ())},  # leds = false
                    {MIX: (cast(mido.ports.BaseOutput, mix), 0), "Pedals": (cast(mido.ports.BaseOutput, pedals), 3)},
                    cast(socket.socket, _FakeExport()), 0.5)
    d.sock.close()
    d.sock = cast(socket.socket, sock)
    d.on_midi(MidiEvent(("Pedals", "note", 18), 127, True))  # same note as LASER_ARM_SW, other device
    d.on_midi(MidiEvent(("Dark", "note", 18), 127, True))
    d.tick(0.001)
    assert sock.sent == [b"AP_PITCH_SW 2\n", b"RF_SW 2\n"]  # a device without LEDs still drives its switches
    assert (mix.sent, mix.channel, pedals.sent, pedals.channel) == ([(18, 0)], 0, [(18, 0)], 3)  # LED per device
    d.close()


def test_dcsbios_export_timeout() -> None:
    d, export, leds, _, shown = _switches(LASER_ARM_SW=DcsSwitch(note(18), 1, 0x4428, 0x8, 3, "LASER", ()))
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


def test_main_paused() -> None:
    def outcome(error: BaseException | None) -> tuple[object, int]:
        """Run main_paused with main raising error; return (exit code or None, Enter prompts)."""
        def main() -> None:
            if error:
                raise error
        prompts = []

        def fake_input(prompt: object = "") -> str:
            prompts.append(prompt)
            return ""
        orig_main, orig_input = midi_joy.main, builtins.input
        midi_joy.main, builtins.input = main, fake_input
        try:
            midi_joy.main_paused()
            return None, len(prompts)
        except SystemExit as e:
            return e.code, len(prompts)
        finally:
            midi_joy.main, builtins.input = orig_main, orig_input

    with redirect_stderr(io.StringIO()) as err:
        assert outcome(None) == (None, 0)
        assert outcome(SystemExit(0)) == (0, 0)  # --help
        assert outcome(SystemExit("config not found: x.toml")) == (1, 1)
        assert outcome(SystemExit(2)) == (1, 1)  # argparse error
        assert outcome(OSError("port busy")) == (1, 1)
    assert "config not found: x.toml" in err.getvalue() and "OSError: port busy" in err.getvalue()


def test_missing_device_and_vjoy() -> None:
    """A port that won't open is skipped with a warning, none opening exits; vJoy mapped without the dll exits."""
    class Quit:  # output that stops run() on its first tick
        closed = False

        def on_midi(self, ev: MidiEvent) -> None:
            pass

        def tick(self, dt: float) -> None:
            raise KeyboardInterrupt

        def close(self) -> None:
            self.closed = True

    def open_input(port: str, callback: object) -> nullcontext[SimpleNamespace]:
        if port == "gone":
            raise OSError("unknown port 'gone'")
        return nullcontext(SimpleNamespace(name="MIDI Mix 1"))

    orig_open, orig_dll = mido.open_input, vjoy.VJOY_DLL
    mido.open_input, vjoy.VJOY_DLL = open_input, str(Path(tempfile.gettempdir(), "no-vjoy", "vJoyInterface.dll"))
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            out = Quit()
            midi_joy.run({"gone": (print, ""), MIX: (print, "")}, [out])
            assert out.closed and "MIDI port 'gone' not opened" in err.getvalue()
            try:
                midi_joy.run({"gone": (print, "")}, [])
                raise AssertionError("ran without a MIDI port")
            except SystemExit as e:
                assert "no MIDI port opened" in str(e.code)
        try:
            midi_joy.open_outputs(_load('[vjoy.axes."MIDI Mix"]\nz = 62'))
            raise AssertionError("ran without vJoy")
        except SystemExit as e:
            assert "vJoy not installed" in str(e.code)
    finally:
        mido.open_input, vjoy.VJOY_DLL = orig_open, orig_dll


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
    test_dcsbios_push_button()
    test_dcsbios_rocker()
    test_dcsbios_hold_switch()
    test_dcsbios_hold_switch_off_at_bottom()
    test_dcsbios_switches_per_device()
    test_dcsbios_export_timeout()
    test_main_paused()
    test_missing_device_and_vjoy()
    print("ok")
