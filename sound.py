"""[sound] section: click when a knob moves a DCS-BIOS switch to another position."""
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
    clicks: tuple[bytes, ...]  # wav files with volume applied, empty = silent


def parse(cfg: ConfigParser, base: Path) -> SoundConfig:
    """click = wav file or folder of wavs (random one per click), relative to base; volume = 0.0-1.0."""
    volume = cfg.getfloat("sound", "volume", fallback=1.0)
    if not 0 <= volume <= 1:
        raise ValueError(f"[sound] volume must be 0.0-1.0, got {volume}")
    src = cfg.get("sound", "click", fallback="")
    if not src:
        return SoundConfig(())
    path = base / src
    files = sorted(path.glob("*.wav")) if path.is_dir() else [path]
    if not files or not files[0].is_file():
        raise ValueError(f"[sound] click: no wav at {path}")
    return SoundConfig(tuple(_scaled(f, volume) for f in files))


def _scaled(path: Path, volume: float) -> bytes:
    try:
        with wave.open(str(path), "rb") as w:
            params = w.getparams()
            frames = w.readframes(params.nframes)
    except (wave.Error, EOFError) as e:
        raise ValueError(f"[sound] click: {path}: {e}")
    if params.sampwidth != 2:
        raise ValueError(f"[sound] click: {path}: 16-bit PCM wav only")
    samples = array.array("h", frames)  # wav is little-endian, as is Windows
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(array.array("h", (int(s * volume) for s in samples)).tobytes())
    return out.getvalue()


class Clicker:
    """winsound can't play from memory asynchronously, so the clicks go to temp files first."""

    def __init__(self, conf: SoundConfig) -> None:
        # removed at interpreter exit; a click still playing then may leave the folder behind in %TEMP%
        self.tmp = tempfile.TemporaryDirectory(prefix="midi_joy_", ignore_cleanup_errors=True)
        self.files = [Path(self.tmp.name, f"click{i}.wav") for i in range(len(conf.clicks))]
        for f, data in zip(self.files, conf.clicks):
            f.write_bytes(data)

    def play(self) -> None:
        # ponytail: winsound plays one sound at a time, a new click cuts the previous one short;
        # sounddevice (new dependency) mixes them if overlapping clicks matter
        winsound.PlaySound(str(random.choice(self.files)),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
