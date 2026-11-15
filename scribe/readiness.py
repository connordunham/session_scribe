"""Pre-trip offline readiness check (R19). Green only if every dependency is
present locally; each check says how to fix itself while still online."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import httpx

from .audio import have_ffmpeg
from .campaign import load_campaign
from .config import Settings, is_private_host
from .jobs import JobQueue

MIN_FREE_BYTES = 5 * 1024**3  # R5: warn under 5 GB


def disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def stt_model_present(model: str) -> tuple[bool, str]:
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        return False, "faster-whisper is not installed (pip install '.[stt]')"
    try:
        p = download_model(model, local_files_only=True)
        return True, f"found at {p}"
    except Exception as e:  # noqa: BLE001 - any failure means "not usable offline"
        return False, f"not in local cache ({type(e).__name__}); run `scribe setup` while online"


def run_checks(s: Settings) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": "" if ok else fix})

    add("ffmpeg", have_ffmpeg(), "ffmpeg and ffprobe on PATH" if have_ffmpeg() else "missing", "Install ffmpeg and put it on PATH.")

    private = is_private_host(s.ollama_host)
    add("Ollama host is local", private, s.ollama_host, "Point ollama_host at localhost or a private-LAN address.")
    models: list[str] = []
    try:
        r = httpx.get(f"{s.ollama_host}/api/tags", timeout=3, trust_env=False)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        add("Ollama running", True, f"{len(models)} model(s) installed")
    except httpx.HTTPError as e:
        add("Ollama running", False, str(e), "Start Ollama (`ollama serve`).")
    want = s.profile.llm_model
    have = want in models or (":" not in want and f"{want}:latest" in models)
    add(f"LLM {want} pulled", have, "present" if have else "not installed", f"While online: `ollama pull {want}`.")

    ok, detail = stt_model_present(s.profile.stt_model)
    add(f"Speech model {s.profile.stt_model}", ok, detail, "While online: `scribe setup`.")

    free = disk_free(s.vault)
    add("Disk space", free >= MIN_FREE_BYTES, f"{free / 1024**3:.1f} GB free", "Free at least 5 GB (a 3-hour session is ~0.3-1 GB).")

    c = load_campaign(s.campaign_dir, s.vault / "config" / "glossary_extra.txt")
    add("Campaign export loaded", bool(c.entities) and bool(c.pcs()),
        f"{c.files} file(s), {len(c.entities)} names, PCs: {', '.join(c.pcs()) or 'none found'}; last session S{c.last_session or '?'}",
        "Export the Campaign Hub to markdown into vault/campaign/ (or list PCs in config/glossary_extra.txt as `Name | pc`).")

    q = JobQueue(s.db_path)
    mic = q.get_setting("mic_test")
    mic_ok = bool(mic) and mic.startswith("ok:") and time.time() - float(mic.split(":")[1]) < 14 * 86400
    add("Test recording", mic_ok, mic or "never run", "Run the test recording on the readiness page.")

    hb = float(q.get_setting("worker_heartbeat", "0") or 0)
    add("Worker running", time.time() - hb < 30, "heartbeat " + (f"{time.time() - hb:.0f}s ago" if hb else "never"), "Start it with `scribe worker` (or `scribe run`).")
    return checks
