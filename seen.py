"""
State persistence via SQLite (seen.db).

Three tables:
  seen           - URL hashes + date_sent, so articles are never resent
                   (purged after SEEN_RETENTION_DAYS)
  archive        - full record of every article ever delivered: date, topic,
                   title, source, url, summary, hashtags. Never purged —
                   the workflows commit seen.db back to the repo, so this
                   doubles as a permanent, searchable news archive.
  telegram_state - last processed update_id for the on-demand poller
"""

import os
import logging
import sqlite3
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode

import config

logger = logging.getLogger(__name__)

# Query parameters that identify a *visit*, not an article. The same story
# arrives with different utm_ tags depending on where the search engine found
# it, so they must not affect the dedup hash.
_TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref_", "cmpid")

# Anchor the DB next to this file so runs from any working directory share state
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.db")


def _get_connection():
    """Get a connection to the SQLite database."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """Initialize the seen.db tables if they don't exist."""
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                url_hash TEXT PRIMARY KEY,
                date_sent TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_sent TEXT NOT NULL,
                topic TEXT,
                title TEXT,
                domain TEXT,
                url TEXT,
                summary TEXT,
                hashtags TEXT
            )
        """)
        conn.commit()
        conn.close()
        logger.info("Initialized seen.db")
    except Exception as e:
        logger.error("Failed to initialize seen.db: %s", e)


def normalize_url(url: str) -> str:
    """Normalize a URL so trivial variants of the same article dedup together:
    lowercase scheme/host, http -> https, drop tracking params, drop fragment,
    drop trailing slash. 'HTTP://Site.com/a/?utm_source=x#top' and
    'https://site.com/a' hash the same."""
    try:
        parts = urlparse(url.strip())
        scheme = "https" if parts.scheme.lower() in ("http", "https") else parts.scheme.lower()
        host = parts.netloc.lower().removeprefix("www.")
        path = parts.path.rstrip("/")
        query_pairs = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
        ]
        query = urlencode(query_pairs)
        return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")
    except ValueError:
        return url.strip()


def _hash_url(url: str) -> str:
    """Generate SHA256 hash of a normalized URL."""
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


def is_seen(url: str) -> bool:
    """Check if a URL has been sent before."""
    url_hash = _hash_url(url)
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen WHERE url_hash = ?", (url_hash,))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    except Exception as e:
        logger.error("Error checking seen status for %s: %s", url, e)
        return False


def mark_seen(url: str) -> None:
    """Mark a URL as sent (insert its hash and today's date)."""
    url_hash = _hash_url(url)
    date_sent = datetime.now().strftime("%Y-%m-%d")
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO seen (url_hash, date_sent) VALUES (?, ?)",
            (url_hash, date_sent),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Error marking URL as seen: %s", e)


def archive_article(article: dict, topic: str) -> None:
    """Append a delivered article to the permanent archive."""
    try:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO archive (date_sent, topic, title, domain, url, summary, hashtags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                topic,
                article.get("title", ""),
                article.get("domain", ""),
                article.get("url", ""),
                article.get("summary", ""),
                " ".join(article.get("hashtags", [])),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Archiving is best-effort: never let it interfere with delivery
        logger.error("Error archiving article: %s", e)


def purge_old_seen() -> None:
    """Delete entries older than SEEN_RETENTION_DAYS from seen.db."""
    cutoff_date = (
        datetime.now() - timedelta(days=config.SEEN_RETENTION_DAYS)
    ).strftime("%Y-%m-%d")
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM seen WHERE date_sent < ?", (cutoff_date,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        logger.info(
            "Purged %d entries older than %s from seen.db",
            deleted,
            cutoff_date,
        )
    except Exception as e:
        logger.error("Error purging old entries from seen.db: %s", e)


def filter_articles(articles: list[dict]) -> list[dict]:
    """Filter out articles that have already been sent."""
    filtered = [a for a in articles if not is_seen(a["url"])]
    if len(filtered) < len(articles):
        logger.info(
            "Filtered %d duplicate article(s) from %d candidates",
            len(articles) - len(filtered),
            len(articles),
        )
    return filtered


def get_last_telegram_update_id() -> int:
    """Retrieve the last processed Telegram update_id from the database."""
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM telegram_state WHERE key = ?", ("last_update_id",))
        result = cursor.fetchone()
        conn.close()
        if result:
            return int(result[0])
        return 0
    except Exception as e:
        logger.error("Error retrieving last Telegram update_id: %s", e)
        return 0


def set_last_telegram_update_id(update_id: int) -> None:
    """Store the last processed Telegram update_id in the database."""
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO telegram_state (key, value) VALUES (?, ?)",
            ("last_update_id", str(update_id)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Error storing last Telegram update_id: %s", e)

