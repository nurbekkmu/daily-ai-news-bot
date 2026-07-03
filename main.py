"""
Entry point: runs the full daily pipeline.

    search -> scrape -> select top N per topic -> summarize -> send to Telegram

Run manually with:  python main.py
Runs automatically via .github/workflows/daily.yml on a daily cron.
"""

import logging
from collections import defaultdict

# Load environment variables FIRST — config.py reads os.environ at import time,
# so .env must be loaded before config (and anything importing it) is imported.
from dotenv import load_dotenv
load_dotenv()

import config
import search
import scrape
import summarize
import telegram_sender
import seen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def select_top_per_topic(articles: list[dict]) -> dict[str, list[dict]]:
    """
    Group scraped articles by topic and keep only the top
    config.ITEMS_PER_TOPIC per topic. Prefers trusted news outlets first,
    then 'scraped' content over 'snippet' fallback.
    """
    by_topic = defaultdict(list)
    for a in articles:
        by_topic[a["topic"]].append(a)

    selected = {}
    for topic, items in by_topic.items():
        items_sorted = sorted(
            items,
            key=lambda a: (
                0 if config.domain_matches(a["domain"], config.TRUSTED_DOMAINS) else 1,
                0 if a["content_source"] == "scraped" else 1,
            ),
        )
        selected[topic] = items_sorted[: config.ITEMS_PER_TOPIC]

    return selected


def run() -> None:
    logger.info("=== Daily AI News Digest: pipeline starting ===")

    logger.info("Initializing deduplication database")
    seen.init_db()

    logger.info("Purging seen entries older than %d days", config.SEEN_RETENTION_DAYS)
    seen.purge_old_seen()

    logger.info("Step 1/4: searching DuckDuckGo for %d topics", len(config.TOPICS))
    candidates = search.gather_candidates()
    if not candidates:
        logger.error("No search candidates found — aborting run (nothing to send).")
        return

    logger.info("Step 2/4: scraping %d candidate articles", len(candidates))
    enriched = scrape.fetch_all(candidates)

    logger.info("Step 2.5/4: filtering duplicate articles")
    enriched = seen.filter_articles(enriched)
    if not enriched:
        logger.error("All articles have been seen before — aborting run.")
        return

    logger.info("Selecting top %d articles per topic", config.ITEMS_PER_TOPIC)
    selected_by_topic = select_top_per_topic(enriched)
    total_selected = sum(len(v) for v in selected_by_topic.values())
    logger.info("Selected %d articles total", total_selected)

    if total_selected == 0:
        logger.error("No articles survived selection — aborting run.")
        return

    logger.info("Step 3/4: summarizing selected articles with Gemini")
    for topic, items in selected_by_topic.items():
        selected_by_topic[topic] = summarize.summarize_all(items)

    logger.info("Step 4/4: sending digest to Telegram (articles are marked seen as they send)")
    telegram_sender.send_digest(selected_by_topic)

    logger.info("=== Pipeline complete: digest sent and state persisted ===")



if __name__ == "__main__":
    run()
