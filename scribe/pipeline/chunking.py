"""Split a transcript into map chunks along likely scene boundaries.

Scene breaks are guessed from what is cheap and certain: long silences and
the DM's own bookmarks. Location and NPC changes need the model, so the map
step labels each chunk's location and the reduce step merges neighbours that
share one. A chunk never exceeds the profile's token budget (R9).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm import estimate_tokens
from .transcript import fmt_ts

SCENE_GAP_S = 45.0  # unvalidated: a pause this long at a table usually means a shift


@dataclass
class Chunk:
    index: int
    lines: list[dict] = field(default_factory=list)
    bookmarks: list[dict] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.lines[0]["start"]

    @property
    def end(self) -> float:
        return self.lines[-1]["end"]

    def render(self, speaker_names: dict | None = None) -> str:
        out = []
        bms = sorted(self.bookmarks, key=lambda b: b["t"])
        for ln in self.lines:
            while bms and bms[0]["t"] <= ln["start"]:
                b = bms.pop(0)
                out.append(f"--- DM BOOKMARK at {fmt_ts(b['t'])}: {b['label']} ---")
            who = ln.get("speaker")
            if who and speaker_names:
                who = speaker_names.get(who, who)
            out.append(line_text(ln, who))
        for b in bms:
            out.append(f"--- DM BOOKMARK at {fmt_ts(b['t'])}: {b['label']} ---")
        return "\n".join(out)


def line_text(ln: dict, who: str | None = None) -> str:
    prefix = f"{ln['id']} [{fmt_ts(ln['start'])}]"
    return f"{prefix} {who}: {ln['text']}" if who else f"{prefix} {ln['text']}"


def chunk_transcript(lines: list[dict], bookmarks: list[dict], max_tokens: int, target_minutes: float) -> list[Chunk]:
    target_s = target_minutes * 60
    bm_times = sorted(b["t"] for b in bookmarks)
    chunks: list[Chunk] = []
    cur: list[dict] = []
    cur_tokens = 0
    prev_end = None
    for ln in lines:
        t = estimate_tokens(line_text(ln, ln.get("speaker"))) + 2
        if t > max_tokens:
            # A single runaway line (Whisper loop) cannot fit anywhere; cut it.
            words = ln["text"].split()
            keep = max(1, int(len(words) * max_tokens / t / 2))
            ln = dict(ln, text=" ".join(words[:keep]) + " [...]")
            t = estimate_tokens(line_text(ln, ln.get("speaker"))) + 2
        if cur:
            dur = ln["start"] - cur[0]["start"]
            gap = ln["start"] - (prev_end or ln["start"])
            bookmark_between = any(prev_end is not None and prev_end < b <= ln["start"] for b in bm_times)
            over_budget = cur_tokens + t > max_tokens
            natural_break = gap >= SCENE_GAP_S or bookmark_between
            if over_budget or (dur >= target_s and (natural_break or gap >= 5)) or (dur >= target_s * 1.5) or (natural_break and dur >= target_s * 0.5):
                chunks.append(Chunk(len(chunks), cur))
                cur, cur_tokens = [], 0
        cur.append(ln)
        cur_tokens += t
        prev_end = ln["end"]
    if cur:
        chunks.append(Chunk(len(chunks), cur))
    for i, c in enumerate(chunks):
        lo = c.start if i else float("-inf")
        hi = chunks[i + 1].start if i + 1 < len(chunks) else float("inf")
        c.bookmarks = [b for b in bookmarks if lo <= b["t"] < hi]
    return chunks
