import argparse
import asyncio
import sys
from datetime import datetime, timedelta

from hypehound.store import init_db


def _parse_args():
    parser = argparse.ArgumentParser(
        prog="hypehound",
        description="HypeHound — newsletter ingestor & digest engine",
    )
    parser.add_argument("--ingest", action="store_true", help="Run ingest pipeline (headless)")
    parser.add_argument("--digest", action="store_true", help="Run digest pipeline (headless)")
    parser.add_argument("--podcast", action="store_true", help="Run digest + podcast script generation")
    parser.add_argument("--mode", choices=["single", "dual"], default="single", help="Podcast narrator mode")
    parser.add_argument("--tts", action="store_true", help="Synthesize audio after script generation")
    parser.add_argument("--out", default="podcast/podcast.json", help="Podcast script output path")
    parser.add_argument("--audio-out", default="podcast/podcast.m4a", help="Podcast audio output path")
    parser.add_argument("--days", type=int, default=1, help="Digest look-back window in days (default: 1)")
    return parser.parse_args()


async def _run_headless(args) -> None:
    from hypehound.models import NarratorMode
    from hypehound.pipeline import run_digest, run_ingest, run_podcast

    period_end = datetime.now()
    period_start = period_end - timedelta(days=args.days)

    if args.ingest:
        await run_ingest()

    if args.digest and not args.podcast:
        await run_digest(period_start, period_end)

    if args.podcast:
        await run_podcast(
            period_start=period_start,
            period_end=period_end,
            mode=NarratorMode(args.mode),
            tts=args.tts,
            json_path=args.out,
            audio_path=args.audio_out,
        )


if __name__ == "__main__":
    args = _parse_args()
    init_db()

    headless = args.ingest or args.digest or args.podcast
    if headless:
        asyncio.run(_run_headless(args))
        sys.exit(0)

    from hypehound.ui import launch
    launch()
