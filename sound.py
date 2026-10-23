"""[sound] section: knob = a knob moves a DCS-BIOS switch to another position, switch = a note steps one."""
import array
import io
import random
import tempfile
import wave
import winsound
from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SoundConfig:
    knob: tuple[bytes, ...]  # wav files with volume applied, empty = silent
    switch: tuple[bytes, ...]


def parse(cfg: ConfigParser, base: Path) -> SoundConfig:
    return SoundConfig(_sounds(cfg, base, "knob"), _sounds(cfg, base, "switch"))


def _sounds(cfg: ConfigParser, base: Path, key: str) -> tuple[bytes, ...]:
    """key = wav file or folder of wavs (random one per sound), relative to base; key_volume = 0.0-1.0."""
    volume = cfg.getfloat("sound", f"{key}_volume", fallback=1.0)
    if not 0 <= volume <= 1:
        raise ValueError(f"[sound] {key}_volume must be 0.0-1.0, got {volume}")
    src = cfg.get("sound", key, fallback="")
    if not src:
        return ()
    path = base / src
    files = sorted(path.glob("*.wav")) if path.is_dir() else [path]
    if not files or not files[0].is_file():
        raise ValueError(f"[sound] {key}: no wav at {path}")
    return tuple(_scaled(f, volume) for f in files)


def _scaled(path: Path, volume: float) -> bytes:
    try:
        with wave.open(str(path), "rb") as w:
            params = w.getparams()
            frames = w.readframes(params.nframes)
    except (wave.Error, EOFError) as e:
        raise ValueError(f"[sound] {path}: {e}")
    if params.sampwidth == 3:  # 24 to 16-bit: keep the top two bytes of each little-endian sample
        b = bytearray(len(frames) // 3 * 2)
        b[0::2], b[1::2] = frames[1::3], frames[2::3]
        frames, params = bytes(b), params._replace(sampwidth=2)
    if params.sampwidth != 2:
        raise ValueError(f"[sound] {path}: 16 or 24-bit PCM wav only")
    samples = array.array("h", frames)  # wav is little-endian, as is Windows
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(array.array("h", (int(s * volume) for s in samples)).tobytes())
    return out.getvalue()


class Clicker:
    """winsound can't play from memory asynchronously, so the sounds go to temp files first."""

    def __init__(self, sounds: tuple[bytes, ...]) -> None:
        # removed at interpreter exit; a sound still playing then may leave the folder behind in %TEMP%
        self.tmp = tempfile.TemporaryDirectory(prefix="midi_joy_", ignore_cleanup_errors=True)
        self.files = [Path(self.tmp.name, f"click{i}.wav") for i in range(len(sounds))]
        for f, data in zip(self.files, sounds):
            f.write_bytes(data)

    def play(self) -> None:
        # ponytail: winsound plays one sound at a time (knob and switch sounds included), a new one cuts
        # the previous one short; sounddevice (new dependency) mixes them if overlapping sounds matter
        winsound.PlaySound(str(random.choice(self.files)),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
