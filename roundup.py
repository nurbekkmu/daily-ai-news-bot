"""
/weekly — a synthesized week-in-review built from the archive.

The archive table records everything the bot ever delivered, so a roundup
needs no new searching or scraping: feed the last 7 days of summaries to
Gemini and ask for themes, not a list.
"""

import logging

import config
import gemini
import seen
import telegram_sender

logger = logging.getLogger(__name__)


def send_weekly() -> None:
    articles = seen.get_archive_since(days=7)
    if not articles:
        telegram_sender.send_notice(
            "The archive has nothing from the last 7 days — "
            "request some news first, then ask for a roundup."
        )
        return

    items = "\n\n".join(
        f"[{a['topic']}] {a['title']} ({a['domain']}, {a['date_sent']})\n{a['summary']}"
        for a in articles
    )
    logger.info("Weekly roundup over %d archived articles", len(articles))

    try:
        text = gemini.generate(config.WEEKLY_PROMPT_TEMPLATE.format(items=items))
    except Exception as e:  # noqa: BLE001
        logger.error("Weekly roundup generation failed: %s", e)
        telegram_sender.send_notice("Couldn't generate the weekly roundup — try again later.")
        return

    header = f"Your week in AI — {len(articles)} articles delivered:\n\n"
    telegram_sender.send_notice(header + text)
