"""
State persistence via SQLite (seen.db).

Tables:
  seen           - URL hashes + date_sent, so articles are never resent
                   (purged after SEEN_RETENTION_DAYS)
  archive        - full record of every article ever delivered: date, topic,
                   title, source, url, summary, hashtags, embedding. Never
                   purged — the workflows commit seen.db back to the repo,
                   so this doubles as a permanent, searchable news archive.
  feedback       - 👍/👎 reactions per article, used to personalize ranking
  topics         - the active search topics, editable from Telegram
  telegram_state - last processed update_id for the on-demand poller
"""

import os
import json
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
                hashtags TEXT,
                url_hash TEXT,
                embedding TEXT
            )
        """)
        # Databases created before these columns existed migrate in place
        for column in ("url_hash TEXT", "embedding TEXT"):
            try:
                cursor.execute(f"ALTER TABLE archive ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # column already exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                url_hash TEXT NOT NULL,
                verdict TEXT NOT NULL,
                date TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                label TEXT PRIMARY KEY,
                query TEXT NOT NULL
            )
        """)
        # Seed topics from config defaults on first run only — after that,
        # Telegram /topics commands are the source of truth.
        if not cursor.execute("SELECT 1 FROM topics LIMIT 1").fetchone():
            cursor.executemany(
                "INSERT INTO topics (label, query) VALUES (?, ?)",
                list(config.TOPICS.items()),
            )
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


def url_short_hash(url: str) -> str:
    """16-char hash used in Telegram callback_data (which caps at 64 bytes)."""
    return _hash_url(url)[:16]


def archive_article(article: dict, topic: str) -> None:
    """Append a delivered article to the permanent archive."""
    embedding = article.get("_embedding")
    try:
        conn = _get_connection()
        conn.execute(
            "INSERT INTO archive (date_sent, topic, title, domain, url, summary, hashtags, url_hash, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                topic,
                article.get("title", ""),
                article.get("domain", ""),
                article.get("url", ""),
                article.get("summary", ""),
                " ".join(article.get("hashtags", [])),
                url_short_hash(article.get("url", "")),
                # 5 decimals is plenty for cosine math and ~2.5x smaller
                # than full-precision floats in this git-committed file
                json.dumps([round(x, 5) for x in embedding]) if embedding else None,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        # Archiving is best-effort: never let it interfere with delivery
        logger.error("Error archiving article: %s", e)


def get_archive_since(days: int) -> list[dict]:
    """Articles delivered in the last N days, oldest first."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    try:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT date_sent, topic, title, domain, summary FROM archive "
            "WHERE date_sent >= ? ORDER BY id",
            (cutoff,),
        ).fetchall()
        conn.close()
        return [
            {"date_sent": r[0], "topic": r[1], "title": r[2], "domain": r[3], "summary": r[4]}
            for r in rows
        ]
    except Exception as e:
        logger.error("Error reading archive: %s", e)
        return []


# ---- feedback (👍/👎 personalization) ----

def record_feedback(url_hash: str, verdict: str) -> None:
    """Store a thumbs up/down reaction for a delivered article.

    One row per article: re-taps replace instead of accumulate (a delayed
    toast makes people tap twice, and duplicates would silently over-weight
    that article in the preference centroid). Tapping the other thumb later
    changes your verdict."""
    if verdict not in ("up", "down"):
        return
    try:
        conn = _get_connection()
        conn.execute("DELETE FROM feedback WHERE url_hash = ?", (url_hash,))
        conn.execute(
            "INSERT INTO feedback (url_hash, verdict, date) VALUES (?, ?, ?)",
            (url_hash, verdict, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        conn.close()
        logger.info("Recorded feedback: %s on %s", verdict, url_hash)
    except Exception as e:
        logger.error("Error recording feedback: %s", e)


def get_feedback_embeddings() -> list[tuple[str, list[float], str]]:
    """(verdict, embedding, date) for every reaction whose article has a
    stored embedding — the training signal for personalized ranking. The
    date lets the ranking decay old reactions."""
    try:
        conn = _get_connection()
        rows = conn.execute(
            "SELECT f.verdict, a.embedding, f.date FROM feedback f "
            "JOIN archive a ON a.url_hash = f.url_hash "
            "WHERE a.embedding IS NOT NULL"
        ).fetchall()
        conn.close()
        return [(verdict, json.loads(emb), date) for verdict, emb, date in rows]
    except Exception as e:
        logger.error("Error reading feedback embeddings: %s", e)
        return []


# ---- topics (managed from Telegram) ----

def get_topics() -> dict[str, str]:
    """Active topics {label: query}. Falls back to config defaults if the
    table is unreadable, so a DB problem can't stop the digest."""
    try:
        conn = _get_connection()
        rows = conn.execute("SELECT label, query FROM topics ORDER BY label").fetchall()
        conn.close()
        return dict(rows) if rows else dict(config.TOPICS)
    except Exception as e:
        logger.error("Error reading topics: %s", e)
        return dict(config.TOPICS)


def add_topic(label: str, query: str) -> None:
    conn = _get_connection()
    conn.execute("INSERT OR REPLACE INTO topics (label, query) VALUES (?, ?)", (label, query))
    conn.commit()
    conn.close()


def remove_topic(label: str) -> bool:
    conn = _get_connection()
    cursor = conn.execute("DELETE FROM topics WHERE label = ?", (label,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


# ---- stats (/stats) ----

def get_stats() -> dict:
    """Aggregates over the archive and feedback tables."""
    month_cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
    try:
        conn = _get_connection()
        total = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        month = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE date_sent >= ?", (month_cutoff,)
        ).fetchone()[0]
        top_domains = conn.execute(
            "SELECT domain, COUNT(*) c FROM archive GROUP BY domain ORDER BY c DESC LIMIT 5"
        ).fetchall()
        top_topics = conn.execute(
            "SELECT topic, COUNT(*) c FROM archive GROUP BY topic ORDER BY c DESC LIMIT 5"
        ).fetchall()
        ups = conn.execute("SELECT COUNT(*) FROM feedback WHERE verdict='up'").fetchone()[0]
        downs = conn.execute("SELECT COUNT(*) FROM feedback WHERE verdict='down'").fetchone()[0]
        conn.close()
        return {
            "total": total, "last_30_days": month,
            "top_domains": top_domains, "top_topics": top_topics,
            "thumbs_up": ups, "thumbs_down": downs,
        }
    except Exception as e:
        logger.error("Error computing stats: %s", e)
        return {}


def purge_old_embeddings() -> None:
    """Null out archived embeddings older than the retention window.
    Feedback taps happen close to delivery, so old vectors add nothing to
    the preference centroid — but at several KB each in a git-committed
    file, they add plenty to repo history. Article text stays forever."""
    cutoff = (
        datetime.now() - timedelta(days=config.ARCHIVE_EMBEDDING_RETENTION_DAYS)
    ).strftime("%Y-%m-%d %H:%M")
    try:
        conn = _get_connection()
        cursor = conn.execute(
            "UPDATE archive SET embedding = NULL "
            "WHERE embedding IS NOT NULL AND date_sent < ?",
            (cutoff,),
        )
        conn.commit()
        conn.close()
        if cursor.rowcount:
            logger.info("Purged %d embeddings older than %s", cursor.rowcount, cutoff)
    except Exception as e:
        logger.error("Error purging old embeddings: %s", e)


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


def get_state(key: str, default: str = "") -> str:
    """Read a value from the generic state table (update ids, auto-push
    toggle, last auto-run timestamp...)."""
    try:
        conn = _get_connection()
        row = conn.execute(
            "SELECT value FROM telegram_state WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception as e:
        logger.error("Error reading state %s: %s", key, e)
        return default


def set_state(key: str, value: str) -> None:
    try:
        conn = _get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO telegram_state (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Error storing state %s: %s", key, e)


def get_last_telegram_update_id() -> int:
    try:
        return int(get_state("last_update_id", "0"))
    except ValueError:
        return 0


def set_last_telegram_update_id(update_id: int) -> None:
    set_state("last_update_id", str(update_id))

