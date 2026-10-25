import re

from pydantic_ai import Agent

from .config import settings
from .inference import get_model
from .log import log, step
from .models import Citation, Digest, NarratorMode, PodcastScript, PodcastSegment, ScriptLine

_SINGLE_SYSTEM = """\
You are a tech podcast scriptwriter. Given a newsletter digest, write a natural spoken monologue.

Rules:
- Total length: 12–18 minutes (approx 1800–2600 words)
- Divide into self-contained 2–3 minute segments (300–450 words each) using [SEGMENT] on its own line
- Plain spoken language — no markdown, no bullet points, no URLs read aloud
- Brief intro segment, body segments per topic cluster, short outro segment
- Cover every item from the digest — do not skip or merge stories
- For each item: introduce it, explain what it means and why it matters, add your own commentary or context
- Mention source names (e.g. "according to TLDR") where it feels natural
- Do not compress — if the digest has 20 items, all 20 should be discussed

Output — start immediately with [SEGMENT], no preamble:
[SEGMENT]
<segment text>

[SEGMENT]
<next segment>
"""

_DUAL_SYSTEM = """\
You are a tech podcast scriptwriter for two hosts: Alex (male) and Sara (female).

Rules:
- Total length: 12–18 minutes (approx 1800–2600 words)
- Divide into self-contained 2–3 minute segments using [SEGMENT] on its own line
- Every spoken line must begin with [ALEX]: or [SARA]:
- Each speaker's turn should be a full paragraph (3–6 sentences). Avoid one-liners.
  Good: [ALEX]: <three sentences covering the topic fully>
        [SARA]: <three sentences reacting and adding context>
  Bad:  [ALEX]: Interesting. [SARA]: Yeah. [ALEX]: So anyway...
- They build on each other's paragraphs, not just acknowledge them
- Cover every item from the digest — do not skip or merge stories
- For each item: introduce it, explain what it means, and add commentary or reaction
- Plain spoken language — no URLs read aloud
- Alex opens every show with a greeting that introduces both hosts by name and sets up the episode.
  Vary the wording each time but always include: the show name (HypeHound), Alex introducing himself,
  mentioning Vivian as co-host, and a one-line preview of what's in the episode.
  Example variations (do not copy verbatim):
    "Welcome to HypeHound, I'm Alex and I have Vivian with me today. Here's what's been happening..."
    "Hey, welcome back to HypeHound. I'm Alex, joined as always by Vivian. A packed episode today..."
    "This is HypeHound, I'm Alex. Vivian's here too — let's get into it. Lots to cover this week..."
- Sara closes with a sign-off that thanks the listener and teases next time

Output — start immediately with [SEGMENT], no preamble:
[SEGMENT]
[ALEX]: <greeting introducing both hosts and previewing the episode>
[SARA]: ...

[SEGMENT]
...
"""

_single_agent: Agent[None, str] = Agent(
    get_model(),
    output_type=str,
    system_prompt=_SINGLE_SYSTEM,
)
_dual_agent: Agent[None, str] = Agent(
    get_model(),
    output_type=str,
    system_prompt=_DUAL_SYSTEM,
)


def _parse_segments(raw: str, mode: NarratorMode) -> list[PodcastSegment]:
    raw = re.sub(r"\[END\]", "", raw, flags=re.IGNORECASE).strip()
    parts = re.split(r"\[SEGMENT\]", raw, flags=re.IGNORECASE)
    segments: list[PodcastSegment] = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if mode == NarratorMode.dual:
            lines: list[ScriptLine] = []
            for raw_line in part.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                m = re.match(r"\[(ALEX|SARA)\]:\s*(.*)", raw_line, re.IGNORECASE)
                if m:
                    lines.append(ScriptLine(speaker=m.group(1).upper(), text=m.group(2).strip()))
                elif lines:
                    # continuation line — append to previous speaker's text
                    prev = lines[-1]
                    lines[-1] = ScriptLine(speaker=prev.speaker, text=prev.text + " " + raw_line)
            if lines:
                segments.append(PodcastSegment(lines=lines))
        else:
            text = " ".join(ln.strip() for ln in part.splitlines() if ln.strip())
            if text:
                segments.append(PodcastSegment(lines=[ScriptLine(speaker=settings.tts_voice_single, text=text)]))

    return segments


def _collect_citations(digest: Digest) -> list[Citation]:
    seen: set[str] = set()
    unique: list[Citation] = []
    for entry in digest.entries:
        for item in entry.items:
            for c in item.citations:
                key = c.url or c.title or ""
                if key and key not in seen:
                    seen.add(key)
                    unique.append(c)
    return unique


async def generate_podcast_script(
    digest: Digest,
    mode: NarratorMode = NarratorMode.single,
) -> PodcastScript:
    digest_text = "\n\n".join(
        f"## {entry.category}\n" + "\n".join(
            f"- {item.headline}: {item.summary}" for item in entry.items
        )
        for entry in digest.entries
    )

    agent = _dual_agent if mode == NarratorMode.dual else _single_agent

    with step(f"Generate {mode.value}-narrator podcast script"):
        result = await agent.run(digest_text)

    raw = result.output
    segments = _parse_segments(raw, mode)

    total_words = sum(
        sum(len(line.text.split()) for line in seg.lines)
        for seg in segments
    )
    log.info(
        f"Script: [bold]{len(segments)}[/bold] segment(s), "
        f"~[bold]{total_words}[/bold] words "
        f"(≈{total_words // 130} min)"
    )

    title = (
        f"HypeHound — {digest.period_start.strftime('%-d %b')} "
        f"to {digest.period_end.strftime('%-d %b %Y')}"
    )

    return PodcastScript(
        title=title,
        mode=mode,
        segments=segments,
        citations=_collect_citations(digest),
    )


if __name__ == "__main__":
    import argparse
    import asyncio
    import json
    from pathlib import Path

    from .store import get_latest_digest_content

    parser = argparse.ArgumentParser(description="Generate a podcast script from the latest digest")
    parser.add_argument("--mode", choices=["single", "dual"], default="single")
    parser.add_argument("--out", default="podcast.json", help="Save script JSON to this path")
    parser.add_argument("--tts", action="store_true", help="Synthesize audio via voicebox")
    parser.add_argument("--audio-out", default="podcast.m4a")
    parser.add_argument("--from-script", metavar="FILE", help="Skip LLM — synthesize from existing script JSON")
    args = parser.parse_args()

    async def _run():
        if args.from_script:
            script = PodcastScript.model_validate_json(Path(args.from_script).read_text())
            print(f"Loaded script: {script.title}  ({len(script.segments)} segments, {script.mode.value} narrator)")
        else:
            from .store import get_latest_digest_content
            content = get_latest_digest_content()
            if not content:
                print("No digest found in database. Run a digest first.")
                raise SystemExit(1)
            digest = Digest.model_validate_json(content)
            mode = NarratorMode(args.mode)
            script = await generate_podcast_script(digest, mode)
            out = Path(args.out)
            out.write_text(script.model_dump_json(indent=2))
            print(f"Script written to {out}  ({len(script.segments)} segments)")
            for i, seg in enumerate(script.segments, 1):
                words = sum(len(l.text.split()) for l in seg.lines)
                print(f"  Segment {i}: {words} words")

        if args.tts or args.from_script:
            from .tts import synthesize_script
            await synthesize_script(script, Path(args.audio_out))

    asyncio.run(_run())
