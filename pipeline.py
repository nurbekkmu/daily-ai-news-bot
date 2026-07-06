"""
The digest pipeline, shared by both entry points (main.py for manual/scheduled
runs, poll_telegram.py for on-demand /news runs):

    search + RSS -> drop already-sent -> semantic dedup -> personalize
                 -> scrape -> select top N per topic -> summarize -> send

Order notes:
  - The seen-filter runs BEFORE scraping so we never spend HTTP requests on
    articles we already delivered.
  - Semantic dedup also runs pre-scrape, on title + snippet, so duplicate
    stories are dropped before we fetch them. It attaches embeddings, which
    personalization then scores against past 👍/👎 feedback.
"""

import logging
from collections import defaultdict

import config
import personalize
import rss
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
                -a.get("_pref", 0.0),  # 👍/👎 history; 0 until feedback exists
                0 if a["content_source"] == "scraped" else 1,
            ),
        )
        selected[topic] = items_sorted[: config.ITEMS_PER_TOPIC]

    return selected


def cap_for_auto(selected_by_topic: dict[str, list[dict]], limit: int) -> dict[str, list[dict]]:
    """Auto-pushes are capped so a busy news morning can't dump a dozen
    messages at once: keep the best `limit` articles across all topics
    (trusted outlets first, then feedback preference)."""
    flat = [(topic, a) for topic, items in selected_by_topic.items() for a in items]
    if len(flat) <= limit:
        return selected_by_topic
    flat.sort(key=lambda ta: (
        0 if config.domain_matches(ta[1]["domain"], config.TRUSTED_DOMAINS) else 1,
        -ta[1].get("_pref", 0.0),
    ))
    capped: dict[str, list[dict]] = {}
    for topic, article in flat[:limit]:
        capped.setdefault(topic, []).append(article)
    return capped


def run(auto: bool = False) -> dict:
    """Run the full digest pipeline.

    auto=True is the scheduled push mode: article count is capped at
    config.AUTO_MAX_ARTICLES (best sources/preference first).

    Returns {"outcome": ..., "counts": {...}} where outcome is one of
    "sent", "no_candidates", "all_seen", "nothing_sent" — so callers can
    tell the user WHY a run produced nothing instead of guessing. The
    counts trace every stage: a run that ends empty is diagnosable from
    the Telegram message alone.
    """
    logger.info("=== AI News Digest: pipeline starting ===")
    # Config sanity up front: a missing GEMINI_API_KEYS secret fails every
    # summary with the same red herring; make it obvious in line one.
    logger.info("Gemini keys configured: %d", len(config.GEMINI_API_KEYS))
    counts = {"search": 0, "rss": 0, "unseen": 0, "deduped": 0, "selected": 0, "sent": 0}

    logger.info("Initializing deduplication database")
    seen.init_db()

    logger.info("Purging seen entries older than %d days", config.SEEN_RETENTION_DAYS)
    seen.purge_old_seen()
    seen.purge_old_embeddings()

    topics = seen.get_topics()
    logger.info("Step 1/5: gathering candidates (%d search topics + %d RSS feeds)",
                len(topics), len(config.RSS_FEEDS))
    candidates = search.gather_candidates(topics)
    counts["search"] = len(candidates)
    known_urls = {c["url"] for c in candidates}
    for c in rss.gather_candidates():
        if c["url"] not in known_urls:
            known_urls.add(c["url"])
            candidates.append(c)
            counts["rss"] += 1
    if not candidates:
        logger.error("No candidates from search OR feeds — likely blocked/stale sources.")
        return {"outcome": "no_candidates", "counts": counts}

    logger.info("Step 2/5: dropping already-sent articles")
    candidates = seen.filter_articles(candidates)
    counts["unseen"] = len(candidates)
    if not candidates:
        logger.warning("All articles have been seen before — nothing new to send.")
        return {"outcome": "all_seen", "counts": counts}

    logger.info("Step 3/5: semantic dedup on %d candidates", len(candidates))
    candidates = semantic.dedupe(candidates)
    counts["deduped"] = len(candidates)

    logger.info("Ranking with feedback history")
    personalize.attach_scores(candidates)

    logger.info("Step 4/5: scraping %d candidate articles", len(candidates))
    enriched = scrape.fetch_all(candidates)

    logger.info("Selecting top %d articles per topic", config.ITEMS_PER_TOPIC)
    selected_by_topic = select_top_per_topic(enriched)
    if auto:
        selected_by_topic = cap_for_auto(selected_by_topic, config.AUTO_MAX_ARTICLES)
    counts["selected"] = sum(len(v) for v in selected_by_topic.values())
    logger.info("Selected %d articles total", counts["selected"])

    logger.info("Step 5/5: summarizing with Gemini and sending to Telegram")
    for topic, items in selected_by_topic.items():
        selected_by_topic[topic] = summarize.summarize_all(items)

    actually_sent = telegram_sender.send_digest(selected_by_topic)
    counts["sent"] = sum(len(v) for v in actually_sent.values())

    logger.info("=== Pipeline complete: %s ===", counts)
    outcome = "sent" if counts["sent"] else "nothing_sent"
    return {"outcome": outcome, "counts": counts}
