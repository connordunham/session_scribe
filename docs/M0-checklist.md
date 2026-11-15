# M0: riskiest slice, run on the real test machine

This has to run on Connor's hardware. The build environment has no GPU, no
microphone and no models. Write what you find to `docs/M0-findings.md`
(template below) **before** starting M1 tuning.

## Steps (1–2 days)

1. **Install while online:** follow the README, then run `scribe setup`.
   Check that each Ollama tag actually exists:
   `ollama pull gemma4:12b`, `ollama pull gemma4:e4b`.
2. **Capture a real call (risk 1):** start `scribe run`, record 20 minutes of
   a real online call with *call audio* ticked, and bookmark twice. Listen
   back on the review screen. Is the call audio there? Is it quiet or
   clipped? Then record the same call in the call app itself and import that
   file. Note which one sounds better.
3. **Capture at the table (risk 2):** record 20 minutes with one mic at the table.
4. **Glossary accuracy (R6, target ≥ 90%):**
   - `scribe transcribe-file sample.wav --out hyp.json` (default: glossary on, spelling fix on)
   - `scribe transcribe-file sample.wav --out hyp_raw.json --no-glossary --no-snap`
   - Correct one transcript by hand into `ref.txt`.
   - `scribe accuracy hyp.json ref.txt` and `scribe accuracy hyp_raw.json ref.txt`.
     This tells you whether the glossary and the spelling fix help, and whether 90% is reachable.
   - Watch for the spelling fix *wrongly* merging a new NPC into an existing name.
5. **Memory (risk 6):** while the worker transcribes and then summarizes,
   watch `nvidia-smi` and Task Manager. Note peak VRAM and RAM for each phase.
   If RAM runs out at 16k context, reduce `llm_context` in `profiles.yaml`.
6. **Token estimate:** the context guard estimates tokens from word counts.
   Compare one map call's estimate with Ollama's `prompt_eval_count`
   (`OLLAMA_DEBUG=1` or the server log). If Ollama's count is higher, raise
   the multiplier in `scribe/llm.py:estimate_tokens`.
7. **Bake-off (R18):**
   `scribe bakeoff S32 --models gemma4:12b,gemma4:e4b,<one more> --minutes 0-15`.
   Read the outputs side by side and pick the test-profile default with
   evidence. Pin it in `profiles.yaml`.
8. **Offline test (R1):** turn Wi-Fi off, block outbound traffic in the
   firewall, and process the 20-minute sample end to end. The firewall log
   should show zero outbound attempts.

## Findings template (`docs/M0-findings.md`)

```
Date / machine (GPU, VRAM, RAM, OS):
Call platform used, and capture result (browser tab audio vs call-app recording):
Table-mic capture result:
Glossary accuracy: raw __% | glossary __% | glossary+snap __%   (names that failed: …)
Wrong spelling fixes seen:
Peak VRAM/RAM: STT __ / __   LLM __ / __   at context __
Token estimate vs prompt_eval_count:
Bake-off: models, timings, notes; chosen default and why:
Offline run: pass/fail, firewall log notes:
Changes made to profiles.yaml:
```
