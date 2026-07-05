"""
The digest pipeline, shared by both entry points (main.py for manual/scheduled
runs, poll_telegram.py for on-demand /news runs):

    search -> drop already-sent -> semantic dedup -> scrape
           -> select top N per topic -> summarize -> send

Order notes:
  - The seen-filter runs BEFORE scraping so we never spend HTTP requests on
    articles we already delivered.
  - Semantic dedup also runs pre-scrape, on title + snippet, so duplicate
    stories are dropped before we fetch them.
"""

import logging
from collections import defaultdict

import config
import search
import scrape
import semantic
import summarize
import telegram_sender
import seen

logger = logging.getLogger(__name__)


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


def run() -> bool:
    """Run the full digest pipeline. Returns True if any articles were sent."""
    logger.info("=== AI News Digest: pipeline starting ===")

    logger.info("Initializing deduplication database")
    seen.init_db()

    logger.info("Purging seen entries older than %d days", config.SEEN_RETENTION_DAYS)
    seen.purge_old_seen()

    logger.info("Step 1/5: searching DuckDuckGo for %d topics", len(config.TOPICS))
    candidates = search.gather_candidates()
    if not candidates:
        logger.error("No search candidates found — aborting run (nothing to send).")
        return False

    logger.info("Step 2/5: dropping already-sent articles")
    candidates = seen.filter_articles(candidates)
    if not candidates:
        logger.warning("All articles have been seen before — nothing new to send.")
        return False

    logger.info("Step 3/5: semantic dedup on %d candidates", len(candidates))
    candidates = semantic.dedupe(candidates)

    logger.info("Step 4/5: scraping %d candidate articles", len(candidates))
    enriched = scrape.fetch_all(candidates)

    logger.info("Selecting top %d articles per topic", config.ITEMS_PER_TOPIC)
    selected_by_topic = select_top_per_topic(enriched)
    total_selected = sum(len(v) for v in selected_by_topic.values())
    logger.info("Selected %d articles total", total_selected)

    if total_selected == 0:
        logger.error("No articles survived selection — aborting run.")
        return False

    logger.info("Step 5/5: summarizing with Gemini and sending to Telegram")
    for topic, items in selected_by_topic.items():
        selected_by_topic[topic] = summarize.summarize_all(items)

    actually_sent = telegram_sender.send_digest(selected_by_topic)

    logger.info("=== Pipeline complete: digest sent and state persisted ===")
    return bool(actually_sent)
