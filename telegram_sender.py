"""
Sends each article as an individual Telegram message: title + summary + source link + hashtags.
Includes delays between sends to avoid rate limiting.
Attaches an inline button for on-demand news refresh.
Marks each article as seen immediately after a successful send, so a crash
mid-digest never causes already-delivered articles to be resent.
"""

import logging
import time
import json

import requests

import config
import seen

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
SEND_DELAY_SECONDS = 2.5  # delay between article sends

# Telegram Markdown (V1) special characters that break parsing when unbalanced
_MARKDOWN_SPECIAL_CHARS = ("_", "*", "`", "[")


def _redact(text: str) -> str:
    """Strip the bot token from text before logging. Exception messages from
    requests include the full API URL (which embeds the token), and GitHub
    Actions logs on a public repo are visible to everyone."""
    if config.TELEGRAM_TOKEN:
        return text.replace(config.TELEGRAM_TOKEN, "***TOKEN***")
    return text


def _escape_markdown(text: str) -> str:
    """Escape Telegram Markdown (V1) special characters in dynamic text.
    Titles and summaries often contain *, _ or ` — unescaped, an unbalanced
    one makes Telegram reject the whole message with a 400."""
    for ch in _MARKDOWN_SPECIAL_CHARS:
        text = text.replace(ch, "\\" + ch)
    return text


def _get_news_button_markup() -> str:
    """Return JSON markup for inline keyboard with 'Get Latest News' button."""
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🔄 Get Latest News",
                    "callback_data": "news_command",
                }
            ]
        ]
    }
    return json.dumps(keyboard)


def _get_article_markup(article: dict) -> str:
    """Per-article keyboard: 👍/👎 feedback (drives personalized ranking)
    plus the refresh button. callback_data caps at 64 bytes, so articles are
    referenced by a 16-char URL hash that maps back through the archive."""
    h = seen.url_short_hash(article["url"])
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "👍", "callback_data": f"fb:up:{h}"},
                {"text": "👎", "callback_data": f"fb:down:{h}"},
            ],
            [
                {"text": "🔄 Get Latest News", "callback_data": "news_command"},
            ],
        ]
    }
    return json.dumps(keyboard)


def _format_article_message(article: dict, topic: str) -> str:
    """Format a single article as a Telegram message (Markdown)."""
    title = _escape_markdown(article["title"])
    url = article["url"]
    summary = _escape_markdown(article["summary"])
    domain = _escape_markdown(article.get("domain", "unknown"))
    hashtags = article.get("hashtags", [])

    lines = [
        f"*{topic}*",
        f"*{title}*",
        f"_{domain}_",
        "",
        summary,
    ]

    # Add hashtags if available
    if hashtags:
        lines.append("")
        lines.append(" ".join(hashtags))

    # Add source link at the end
    lines.append("")
    lines.append(f"[Read more]({url})")

    return "\n".join(lines)


def _send_message(text: str, reply_markup: str = None) -> None:
    """Send a single message to Telegram with retries.
    If Telegram rejects the Markdown formatting (400), the message is resent
    as plain text instead of failing the digest."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set (check .env / GitHub secrets).")

    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_TOKEN)
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text[: config.TELEGRAM_MAX_MESSAGE_LEN],
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }

    # Add inline keyboard if reply_markup is provided
    # reply_markup is a JSON string, so parse it back to dict
    if reply_markup:
        payload["reply_markup"] = json.loads(reply_markup)

    last_err = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 400 and "parse_mode" in payload:
                # Malformed Markdown (unbalanced entity etc.) — degrade to plain text
                logger.warning(
                    "Telegram rejected Markdown (%s) — resending as plain text",
                    resp.text[:200],
                )
                payload.pop("parse_mode")
                resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            logger.info("Sent article message to Telegram")
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(
                "Telegram send failed (attempt %d/%d): %s",
                attempt, config.MAX_RETRIES, _redact(str(e)),
            )
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)

    raise last_err


def send_notice(text: str) -> None:
    """Send a short service message (keeps the refresh button attached).
    Used so an on-demand request always gets SOME reply — silence after a
    button tap is indistinguishable from the bot being broken."""
    try:
        _send_message(text, reply_markup=_get_news_button_markup())
    except Exception as e:  # noqa: BLE001 - a notice is best-effort
        logger.error("Failed to send notice: %s", _redact(str(e)))


def send_digest(articles_by_topic: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Send each article as an individual message with delays between sends.
    Skips articles that failed to summarize (summarization_failed=True or summary=None).
    Each article is marked as seen in seen.db immediately after its send succeeds,
    and a send failure on one article is logged and skipped so it can't kill the
    rest of the digest. Returns the dict of articles that were actually sent."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set (check .env / GitHub secrets).")

    total_skipped = 0

    # Filter out articles flagged as unreliable or that failed to summarize
    filtered_articles = {}
    for topic, articles in articles_by_topic.items():
        valid_articles = []
        for article in articles:
            if article.get("unreliable"):
                logger.warning("Skipping article '%s' (not a real news article)", article["title"])
                # Mark seen so future runs don't waste a Gemini call re-checking it
                seen.mark_seen(article["url"])
                total_skipped += 1
            elif article.get("summarization_failed") or article.get("summary") is None:
                logger.warning("Skipping article '%s' (summarization failed)", article["title"])
                total_skipped += 1
            else:
                valid_articles.append(article)
        if valid_articles:
            filtered_articles[topic] = valid_articles

    if not filtered_articles:
        logger.warning("No articles to send (all failed to summarize)")
        return {}

    total_articles = sum(len(a) for a in filtered_articles.values())
    sent_count = 0
    failed_count = 0
    actually_sent = {}

    for topic, articles in filtered_articles.items():
        for article in articles:
            message = _format_article_message(article, topic)
            try:
                _send_message(message, reply_markup=_get_article_markup(article))
            except Exception as e:  # noqa: BLE001 - one bad article must not kill the digest
                failed_count += 1
                logger.error(
                    "Giving up on article '%s' after retries: %s",
                    article["title"], _redact(str(e)),
                )
            else:
                seen.mark_seen(article["url"])
                seen.archive_article(article, topic)
                actually_sent.setdefault(topic, []).append(article)
                sent_count += 1

            # Add delay between sends (except after the last article)
            if sent_count + failed_count < total_articles:
                logger.info("Waiting %.1f seconds before next article...", SEND_DELAY_SECONDS)
                time.sleep(SEND_DELAY_SECONDS)

    logger.info(
        "Digest done: %d sent, %d send failures, %d skipped (no summary)",
        sent_count,
        failed_count,
        total_skipped,
    )

    return actually_sent
