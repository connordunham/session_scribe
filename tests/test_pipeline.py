import pytest

from scribe.campaign import load_campaign
from scribe.config import load_settings
from scribe.llm import estimate_tokens
from scribe.pipeline.chunking import chunk_transcript
from scribe.pipeline.logformat import DEFAULT_TEMPLATE, check, render
from scribe.pipeline.summarize import summarize

from .conftest import FakeLLM, synthetic_lines


@pytest.mark.parametrize("profile", ["test-8gb", "target-24gb"])
def test_three_hour_transcript_fits_both_profiles(vault, profile):
    """R9: ~27k words summarize with no context overflow on both profiles."""
    s = load_settings(vault, profile=profile)
    lines = synthetic_lines()
    assert sum(len(l["text"].split()) for l in lines) >= 26000
    llm = FakeLLM(context=s.profile.llm_context)
    d = summarize(lines, [], {}, load_campaign(vault / "campaign"), llm, s.profile)
    assert llm.max_prompt <= s.profile.llm_context
    assert d["chunks"] > 1 and d["sections"]["session_notes"]


def test_chunks_respect_budget_and_split_at_bookmarks():
    lines = synthetic_lines(3000)
    bm = {"id": "B1", "t": lines[150]["start"] - 0.5, "label": "reveal"}
    chunks = chunk_transcript(lines, [bm], max_tokens=3500, target_minutes=12)
    assert all(sum(estimate_tokens(l["text"]) for l in c.lines) <= 3500 for c in chunks)
    # With the token budget out of the way, the bookmark decides the cut.
    chunks = chunk_transcript(lines, [bm], max_tokens=8000, target_minutes=20)
    assert any(c.lines[0]["id"] == "L0151" for c in chunks), "a bookmark well into a chunk should start a new scene"
    assert sum(len(c.bookmarks) for c in chunks) == 1
    assert [l["id"] for c in chunks for l in c.lines] == [l["id"] for l in lines]


def _draft(vault):
    s = load_settings(vault)
    lines = [
        {"id": "L0001", "start": 0, "end": 4, "text": "Lucan draws his sword at the gate of Vogler.", "speaker": None},
        {"id": "L0002", "start": 5, "end": 9, "text": "Who wants pizza?", "speaker": None},
        {"id": "L0003", "start": 10, "end": 14, "text": "Becklin says the Raven has been seen near Kalaman.", "speaker": None},
        {"id": "L0004", "start": 15, "end": 19, "text": "We still don't know who sent the letter.", "speaker": None},
    ]
    return summarize(lines, [], {}, load_campaign(vault / "campaign"), FakeLLM(), s.profile), lines


def test_grounding_drops_and_flags_uncited_facts(vault):
    """R12: every bullet cites real lines; uncited facts are flagged and left out."""
    d, lines = _draft(vault)
    ids = {l["id"] for l in lines}
    for sec in ("session_notes", "npc_changes", "unresolved_threads"):
        for it in d["sections"][sec]:
            assert it["lines"] and set(it["lines"]) <= ids
            assert it["start"] is not None
    texts = " ".join(i["text"] for i in d["sections"]["session_notes"])
    assert "Zarkoth" not in texts and "nobody said" not in texts
    reasons = {f["text"]: f["reason"] for f in d["flagged"]}
    assert "The dragon Zarkoth attacked." in reasons and "Something nobody said happened." in reasons


def test_table_talk_left_out_of_log(vault):
    """R10: off-topic lines are reported, and never become log bullets."""
    d, _ = _draft(vault)
    assert d["off_topic"] == ["L0002"]
    assert all("pizza" not in i["text"] for s in d["sections"].values() for i in s)


def test_new_canon_is_tagged_and_secrets_marked(vault):
    """R14: a name not in the campaign gets [INFERRED]; known names don't."""
    d, _ = _draft(vault)
    notes = {i["text"].split(" [")[0]: i for i in d["sections"]["session_notes"]}
    morwen = next(i for k, i in notes.items() if "Morwen" in k)
    assert "[INFERRED]" in morwen["text"] and "[DM-ONLY]" in morwen["text"]
    assert "Morwen" in morwen["unknown_names"]
    lucan = next(i for k, i in notes.items() if k.startswith("Lucan"))
    assert "[INFERRED]" not in lucan["text"]
    npc = d["sections"]["npc_changes"][0]
    assert npc["text"].startswith("**Becklin** — Ally")
    assert d["sections"]["unresolved_threads"][0]["text"].startswith("Who sent the letter")
    assert d["sections"]["verify"][0]["text"].startswith("Was it Ana or Izali")


def test_rendered_log_passes_template_check(vault):
    """R11: all headings present, all tags valid."""
    d, _ = _draft(vault)
    md = render({"id": "S32", "number": 32, "date": "2026-11-21"}, d["sections"], DEFAULT_TEMPLATE)
    assert check(md, DEFAULT_TEMPLATE, {"ALLY"}) == []
    assert "[VERIFY V1]" in md and "<!-- t=00:00:00" in md


def test_template_check_catches_problems():
    md = "## Session Number\n32\n## Date\nx\n## Prep\n- \n## Session Notes\n- Fought [BOGUS]\n"
    problems = check(md, DEFAULT_TEMPLATE)
    assert "missing heading: NPC Changes/Statuses" in problems and "unknown tag: [BOGUS]" in problems


def test_player_safe_export_strips_dm_only(vault):
    d, _ = _draft(vault)
    md = render({"id": "S32", "number": 32, "date": "x"}, d["sections"], DEFAULT_TEMPLATE, cite_mode="none", player_safe=True)
    assert "[DM-ONLY]" not in md and "Morwen" not in md and "[PLAYER-SAFE]" in md
    assert check(md, DEFAULT_TEMPLATE) == []
