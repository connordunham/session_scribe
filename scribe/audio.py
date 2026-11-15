"""Audio import and probing via ffmpeg (a local binary, so R1 holds)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

SEGMENT_SECONDS = 600  # R3/R4: ~10-minute segments


class AudioError(RuntimeError):
    pass


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def duration(path: Path) -> float | None:
    """Seconds, or None when ffprobe cannot tell (e.g. a .part webm whose
    header never got a duration). Callers must treat None as unknown, not 0."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
        d = json.loads(out).get("format", {}).get("duration")
        return float(d) if d not in (None, "N/A") else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def decoded_duration(path: Path) -> float | None:
    """Duration by decoding. Live MediaRecorder webm files carry no duration
    in their header, so ffprobe's fast path returns nothing for them."""
    d = duration(path)
    if d is not None:
        return d
    try:
        err = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=600,
        ).stderr
    except (subprocess.SubprocessError, OSError):
        return None
    times = [line.rsplit("time=", 1)[1].split()[0] for line in err.splitlines() if "time=" in line]
    if not times:
        return None
    h, m, s = times[-1].split(":")
    try:
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None


def split_import(src: Path, out_dir: Path, segment_seconds: int = SEGMENT_SECONDS) -> list[dict]:
    """Split any ffmpeg-readable file (mp3, wav, m4a, webm...) into 16 kHz
    mono WAV segments. 16 kHz mono is what Whisper resamples to anyway, so
    nothing is lost and decoding is done once instead of per retry."""
    if not have_ffmpeg():
        raise AudioError("ffmpeg/ffprobe not found on PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "seg_%03d.wav"
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1", str(pattern)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise AudioError(f"ffmpeg failed: {r.stderr.strip()[-500:]}")
    segs, offset = [], 0.0
    for i, f in enumerate(sorted(out_dir.glob("seg_*.wav"))):
        d = duration(f)
        if d is None:
            raise AudioError(f"could not read duration of {f.name}")
        segs.append({"index": i, "file": f.name, "offset": round(offset, 3), "duration": round(d, 3), "final": True})
        offset += d
    if not segs:
        raise AudioError("no audio found in file")
    return segs
