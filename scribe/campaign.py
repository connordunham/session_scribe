"""Campaign loader (R15).

Reads the markdown export of the Campaign Hub from `vault/campaign/` (the app
never writes there) and builds the glossary and known-entity list that feed the
speech-to-text hint (R6) and the canon rule (R14).

The Hub's exact export layout was not available when this was written, so the
parser is heuristic and deliberately tolerant. It recognises entities in three
common markdown shapes, under any heading whose text names a category:

    ## NPCs                      ## Places              | Name | Status |
    ### Becklin                  - **Kalaman** - city   |------|--------|
    - **Rynick**: ...            - Vogler: village      | Raven | Alive |

Anything it misses can be added by hand to `vault/config/glossary_extra.txt`
(one name per line, optionally `Name | category`). Check the result on the
readiness page after every export: a missing PC name costs transcript accuracy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Checked in order; first match wins. Word boundaries matter: "NPCs"
# contains "pcs", and "Player Characters" contains "characters".
CATEGORY_PATTERNS = [
    ("pc", r"\bpcs?\b|player characters?|\bparty\b|\bplayers\b"),
    ("npc", r"\bnpcs?\b|non-player|\bcharacters\b|\ballies\b|\bvillains\b|\bantagonists\b"),
    ("place", r"\bplaces?\b|\blocations?\b|\bregions?\b|\bcit(y|ies)\b|\btowns?\b|\bgeography\b"),
    ("item", r"\bitems?\b|\bloot\b|\bartifacts?\b|\bartefacts?\b|\btreasure\b|\binventory\b|\bequipment\b"),
    ("faction", r"\bfactions?\b|\borgani[sz]ations?\b|\bguilds?\b|\borders?\b"),
    ("deity", r"\bdeit(y|ies)\b|\bgods?\b|\bmoons?\b|\bpantheon\b"),
]
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BOLD_ITEM = re.compile(r"^\s*[-*+]\s+\*\*([^*]{2,60})\*\*")
PLAIN_ITEM = re.compile(r"^\s*[-*+]\s+([A-Z][\w'’\- ]{1,50}?)\s*(?::|—|–| - )")
TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
SESSION_REF = re.compile(r"\b(?:S|Session\s+)(\d{1,4})\b")
ANY_TAG = re.compile(r"\[([A-Z][A-Z0-9 \-]{2,20})\]")
NOT_NAMES = {"name", "status", "notes", "description", "summary", "overview", "todo", "none", "n/a"}


@dataclass
class Campaign:
    entities: dict[str, str] = field(default_factory=dict)  # name -> category
    last_session: int | None = None
    tags: set[str] = field(default_factory=set)
    files: int = 0

    @property
    def glossary(self) -> list[str]:
        order = {"pc": 0, "npc": 1, "place": 2, "deity": 3, "faction": 4, "item": 5}
        return sorted(self.entities, key=lambda n: (order.get(self.entities[n], 9), n.lower()))

    def is_known(self, name: str) -> bool:
        n = _norm(name)
        if not n:
            return True
        known = {_norm(k) for k in self.entities}
        if n in known:
            return True
        # "Kalisandi" alone counts as known when "Kalisandi Fire Eyes" is; the
        # length floor stops "the" or "of" matching every multi-word name.
        return len(n) > 3 and any(n in k.split() for k in known)

    def pcs(self) -> list[str]:
        return [n for n, c in self.entities.items() if c == "pc"]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def _category(heading: str) -> str | None:
    h = heading.lower()
    for cat, pat in CATEGORY_PATTERNS:
        if re.search(pat, h):
            return cat
    return None


def _clean(name: str) -> str | None:
    name = re.sub(r"\[[^\]]*\]", "", name)  # drop inline tags
    name = re.sub(r"[*_`]", "", name).strip(" :-—–.,")
    if not name or len(name) > 50 or name.lower() in NOT_NAMES:
        return None
    if not name[0].isupper():
        return None
    return name


def parse_markdown(text: str, campaign: Campaign) -> None:
    stack: list[tuple[int, str | None]] = []
    table_header_seen = False
    campaign.tags.update(ANY_TAG.findall(text))
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent_cat = next((c for _, c in reversed(stack) if c), None)
            own = _category(title)
            if parent_cat and not own:
                # A sub-heading under "NPCs" is an entity, not a new section.
                name = _clean(title)
                if name:
                    campaign.entities.setdefault(name, parent_cat)
            stack.append((level, own or parent_cat))
            table_header_seen = False
            continue
        for s in SESSION_REF.findall(line):
            n = int(s)
            if n < 1000:
                campaign.last_session = max(campaign.last_session or 0, n)
        cat = next((c for _, c in reversed(stack) if c), None)
        if not cat:
            continue
        m = BOLD_ITEM.match(line) or PLAIN_ITEM.match(line)
        if m:
            name = _clean(m.group(1))
            if name:
                campaign.entities.setdefault(name, cat)
            continue
        t = TABLE_ROW.match(line)
        if t:
            cells = [c.strip() for c in t.group(1).split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if not table_header_seen:
                table_header_seen = True  # first row is the header
                continue
            name = _clean(cells[0]) if cells else None
            if name:
                campaign.entities.setdefault(name, cat)


def load_campaign(campaign_dir: Path, extra_file: Path | None = None) -> Campaign:
    c = Campaign()
    if campaign_dir.exists():
        for f in sorted(campaign_dir.rglob("*.md")):
            parse_markdown(f.read_text(encoding="utf-8", errors="replace"), c)
            c.files += 1
    if extra_file and extra_file.exists():
        for line in extra_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, cat = line.partition("|")
            if name.strip():
                c.entities[name.strip()] = cat.strip().lower() or "npc"
    return c


def stt_prompt(c: Campaign, limit_chars: int = 600) -> str:
    """Whisper's prompt window is ~224 tokens; PCs and NPCs go first because
    they are spoken most and misspelled most."""
    out, n = [], 0
    for name in c.glossary:
        if n + len(name) + 2 > limit_chars:
            break
        out.append(name)
        n += len(name) + 2
    return "Dungeons & Dragons session. Names: " + ", ".join(out) + "."
