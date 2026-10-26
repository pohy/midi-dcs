# midi-joy

Maps a MIDI controller (e.g. Akai MIDI Mix) to vJoy axes/buttons and DCS-BIOS cockpit controls. Windows only. Configured in `midi_joy.ini`.

## Features

- **vJoy axes**: CC knobs/faders to vJoy axes, with optional smoothing.
- **vJoy buttons**: notes or CCs (>= 64 = pressed) to vJoy buttons.
- **DCS-BIOS knobs**: CC sets a switch position (knob travel split into equal zones) or a dial value.
- **DCS-BIOS switches**: note steps a switch of up to 5 positions; the button LED shows the position DCS reports.
- **Hold switches**: 3-position switches with a long press to OFF, short press to flip between the other two.
- **Sounds**: click on knob-driven position changes and switch presses.
- **Overlay**: on-screen text showing control name and new position.

## Requirements

- Python 3.10+, `pip install -r requirements.txt`
- [vJoy](https://github.com/jshafer817/vJoy) at `C:\Program Files\vJoy` (for `[axes]` / `[buttons]`)
- [DCS-BIOS](https://github.com/DCS-Skunkworks/dcs-bios) in `Saved Games\DCS\Scripts\DCS-BIOS` (for `[dcsbios:...]`)

## Usage

```
python midi_joy.py --list       # list MIDI input ports
python midi_joy.py --monitor    # print incoming MIDI in config syntax ("ch1 cc:62 = 127", "ch1 note:22 press")
python midi_joy.py              # run with midi_joy.ini next to the script
python midi_joy.py --config other.ini
```

## Config

### MIDI

```ini
[midi]
port = MIDI Mix   ; substring of the port name; omit = system default
channel = 1       ; 1-16; omit = any channel
hold_s = 0.5      ; long-press threshold for hold switches
```

### vJoy

```ini
[vjoy]
device = 1
smooth = 0.1      ; low-pass time constant in seconds, 0 = off

[axes]            ; axis = CC number; axes: x y z rx ry rz sl0 sl1
z = 62
rx = 58

[buttons]         ; button 1-128 = note:N | cc:N
1 = note:22
2 = cc:64
```

Each axis and button must be enabled in Configure vJoy.

### DCS-BIOS

One `[dcsbios:AIRCRAFT]` section; `AIRCRAFT` is the JSON name in `Saved Games\DCS\Scripts\DCS-BIOS\doc\json`. Look up identifiers in Bort.

```ini
[dcsbios:F-16C_50]
; knob: one equal zone per switch position; dials (max 65535) map the full range
ANTI_COLL_LIGHT_KNB = cc:17
HMCS_INT_KNB = cc:59

; note: each press steps one position, turning around at the ends
; 3-position: mid -> up -> mid -> down -> mid
RF_SW = note:16

; hold: long press = OFF position, short press flips between the other two
; OFF = 0 (bottom) to 2; omitted = 1 (middle); args in any order (hold:0 note:13)
AP_PITCH_SW = note:19 hold
MASTER_ARM_SW = note:13 hold:0
```

Note switches need `[midi] port` set (LED output goes to the same device).

Button LED per position, counted from the top: solid, dark, slow blink, fast blink, double flash. Hold switches: dark in OFF, solid in the upper, slow blink in the lower position. All LEDs go dark at mission end, with no aircraft, or after 3 s without DCS-BIOS data.

### Sound

```ini
[sound]           ; wav file, or folder = random wav each time; 16/24-bit PCM; relative to the ini; omit = silent
knob = resources/sfx-knob     ; knob moves a switch to another position (dials stay silent)
knob_volume = 0.2             ; 0.0-1.0
switch = resources/sfx-switch ; every note press
switch_volume = 0.2
```

### Overlay

```ini
[overlay]         ; omit the section = off
position = N      ; N NE E SE S SW W NW C
offset_x = 0      ; pixels, + = right
offset_y = 60     ; pixels, + = down
timeout_s = 1.5
```

Shows knob moves, positions DCS reports within 1 s of a note press, and DCS-BIOS (dis)connects. Needs DCS in borderless mode (Full Screen off), primary monitor only.
