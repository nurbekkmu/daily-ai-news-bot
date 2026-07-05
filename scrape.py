"""
Fetches full article text for candidate URLs. Falls back to the DuckDuckGo
snippet when scraping fails (paywall, Cloudflare block, timeout, etc.)
so the pipeline never hard-fails on a single bad source.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": config.USER_AGENT}


def _extract_text(html: str) -> str:
    """Pull main readable text out of a page. Simple heuristic: prefer <article>,
    fall back to all <p> tags, strip nav/script/style noise."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()

    article = soup.find("article")
    container = article if article else soup

    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    text = "\n".join(p for p in paragraphs if len(p) > 40)  # drop short junk lines
    return text.strip()


def fetch_article(candidate: dict) -> dict:
    """
    Attempt to scrape full article text for a single candidate dict
    (from search.py). Returns the candidate enriched with 'content' and
    'content_source' ('scraped' or 'snippet').
    """
    url = candidate["url"]
    try:
        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        resp.raise_for_status()
        # requests falls back to ISO-8859-1 when the server sends no charset
        # header, which garbles pages that are actually UTF-8. Trust the
        # header when present, otherwise use the encoding sniffed from content.
        if "charset" not in resp.headers.get("Content-Type", "").lower():
            resp.encoding = resp.apparent_encoding
        # Cap how much HTML we parse — some pages are megabytes of markup and
        # only MAX_ARTICLE_CHARS of extracted text is used anyway.
        text = _extract_text(resp.text[:500_000])

        if len(text) >= config.MIN_ARTICLE_CHARS:
            candidate["content"] = text[: config.MAX_ARTICLE_CHARS]
            candidate["content_source"] = "scraped"
            logger.info("Scraped OK (%d chars): %s", len(text), url)
            return candidate

        logger.warning("Scrape too short (%d chars), falling back to snippet: %s", len(text), url)

    except Exception as e:  # noqa: BLE001 - any scrape failure should fall back, not crash
        logger.warning("Scrape failed (%s), falling back to snippet: %s", e, url)

    # Fallback: use the search snippet so we still have *something* to summarize.
    candidate["content"] = candidate.get("snippet", "") or candidate["title"]
    candidate["content_source"] = "snippet"
    return candidate


def fetch_all(candidates: list[dict]) -> list[dict]:
    """Scrape all candidates in parallel (they're independent HTTP fetches,
    so sequential fetching just stacks up timeouts). Order is preserved."""
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=config.SCRAPE_MAX_WORKERS) as pool:
        return list(pool.map(fetch_article, candidates))


if __name__ == "__main__":
    import search

    logging.basicConfig(level=logging.INFO)
    cands = search.gather_candidates()
    for c in fetch_all(cands):
        print(f"[{c['topic']}] ({c['content_source']}) {c['title']} - {len(c['content'])} chars")
