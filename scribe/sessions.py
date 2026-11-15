"""Session folders on disk.

Layout under the vault (spec 6.4):

    sessions/S32/session.json      metadata and status
    sessions/S32/audio/            seg_000.webm[.part] (live) or seg_000.wav (import)
    sessions/S32/segments.json     timeline: where each segment sits in session time
    sessions/S32/bookmarks.json
    sessions/S32/stt/seg_000.json  per-segment transcription (resume unit, R7)
    sessions/S32/transcript.json   assembled timeline transcript
    sessions/S32/log_draft.json    structured draft with citations
    sessions/S32/log.md            the DM-approved log (written only on save, R13)

Files are written via a temp file and rename so a crash never leaves a
half-written JSON that would wedge recovery.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

SESSION_ID = re.compile(r"^S(\d{1,4})$")
SEGMENT_NAME = re.compile(r"^seg_(\d{3,5})\.(webm|ogg|wav|m4a|mp3|flac|mp4)(\.part)?$")
_lock = threading.Lock()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class SessionStore:
    def __init__(self, sessions_dir: Path):
        self.root = sessions_dir

    def path(self, sid: str) -> Path:
        if not SESSION_ID.match(sid):
            raise ValueError(f"bad session id {sid!r}")
        return self.root / sid

    def exists(self, sid: str) -> bool:
        return (self.path(sid) / "session.json").exists()

    def list(self) -> list[dict]:
        out = []
        if self.root.exists():
            for d in sorted(self.root.iterdir(), key=lambda p: int(p.name[1:]) if SESSION_ID.match(p.name) else -1):
                meta = read_json(d / "session.json")
                if meta:
                    out.append(meta)
        return out

    def next_number(self, campaign_last: int | None) -> int:
        nums = [m["number"] for m in self.list()]
        return max(nums + [campaign_last or 0]) + 1

    def create(self, number: int, source: str, session_date: str | None = None) -> dict:
        sid = f"S{number}"
        with _lock:
            if self.exists(sid):
                raise FileExistsError(sid)
            meta = {
                "id": sid,
                "number": number,
                "date": session_date or date.today().isoformat(),
                "source": source,
                "status": "recording" if source == "live" else "importing",
                "created": time.time(),
                "error": None,
            }
            write_json(self.path(sid) / "session.json", meta)
            write_json(self.path(sid) / "segments.json", [])
            write_json(self.path(sid) / "bookmarks.json", [])
            (self.path(sid) / "audio").mkdir(exist_ok=True)
        return meta

    def meta(self, sid: str) -> dict:
        m = read_json(self.path(sid) / "session.json")
        if m is None:
            raise FileNotFoundError(sid)
        return m

    def update(self, sid: str, **fields: Any) -> dict:
        with _lock:
            m = self.meta(sid)
            m.update(fields)
            write_json(self.path(sid) / "session.json", m)
            return m

    # --- audio timeline -------------------------------------------------

    def segments(self, sid: str) -> list[dict]:
        return read_json(self.path(sid) / "segments.json", [])

    def upsert_segment(self, sid: str, seg: dict) -> list[dict]:
        with _lock:
            segs = [s for s in self.segments(sid) if s["index"] != seg["index"]]
            segs.append(seg)
            segs.sort(key=lambda s: s["index"])
            write_json(self.path(sid) / "segments.json", segs)
            return segs

    def audio_path(self, sid: str, filename: str) -> Path:
        if not SEGMENT_NAME.match(filename):
            raise ValueError(f"bad segment name {filename!r}")
        return self.path(sid) / "audio" / filename

    def bookmarks(self, sid: str) -> list[dict]:
        return read_json(self.path(sid) / "bookmarks.json", [])

    def add_bookmark(self, sid: str, t: float, label: str) -> dict:
        with _lock:
            bms = self.bookmarks(sid)
            bm = {"id": f"B{len(bms) + 1}", "t": round(float(t), 2), "label": label.strip() or "Bookmark"}
            bms.append(bm)
            write_json(self.path(sid) / "bookmarks.json", bms)
            return bm

    def delete_audio(self, sid: str) -> int:
        """R21: drop audio, keep transcript and log. Returns bytes freed."""
        d = self.path(sid) / "audio"
        freed = sum(f.stat().st_size for f in d.glob("*") if f.is_file()) if d.exists() else 0
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(exist_ok=True)
        self.update(sid, audio_deleted=True)
        return freed

    def unfinished(self) -> list[dict]:
        """Sessions a crash or closed tab left mid-recording (scenario C)."""
        return [m for m in self.list() if m["status"] in ("recording", "paused")]
