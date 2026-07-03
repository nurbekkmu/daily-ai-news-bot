"""
Searches DuckDuckGo for each configured topic and returns candidate
(title, url, snippet) results, deduped across topics.
"""

import time
import logging
from urllib.parse import urlparse

from ddgs import DDGS

import config

logger = logging.getLogger(__name__)


def _retry(fn, *args, **kwargs):
    """Small retry wrapper — replaces Prefect's retry decorator for a script this size."""
    last_err = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - we want to retry on anything and log it
            last_err = e
            logger.warning("Attempt %d/%d failed: %s", attempt, config.MAX_RETRIES, e)
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    raise last_err


def _search_topic(topic: str, query: str, max_results: int) -> list[dict]:
    """Run a single DDG search against the NEWS index (real, dated articles)
    and normalize results. Falls back to the general text index only if the
    news index returns nothing. Blocked (unreliable) domains are dropped."""

    def _do_news_search():
        with DDGS() as ddgs:
            return list(ddgs.news(query, max_results=max_results, timelimit="d"))

    def _do_text_search():
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results, timelimit="d"))

    raw_results = []
    try:
        raw_results = _retry(_do_news_search)
    except Exception as e:
        logger.warning("News search failed for topic %s: %s", topic, e)
    if not raw_results:
        logger.info("News index empty for topic %s — falling back to text search", topic)
        try:
            raw_results = _retry(_do_text_search)
        except Exception as e:
            logger.error("Search failed for topic %s after retries: %s", topic, e)
            return []

    results = []
    for r in raw_results:
        url = r.get("href") or r.get("url")
        title = (r.get("title") or "").strip()
        snippet = (r.get("body") or "").strip()
        if not url or not title:
            continue
        domain = urlparse(url).netloc.replace("www.", "")
        if config.domain_matches(domain, config.BLOCKED_DOMAINS):
            logger.info("Dropping blocked domain %s: %s", domain, url)
            continue
        results.append(
            {
                "topic": topic,
                "title": title,
                "url": url,
                "snippet": snippet,
                "domain": domain,
                "published": (r.get("date") or "").strip(),
            }
        )
    return results


def gather_candidates() -> list[dict]:
    """
    Search all topics from config.TOPICS, dedupe by URL, and return a flat list
    of candidate articles tagged with their topic.
    """
    seen_urls = set()
    all_candidates = []

    for topic, query in config.TOPICS.items():
        logger.info("Searching topic '%s' -> query: %s", topic, query)
        results = _search_topic(topic, query, config.RESULTS_PER_TOPIC)
        for r in results:
            if r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            all_candidates.append(r)

    logger.info("Gathered %d unique candidates across %d topics", len(all_candidates), len(config.TOPICS))
    return all_candidates


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for c in gather_candidates():
        print(f"[{c['topic']}] {c['title']} -> {c['url']}")
