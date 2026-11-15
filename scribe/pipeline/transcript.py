"""Assemble per-segment STT output into one session timeline."""

from __future__ import annotations

from pathlib import Path

from ..sessions import SessionStore, read_json, write_json


def fmt_ts(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def stt_file(store: SessionStore, sid: str, index: int) -> Path:
    return store.path(sid) / "stt" / f"seg_{index:03d}.json"


def assemble(store: SessionStore, sid: str) -> dict:
    segs = store.segments(sid)
    pieces = []
    for seg in segs:
        out = read_json(stt_file(store, sid, seg["index"]))
        if out is None:
            raise RuntimeError(f"segment {seg['index']} has not been transcribed")
        for p in out["pieces"]:
            pieces.append({
                "start": round(seg["offset"] + p["start"], 2),
                "end": round(seg["offset"] + p["end"], 2),
                "text": p["text"],
                **({"raw": p["raw"]} if "raw" in p else {}),
                "speaker": seg.get("track"),
                "segment": seg["index"],
            })
    # Separate per-speaker tracks interleave by time; a single mixed stream
    # is already in order and the sort is a no-op.
    pieces.sort(key=lambda p: (p["start"], p["end"]))
    lines = [dict(p, id=f"L{i + 1:04d}", table_talk=False) for i, p in enumerate(pieces)]
    old = read_json(store.path(sid) / "transcript.json") or {}
    transcript = {
        "session": sid,
        "lines": lines,
        "bookmarks": store.bookmarks(sid),
        "speakers": old.get("speakers", {}),
    }
    write_json(store.path(sid) / "transcript.json", transcript)
    return transcript


def load(store: SessionStore, sid: str) -> dict | None:
    return read_json(store.path(sid) / "transcript.json")


def speaker_name(transcript: dict, raw: str | None) -> str | None:
    """Display name for a track label (R8: renaming is one mapping entry)."""
    if raw is None:
        return None
    return transcript.get("speakers", {}).get(raw, raw)
