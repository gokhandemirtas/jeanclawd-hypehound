from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pydantic_ai import Agent

from .inference import get_model
from .log import log, step
from .models import Digest, DigestEntry
from .store import create_digest, get_undigested_items, save_digest_items, update_digest_content

_DIGESTS_DIR = Path("digests")


def render_markdown(digest: Digest) -> str:
    lines: list[str] = [
        f"# HypeHound Digest",
        f"",
        f"**Period:** {digest.period_start.strftime('%-d %b %Y')} — {digest.period_end.strftime('%-d %b %Y')}",
        f"",
    ]
    for entry in digest.entries:
        lines.append(f"## {entry.category}")
        lines.append("")
        for item in entry.items:
            lines.append(f"### {item.headline}")
            lines.append("")
            lines.append(item.summary)
            if item.citations:
                lines.append("")
                for c in item.citations:
                    if c.url and c.title:
                        lines.append(f"- [{c.title}]({c.url})")
                    elif c.url:
                        lines.append(f"- <{c.url}>")
                    elif c.title:
                        lines.append(f"- {c.title}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_digest_markdown(digest: Digest) -> Path:
    _DIGESTS_DIR.mkdir(exist_ok=True)
    date_str = digest.period_end.strftime("%Y-%m-%d")
    version = 1
    while True:
        path = _DIGESTS_DIR / f"digest_{date_str}_v{version}.md"
        if not path.exists():
            break
        version += 1
    path.write_text(render_markdown(digest))
    return path

_agent: Agent[None, DigestEntry] = Agent(
    get_model(),
    output_type=DigestEntry,
    retries=2,
    system_prompt=(
        "You are a newsletter digest writer. Given news items for a single category, "
        "produce a clean digest section. Write a brief 1-2 sentence intro for the category, "
        "then include each item. Preserve all citation URLs."
    ),
)


async def generate_digest(period_start: datetime, period_end: datetime) -> Digest:
    with step("Fetch undigested items"):
        rows = get_undigested_items(since=period_start)
    if not rows:
        raise ValueError("No undigested items in period")

    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(dict(row))

    category_summary = ", ".join(
        f"{cat} ({len(items)})" for cat, items in sorted(by_category.items())
    )
    log.info(
        f"Digesting [bold]{len(rows)}[/bold] item(s) across "
        f"[bold]{len(by_category)}[/bold] categor(ies): [dim]{category_summary}[/dim]"
    )

    digest_id = create_digest(period_start, period_end)
    entries: list[DigestEntry] = []

    for cat, items in sorted(by_category.items()):
        items_text = "\n".join(f"- {item['headline']}: {item['summary']}" for item in items)
        prompt = (
            f"Category: {cat}\n"
            f"Period: {period_start.date()} to {period_end.date()}\n\n"
            f"{items_text}"
        )
        try:
            with step(f"LLM · {cat} ({len(items)} item(s))"):
                result = await _agent.run(prompt)
        except Exception as exc:
            log.error(f"[red]Failed to digest category {cat!r}: {type(exc).__name__}: {exc!s:.200}[/red]")
            continue

        entries.append(result.output)
        item_ids = [item["id"] for item in items]
        save_digest_items(digest_id, item_ids)

        usage = result.usage
        tok = f"  [dim]{usage.total_tokens} tok[/dim]" if usage.total_tokens else ""
        log.info(f"  {cat}: [green]{len(result.output.items)} item(s)[/green]{tok}")

        digest_so_far = Digest(period_start=period_start, period_end=period_end, entries=entries)
        update_digest_content(digest_id, digest_so_far.model_dump_json())

    if not entries:
        raise ValueError("All categories failed — no digest produced")

    result_digest = Digest(period_start=period_start, period_end=period_end, entries=entries)
    md_path = write_digest_markdown(result_digest)
    log.info(f"[dim]Digest saved → {md_path}[/dim]")
    return result_digest
