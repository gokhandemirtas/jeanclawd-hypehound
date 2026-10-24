# HypeHound

AI-powered newsletter ingestor, classifier, and digest engine.

## What it does

HypeHound ingests newsletters delivered to a dedicated Resend-managed mailbox, then uses a local LLM to classify and summarize content into:

- **Daily digests** — concise summaries grouped by topic category
- **Podcast scripts** — 5–8 minute narrated scripts (single or dual host) ready for TTS synthesis

## Stack

- **Python 3.12** — managed with `uv`
- **Resend** — mailbox provider + SDK for email ingestion
- **PydanticAI** — structured LLM output for classification and digest generation
- **LM Studio** (default) — local OpenAI-compatible inference; swap via `INFERENCE_BASE_URL` + `INFERENCE_MODEL`
- **jeanclawd-voicebox** — TTS synthesis for podcast audio; swap via `VOICEBOX_URL`
- **Textual** — terminal UI
- **ffmpeg** — audio segment joining for podcast output

## Setup

```bash
uv sync
cp .env.example .env  # fill in Resend credentials and adjust inference/TTS endpoints
uv run python main.py
```

For podcast audio synthesis, start voicebox first:

```bash
cd ../jeanclawd-voicebox && uv run jeanclawd-voicebox
```

ffmpeg is also required: `brew install ffmpeg`

## Usage

### TUI

```bash
uv run python main.py
```

Keybindings: `i` ingest · `d` digest · `q` quit

### Headless (CLI / cron)

```bash
# ingest only
uv run python main.py --ingest

# digest only (last 24h by default)
uv run python main.py --digest

# digest + podcast script
uv run python main.py --podcast

# digest + podcast script + TTS audio
uv run python main.py --podcast --tts

# dual-host script, custom look-back window
uv run python main.py --podcast --mode dual --days 7 --tts

# re-synthesize audio from an existing script (skips LLM)
uv run python -m hypehound.podcast --from-script podcast/podcast.json --tts
```

Output files are written to `podcast/`:
- `podcast/podcast.json` — structured script
- `podcast/podcast.m4a` — synthesized audio (when `--tts` is passed)

Paths are overridable with `--out` and `--audio-out`.

## Architecture

```
Resend mailbox (cron poll)
  → ingest    fetch + dedup by Message-ID; resolve tracking URLs at ingest time
  → classify  structured LLM output; extract items with category tags and citations
  → digest    time-based accumulation; merge items across newsletters by category
  → podcast   narrated script (LLM) → TTS synthesis → M4A (optional)
```

Pipeline orchestration: `hypehound/pipeline.py`
State: SQLite via `hypehound/store.py`
Inference: `hypehound/inference.py` — swap provider via `.env`
