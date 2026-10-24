import re
from datetime import datetime
from pydantic_ai import Agent

from .inference import get_model
from .log import log, step
from .models import Citation, NewsletterItem

_agent: Agent[None, list[NewsletterItem]] = Agent(
    get_model(),
    output_type=list[NewsletterItem],
    retries=2,
    system_prompt=(
        "You are a newsletter analyst. Given a section of newsletter text and its resolved URLs, "
        "extract individual news items. For each item provide: a category (e.g. AI, DevTools, "
        "Security, Web, Industry), a one-line headline, a 2-3 sentence summary, and citations "
        "linking to the original source URLs. Only include URLs from the provided resolved_urls map. "
        "Return an empty list if the section contains no distinct news items."
    ),
)

_CHUNK_WORDS = 600


def _split_chunks(text: str) -> list[str]:
    """Split newsletter text at section boundaries, capped at ~_CHUNK_WORDS words per chunk."""
    # Split on markdown headers or lines of dashes/equals (common in newsletters)
    boundary = re.compile(r"\n(?=#{1,3}\s|\s*[-=]{4,}\s*\n)")
    sections = [s.strip() for s in boundary.split(text) if s.strip()]

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0

    for section in sections:
        words = len(section.split())
        if current_words + words > _CHUNK_WORDS and current:
            chunks.append("\n\n".join(current))
            current = [section]
            current_words = words
        else:
            current.append(section)
            current_words += words

    if current:
        chunks.append("\n\n".join(current))

    return chunks or [text]


def _display_name(sender: str) -> str:
    if "<" in sender:
        return sender[:sender.index("<")].strip().strip('"\'')
    return sender.strip()


async def classify(
    text: str,
    resolved_urls: dict[str, str],
    sender: str = "",
    received_at: datetime | None = None,
) -> list[NewsletterItem]:
    chunks = _split_chunks(text)

    log.info(f"Classify: [bold]{len(chunks)}[/bold] chunk(s), ~[bold]{len(text.split())}[/bold] words total")

    all_items: list[NewsletterItem] = []
    failed = 0
    for i, chunk in enumerate(chunks, 1):
        preview = chunk[:60].replace("\n", " ")
        chunk_urls = {orig: res for orig, res in resolved_urls.items() if orig in chunk}
        url_context = "\n".join(f"- {orig} → {res}" for orig, res in chunk_urls.items())
        try:
            with step(f"LLM · chunk {i}/{len(chunks)}  [dim]{preview}…[/dim]"):
                result = await _agent.run(
                    f"Newsletter section:\n\n{chunk}\n\nResolved URLs:\n{url_context}"
                )
                items = result.output
                usage = result.usage()
                tok = f"[dim]{usage.total_tokens} tok[/dim]" if usage.total_tokens else ""
                log.info(f"  chunk {i}/{len(chunks)}: [green]{len(items)} item(s)[/green] extracted  {tok}")
                all_items.extend(items)
        except Exception as exc:
            log.warning(f"[yellow]  chunk {i}/{len(chunks)} failed: {exc} — skipping[/yellow]")
            failed += 1

    if failed == len(chunks):
        raise RuntimeError(f"All {len(chunks)} chunk(s) failed — LLM may be unavailable")

    # Deduplicate by headline (same headline from overlapping chunks)
    seen: set[str] = set()
    unique: list[NewsletterItem] = []
    for item in all_items:
        key = item.headline.lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    if len(unique) < len(all_items):
        log.debug(f"Deduped {len(all_items) - len(unique)} duplicate item(s)")

    source = _display_name(sender) if sender else "newsletter"
    date_str = received_at.strftime("%-d %b %Y") if received_at else ""
    fallback_title = f"Extracted from {source}" + (f" received on {date_str}" if date_str else "")
    fallback = Citation(title=fallback_title)
    for item in unique:
        if not item.citations:
            item.citations = [fallback]

    return unique
