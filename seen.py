"""
Deduplication via SQLite (seen.db).
Stores URL hashes (SHA256) and date_sent to avoid resending articles.
Provides filtering and purge functions for state persistence across runs.
"""

import os
import logging
import sqlite3
import hashlib
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)

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
        conn.commit()
        conn.close()
        logger.info("Initialized seen.db")
    except Exception as e:
        logger.error("Failed to initialize seen.db: %s", e)


def _hash_url(url: str) -> str:
    """Generate SHA256 hash of a URL."""
    return hashlib.sha256(url.encode()).hexdigest()


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

