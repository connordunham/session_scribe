# Requirement traceability

Status key: **tested** = an automated test checks the acceptance criterion
(with the speech model and LLM faked); **built** = implemented but only
checkable on real hardware or real audio; **M0** = needs Connor's machine.

| ID | Status | Where / evidence |
|---|---|---|
| R1 offline | tested (with models faked); firewall test is M0/M3 | `test_live_recording_to_saved_log` runs under a non-loopback connection guard; `is_private_host`; worker sets `HF_HUB_OFFLINE` |
| R2 Ollama only | built | `scribe/llm.py`; host is config; model is a profile value |
| R3 segmented recorder | tested + driven in Chromium | `record.html`; `test_closed_tab_keeps_uploaded_audio_and_resumes`; browser run with 8 s rotation produced 3 contiguous segments |
| R4 import | tested | `test_import_splits_long_file_and_tracks` (21 min mp3 → 3 segments on one timeline) |
| R5 controls | built, driven in Chromium | pause/resume, meter, B shortcut, disk warning, wake lock; bookmark landed at 10.99 s for a press at ~11 s |
| R6 STT + VAD + glossary | built; accuracy is M0 | `scribe/transcribe.py`; `scribe accuracy` measures it |
| R7 resumable queue | tested | `test_killed_worker_resumes_from_last_finished_segment` |
| R8 speaker tracks | tested | multi-file import + rename in `test_import_splits_long_file_and_tracks` |
| R9 map-reduce fits | tested | `test_three_hour_transcript_fits_both_profiles` (27k words, both profiles) |
| R10 table talk | tested | `test_table_talk_left_out_of_log`; lines kept and marked in the transcript |
| R11 format | tested | `test_rendered_log_passes_template_check` |
| R12 citations | tested + driven | `test_grounding_drops_and_flags_uncited_facts`; clicking a citation in the browser highlights the lines and loads the audio |
| R13 approve + diff | tested + driven | stale-base 409, diff on every save, `log.md` absent until save |
| R14 tags | tested | `test_new_canon_is_tagged_and_secrets_marked` |
| R15 campaign loader | tested on a synthetic export | real Hub export not seen; see the README caveat |
| R16 profiles | tested | `test_profiles_load_and_switch`; overflow guard |
| R17 server-ready | tested (login), built (container) | `test_login_required_beyond_localhost`; Dockerfile/compose not yet built |
| R18 bake-off | built | `scribe bakeoff` |
| R19 readiness | tested | `test_readiness_is_red_without_dependencies`, `test_mic_test_round_trip` |
| R20 export | tested | log, player-safe log, transcript, notes |
| R21 delete audio | tested | log unchanged after deletion |
| R22, R23 | out of scope (M4) | |
