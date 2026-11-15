"""`scribe` command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import load_settings


def _settings(a):
    return load_settings(a.vault, **({"profile": a.profile} if a.profile else {}))


def cmd_serve(a):
    import uvicorn

    from .api import create_app

    s = _settings(a)
    if a.bind:
        s.bind = a.bind
    if a.port:
        s.port = a.port
    if s.requires_login and not s.ssl_certfile:
        print("WARNING: browsers only allow microphone access over HTTPS (or on localhost). "
              "Set SCRIBE_SSL_CERT/SCRIBE_SSL_KEY to record from other machines.", file=sys.stderr)
    uvicorn.run(create_app(s), host=s.bind, port=s.port, ssl_certfile=s.ssl_certfile, ssl_keyfile=s.ssl_keyfile, log_level="info")


def cmd_worker(a):
    from .worker import Worker

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Worker(_settings(a)).run_forever()


def cmd_run(a):
    """API and worker together, as two processes (the worker owns the GPU)."""
    base = [sys.executable, "-m", "scribe.cli"] + (["--vault", a.vault] if a.vault else []) + (["--profile", a.profile] if a.profile else [])
    w = subprocess.Popen(base + ["worker"])
    try:
        cmd_serve(a)
    finally:
        w.terminate()
        w.wait(10)


def cmd_setup(a):
    """Run ONCE while online: fetch the speech model(s) into the local cache
    and pull the LLM(s) for every profile."""
    s = _settings(a)
    os.environ.pop("HF_HUB_OFFLINE", None)
    from faster_whisper.utils import download_model

    profiles = s.profiles.values() if a.all_profiles else [s.profile]
    for p in profiles:
        for m in filter(None, [p.stt_model, p.stt_fallback_model]):
            print(f"speech model {m}: {download_model(m)}")
        print(f"ollama pull {p.llm_model}")
        subprocess.run(["ollama", "pull", p.llm_model], check=False)
    print("Now run `scribe check` with the network off.")


def cmd_check(a):
    from .readiness import run_checks

    checks = run_checks(_settings(a))
    for c in checks:
        print(f"[{'OK ' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}" + (f"\n       fix: {c['fix']}" if c["fix"] else ""))
    ready = all(c["ok"] for c in checks)
    print("READY" if ready else "NOT READY")
    sys.exit(0 if ready else 1)


def cmd_transcribe_file(a):
    """M0: transcribe one file with the glossary hint, print timings and memory."""
    from .campaign import load_campaign, stt_prompt
    from .transcribe import WhisperTranscriber, snap_to_glossary

    s = _settings(a)
    c = load_campaign(s.campaign_dir, s.vault / "config" / "glossary_extra.txt")
    p = s.profile
    t0 = time.time()
    tr = WhisperTranscriber(a.model or p.stt_model, p.stt_device, p.stt_compute_type)
    pieces = tr.transcribe(Path(a.file), stt_prompt(c) if not a.no_glossary else "", [] if a.no_glossary else c.glossary)
    out = [{"start": x.start, "end": x.end, "text": x.text if a.no_snap else snap_to_glossary(x.text, c.glossary)} for x in pieces]
    Path(a.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"{len(out)} lines in {time.time() - t0:.0f}s -> {a.out}")


def cmd_accuracy(a):
    """M0/R6: glossary-name accuracy of a transcript against a hand-corrected reference."""
    from .campaign import load_campaign
    from .transcribe import glossary_accuracy

    s = _settings(a)
    c = load_campaign(s.campaign_dir, s.vault / "config" / "glossary_extra.txt")
    hyp = Path(a.hypothesis).read_text(encoding="utf-8")
    if a.hypothesis.endswith(".json"):
        data = json.loads(hyp)
        lines = data["lines"] if isinstance(data, dict) else data
        hyp = "\n".join(x["text"] for x in lines)
    r = glossary_accuracy(hyp, Path(a.reference).read_text(encoding="utf-8"), c.glossary)
    for name, v in sorted(r["per_name"].items()):
        print(f"{name:30} {v['matched']}/{v['reference']}")
    acc = r["accuracy"]
    print(f"glossary accuracy: {r['matched']}/{r['total']} = {acc:.1%}" if acc is not None else "no glossary names in reference")


def cmd_bakeoff(a):
    from .bakeoff import run_bakeoff
    from .campaign import load_campaign
    from .sessions import SessionStore
    from .pipeline import transcript as tx

    s = _settings(a)
    store = SessionStore(s.sessions_dir)
    t = tx.load(store, a.session)
    if t is None:
        sys.exit(f"{a.session} has no transcript yet")
    lo, hi = (float(x) * 60 for x in a.minutes.split("-"))
    lines = [ln for ln in t["lines"] if lo <= ln["start"] < hi]
    bms = [b for b in t["bookmarks"] if lo <= b["t"] < hi]
    c = load_campaign(s.campaign_dir, s.vault / "config" / "glossary_extra.txt")
    out = s.vault / "bakeoff" / f"{a.session}_{int(time.time())}.html"
    print(run_bakeoff(s, lines, bms, c, [m.strip() for m in a.models.split(",")], out, a.context))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scribe")
    ap.add_argument("--vault", help="vault folder (default $SCRIBE_VAULT or ./vault)")
    ap.add_argument("--profile", help="override the active hardware profile")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("serve", cmd_serve), ("run", cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("--bind")
        p.add_argument("--port", type=int)
        p.set_defaults(fn=fn)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    p = sub.add_parser("setup")
    p.add_argument("--all-profiles", action="store_true")
    p.set_defaults(fn=cmd_setup)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("transcribe-file")
    p.add_argument("file")
    p.add_argument("--out", default="transcript_m0.json")
    p.add_argument("--model")
    p.add_argument("--no-glossary", action="store_true")
    p.add_argument("--no-snap", action="store_true")
    p.set_defaults(fn=cmd_transcribe_file)
    p = sub.add_parser("accuracy")
    p.add_argument("hypothesis")
    p.add_argument("reference")
    p.set_defaults(fn=cmd_accuracy)
    p = sub.add_parser("bakeoff")
    p.add_argument("session")
    p.add_argument("--models", required=True, help="comma-separated Ollama tags")
    p.add_argument("--minutes", default="0-15", help="excerpt range, e.g. 30-45")
    p.add_argument("--context", type=int)
    p.set_defaults(fn=cmd_bakeoff)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
