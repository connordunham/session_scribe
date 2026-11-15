"""Background worker: transcription, then summarization, one GPU job at a time.

Runs as its own process (`scribe worker`) so the UI/API never loads a model,
and so the server deployment can put it on the GPU box alone (R17).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
import traceback
from dataclasses import asdict
from typing import Callable

from .campaign import load_campaign, stt_prompt
from .config import Settings
from .jobs import JobQueue
from .llm import LLM, OllamaLLM
from .pipeline import transcript as tx
from .pipeline.logformat import check, load_template, render
from .pipeline.summarize import summarize
from .sessions import SessionStore, read_json, write_json
from .transcribe import Transcriber, WhisperTranscriber, snap_to_glossary

log = logging.getLogger("scribe.worker")


def enqueue_processing(q: JobQueue, store: SessionStore, sid: str) -> None:
    """Queue STT for every segment that has no output yet, then the summary.
    Safe to call repeatedly: dedupe keys make it idempotent."""
    for seg in store.segments(sid):
        if not tx.stt_file(store, sid, seg["index"]).exists():
            q.enqueue(sid, "transcribe", {"index": seg["index"]}, dedupe=f"{sid}:stt:{seg['index']}:{seg.get('version', 0)}")
    q.enqueue(sid, "summarize", {}, dedupe=f"{sid}:sum:{len(store.segments(sid))}:{int(store.meta(sid).get('resummarize', 0))}")
    store.update(sid, status="transcribing", error=None)


class Worker:
    def __init__(self, settings: Settings,
                 make_transcriber: Callable[[], Transcriber] | None = None,
                 make_llm: Callable[[], LLM] | None = None):
        self.s = settings
        self.q = JobQueue(settings.db_path)
        self.store = SessionStore(settings.sessions_dir)
        p = settings.profile
        self.make_transcriber = make_transcriber or (lambda: WhisperTranscriber(p.stt_model, p.stt_device, p.stt_compute_type, p.stt_fallback_model))
        self.make_llm = make_llm or (lambda: OllamaLLM(settings.ollama_host, p.llm_model, p.llm_context, p.llm_num_gpu))
        self._stt: Transcriber | None = None
        self._llm: LLM | None = None
        self._stop = False

    # --- GPU residency: never both models at once (spec 6.5 resource rule)

    def _transcriber(self) -> Transcriber:
        if self._llm is not None:
            unload = getattr(self._llm, "unload", None)
            if unload:
                unload()
            self._llm = None
        if self._stt is None:
            self._stt = self.make_transcriber()
        return self._stt

    def _llm_client(self) -> LLM:
        if self._stt is not None:
            self._stt.close()
            self._stt = None
        if self._llm is None:
            self._llm = self.make_llm()
        return self._llm

    # --- jobs

    def run_transcribe(self, sid: str, index: int) -> None:
        seg = next((s for s in self.store.segments(sid) if s["index"] == index), None)
        if seg is None:
            return
        campaign = load_campaign(self.s.campaign_dir, self.s.vault / "config" / "glossary_extra.txt")
        path = self.store.audio_path(sid, seg["file"])
        if not path.exists():
            raise FileNotFoundError(f"audio for segment {index} is missing ({seg['file']})")
        pieces = self._transcriber().transcribe(path, stt_prompt(campaign), campaign.glossary)
        out = []
        for p in pieces:
            d = asdict(p)
            fixed = snap_to_glossary(d["text"], campaign.glossary)
            if fixed != d["text"]:
                d["raw"] = d["text"]  # keep what Whisper heard, for review
                d["text"] = fixed
            out.append(d)
        write_json(tx.stt_file(self.store, sid, index), {"index": index, "file": seg["file"], "pieces": out})

    def run_summarize(self, sid: str, progress: Callable[[str], None]) -> None:
        jobs = self.q.for_session(sid)
        failed = [j for j in jobs if j["kind"] == "transcribe" and j["status"] == "failed"]
        if failed:
            raise RuntimeError(f"{len(failed)} segment(s) failed to transcribe; fix and retry before summarizing")
        self.store.update(sid, status="summarizing")
        transcript = tx.assemble(self.store, sid)
        campaign = load_campaign(self.s.campaign_dir, self.s.vault / "config" / "glossary_extra.txt")
        map_dir = self.store.path(sid) / "map"
        cache = {}
        for f in map_dir.glob("chunk_*.json") if map_dir.exists() else []:
            cache[int(f.stem.split("_")[1])] = read_json(f)
        draft = summarize(
            transcript["lines"], transcript["bookmarks"], transcript.get("speakers", {}), campaign,
            self._llm_client(), self.s.profile, progress=progress, map_cache=cache,
            save_map=lambda i, d: write_json(map_dir / f"chunk_{i:03d}.json", d),
        )
        off = set(draft["off_topic"])
        for ln in transcript["lines"]:
            ln["table_talk"] = ln["id"] in off  # R10: kept, but marked
        write_json(self.store.path(sid) / "transcript.json", transcript)
        meta = self.store.meta(sid)
        template = load_template(self.s.vault)
        md = render(meta, draft["sections"], template)
        draft["check"] = check(md, template, campaign.tags)
        write_json(self.store.path(sid) / "log_draft.json", draft)
        (self.store.path(sid) / "log_draft.md").write_text(md, encoding="utf-8")
        self.store.update(sid, status="drafted", progress=None)

    def step(self) -> bool:
        job = self.q.claim()
        if job is None:
            return False
        sid = job["session"]
        try:
            if job["kind"] == "transcribe":
                self.store.update(sid, status="transcribing", progress=f"segment {job['payload']['index'] + 1}/{len(self.store.segments(sid))}")
                self.run_transcribe(sid, job["payload"]["index"])
            elif job["kind"] == "summarize":
                self.run_summarize(sid, lambda msg: self.store.update(sid, progress=msg))
            self.q.finish(job["id"])
        except Exception as e:  # noqa: BLE001 - job failures must never kill the loop
            log.error("job %s failed: %s", job["id"], traceback.format_exc())
            self.q.fail(job["id"], f"{type(e).__name__}: {e}", job["attempts"])
            if job["attempts"] >= 3:
                self.store.update(sid, status="error", error=f"{job['kind']}: {e}")
        return True

    def run_forever(self, idle_sleep: float = 2.0) -> None:
        recovered = self.q.recover()
        if recovered:
            log.info("re-queued %d interrupted job(s)", recovered)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))

        # A heartbeat thread, not a per-job write: one transcription or map
        # call can run for minutes and the readiness page must not call a
        # busy worker dead.
        def beat() -> None:
            while not self._stop:
                self.q.set_setting("worker_heartbeat", str(time.time()))
                time.sleep(10)

        threading.Thread(target=beat, daemon=True).start()
        while not self._stop:
            if not self.step():
                time.sleep(idle_sleep)
