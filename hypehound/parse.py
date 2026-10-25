import asyncio

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify


def html_to_text(html: str) -> str:
    return markdownify(html, heading_style="ATX", strip=["script", "style"])


def extract_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("http")]


async def _resolve_one(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    try:
        response = await client.head(url)
        return url, str(response.url)
    except Exception:
        return url, url


async def resolve_urls(urls: list[str], timeout: float = 5.0) -> dict[str, str]:
    """Resolve all tracking URLs concurrently; return original → resolved mapping."""
    if not urls:
        return {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        pairs = await asyncio.gather(*(_resolve_one(client, u) for u in urls))
    return dict(pairs)
