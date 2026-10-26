import re

from pydantic_ai import Agent

from .config import settings
from .inference import get_model
from .log import log, step
from .models import Citation, ContentDensity, Digest, DigestEntry, NarratorMode, PodcastScript, PodcastSegment, ScriptLine

_DENSITY_SINGLE_GUIDE: dict[ContentDensity, str] = {
    ContentDensity.brief: "Each item gets 1–2 sentences: name it and state the single key fact.",
    ContentDensity.standard: "Each item gets 2–3 sentences: introduce it, explain what it means, add brief commentary.",
    ContentDensity.detailed: "Each item gets 4–6 sentences: introduce it, explain the context, discuss implications, add commentary.",
}

_DENSITY_DUAL_GUIDE: dict[ContentDensity, str] = {
    ContentDensity.brief: "Each speaker's turn should be 1–2 sentences. Stay tight.",
    ContentDensity.standard: "Each speaker's turn should be a full paragraph (3–6 sentences). No one-liners.",
    ContentDensity.detailed: "Each speaker's turn should be a rich paragraph (5–8 sentences) with context, analysis, and back-and-forth depth.",
}


def _single_category_system(density: ContentDensity) -> str:
    return f"""\
You are a tech podcast scriptwriter generating one section of a longer episode.

Rules:
- Write 1–3 natural spoken segments covering ALL items in the list provided
- Mark each segment with [SEGMENT] on its own line
- Plain spoken language — no markdown, no bullet points, no URLs read aloud
- Cover EVERY item — no skipping, no merging stories together
- {_DENSITY_SINGLE_GUIDE[density]}
- Mention source names (e.g. "according to TLDR") where it feels natural

Output — start immediately with [SEGMENT], no preamble.
"""


def _dual_category_system(density: ContentDensity = ContentDensity.standard) -> str:
    male = settings.tts_voice_male.capitalize()
    female = settings.tts_voice_female.capitalize()
    turn_guide = _DENSITY_DUAL_GUIDE[density]
    return f"""\
You are a tech podcast scriptwriter generating one section of a longer episode for two hosts: {male} (male) and {female} (female).

Rules:
- Write 1–3 segments covering ALL items in the list provided
- Mark each segment with [SEGMENT] on its own line
- Every spoken line must begin with [{male.upper()}]: or [{female.upper()}]:
- {turn_guide}
  Good: [{male.upper()}]: <sentences covering the topic fully>
        [{female.upper()}]: <sentences reacting and adding context>
  Bad:  [{male.upper()}]: Interesting. [{female.upper()}]: Yeah.
- They build on each other's paragraphs, not just acknowledge them
- Cover EVERY item — no skipping, no merging stories together
- Plain spoken language — no URLs read aloud

Output — start immediately with [SEGMENT], no preamble.
"""


def _make_agents(density: ContentDensity) -> tuple[Agent, Agent]:
    single: Agent[None, str] = Agent(
        get_model(),
        output_type=str,
        system_prompt=_single_category_system(density),
    )
    dual: Agent[None, str] = Agent(
        get_model(),
        output_type=str,
        system_prompt=_dual_category_system(density),
    )
    return single, dual


def _build_category_prompt(
    entry: DigestEntry,
    mode: NarratorMode,
    is_first: bool,
    is_last: bool,
) -> str:
    male = settings.tts_voice_male.capitalize()
    female = settings.tts_voice_female.capitalize()
    n = len(entry.items)

    lines = [f"Category: {entry.category} ({n} items — cover all {n})\n"]
    for item in entry.items:
        lines.append(f"- {item.headline}: {item.summary}")

    if is_first:
        if mode == NarratorMode.dual:
            lines.append(
                f"\nOpen this section with [{male.upper()}]: greeting that introduces both "
                f"hosts by name ({male} and {female}), names the show (HypeHound), and "
                f"previews the episode. Vary the wording — don't use a template."
            )
        else:
            lines.append(
                "\nOpen this section with a brief host greeting that names the show "
                "(HypeHound) and previews the episode."
            )

    if is_last:
        if mode == NarratorMode.dual:
            lines.append(
                f"\nClose this section with [{female.upper()}]: sign-off that thanks "
                f"listeners and teases next time."
            )
        else:
            lines.append(
                "\nClose this section with a brief outro that thanks listeners and "
                "teases next time."
            )

    return "\n".join(lines)


def _parse_segments(raw: str, mode: NarratorMode) -> list[PodcastSegment]:
    raw = re.sub(r"\[END\]", "", raw, flags=re.IGNORECASE).strip()
    parts = re.split(r"\[SEGMENT\]", raw, flags=re.IGNORECASE)
    segments: list[PodcastSegment] = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if mode == NarratorMode.dual:
            male = settings.tts_voice_male.upper()
            female = settings.tts_voice_female.upper()
            lines: list[ScriptLine] = []
            for raw_line in part.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                m = re.match(rf"\[({male}|{female})\]:\s*(.*)", raw_line, re.IGNORECASE)
                if m:
                    lines.append(ScriptLine(speaker=m.group(1).upper(), text=m.group(2).strip()))
                elif lines:
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
    density: ContentDensity = ContentDensity.standard,
) -> PodcastScript:
    single_agent, dual_agent = _make_agents(density)
    agent = dual_agent if mode == NarratorMode.dual else single_agent
    n_categories = len(digest.entries)
    all_segments: list[PodcastSegment] = []
    total_items = sum(len(e.items) for e in digest.entries)

    log.info(
        f"Generating {mode.value}-narrator script: "
        f"[bold]{n_categories}[/bold] categories, "
        f"[bold]{total_items}[/bold] items  "
        f"[dim]density={density.value}[/dim]"
    )

    for i, entry in enumerate(digest.entries):
        is_first = i == 0
        is_last = i == n_categories - 1
        prompt = _build_category_prompt(entry, mode, is_first, is_last)

        with step(
            f"Script [{i + 1}/{n_categories}] {entry.category} "
            f"({len(entry.items)} item{'s' if len(entry.items) != 1 else ''})",
            show_start=True,
        ):
            result = await agent.run(prompt)

        segs = _parse_segments(result.output, mode)
        all_segments.extend(segs)

    total_words = sum(
        sum(len(line.text.split()) for line in seg.lines)
        for seg in all_segments
    )
    log.info(
        f"Script: [bold]{len(all_segments)}[/bold] segment(s), "
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
        segments=all_segments,
        citations=_collect_citations(digest),
    )


if __name__ == "__main__":
    import argparse
    import asyncio
    from pathlib import Path

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
