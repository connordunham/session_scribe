"""Local speech-to-text (R6).

faster-whisper with its built-in Silero VAD. The campaign glossary goes in
twice: as the initial prompt (biases spelling from the first window) and as
`hotwords` (re-applied every window, so a name first spoken at hour two
still gets the bias).

A conservative post-pass then snaps near-miss spellings of mid-sentence
capitalised words onto glossary names ("Izzaly" -> "Izali"). Thresholds are unvalidated inference: M0 measures
glossary accuracy with and without it (`scribe accuracy`), and it can be
switched off per call.
"""

from __future__ import annotations

import difflib
import gc
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class Piece:
    start: float
    end: float
    text: str


class Transcriber(Protocol):
    def transcribe(self, path: Path, prompt: str, hotwords: list[str]) -> list[Piece]: ...
    def close(self) -> None: ...


class WhisperTranscriber:
    def __init__(self, model: str, device: str, compute_type: str, fallback: str | None = None):
        # Offline by construction: never let the hub client reach out mid-session.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from faster_whisper import WhisperModel  # optional dependency

        try:
            self.model = WhisperModel(model, device=device, compute_type=compute_type, local_files_only=True)
        except Exception:
            if not fallback:
                raise
            self.model = WhisperModel(fallback, device=device, compute_type=compute_type, local_files_only=True)

    def transcribe(self, path: Path, prompt: str, hotwords: list[str]) -> list[Piece]:
        segments, _info = self.model.transcribe(
            str(path),
            language="en",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 700},
            initial_prompt=prompt,
            hotwords=" ".join(hotwords[:60]) or None,
            condition_on_previous_text=False,  # stops one hallucination repeating for minutes
            beam_size=5,
        )
        return [Piece(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]

    def close(self) -> None:
        # Free VRAM before the LLM phase: on 8 GB the two never share the GPU.
        del self.model
        gc.collect()


WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]+")


def snap_to_glossary(text: str, glossary: list[str], cutoff: float = 0.82) -> str:
    """Replace single capitalised words that are near-misses of a glossary
    word. Only capitalised words of 4+ letters are touched: common words are
    never rewritten, and short names collide too easily."""
    vocab: dict[str, str] = {}
    for name in glossary:
        for w in name.split():
            if len(w) >= 4 and w[0].isupper():
                vocab.setdefault(w.lower(), w)
    if not vocab:
        return text
    keys = list(vocab)

    def fix(m: re.Match) -> str:
        w = m.group(0)
        if len(w) < 4 or not w[0].isupper():
            return w
        if w.lower() in vocab:
            return vocab[w.lower()]
        # A capital at sentence start says nothing about being a name:
        # "Barely." would otherwise snap to "Barley".
        before = text[: m.start()].rstrip()
        if not before or before[-1] in ".!?\"'":
            return w
        best = difflib.get_close_matches(w.lower(), keys, n=1, cutoff=cutoff)
        return vocab[best[0]] if best else w

    return WORD.sub(fix, text)


def glossary_accuracy(hypothesis: str, reference: str, glossary: list[str]) -> dict:
    """M0 metric for R6: of the glossary-name mentions in a hand-corrected
    reference, how many appear correctly spelled in the hypothesis. Counts
    per name, capped at the reference count, so extra mentions don't help."""
    found, total, per = 0, 0, {}
    for name in glossary:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        r = len(pat.findall(reference))
        if not r:
            continue
        h = min(len(pat.findall(hypothesis)), r)
        per[name] = {"reference": r, "matched": h}
        total += r
        found += h
    return {"total": total, "matched": found, "accuracy": (found / total) if total else None, "per_name": per}
