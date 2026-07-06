"""
RSS/Atom feed sources, complementing DuckDuckGo search.

Two reasons these exist: primary sources (lab blogs, arXiv) publish before
news outlets write about them, and a second source keeps the digest alive
if DDG search ever breaks or blocks us.

Parsed with the stdlib ElementTree — the feeds we consume are plain RSS 2.0
or Atom, which doesn't justify a feedparser dependency. Every feed fails
open: one broken feed is logged and skipped, never fatal.
"""

import logging
import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"


def _strip_html(text: str) -> str:
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def _parse_date(raw: str):
    """Parse RFC 822 (RSS) or ISO 8601 (Atom) dates. None if unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_recent(raw_date: str) -> bool:
    """True if the entry is fresh enough — or undated (URL dedup and the
    seen-filter catch repeats, so unknown dates shouldn't discard entries)."""
    parsed = _parse_date(raw_date)
    if parsed is None:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.RSS_MAX_AGE_HOURS)
    return parsed >= cutoff


def parse_feed(xml_text: str) -> list[dict]:
    """Extract (title, url, snippet, published) entries from RSS 2.0 or Atom."""
    root = ET.fromstring(xml_text)
    entries = []

    # RSS 2.0: <channel><item>
    for item in root.iter("item"):
        entries.append({
            "title": (item.findtext("title") or "").strip(),
            "url": (item.findtext("link") or "").strip(),
            "snippet": _strip_html(item.findtext("description") or "")[:500],
            "published": (item.findtext("pubDate") or "").strip(),
        })

    # Atom: <feed><entry>
    for entry in root.iter(f"{_ATOM}entry"):
        link = ""
        for l in entry.findall(f"{_ATOM}link"):
            link = l.get("href", "")
            if l.get("rel", "alternate") == "alternate":
                break
        entries.append({
            "title": (entry.findtext(f"{_ATOM}title") or "").strip(),
            "url": link.strip(),
            "snippet": _strip_html(
                entry.findtext(f"{_ATOM}summary") or entry.findtext(f"{_ATOM}content") or ""
            )[:500],
            "published": (
                entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated") or ""
            ).strip(),
        })

    return [e for e in entries if e["title"] and e["url"]]


def gather_candidates() -> list[dict]:
    """Fetch all configured feeds and return candidates in the same shape
    search.py produces, tagged with the feed's topic label."""
    candidates = []
    for feed_url, topic in config.RSS_FEEDS.items():
        try:
            resp = requests.get(
                feed_url,
                headers={"User-Agent": config.USER_AGENT},
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            entries = parse_feed(resp.text)
        except Exception as e:  # noqa: BLE001 - a dead feed must not kill the digest
            logger.warning("Feed failed, skipping: %s (%s)", feed_url, e)
            continue

        kept = 0
        for e in entries:
            if kept >= config.RSS_MAX_ITEMS_PER_FEED:
                break
            if not _is_recent(e["published"]):
                continue
            domain = urlparse(e["url"]).netloc.replace("www.", "")
            if config.domain_matches(domain, config.BLOCKED_DOMAINS):
                continue
            candidates.append({
                "topic": topic,
                "title": e["title"],
                "url": e["url"],
                "snippet": e["snippet"],
                "domain": domain,
                "published": e["published"],
            })
            kept += 1

        logger.info("Feed %s: %d entries kept", feed_url, kept)

    logger.info("RSS: %d candidates from %d feeds", len(candidates), len(config.RSS_FEEDS))
    return candidates
