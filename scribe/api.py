"""HTTP API and static UI. Never loads a model: the worker does that (R17)."""

from __future__ import annotations

import difflib
import hashlib
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, auth
from .campaign import load_campaign
from .config import Settings, set_active_profile
from .jobs import JobQueue
from .pipeline import transcript as tx
from .pipeline.logformat import SECTIONS, check, load_template, render
from .readiness import MIN_FREE_BYTES, disk_free, run_checks
from .sessions import SEGMENT_NAME, SessionStore, read_json, write_json
from .worker import enqueue_processing

STATIC = Path(__file__).with_name("static")
OPEN_PATHS = {"/login.html", "/api/login", "/app.css"}
LIVE_EXT = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4"}


class NewSession(BaseModel):
    date: str | None = None
    number: int | None = None


class Bookmark(BaseModel):
    t: float
    label: str = "Bookmark"


class SegmentFinish(BaseModel):
    duration: float | None = None


class Status(BaseModel):
    status: str


class Login(BaseModel):
    password: str


class Sections(BaseModel):
    sections: dict[str, list[dict]]
    base: str | None = None


class ProfileChoice(BaseModel):
    name: str


class SpeakerRename(BaseModel):
    raw: str
    name: str


def _hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def create_app(settings: Settings) -> FastAPI:
    if settings.requires_login and not settings.password:
        raise SystemExit(f"Refusing to bind {settings.bind} without SCRIBE_PASSWORD: login is required beyond localhost.")
    app = FastAPI(title="Session Scribe", docs_url=None, redoc_url=None)
    q = JobQueue(settings.db_path)
    store = SessionStore(settings.sessions_dir)

    def campaign():
        return load_campaign(settings.campaign_dir, settings.vault / "config" / "glossary_extra.txt")

    def meta_or_404(sid: str) -> dict:
        try:
            return store.meta(sid)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "no such session")

    @app.middleware("http")
    async def login_gate(request: Request, call_next):
        if settings.requires_login and request.url.path not in OPEN_PATHS and not auth.valid_token(q, request.cookies.get(auth.COOKIE)):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "login required"}, status_code=401)
            return RedirectResponse("/login.html")
        return await call_next(request)

    @app.post("/api/login")
    def login(body: Login):
        if not settings.password or not auth.check_password(body.password, settings.password):
            raise HTTPException(401, "wrong password")
        r = JSONResponse({"ok": True})
        r.set_cookie(auth.COOKIE, auth.make_token(q), httponly=True, samesite="strict",
                     secure=bool(settings.ssl_certfile), max_age=auth.TTL)
        return r

    # --- status, profiles, readiness ------------------------------------

    @app.get("/api/status")
    def status():
        free = disk_free(settings.vault)
        return {
            "profile": settings.profile.name,
            "profiles": {n: p.description for n, p in settings.profiles.items()},
            "llm_model": settings.profile.llm_model,
            "stt_model": settings.profile.stt_model,
            "disk_free": free,
            "disk_low": free < MIN_FREE_BYTES,
            "unfinished": store.unfinished(),
            "pending_jobs": q.pending(),
        }

    @app.post("/api/profile")
    def choose_profile(body: ProfileChoice):
        try:
            set_active_profile(settings, body.name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        # The worker reads the profile at startup; tell the DM to restart it.
        return {"profile": settings.profile.name, "note": "Restart the worker for the new profile to take effect."}

    @app.get("/api/readiness")
    def readiness():
        checks = run_checks(settings)
        return {"ready": all(c["ok"] for c in checks), "checks": checks}

    @app.post("/api/readiness/mic")
    async def mic_test(request: Request):
        """A real round trip: the browser records a few seconds, we decode it
        with the same ffmpeg the pipeline uses and measure the level."""
        tmp = settings.vault / "mic_test.webm"
        tmp.write_bytes(await request.body())
        d = audio.decoded_duration(tmp)
        tmp.unlink(missing_ok=True)
        ok = d is not None and d >= 1.5
        q.set_setting("mic_test", f"{'ok' if ok else 'fail'}:{time.time()}:{d}")
        return {"ok": ok, "duration": d}

    @app.get("/api/campaign")
    def campaign_info():
        c = campaign()
        return {"files": c.files, "entities": c.entities, "pcs": c.pcs(), "last_session": c.last_session, "tags": sorted(c.tags)}

    # --- sessions ---------------------------------------------------------

    @app.get("/api/sessions")
    def sessions():
        return store.list()

    @app.post("/api/sessions")
    def new_session(body: NewSession):
        n = body.number or store.next_number(campaign().last_session)
        try:
            return store.create(n, "live", body.date)
        except FileExistsError:
            raise HTTPException(409, f"S{n} already exists")

    @app.get("/api/sessions/{sid}")
    def session(sid: str):
        m = meta_or_404(sid)
        return {**m, "segments": store.segments(sid), "bookmarks": store.bookmarks(sid), "jobs": q.for_session(sid),
                "has_transcript": (store.path(sid) / "transcript.json").exists(),
                "has_draft": (store.path(sid) / "log_draft.json").exists(),
                "has_log": (store.path(sid) / "log.md").exists()}

    @app.put("/api/sessions/{sid}/segments/{index}")
    async def upload_chunk(sid: str, index: int, request: Request, pos: int, offset: float, mime: str = "audio/webm"):
        """Append one MediaRecorder chunk to a segment's .part file.

        The browser sends a chunk every few seconds, so a closed tab loses
        seconds, not the segment (R3 asks for at most a segment). `pos` is
        the byte offset the client believes it is at, which makes retries
        after a dropped request idempotent."""
        m = meta_or_404(sid)
        if m["status"] not in ("recording", "paused"):
            raise HTTPException(409, f"session is {m['status']}")
        ext = LIVE_EXT.get(mime.split(";")[0])
        if ext is None:
            raise HTTPException(415, f"unsupported recording type {mime}")
        if not 0 <= index < 10000:
            raise HTTPException(400, "bad index")
        name = f"seg_{index:03d}.{ext}"
        part = store.audio_path(sid, name + ".part")
        if store.audio_path(sid, name).exists():
            raise HTTPException(409, "segment already finished")
        size = part.stat().st_size if part.exists() else 0
        if pos > size:
            raise HTTPException(409, detail={"size": size})
        data = await request.body()
        with open(part, "r+b" if part.exists() else "wb") as f:
            f.truncate(pos)
            f.seek(pos)
            f.write(data)
        if size == 0 or pos == 0:
            store.upsert_segment(sid, {"index": index, "file": name + ".part", "offset": round(offset, 3), "duration": None, "final": False})
        return {"size": pos + len(data)}

    def _finish_segment(sid: str, index: int, duration: float | None) -> dict | None:
        seg = next((s for s in store.segments(sid) if s["index"] == index), None)
        if seg is None:
            return None
        if seg["file"].endswith(".part"):
            final = seg["file"][:-5]
            src = store.audio_path(sid, seg["file"])
            if src.exists():
                src.rename(store.audio_path(sid, final))
            seg["file"] = final
        if duration is None:
            duration = seg.get("duration") or audio.decoded_duration(store.audio_path(sid, seg["file"]))
        seg.update(duration=duration, final=True)
        store.upsert_segment(sid, seg)
        return seg

    @app.post("/api/sessions/{sid}/segments/{index}/finish")
    def finish_segment(sid: str, index: int, body: SegmentFinish):
        meta_or_404(sid)
        seg = _finish_segment(sid, index, body.duration)
        if seg is None:
            raise HTTPException(404, "no such segment")
        return seg

    @app.post("/api/sessions/{sid}/bookmarks")
    def bookmark(sid: str, body: Bookmark):
        meta_or_404(sid)
        return store.add_bookmark(sid, body.t, body.label)

    @app.post("/api/sessions/{sid}/status")
    def set_status(sid: str, body: Status):
        m = meta_or_404(sid)
        if body.status not in ("recording", "paused") or m["status"] not in ("recording", "paused"):
            raise HTTPException(400, "only recording <-> paused here")
        return store.update(sid, status=body.status)

    @app.get("/api/sessions/{sid}/elapsed")
    def elapsed(sid: str):
        """Where a resumed recording continues on the timeline (scenario C)."""
        meta_or_404(sid)
        end = 0.0
        for seg in store.segments(sid):
            d = seg.get("duration")
            if d is None:
                d = audio.decoded_duration(store.audio_path(sid, seg["file"])) or 0.0
            end = max(end, seg["offset"] + d)
        nxt = max((s["index"] for s in store.segments(sid)), default=-1) + 1
        return {"elapsed": end, "next_index": nxt}

    @app.post("/api/sessions/{sid}/stop")
    def stop(sid: str):
        m = meta_or_404(sid)
        if m["status"] not in ("recording", "paused"):
            raise HTTPException(409, f"session is {m['status']}")
        for seg in store.segments(sid):
            if not seg.get("final"):
                _finish_segment(sid, seg["index"], None)
        if not store.segments(sid):
            raise HTTPException(400, "nothing was recorded")
        store.update(sid, status="recorded")
        enqueue_processing(q, store, sid)
        return store.meta(sid)

    @app.post("/api/sessions/import")
    async def import_audio(request: Request):
        """R4 (and R8 when a platform gives one file per speaker): every
        uploaded file is a track; with several, each is labelled with its
        speaker name and the transcripts interleave by time."""
        form = await request.form()
        files: list[UploadFile] = [f for f in form.getlist("files") if hasattr(f, "filename")]
        speakers = [str(s) for s in form.getlist("speakers")]
        if not files:
            raise HTTPException(400, "no file")
        number = int(form.get("number") or 0) or store.next_number(campaign().last_session)
        try:
            meta = store.create(number, "import", str(form.get("date") or "") or None)
        except FileExistsError:
            raise HTTPException(409, f"S{number} already exists")
        sid = meta["id"]
        upload_dir = store.path(sid) / "upload"
        upload_dir.mkdir(exist_ok=True)
        saved = []
        for i, f in enumerate(files):
            dest = upload_dir / f"track_{i}{Path(f.filename or '').suffix.lower()[:6]}"
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out, 1024 * 1024)
            saved.append((dest, speakers[i].strip() if i < len(speakers) and speakers[i].strip() else (None if len(files) == 1 else f"Track {i + 1}")))

        def split() -> None:
            try:
                index = 0
                for dest, speaker in saved:
                    tmp = store.path(sid) / "split_tmp"
                    shutil.rmtree(tmp, ignore_errors=True)
                    for seg in audio.split_import(dest, tmp):
                        name = f"seg_{index:03d}.wav"
                        (tmp / seg["file"]).rename(store.audio_path(sid, name))
                        store.upsert_segment(sid, {**seg, "index": index, "file": name, "track": speaker})
                        index += 1
                    shutil.rmtree(tmp, ignore_errors=True)
                    dest.unlink(missing_ok=True)
                store.update(sid, status="recorded")
                enqueue_processing(q, store, sid)
            except Exception as e:  # noqa: BLE001
                store.update(sid, status="error", error=f"import: {e}")

        # Splitting three hours of audio takes a while; don't hold the request.
        threading.Thread(target=split, daemon=True).start()
        return meta

    @app.post("/api/sessions/{sid}/retry")
    def retry(sid: str):
        meta_or_404(sid)
        enqueue_processing(q, store, sid)
        return store.meta(sid)

    @app.post("/api/sessions/{sid}/resummarize")
    def resummarize(sid: str):
        m = meta_or_404(sid)
        shutil.rmtree(store.path(sid) / "map", ignore_errors=True)
        store.update(sid, resummarize=int(m.get("resummarize", 0)) + 1)
        enqueue_processing(q, store, sid)
        return store.meta(sid)

    @app.delete("/api/sessions/{sid}/audio")
    def delete_audio(sid: str):
        meta_or_404(sid)
        if not (store.path(sid) / "transcript.json").exists():
            raise HTTPException(409, "transcribe first: deleting audio now would lose the session")
        return {"freed": store.delete_audio(sid)}

    @app.get("/api/sessions/{sid}/audio/{name}")
    def get_audio(sid: str, name: str):
        meta_or_404(sid)
        if not SEGMENT_NAME.match(name):
            raise HTTPException(400, "bad name")
        p = store.audio_path(sid, name)
        if not p.exists():
            raise HTTPException(404, "audio not available")
        return FileResponse(p)  # supports Range, so the review player can seek

    # --- transcript, draft, review ----------------------------------------

    @app.get("/api/sessions/{sid}/transcript")
    def transcript(sid: str):
        meta_or_404(sid)
        t = tx.load(store, sid)
        if t is None:
            raise HTTPException(404, "not transcribed yet")
        return {**t, "segments": store.segments(sid)}

    @app.post("/api/sessions/{sid}/speakers")
    def rename_speaker(sid: str, body: SpeakerRename):
        meta_or_404(sid)
        p = store.path(sid) / "transcript.json"
        t = read_json(p)
        if t is None:
            raise HTTPException(404, "not transcribed yet")
        t.setdefault("speakers", {})[body.raw] = body.name.strip() or body.raw
        write_json(p, t)
        return t["speakers"]

    def _saved_log(sid: str) -> str | None:
        p = store.path(sid) / "log.md"
        return p.read_text(encoding="utf-8") if p.exists() else None

    @app.get("/api/sessions/{sid}/draft")
    def draft(sid: str):
        meta_or_404(sid)
        d = read_json(store.path(sid) / "log_draft.json")
        if d is None:
            raise HTTPException(404, "no draft yet")
        saved = read_json(store.path(sid) / "log_saved.json")
        saved_md = _saved_log(sid)
        return {**d, "saved_sections": saved, "base": _hash(saved_md), "saved_markdown": saved_md}

    def _render(sid: str, sections: dict) -> tuple[str, list[str]]:
        clean = {k: [dict(i, text=str(i.get("text", "")).strip()) for i in sections.get(k, []) if str(i.get("text", "")).strip()] for k in SECTIONS}
        template = load_template(settings.vault)
        md = render(store.meta(sid), clean, template)
        return md, check(md, template, campaign().tags)

    def _diff(old: str | None, new: str, sid: str) -> str:
        return "".join(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True), f"{sid}/log.md (saved)", f"{sid}/log.md (new)"))

    @app.post("/api/sessions/{sid}/preview-save")
    def preview_save(sid: str, body: Sections):
        meta_or_404(sid)
        md, problems = _render(sid, body.sections)
        old = _saved_log(sid)
        return {"markdown": md, "diff": _diff(old, md, sid), "problems": problems, "base": _hash(old)}

    @app.post("/api/sessions/{sid}/save")
    def save(sid: str, body: Sections):
        """R13: the only route that writes the log, and only after the DM saw
        the diff. `base` must match what the diff was computed against, so a
        second tab cannot silently overwrite the first."""
        meta_or_404(sid)
        old = _saved_log(sid)
        if body.base != _hash(old):
            raise HTTPException(409, "the saved log changed since you previewed; preview again")
        md, problems = _render(sid, body.sections)
        diff = _diff(old, md, sid)
        hist = store.path(sid) / "history"
        hist.mkdir(exist_ok=True)
        if old is not None:
            (hist / f"log_{int(time.time())}.md").write_text(old, encoding="utf-8")
        (store.path(sid) / "log.md").write_text(md, encoding="utf-8")
        write_json(store.path(sid) / "log_saved.json", body.sections)
        store.update(sid, status="saved", saved=time.time())
        return {"saved": True, "diff": diff, "problems": problems, "base": _hash(md)}

    # --- export (R20) ------------------------------------------------------

    def _transcript_md(sid: str) -> str:
        t = tx.load(store, sid)
        if t is None:
            raise HTTPException(404, "not transcribed yet")
        bms = sorted(t.get("bookmarks", []), key=lambda b: b["t"])
        out = [f"# {sid} transcript", ""]
        for ln in t["lines"]:
            while bms and bms[0]["t"] <= ln["start"]:
                b = bms.pop(0)
                out.append(f"> **Bookmark {tx.fmt_ts(b['t'])}:** {b['label']}\n")
            who = tx.speaker_name(t, ln.get("speaker"))
            text = f"**[{tx.fmt_ts(ln['start'])}]** " + (f"{who}: " if who else "") + ln["text"]
            out.append(f"*{text} (table talk)*" if ln.get("table_talk") else text)
            out.append("")
        return "\n".join(out)

    def _notes_md(sid: str) -> str:
        d = read_json(store.path(sid) / "log_draft.json") or {}
        out = [f"# {sid} review notes", "", "## Bookmarks"]
        out += [f"- {tx.fmt_ts(b['t'])} — {b['label']}" for b in store.bookmarks(sid)] or ["- (none)"]
        out += ["", "## Scenes"]
        out += [f"- {tx.fmt_ts(s['start'])} {s['title']} ({s['location']})" for s in d.get("scenes", [])] or ["- (none)"]
        out += ["", "## Flagged (left out of the log: no supporting lines)"]
        out += [f"- {f['text']} — {f['reason']}" for f in d.get("flagged", [])] or ["- (none)"]
        return "\n".join(out) + "\n"

    @app.get("/api/sessions/{sid}/export/{kind}")
    def export(sid: str, kind: str, save: bool = False):
        meta_or_404(sid)
        if kind == "log":
            md = _saved_log(sid)
            if md is None:
                raise HTTPException(409, "save the log first (nothing unapproved is exported)")
        elif kind == "log-player-safe":
            saved = read_json(store.path(sid) / "log_saved.json")
            if saved is None:
                raise HTTPException(409, "save the log first")
            md = render(store.meta(sid), saved, load_template(settings.vault), cite_mode="none", player_safe=True)
        elif kind == "transcript":
            md = _transcript_md(sid)
        elif kind == "notes":
            md = _notes_md(sid)
        else:
            raise HTTPException(404, "unknown export")
        name = f"{sid}_{kind}.md"
        if save:
            d = settings.vault / "exports" / sid
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(md, encoding="utf-8")
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/")
    def index():
        return RedirectResponse("/index.html")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    app.mount("/", StaticFiles(directory=STATIC), name="static")
    return app
