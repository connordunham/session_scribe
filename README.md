# Session Scribe

A local-first web app for a Dungeon Master. It records or imports a whole D&D
session, transcribes it on your own machine with faster-whisper, and drafts a
session log in the campaign's format using Ollama. Every bullet in the draft
cites the transcript lines it came from. Nothing leaves the computer, and
nothing reaches your campaign files until you review the draft and save it.

**Status:** v1 code for milestones M1–M3 is written and tested with the speech
and language models faked. M0 (real audio, real models, on the real hardware)
has not been run yet; see [`docs/M0-checklist.md`](docs/M0-checklist.md).
Until it has, the default model choices are the spec's recommendations, not
measured results.

## How it fits together

```
browser (recorder, review)  ──HTTP──▶  scribe serve  (API + UI, never loads a model)
                                           │  files + app.db job queue
                                           ▼
                                       scribe worker (one GPU job at a time)
                                           ├─ faster-whisper (speech-to-text)
                                           └─ Ollama  (extractor + writer)
```

Everything lives in one folder, the **vault** (`$SCRIBE_VAULT`, default `./vault`):

| Path | What |
|---|---|
| `campaign/` | Markdown export of the Campaign Hub. The app only reads it. |
| `sessions/S32/` | Audio segments, `transcript.json`, `bookmarks.json`, `log_draft.json/.md`, and `log.md` (written only when you save) |
| `config/profiles.yaml` | Hardware profiles; `active:` switches between them |
| `config/glossary_extra.txt` | Optional extra names, `Name \| category` per line |
| `config/log_template.md` | Optional: your log layout (copy the default from `scribe/pipeline/logformat.py`) |
| `exports/` | Markdown exports |
| `app.db` | Job queue and settings |

## Install (do this while online)

**Windows, one step:** in PowerShell run

```powershell
Set-ExecutionPolicy -Scope Process Bypass
irm https://raw.githubusercontent.com/connordunham/session_scribe/main/install.ps1 | iex
```

That installs everything into `Documents\DND SCRIBE` and puts a **Session
Scribe** shortcut on the desktop. To pick another folder, download the
script and run `.\install.ps1 "D:\Scribe"`. The manual steps are
below.

Requirements: Python 3.10+, ffmpeg on PATH, [Ollama](https://ollama.com), and an
Nvidia GPU with a recent driver (Windows or Linux).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (Linux/macOS: source .venv/bin/activate)
pip install ".[stt]"
scribe setup                      # downloads the speech model(s) and runs `ollama pull` for the active profile
scribe setup --all-profiles       # also gets the 3090 profile's models
```

Then export the Campaign Hub to markdown into `vault/campaign/`.

## Verify readiness, then go offline

1. Start everything: `scribe run` (API and worker together). Open http://localhost:8765.
2. Under **Offline readiness**, click **Test recording**, then **Run check**.
3. **Turn Wi-Fi off and run the check again.** Go only when it shows green.
   On the command line, `scribe check` does the same and exits non-zero if
   anything is missing.

The worker sets `HF_HUB_OFFLINE=1`, so the speech library can't try to
download anything mid-session. The Ollama host has to be localhost or a
private-LAN address, and the readiness check enforces that.

## Use

- **Record:** click Record, pick the mic, and tick *call audio* for an online
  session (Chrome/Edge: choose the call's tab and tick *share audio*). Audio
  uploads every 5 seconds into roughly 10-minute segments. Press **B** or the
  Bookmark button at big moments. Stop when the session ends; transcription
  and the draft run in the background.
- **Import:** pick one file (mp3/wav/m4a/…) or one file per speaker. With one
  file per speaker, lines are labelled with that speaker's name.
- **If the tab closed or the laptop slept:** the home page shows the
  unfinished session. Everything uploaded is kept (at most the last ~5
  seconds are lost). You can resume recording on the same timeline, or
  finish and process what you have.
- **Review:** click any timestamp chip to highlight the supporting lines and
  hear them. Edit, add or remove bullets, then **Save…**. You see the diff
  first, and nothing is written until you confirm. Export the log (or a
  player-safe copy without `[DM-ONLY]` lines), the transcript, or the review
  notes.
- **Delete audio** after review to free disk space. The transcript and log are kept.

## Hardware profiles

`config/profiles.yaml` holds `test-8gb` (8 GB VRAM / 16 GB RAM) and
`target-24gb` (RTX 3090). Each sets the speech model, the LLM, its context
size and the chunk sizes. Switch with the dropdown on the home page, by
editing `active:`, or with `SCRIBE_PROFILE`. Restart the worker afterwards.
On load, the profile is rejected if its chunk size plus output size doesn't
fit the context window. At run time, any prompt that would overflow is
refused rather than silently truncated.

The model tags (`gemma4:12b`, `qwen3.8:27b`) come from the design spec. They
have **not** been checked here. Confirm each with `ollama pull`, then let the
bake-off pick:

```bash
scribe bakeoff S32 --models gemma4:12b,gemma4:e4b --minutes 30-45   # writes vault/bakeoff/S32_<time>.html
```

## Running on a home server

`compose.yaml` runs the API, the worker and Ollama, with the same code as the
laptop. Binding to anything other than localhost requires `SCRIBE_PASSWORD`
(the app refuses to start without it). Browsers only allow the microphone
over HTTPS away from localhost, so give it a certificate:

```bash
mkdir -p vault/certs && openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout vault/certs/scribe.key -out vault/certs/scribe.crt -subj "/CN=scribe.lan" \
  -addext "subjectAltName=DNS:scribe.lan,IP:192.168.1.50"
SCRIBE_PASSWORD=... docker compose up -d
docker compose run --rm worker scribe setup     # once, while online
```

Then trust `scribe.crt` on the laptop. You can also keep recording on the
laptop and upload the file to the server. The container setup has not been
built or run yet (M3 work).

## Decisions taken where the spec left a choice

- **Open question 1 (call platform / per-player tracks):** I went with the
  stated default. The browser records your mic plus the call's audio as one
  mixed stream, with no speaker labels. If the platform gives one file per
  player (for example a Discord recording bot), import them together: each
  file becomes a labelled track (R8), and you can rename a speaker once for
  the whole transcript.
- **Uploads every 5 seconds, not every 10 minutes:** R3 allows losing up to
  one segment. Uploading 5-second chunks into the current segment's file
  means a crash loses about 5 seconds.
- **Glossary spelling fix:** after transcription, a capitalised word in the
  middle of a sentence that closely matches a glossary name is replaced with
  that name ("Izalli" → "Izali"). The original wording is kept, and the
  review screen shows it on hover. The match threshold is my own guess and
  hasn't been tested on real audio; M0 measures it.
- **NPC changes are merged in code, not by the model:** the newest stated
  status wins, which follows the campaign's "newest entry wins" rule.
- **The log layout is a template** (`config/log_template.md`). The real Hub
  docs weren't available, so the default layout is a guess built from the
  headings the spec lists. The template check treats every `##` heading in
  the template as required, and accepts every `[TAG]` the campaign export
  itself uses.
- **The campaign parser is heuristic,** for the same reason. Check its
  output on the readiness line ("PCs: …") after each export, and add any
  missing names to `glossary_extra.txt`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests replace Whisper and Ollama with fakes. The fake LLM enforces the
same context limit as the real client, so the R9 overflow tests stay
meaningful. A guard fails any test that opens a connection to a
non-loopback address.
