"""Chunked map-reduce from transcript to a cited session-log draft.

Map (extractor role, strict JSON, low temperature): one chunk at a time,
returns in-game facts, each citing transcript line ids, plus the lines that
were table talk. Reduce (writer role): merges facts into log bullets that
cite fact ids, which resolve back to line ids.

The grounding rule (R12) is enforced in code, not trusted to the prompt: a
fact or bullet whose citations do not resolve to real lines is dropped and
listed under `flagged` for the DM to see.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..campaign import Campaign
from ..config import Profile
from ..llm import LLM, ContextOverflow, estimate_tokens
from .canon import ensure_tag, unknown_names
from .chunking import Chunk, chunk_transcript
from .transcript import fmt_ts

FACT_KINDS = ["event", "reveal", "combat", "npc", "thread", "loot", "location", "decision"]

MAP_SYSTEM = """You extract facts from a Dungeons & Dragons session transcript for the Dungeon Master's session log.
Each transcript line starts with an id like L0123 and a timestamp.
Rules:
- Only in-game events: what characters did, said, learned, found, decided; NPC status changes; open questions and unresolved threads.
- Table talk (rules discussion, snacks, scheduling, jokes about real life, off-topic chat) is NOT a fact. List those line ids in off_topic_line_ids.
- Every fact MUST cite the line ids that support it in "lines". Never state anything the cited lines do not say.
- Spell names exactly as in the known-names list when they match.
- If you are unsure who did something or how a name is spelled, put it in "unsure" instead of guessing.
- Mark dm_only true for anything the players' characters should not know (DM asides, hidden motives revealed only to the DM).
- Be concise: one short sentence per fact, past tense."""

REDUCE_SYSTEM = """You write the "Session Notes" section of a Dungeons & Dragons session log from extracted facts.
Each fact has an id like F12. Write concise past-tense bullet points in chronological order, merging facts that describe the same moment.
Every bullet MUST list the fact ids it is based on in "facts". Do not add anything the facts do not say. Do not invent names.
Keep names spelled exactly as in the facts."""

THREAD_SYSTEM = """You merge a list of unresolved plot threads from a D&D session into a deduplicated list.
Each input thread has an id like F7. Every output thread MUST list the input ids it merges in "facts". Do not add new threads."""

MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "object", "properties": {"title": {"type": "string"}, "location": {"type": "string"}}, "required": ["title", "location"]},
        "off_topic_line_ids": {"type": "array", "items": {"type": "string"}},
        "facts": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"},
            "kind": {"type": "string", "enum": FACT_KINDS},
            "lines": {"type": "array", "items": {"type": "string"}},
            "npc_name": {"type": "string"},
            "npc_status": {"type": "string"},
            "dm_only": {"type": "boolean"},
        }, "required": ["text", "kind", "lines"]}},
        "unsure": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "lines": {"type": "array", "items": {"type": "string"}}}, "required": ["text"]}},
    },
    "required": ["scene", "off_topic_line_ids", "facts", "unsure"],
}
BULLETS_SCHEMA = {
    "type": "object",
    "properties": {"bullets": {"type": "array", "items": {"type": "object", "properties": {
        "text": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}}}, "required": ["text", "facts"]}}},
    "required": ["bullets"],
}

Progress = Callable[[str], None]


def _known_names_block(campaign: Campaign) -> str:
    names = campaign.glossary
    return "Known names: " + (", ".join(names) if names else "(none loaded)")


def map_chunk(llm: LLM, profile: Profile, chunk: Chunk, campaign: Campaign, speaker_names: dict) -> dict:
    user = f"{_known_names_block(campaign)}\n\nTranscript {fmt_ts(chunk.start)}-{fmt_ts(chunk.end)}:\n{chunk.render(speaker_names)}"
    return llm.chat_json(MAP_SYSTEM, user, MAP_SCHEMA, max_tokens=profile.max_output_tokens, temperature=0.1)


def _valid_ids(ids: Any, allowed: set[str]) -> list[str]:
    if not isinstance(ids, list):
        return []
    return [i for i in dict.fromkeys(str(x).strip() for x in ids) if i in allowed]


def ground_map(raw: dict, chunk: Chunk, flagged: list[dict]) -> tuple[dict, list[dict], list[str], list[dict]]:
    """Validate one map result. Returns (scene, facts, off_topic_ids, unsure)."""
    allowed = {ln["id"] for ln in chunk.lines}
    facts = []
    for f in raw.get("facts") or []:
        text = str(f.get("text", "")).strip()
        if not text:
            continue
        lines = _valid_ids(f.get("lines"), allowed)
        if not lines:
            flagged.append({"text": text, "reason": "no supporting transcript lines", "chunk": chunk.index})
            continue
        kind = f.get("kind") if f.get("kind") in FACT_KINDS else "event"
        facts.append({
            "text": text, "kind": kind, "lines": lines,
            "npc_name": (f.get("npc_name") or "").strip() or None,
            "npc_status": (f.get("npc_status") or "").strip() or None,
            "dm_only": bool(f.get("dm_only")), "chunk": chunk.index,
        })
    off = _valid_ids(raw.get("off_topic_line_ids"), allowed)
    unsure = [{"text": str(u.get("text", "")).strip(), "lines": _valid_ids(u.get("lines"), allowed)}
              for u in raw.get("unsure") or [] if str(u.get("text", "")).strip()]
    scene = raw.get("scene") or {}
    return {"title": str(scene.get("title", "")).strip(), "location": str(scene.get("location", "")).strip()}, facts, off, unsure


def _group_scenes(scenes: list[dict]) -> list[list[int]]:
    """Merge consecutive chunks that share a location into one scene."""
    groups: list[list[int]] = []
    for i, s in enumerate(scenes):
        loc = s["location"].lower()
        if groups and loc and scenes[groups[-1][-1]]["location"].lower() == loc:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def _fact_line(fid: str, f: dict) -> str:
    return f"{fid} ({f['kind']}): {f['text']}"


def _pack(items: list[tuple[str, dict]], budget: int, system: str) -> list[list[tuple[str, dict]]]:
    budget -= estimate_tokens(system) + 200
    out: list[list[tuple[str, dict]]] = [[]]
    used = 0
    for fid, f in items:
        t = estimate_tokens(_fact_line(fid, f)) + 2
        if out[-1] and used + t > budget:
            out.append([])
            used = 0
        out[-1].append((fid, f))
        used += t
    return [g for g in out if g]


def _bullets(llm: LLM, profile: Profile, system: str, header: str, facts: list[tuple[str, dict]], flagged: list[dict], temperature: float) -> list[dict]:
    by_id = dict(facts)
    user = header + "\n\nFacts:\n" + "\n".join(_fact_line(fid, f) for fid, f in facts)
    raw = llm.chat_json(system, user, BULLETS_SCHEMA, max_tokens=profile.max_output_tokens, temperature=temperature)
    out = []
    for b in raw.get("bullets") or []:
        text = str(b.get("text", "")).strip()
        fids = _valid_ids(b.get("facts"), set(by_id))
        if not text:
            continue
        if not fids:
            flagged.append({"text": text, "reason": "bullet cites no extracted fact"})
            continue
        lines = sorted({ln for fid in fids for ln in by_id[fid]["lines"]})
        out.append({"text": text, "lines": lines, "dm_only": any(by_id[f]["dm_only"] for f in fids)})
    return out


def summarize(lines: list[dict], bookmarks: list[dict], speaker_names: dict, campaign: Campaign, llm: LLM, profile: Profile,
              progress: Progress = lambda s: None, map_cache: dict | None = None, save_map: Callable[[int, dict], None] | None = None) -> dict:
    """Run the whole map-reduce. `map_cache`/`save_map` let the worker persist
    each chunk's map output so a restart resumes instead of re-running hours
    of LLM work."""
    by_line = {ln["id"]: ln for ln in lines}
    flagged: list[dict] = []
    chunks = chunk_transcript(lines, bookmarks, profile.chunk_tokens, profile.chunk_minutes)
    scenes, all_facts, off_topic, unsure = [], [], [], []
    for c in chunks:
        progress(f"map {c.index + 1}/{len(chunks)}")
        cached = (map_cache or {}).get(c.index)
        # Cached map output is only reusable if the chunk still spans the
        # same lines (a re-transcription shifts every id).
        if cached and cached.get("first") == c.lines[0]["id"] and cached.get("last") == c.lines[-1]["id"]:
            raw = cached["raw"]
        else:
            raw = map_chunk(llm, profile, c, campaign, speaker_names)
            if save_map:
                save_map(c.index, {"first": c.lines[0]["id"], "last": c.lines[-1]["id"], "raw": raw})
        scene, facts, off, uns = ground_map(raw, c, flagged)
        scenes.append(scene)
        all_facts.extend(facts)
        off_topic.extend(off)
        unsure.extend(uns)

    fid = {id(f): f"F{i + 1}" for i, f in enumerate(all_facts)}
    notes: list[dict] = []
    for group in _group_scenes(scenes) if chunks else []:
        gfacts = [(fid[id(f)], f) for f in all_facts if f["chunk"] in group and f["kind"] not in ("thread",)]
        if not gfacts:
            continue
        title = next((scenes[i]["title"] for i in group if scenes[i]["title"]), "")
        loc = scenes[group[0]]["location"]
        for part in _pack(gfacts, profile.reduce_input_tokens, REDUCE_SYSTEM):
            progress(f"reduce scene {title or loc or group[0]}")
            notes.extend(_bullets(llm, profile, REDUCE_SYSTEM, f"{_known_names_block(campaign)}\nScene: {title} ({loc})", part, flagged, 0.4))

    thread_facts = [(fid[id(f)], f) for f in all_facts if f["kind"] == "thread"]
    threads: list[dict] = []
    if thread_facts:
        progress("reduce threads")
        try:
            for part in _pack(thread_facts, profile.reduce_input_tokens, THREAD_SYSTEM):
                threads.extend(_bullets(llm, profile, THREAD_SYSTEM, _known_names_block(campaign), part, flagged, 0.2))
        except ContextOverflow:
            threads = [{"text": f["text"], "lines": f["lines"], "dm_only": f["dm_only"]} for _, f in thread_facts]

    # NPC changes are merged in code: the newest statement of a status wins
    # (campaign rule), and no model call can blur whose status changed.
    npcs: dict[str, dict] = {}
    for f in all_facts:
        if f["kind"] != "npc" or not f["npc_name"]:
            continue
        key = f["npc_name"].lower()
        e = npcs.setdefault(key, {"name": f["npc_name"], "status": None, "changes": [], "lines": [], "dm_only": False})
        if f["npc_status"]:
            e["status"] = f["npc_status"]
        e["changes"].append(f["text"])
        e["lines"].extend(f["lines"])
        e["dm_only"] = e["dm_only"] or f["dm_only"]
    npc_items = []
    for e in npcs.values():
        text = f"**{e['name']}**" + (f" — {e['status']}" if e["status"] else "") + ": " + " ".join(e["changes"])
        npc_items.append({"text": text, "lines": sorted(set(e["lines"])), "dm_only": e["dm_only"], "npc": e["name"]})

    verify = [{"text": u["text"], "lines": u["lines"]} for u in unsure]

    def finish(items: list[dict], prefix: str) -> list[dict]:
        out = []
        for i, it in enumerate(items):
            text = it["text"]
            unknown = unknown_names(text, campaign)
            if unknown:
                text = ensure_tag(text, "INFERRED")
            if it.get("dm_only"):
                text = ensure_tag(text, "DM-ONLY")
            ls = [by_line[x] for x in it["lines"] if x in by_line]
            out.append({
                "id": f"{prefix}{i + 1}", "text": text, "lines": it["lines"],
                "start": min((l["start"] for l in ls), default=None), "end": max((l["end"] for l in ls), default=None),
                "unknown_names": unknown,
            })
        return out

    return {
        "created": time.time(),
        "model": getattr(llm, "model", "?"),
        "profile": profile.name,
        "chunks": len(chunks),
        "sections": {
            "prep": [],
            "session_notes": finish(notes, "N"),
            "npc_changes": finish(npc_items, "P"),
            "unresolved_threads": finish(threads, "T"),
            "verify": finish(verify, "V"),
        },
        "scenes": [{"chunk": i, **s, "start": chunks[i].start, "end": chunks[i].end} for i, s in enumerate(scenes)],
        "off_topic": sorted(set(off_topic)),
        "flagged": flagged,
    }
