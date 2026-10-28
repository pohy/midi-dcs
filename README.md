# midi-joy

Maps MIDI controllers (e.g. Akai MIDI Mix) to vJoy axes/buttons and DCS-BIOS cockpit controls. Windows only. Configured in `midi_joy.toml`.

## Features

- **vJoy axes**: CC knobs/faders to vJoy axes, with optional smoothing.
- **vJoy buttons**: notes or CCs (>= 64 = pressed) to vJoy buttons.
- **DCS-BIOS knobs**: CC sets a switch position (knob travel split into equal zones) or a dial value.
- **DCS-BIOS switches**: note steps a switch of up to 5 positions; the button LED shows the position DCS reports.
- **Push buttons**: held down in the cockpit while the note is held, or toggled per press with `latch`.
- **Rockers**: two notes hold a spring-loaded 3-position switch up or down, centered on release.
- **Hold switches**: 3-position switches with a long press to OFF, short press to flip between the other two.
- **Sounds**: click on knob-driven position changes and switch presses.
- **Overlay**: on-screen text showing control name and new position.

## Requirements

- Python 3.12+, `pip install --group run` (pip 25.1+, `python -m pip install -U pip`); `--group dev` adds mypy, ruff and PyInstaller
- [vJoy](https://github.com/jshafer817/vJoy) at `C:\Program Files\vJoy` (for `[vjoy.axes.DEVICE]` / `[vjoy.buttons.DEVICE]`)
- [DCS-BIOS](https://github.com/DCS-Skunkworks/dcs-bios) in `Saved Games\DCS\Scripts\DCS-BIOS` (for `[dcsbios.AIRCRAFT.DEVICE]`)

## Usage

```
python midi_joy.py --list       # list MIDI input ports
python midi_joy.py --monitor    # print incoming MIDI from every configured device (system default port without any):
                                # '"MIDI Mix" ch1 cc:62 = 127' maps as { cc = 62 } in a ."MIDI Mix" table, "note:22 press" as { note = 22 }
python midi_joy.py              # run with midi_joy.toml next to the script
python midi_joy.py --config other.toml
```

## Build

```
make    # with the venv active; rebuilds when a .py changes
```

Produces `dist/midi_joy.exe`. Ship it with `midi_joy.toml` and `resources/` next to it; the exe takes the same arguments.

## Config

[TOML](https://toml.io). Every table and key is optional unless noted. Strings need quotes; in paths use `/` or single quotes (`'C:\sfx'`), since `\` starts an escape in `"..."`.

### MIDI

```toml
[midi]
hold_s = 0.5      # long-press threshold for hold switches

[midi."MIDI Mix"] # per device, optional
channel = 1       # 1-16; omit = any channel

[midi.mfd_left]   # a name for one channel of a port
port = "LPProMK3" # substring of the port name; omit = the device name
channel = 1

[midi.mfd_right]  # same port, other channel (e.g. Launchpad custom modes)
port = "LPProMK3"
channel = 2
leds = false      # no switch LED messages, the controller keeps its own colors; omit = true
```

`DEVICE` is a substring of the port name (see `--list`), or a name with its own `port`; quote it if it has spaces or dots. Every mapping table below ends in one: `[vjoy.axes."MIDI Mix"]`, `[dcsbios.F-16C_50.mfd_left]`. Every device with a mapping or a `[midi.DEVICE]` table opens at start and must be plugged in; devices on one port share it, each gets the messages on its channel. An axis, button or DCS-BIOS control takes one device.

### vJoy

```toml
[vjoy]
device = 1
smooth = 0.1      # low-pass time constant in seconds, 0 = off

[vjoy.axes."MIDI Mix"]     # axis = CC number; axes: x y z rx ry rz sl0 sl1
z = 62
rx = 58

[vjoy.buttons."MIDI Mix"]  # button 1-128 = { note = N } | { cc = N }
1 = { note = 22 }
2 = { cc = 64 }
```

Each axis and button must be enabled in Configure vJoy.

### DCS-BIOS

`[dcsbios.AIRCRAFT.DEVICE]` tables, one aircraft; `AIRCRAFT` is the JSON name in `Saved Games\DCS\Scripts\DCS-BIOS\doc\json` (quote it if it has spaces: `[dcsbios."Christen Eagle II"."MIDI Mix"]`). Look up identifiers in Bort.

```toml
[dcsbios.F-16C_50."MIDI Mix"]
# knob: one equal zone per switch position; dials (max 65535) map the full range
ANTI_COLL_LIGHT_KNB = { cc = 17 }
HMCS_INT_KNB = { cc = 59 }

# note: each press steps one position, turning around at the ends
# 3-position: mid -> up -> mid -> down -> mid
RF_SW = { note = 16 }

# push button (momentary in the DCS-BIOS json): held down while the note is
# latch = true: each press toggles it instead (pressed, released, ...)
MASTER_CAUTION = { note = 25 }
ECM_RESET_BTN = { note = 72, latch = true }

# rocker: up holds the top position, down the bottom, release centers; 3-position switches only
MFD_L_BRT_SW = { up = 31, down = 32 }

# hold: long press = OFF position, short press flips between the other two (from OFF to the upper one)
# off = 0 (bottom) to 2, omitted = 1 (middle); 3-position switches only
AP_PITCH_SW = { note = 19, hold = true }
MASTER_ARM_SW = { note = 13, hold = true, off = 0 }

# dark = position with the dark LED, 0 (bottom) up; any note switch
ANTI_SKID_SW = { note = 22, dark = 2 }
```

A note switch's LED goes to the device the note comes from, on its `channel` (1 without one); none with `leds = false`.

Hold switches move one position at a time like the real switch, so up -> down passes the middle.

Button LED per position, counted from the top: solid, dark, slow blink, fast blink, double flash. Hold switches: dark in OFF, solid in the upper, slow blink in the lower position. `dark` moves the dark LED to that position, the others keep their order from the top (`ANTI_SKID_SW` above: top dark, middle solid, bottom slow blink). Rockers: each side lit while DCS holds the switch there. All LEDs go dark at mission end, with no aircraft, or after 3 s without DCS-BIOS data.

### Sound

```toml
[sound]           # wav file, or folder = random wav each time; 16/24-bit PCM; relative to the config; omit = silent
knob = "resources/sfx-knob"     # knob moves a switch to another position (dials stay silent)
knob_volume = 0.2               # 0.0-1.0
switch = "resources/sfx-switch" # every note press
switch_volume = 0.2
```

### Overlay

```toml
[overlay]         # omit the table = off
position = "N"    # N NE E SE S SW W NW C
offset_x = 0      # pixels, + = right; SW with a 20 px margin: offset_x = 20, offset_y = -20
offset_y = 60     # pixels, + = down
timeout_s = 1.5
```

Shows knob moves (the position sent), positions DCS reports within 1 s of a note press, and DCS-BIOS (dis)connects (only with note switches mapped, they read its data). DCS changing a switch on its own doesn't show. Needs DCS in borderless mode (Full Screen off), primary monitor only.
