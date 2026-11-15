import socket
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from scribe.api import create_app
from scribe.config import load_settings
from scribe.jobs import JobQueue
from scribe.sessions import SessionStore
from scribe.worker import Worker

from .conftest import FakeLLM, FakeTranscriber, make_tone


@pytest.fixture
def no_outbound(monkeypatch):
    """R1 in miniature: any socket connection to a non-loopback address fails
    the test. The pipeline (with model calls faked) must not need one."""
    real = socket.socket.connect

    def guard(self, addr):
        host = addr[0] if isinstance(addr, tuple) else addr
        if isinstance(host, str) and not (host.startswith("127.") or host in ("::1", "localhost")) and self.family != socket.AF_UNIX:
            raise AssertionError(f"outbound connection attempted to {addr}")
        return real(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guard)


def drain(worker):
    while worker.step():
        pass


def webm_chunks(tmp_path, seconds):
    f = make_tone(tmp_path / "live.webm", seconds, ("-ac", "1", "-c:a", "libopus", "-b:a", "24k"))
    data = f.read_bytes()
    third = len(data) // 3
    return [data[:third], data[third:2 * third], data[2 * third:]]


def test_live_recording_to_saved_log(settings, tmp_path, no_outbound):
    client = TestClient(create_app(settings))
    stt, llm = FakeTranscriber(), FakeLLM()
    worker = Worker(settings, make_transcriber=lambda: stt, make_llm=lambda: llm)

    m = client.post("/api/sessions", json={"date": "2026-11-21"}).json()
    assert m["id"] == "S32", "next number follows the campaign's last session (S31)"
    sid = m["id"]
    pos = 0
    chunks = webm_chunks(tmp_path, 30)
    for i, c in enumerate(chunks):
        r = client.put(f"/api/sessions/{sid}/segments/0?pos={pos}&offset=0&mime=audio/webm", content=c)
        assert r.status_code == 200, r.text
        if i == 1:
            # A retried upload of the same chunk must not duplicate bytes.
            assert client.put(f"/api/sessions/{sid}/segments/0?pos={pos}&offset=0&mime=audio/webm", content=c).json()["size"] == r.json()["size"]
        pos = r.json()["size"]
    assert client.put(f"/api/sessions/{sid}/segments/0?pos={pos + 10}&offset=0", content=b"x").status_code == 409
    client.post(f"/api/sessions/{sid}/bookmarks", json={"t": 12.0, "label": "Big reveal"})
    client.post(f"/api/sessions/{sid}/segments/0/finish", json={"duration": 30})
    assert client.post(f"/api/sessions/{sid}/stop").json()["status"] == "transcribing"

    drain(worker)
    meta = client.get(f"/api/sessions/{sid}").json()
    assert meta["status"] == "drafted", meta

    t = client.get(f"/api/sessions/{sid}/transcript").json()
    assert len(t["lines"]) == 6
    assert any("Izali" in l["text"] and l.get("raw") for l in t["lines"]), "glossary snap applied, raw kept"
    assert [l["table_talk"] for l in t["lines"]].count(True) == 1
    assert t["bookmarks"][0]["label"] == "Big reveal"

    d = client.get(f"/api/sessions/{sid}/draft").json()
    assert d["check"] == []
    # Nothing exported before the DM saves (R13).
    assert client.get(f"/api/sessions/{sid}/export/log").status_code == 409
    assert not (settings.sessions_dir / sid / "log.md").exists()

    sections = d["sections"]
    sections["prep"] = [{"id": "D1", "text": "Prepared the Vogler ambush.", "lines": []}]
    prev = client.post(f"/api/sessions/{sid}/preview-save", json={"sections": sections}).json()
    assert "+- Prepared the Vogler ambush." in prev["diff"]
    assert client.post(f"/api/sessions/{sid}/save", json={"sections": sections, "base": "stale"}).status_code == 409
    saved = client.post(f"/api/sessions/{sid}/save", json={"sections": sections, "base": prev["base"]}).json()
    assert saved["saved"] and saved["diff"]

    # A second save shows only what changed (R13: each save shows a diff).
    sections["prep"][0]["text"] = "Prepared the Vogler ambush and the letter."
    prev2 = client.post(f"/api/sessions/{sid}/preview-save", json={"sections": sections}).json()
    assert "-- Prepared the Vogler ambush.\n" in prev2["diff"] and "+- Prepared the Vogler ambush and the letter." in prev2["diff"]

    log = client.get(f"/api/sessions/{sid}/export/log?save=true").text
    assert "## Session Notes" in log and "Prepared the Vogler ambush." in log
    assert (settings.vault / "exports" / sid / f"{sid}_log.md").exists()
    safe = client.get(f"/api/sessions/{sid}/export/log-player-safe").text
    assert "[DM-ONLY]" not in safe
    tr = client.get(f"/api/sessions/{sid}/export/transcript").text
    assert "(table talk)" in tr and "Bookmark 00:00:12" in tr
    assert "Flagged" in client.get(f"/api/sessions/{sid}/export/notes").text

    # R21: audio can go, the log stays.
    assert client.delete(f"/api/sessions/{sid}/audio").json()["freed"] > 0
    assert client.get(f"/api/sessions/{sid}/export/log").text == log
    assert client.get(f"/api/sessions/{sid}/audio/seg_000.webm").status_code == 404


def test_closed_tab_keeps_uploaded_audio_and_resumes(settings, tmp_path):
    """Scenario C / R3: nothing uploaded is lost; the timeline continues."""
    client = TestClient(create_app(settings))
    sid = client.post("/api/sessions", json={}).json()["id"]
    c = webm_chunks(tmp_path, 20)
    r = client.put(f"/api/sessions/{sid}/segments/0?pos=0&offset=0", content=c[0] + c[1] + c[2])
    assert r.status_code == 200
    # ...tab closes here: no finish, no stop.
    status = client.get("/api/status").json()
    assert [m["id"] for m in status["unfinished"]] == [sid]
    el = client.get(f"/api/sessions/{sid}/elapsed").json()
    assert el["next_index"] == 1 and 19 <= el["elapsed"] <= 21
    client.put(f"/api/sessions/{sid}/segments/1?pos=0&offset={el['elapsed']}", content=c[0] + c[1] + c[2])
    client.post(f"/api/sessions/{sid}/stop")
    segs = SessionStore(settings.sessions_dir).segments(sid)
    assert [s["file"] for s in segs] == ["seg_000.webm", "seg_001.webm"] and all(s["final"] for s in segs)
    assert segs[1]["offset"] == pytest.approx(el["elapsed"])


def test_import_splits_long_file_and_tracks(settings, tmp_path):
    """R4: a long file becomes ~10-minute segments on one timeline; R8: one
    file per speaker gives labelled lines."""
    client = TestClient(create_app(settings))
    f = make_tone(tmp_path / "long.mp3", 21 * 60)
    r = client.post("/api/sessions/import", files={"files": ("long.mp3", f.read_bytes(), "audio/mpeg")}, data={"date": "2026-11-28"})
    sid = r.json()["id"]
    for _ in range(200):
        if client.get(f"/api/sessions/{sid}").json()["status"] != "importing":
            break
        time.sleep(0.1)
    segs = client.get(f"/api/sessions/{sid}").json()["segments"]
    assert len(segs) == 3 and segs[1]["offset"] == pytest.approx(600, abs=0.1) and segs[2]["offset"] == pytest.approx(1200, abs=0.2)
    assert len(JobQueue(settings.db_path).for_session(sid)) == 4  # 3 STT + summary

    a, b = make_tone(tmp_path / "a.wav", 10), make_tone(tmp_path / "b.wav", 10)
    r = client.post("/api/sessions/import", files=[("files", ("lucan.wav", a.read_bytes())), ("files", ("ana.wav", b.read_bytes()))],
                    data={"speakers": ["Connor", "Sam"]})
    sid2 = r.json()["id"]
    for _ in range(100):
        if client.get(f"/api/sessions/{sid2}").json()["status"] != "importing":
            break
        time.sleep(0.1)
    w = Worker(settings, make_transcriber=FakeTranscriber, make_llm=FakeLLM)
    drain(w)
    t = client.get(f"/api/sessions/{sid2}/transcript").json()
    assert {l["speaker"] for l in t["lines"]} == {"Connor", "Sam"}
    client.post(f"/api/sessions/{sid2}/speakers", json={"raw": "Sam", "name": "Ana's player"})
    assert "Ana's player:" in client.get(f"/api/sessions/{sid2}/export/transcript").text


def test_killed_worker_resumes_from_last_finished_segment(settings, tmp_path):
    """R7: kill mid-job, restart, and only unfinished segments are redone."""
    store = SessionStore(settings.sessions_dir)
    m = store.create(40, "import")
    for i in range(3):
        make_tone(store.audio_path(m["id"], f"seg_{i:03d}.wav"), 10)
        store.upsert_segment(m["id"], {"index": i, "file": f"seg_{i:03d}.wav", "offset": i * 10.0, "duration": 10.0, "final": True})
    from scribe.worker import enqueue_processing
    q = JobQueue(settings.db_path)
    enqueue_processing(q, store, m["id"])

    first = FakeTranscriber()
    w1 = Worker(settings, make_transcriber=lambda: first, make_llm=FakeLLM)
    w1.step()                      # segment 0 done
    q.claim()                      # segment 1 claimed... and the process dies
    assert first.calls == ["seg_000.wav"]

    second = FakeTranscriber()
    w2 = Worker(settings, make_transcriber=lambda: second, make_llm=FakeLLM)
    assert w2.q.recover() == 1
    drain(w2)
    assert second.calls == ["seg_001.wav", "seg_002.wav"]
    assert store.meta(m["id"])["status"] == "drafted"


def test_models_never_share_the_gpu(settings, tmp_path):
    """Spec 6.5: STT is released before the LLM loads, and vice versa."""
    events = []

    class T(FakeTranscriber):
        def close(self):
            events.append("stt-close")

    class L(FakeLLM):
        def unload(self):
            events.append("llm-unload")

    w = Worker(settings, make_transcriber=lambda: (events.append("stt-load"), T())[1], make_llm=lambda: (events.append("llm-load"), L())[1])
    w._transcriber()
    w._llm_client()
    w._transcriber()
    assert events == ["stt-load", "stt-close", "llm-load", "llm-unload", "stt-load"]


def test_login_required_beyond_localhost(vault):
    s = load_settings(vault)
    s.bind = "0.0.0.0"
    with pytest.raises(SystemExit):
        create_app(s)
    s.password = "hunter2"
    c = TestClient(create_app(s))
    assert c.get("/api/sessions").status_code == 401
    assert c.get("/index.html", follow_redirects=False).headers["location"] == "/login.html"
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"password": "hunter2"}).status_code == 200
    assert c.get("/api/sessions").status_code == 200


def test_localhost_needs_no_login(settings):
    assert TestClient(create_app(settings)).get("/api/sessions").status_code == 200


def test_readiness_is_red_without_dependencies(settings):
    from scribe.readiness import run_checks
    checks = {c["name"]: c for c in run_checks(settings)}
    assert checks["ffmpeg"]["ok"] and checks["Campaign export loaded"]["ok"]
    assert not checks["Ollama running"]["ok"] and not checks["Test recording"]["ok"]
    assert all(c["fix"] for c in checks.values() if not c["ok"])


def test_mic_test_round_trip(settings, tmp_path):
    c = TestClient(create_app(settings))
    data = make_tone(tmp_path / "m.webm", 3, ("-ac", "1", "-c:a", "libopus")).read_bytes()
    assert c.post("/api/readiness/mic", content=data).json()["ok"]
    assert not c.post("/api/readiness/mic", content=b"garbage").json()["ok"]
