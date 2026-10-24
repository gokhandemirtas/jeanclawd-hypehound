import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify


def html_to_text(html: str) -> str:
    return markdownify(html, heading_style="ATX", strip=["script", "style"])


def extract_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("http")]


def resolve_url(url: str, timeout: float = 5.0) -> str:
    """Follow redirects to get the real URL behind a tracking link."""
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            response = client.head(url)
            return str(response.url)
    except Exception:
        return url


def resolve_urls(urls: list[str]) -> dict[str, str]:
    """Return mapping of original → resolved URL."""
    return {url: resolve_url(url) for url in urls}
