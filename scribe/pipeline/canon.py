"""Canon and tag rules (R14, spec 6.5).

New canon is never silent: any capitalised name in generated text that the
campaign export does not know gets an [INFERRED] tag on its bullet.
"""

from __future__ import annotations

import re

from ..campaign import Campaign

ALLOWED_TAGS = re.compile(r"^(DM-ONLY|INFERRED|SUGGESTION|PLAYER-SAFE|VERIFY(?: V\d+)?)$")
TAG_IN_TEXT = re.compile(r"\[([A-Z][A-Z0-9 \-]{2,20})\]")
CAP_WORD = re.compile(r"(?<![\w'’])([A-Z][a-z'’]+(?:\s+[A-Z][a-z'’]+)*)")
# Capitalised words that are not names. Unvalidated, extended as false
# positives show up in review; a false positive only costs an extra tag.
COMMON = set("""
I I'm I've I'd The A An And But Or If Then When While After Before During Later Meanwhile
He She They It We You His Her Their Its Our Your This That These Those There Here
DM PC PCs NPC NPCs Session Party Group Everyone Someone Nobody
Monday Tuesday Wednesday Thursday Friday Saturday Sunday
January February March April May June July August September October November December
Dragon Dragons Dungeon Dungeons Dragonlance D&D Unknown Unclear
""".split())


def unknown_names(text: str, campaign: Campaign) -> list[str]:
    text = TAG_IN_TEXT.sub(" ", text)
    out = []
    for m in CAP_WORD.finditer(text):
        name = m.group(1)
        before = text[: m.start()].rstrip()
        words = name.split()
        # Sentence-initial word: drop it, keep any following capitalised words.
        if not before or before[-1] in ".!?:;\"(":
            words = words[1:]
        while words and words[0] in COMMON:
            words = words[1:]
        if not words:
            continue
        cand = " ".join(words)
        if not campaign.is_known(cand) and not all(campaign.is_known(w) or w in COMMON for w in words):
            out.append(cand)
    return list(dict.fromkeys(out))


def bad_tags(text: str, extra: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Tags outside the campaign's vocabulary. `extra` is every bracketed
    label the campaign export itself uses (its status labels among them), so
    the check follows the docs rather than a hard-coded list."""
    return [t for t in TAG_IN_TEXT.findall(text) if not ALLOWED_TAGS.match(t) and t not in extra]


def ensure_tag(text: str, tag: str) -> str:
    return text if f"[{tag}]" in text else f"{text} [{tag}]"
