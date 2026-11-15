import json
import re
import subprocess
from pathlib import Path

import pytest

from scribe.audio import decoded_duration
from scribe.config import load_settings
from scribe.llm import ContextOverflow, estimate_tokens
from scribe.transcribe import Piece

CAMPAIGN_MD = """# Dragonlance Campaign Hub

Last played: S31 (Session 31). [DM-ONLY] notes live in the DM tab.

## Player Characters
- **Lucan** — paladin
- **Ana** — rogue
- **Izali**: wizard
- **Spooky** — warlock
- **Barley** — cleric

## NPCs
### Becklin
Status: [ALLY]
### Rynick
| Name | Status |
|------|--------|
| Raven | Missing |
| Malfot | Hostile |

## Places
- Kalaman: port city
- **Vogler** - village

## Deities and Moons
- Solinari: white moon
"""


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "campaign").mkdir(parents=True)
    (v / "campaign" / "hub.md").write_text(CAMPAIGN_MD, encoding="utf-8")
    return v


@pytest.fixture
def settings(vault):
    return load_settings(vault)


def make_tone(path: Path, seconds: float, fmt_args=("-ac", "1", "-ar", "8000")) -> Path:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", *fmt_args, str(path)], check=True)
    return path


class FakeTranscriber:
    """One line every 5 s of audio. Names come out misspelled the way Whisper
    tends to ('Izzaly'), so the glossary snap is exercised end to end."""

    SCRIPT = [
        "Lucan draws his sword at the gate of Vogler.",
        "Who wants pizza? I ordered pizza for everyone.",
        "Then Izalli casts a spell on the guard.",
        "Becklin says the Raven has been seen near Kalaman.",
        "We still don't know who sent the letter.",
        "Barley heals Ana after the fight.",
    ]

    def __init__(self):
        self.calls = []

    def transcribe(self, path, prompt, hotwords):
        self.calls.append(Path(path).name)
        d = decoded_duration(Path(path)) or 0
        out, t, i = [], 0.0, 0
        while t + 4 <= d + 0.01:
            out.append(Piece(t, t + 4, self.SCRIPT[i % len(self.SCRIPT)]))
            t += 5
            i += 1
        return out

    def close(self):
        pass


LINE = re.compile(r"^(L\d{4}) \[[\d:]+\] (?:[^:]+: )?(.*)$", re.M)
FACT = re.compile(r"^(F\d+) \((\w+)\): (.*)$", re.M)


class FakeLLM:
    """Deterministic stand-in for Ollama. Enforces the context budget exactly
    as the real client does, so R9 tests fail if a prompt would overflow."""

    model = "fake"

    def __init__(self, context=16384):
        self.context = context
        self.calls = 0
        self.max_prompt = 0

    def chat_json(self, system, user, schema, max_tokens, temperature=0.1):
        self.calls += 1
        need = estimate_tokens(system) + estimate_tokens(user) + max_tokens
        self.max_prompt = max(self.max_prompt, need)
        if need > self.context:
            raise ContextOverflow(need)
        if "facts" in schema["properties"]:
            return self._map(user)
        return self._reduce(user)

    def _map(self, user):
        facts, off = [], []
        for lid, text in LINE.findall(user):
            if "pizza" in text:
                off.append(lid)
            elif "Becklin" in text:
                facts.append({"text": "Becklin reported the Raven near Kalaman.", "kind": "npc", "lines": [lid], "npc_name": "Becklin", "npc_status": "Ally"})
            elif "letter" in text:
                facts.append({"text": "Who sent the letter is unknown.", "kind": "thread", "lines": [lid]})
            else:
                facts.append({"text": text, "kind": "event", "lines": [lid]})
        # Two things the grounding rule must catch:
        facts.append({"text": "The dragon Zarkoth attacked.", "kind": "event", "lines": ["L9999"]})
        facts.append({"text": "A hooded figure named Morwen watched from the tavern.", "kind": "event", "lines": [LINE.findall(user)[0][0]], "dm_only": True})
        return {"scene": {"title": "Vogler", "location": "Vogler"}, "off_topic_line_ids": off, "facts": facts,
                "unsure": [{"text": "Was it Ana or Izali who opened the door?", "lines": []}]}

    def _reduce(self, user):
        ids = FACT.findall(user)
        bullets = [{"text": text, "facts": [fid]} for fid, _, text in ids[:40]]
        bullets.append({"text": "Something nobody said happened.", "facts": ["F99999"]})
        return {"bullets": bullets}


def synthetic_lines(n_words=27000, words_per_line=12, seconds_per_line=4.8):
    lines, t = [], 0.0
    base = "Lucan and Ana walk the road to Kalaman while Izali studies the map and Barley counts coins".split()
    for i in range(n_words // words_per_line):
        text = " ".join(base[(i + k) % len(base)] for k in range(words_per_line))
        lines.append({"id": f"L{i + 1:04d}", "start": round(t, 2), "end": round(t + 4, 2), "text": text, "speaker": None, "segment": 0})
        t += seconds_per_line
    return lines
