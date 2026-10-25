from bs4 import BeautifulSoup
from markdownify import markdownify


def html_to_text(html: str) -> str:
    return markdownify(html, heading_style="ATX", strip=["script", "style"])


def extract_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("http")]
