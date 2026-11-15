"""Render a draft into the campaign's session-log markdown, and check it (R11).

The heading layout lives in a template so it can be matched to the existing
docs without code changes: copy DEFAULT_TEMPLATE to
`vault/config/log_template.md` and edit it. Every `## ` heading in the
template is a required heading in the output.
"""

from __future__ import annotations

import re
from pathlib import Path

from .canon import bad_tags
from .transcript import fmt_ts

DEFAULT_TEMPLATE = """# {session_id} — {date}

## Session Number
{number}

## Date
{date}

## Prep
{prep}

## Session Notes
{session_notes}

## NPC Changes/Statuses
{npc_changes}

## Unresolved Threads
{unresolved_threads}

## [VERIFY]
{verify}
"""
SECTIONS = ("prep", "session_notes", "npc_changes", "unresolved_threads", "verify")
REQUIRED = ("Session Number", "Date", "Prep", "Session Notes", "NPC Changes/Statuses", "Unresolved Threads")


def load_template(vault: Path) -> str:
    f = vault / "config" / "log_template.md"
    return f.read_text(encoding="utf-8") if f.exists() else DEFAULT_TEMPLATE


def _cite(item: dict, mode: str) -> str:
    if mode == "none" or item.get("start") is None:
        return ""
    span = f"{fmt_ts(item['start'])}–{fmt_ts(item['end'])}"
    if mode == "inline":
        return f" ({span})"
    # An HTML comment keeps the citation in the file (and in git) without
    # showing up in rendered markdown.
    return f" <!-- t={span} lines={','.join(item['lines'][:12])} -->"


def render(meta: dict, sections: dict, template: str, cite_mode: str = "comment", player_safe: bool = False) -> str:
    def block(name: str) -> str:
        items = sections.get(name) or []
        if player_safe:
            # Secrets rule: [DM-ONLY] never reaches a [PLAYER-SAFE] document.
            items = [i for i in items if "[DM-ONLY]" not in i["text"]]
        if name == "verify":
            return "\n".join(f"- [VERIFY V{n}] {i['text'].strip()}{_cite(i, cite_mode)}" for n, i in enumerate(items, 1)) or "- (none)"
        return "\n".join(f"- {i['text'].strip()}{_cite(i, cite_mode)}" for i in items) or "- "

    out = template.format(
        session_id=meta["id"], number=meta["number"], date=meta["date"],
        **{s: block(s) for s in SECTIONS},
    )
    if player_safe:
        out = out.replace("\n", " [PLAYER-SAFE]\n", 1)
    return out


def required_headings(template: str) -> list[str]:
    hs = [m.group(1).strip() for m in re.finditer(r"^#{2,6}\s+(.+)$", template, re.M)]
    return hs or list(REQUIRED)


def check(markdown: str, template: str, extra_tags: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Template check for R11: every heading present, every tag valid.
    Returns problems; empty means pass."""
    problems = []
    present = {m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+)$", markdown, re.M)}
    for h in required_headings(template):
        if h not in present:
            problems.append(f"missing heading: {h}")
    body = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    for t in bad_tags(body, extra_tags):
        problems.append(f"unknown tag: [{t}]")
    if "[PLAYER-SAFE]" in body and "[DM-ONLY]" in body:
        problems.append("[DM-ONLY] content inside a [PLAYER-SAFE] document")
    return problems
