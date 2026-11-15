"""Model bake-off (R18): one transcript excerpt, several installed models,
one side-by-side HTML page. No network beyond the configured Ollama host."""

from __future__ import annotations

import html
import json
import time
from dataclasses import replace
from pathlib import Path

from .campaign import Campaign
from .config import Settings
from .llm import OllamaLLM
from .pipeline.logformat import DEFAULT_TEMPLATE, render
from .pipeline.summarize import summarize


def run_bakeoff(s: Settings, lines: list[dict], bookmarks: list[dict], campaign: Campaign, models: list[str], out: Path,
                context: int | None = None) -> Path:
    results = []
    for model in models:
        llm = OllamaLLM(s.ollama_host, model, context or s.profile.llm_context, s.profile.llm_num_gpu)
        profile = replace(s.profile, llm_model=model, llm_context=context or s.profile.llm_context)
        t0 = time.time()
        try:
            d = summarize(lines, bookmarks, {}, campaign, llm, profile)
            md = render({"id": "EXCERPT", "number": 0, "date": "-"}, d["sections"], DEFAULT_TEMPLATE, cite_mode="inline")
            results.append({"model": model, "seconds": time.time() - t0, "markdown": md,
                            "flagged": len(d["flagged"]), "bullets": len(d["sections"]["session_notes"]), "error": None})
        except Exception as e:  # noqa: BLE001 - one broken model must not sink the comparison
            results.append({"model": model, "seconds": time.time() - t0, "markdown": "", "flagged": 0, "bullets": 0, "error": f"{type(e).__name__}: {e}"})
        finally:
            llm.unload()
    cols = "".join(
        f"<section><h2>{html.escape(r['model'])}</h2><p class=meta>{r['seconds']:.0f}s · {r['bullets']} bullets · "
        f"{r['flagged']} flagged (ungrounded, dropped)</p>"
        + (f"<p class=err>{html.escape(r['error'])}</p>" if r["error"] else f"<pre>{html.escape(r['markdown'])}</pre>")
        + "</section>" for r in results)
    span = f"{lines[0]['id']}–{lines[-1]['id']}" if lines else "empty"
    page = f"""<!doctype html><meta charset=utf-8><title>Bake-off</title>
<style>body{{font:14px system-ui;margin:16px;background:#fff;color:#222}}main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}}
section{{border:1px solid #ccc;border-radius:6px;padding:12px}}pre{{white-space:pre-wrap}}.meta{{color:#666}}.err{{color:#b00}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#ddd}}section{{border-color:#444}}}}</style>
<h1>Model bake-off</h1><p>Excerpt {html.escape(span)}, {len(lines)} lines, profile {html.escape(s.profile.name)}. Same prompts for every model.</p>
<main>{cols}</main><script type=application/json id=data>{html.escape(json.dumps(results))}</script>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out
