import pytest

from scribe.campaign import load_campaign, stt_prompt
from scribe.config import Profile, is_private_host, load_settings, set_active_profile


def test_campaign_export_yields_pcs_npcs_places(vault):
    c = load_campaign(vault / "campaign")
    for name in ["Lucan", "Ana", "Izali", "Spooky", "Barley"]:
        assert c.entities[name] == "pc"
    for name in ["Becklin", "Rynick", "Raven", "Malfot"]:
        assert c.entities[name] == "npc"
    assert c.entities["Kalaman"] == "place" and c.entities["Vogler"] == "place"
    assert c.entities["Solinari"] == "deity"
    assert c.last_session == 31
    assert {"DM-ONLY", "ALLY"} <= c.tags
    assert "Izali" in stt_prompt(c)


def test_glossary_extra_file(vault):
    (vault / "config").mkdir(exist_ok=True)
    (vault / "config" / "glossary_extra.txt").write_text("# comment\nKalisandi Fire Eyes | npc\nOnyari\n")
    c = load_campaign(vault / "campaign", vault / "config" / "glossary_extra.txt")
    assert c.entities["Kalisandi Fire Eyes"] == "npc" and "Onyari" in c.entities
    assert c.is_known("Kalisandi")  # part of a known multi-word name
    assert not c.is_known("Zarkoth")


def test_profiles_load_and_switch(settings):
    assert settings.profile.name == "test-8gb" and settings.profile.llm_context == 16384
    set_active_profile(settings, "target-24gb")
    assert load_settings(settings.vault).profile.llm_model == "qwen3.8:27b"


def test_profile_rejects_chunks_that_cannot_fit():
    with pytest.raises(ValueError):
        Profile.from_dict("x", dict(stt_model="a", stt_device="cpu", stt_compute_type="int8", llm_model="m", llm_context=4096,
                                     chunk_minutes=10, chunk_tokens=3500, reduce_input_tokens=1000, max_output_tokens=1000))


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:11434", True), ("http://localhost:11434", True), ("http://192.168.1.20:11434", True),
    ("http://gpu-box:11434", True), ("http://10.0.0.5:11434", True), ("https://api.example.com", False), ("http://8.8.8.8:11434", False),
])
def test_ollama_host_must_be_private(url, ok):
    assert is_private_host(url) is ok
