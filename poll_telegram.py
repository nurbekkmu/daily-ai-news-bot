"""
Poll-based on-demand news trigger via Telegram commands/buttons.

TRADEOFF: This uses polling (5-minute intervals) instead of a live webhook listener.
This means there's up to ~5 minutes of delay between when you send /news or tap
the button and when you receive the latest articles. The upside: no server needed,
stays serverless and free using GitHub Actions.

Flow:
  1. Check for new Telegram updates (getUpdates) since last processed update_id
  2. If update contains "/news" command or "news_command" callback_query, trigger run
  3. Run the full pipeline: search → scrape → dedup → summarize → send
  4. Update the stored update_id so the same command is never processed twice
  5. Exit cleanly if nothing new found
"""

# Load environment variables FIRST, before any other imports that read os.environ
from dotenv import load_dotenv
load_dotenv()

import logging
import requests
from collections import defaultdict

import config
import search
import scrape
import summarize
import seen
import telegram_sender


logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


def _get_updates(offset: int = None) -> list[dict]:
    """Fetch new Telegram updates (messages, callback queries)."""
    if not config.TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set")
    
    url = f"{TELEGRAM_API_BASE.format(token=config.TELEGRAM_TOKEN)}/getUpdates"
    params = {"timeout": 5}  # short poll timeout
    if offset:
        params["offset"] = offset
    
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if result.get("ok"):
            return result.get("result", [])
        logger.error("Telegram API error: %s", result.get("description"))
        return []
    except Exception as e:
        logger.error("Error fetching Telegram updates: %s", telegram_sender._redact(str(e)))
        return []


def _answer_callback_query(callback_query_id: str, text: str = "Latest news requested!") -> None:
    """Answer a callback_query to dismiss the button loading state."""
    if not config.TELEGRAM_TOKEN:
        return
    
    url = f"{TELEGRAM_API_BASE.format(token=config.TELEGRAM_TOKEN)}/answerCallbackQuery"
    payload = {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": False,
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=config.REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        logger.info("Answered callback query")
    except Exception as e:
        logger.warning("Error answering callback query: %s", telegram_sender._redact(str(e)))


def _select_top_per_topic(articles: list[dict]) -> dict[str, list[dict]]:
    """Group articles by topic and keep top N per topic — trusted news
    outlets first, then scraped content over snippet fallback."""
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


def run_on_demand_pipeline() -> bool:
    """Run the full pipeline on-demand. Returns True if articles were sent."""
    logger.info("=== On-demand pipeline triggered ===")
    
    logger.info("Step 1/4: searching DuckDuckGo for %d topics", len(config.TOPICS))
    candidates = search.gather_candidates()
    if not candidates:
        logger.error("No search candidates found — aborting run.")
        return False

    logger.info("Step 2/4: scraping %d candidate articles", len(candidates))
    enriched = scrape.fetch_all(candidates)

    logger.info("Step 2.5/4: filtering duplicate articles")
    enriched = seen.filter_articles(enriched)
    if not enriched:
        logger.warning("All articles have been seen before — nothing new to send.")
        return False
    
    logger.info("Selecting top %d articles per topic", config.ITEMS_PER_TOPIC)
    selected_by_topic = _select_top_per_topic(enriched)
    total_selected = sum(len(v) for v in selected_by_topic.values())
    logger.info("Selected %d articles total", total_selected)

    if total_selected == 0:
        logger.error("No articles survived selection — aborting run.")
        return False

    logger.info("Step 3/4: summarizing selected articles with Gemini")
    for topic, items in selected_by_topic.items():
        selected_by_topic[topic] = summarize.summarize_all(items)

    logger.info("Step 4/4: sending digest to Telegram (articles are marked seen as they send)")
    actually_sent = telegram_sender.send_digest(selected_by_topic)

    logger.info("=== On-demand pipeline complete ===")
    return bool(actually_sent)


def poll():
    """Poll for new Telegram updates and trigger pipeline if /news command detected."""
    logger.info("Polling for new Telegram updates...")
    
    # Initialize DB
    seen.init_db()
    
    # Get last processed update_id
    last_update_id = seen.get_last_telegram_update_id()
    logger.info("Last processed update_id: %d", last_update_id)
    
    # Fetch new updates
    updates = _get_updates(offset=last_update_id + 1 if last_update_id > 0 else None)
    
    if not updates:
        logger.info("No new updates found.")
        return
    
    logger.info("Found %d new update(s)", len(updates))

    # Persist the newest update_id IMMEDIATELY, before doing anything else.
    # If the pipeline below crashes, the same /news command must not be
    # re-processed on every 5-minute poll forever.
    newest_update_id = max(u.get("update_id", 0) for u in updates)
    if newest_update_id:
        seen.set_last_telegram_update_id(newest_update_id)

    found_trigger = False
    callback_query_id = None

    owner_chat_id = str(config.TELEGRAM_CHAT_ID)

    for update in updates:
        update_id = update.get("update_id")

        # Check for "/news" text command (only from the owner's personal chat)
        message = update.get("message", {})
        if message:
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = message.get("text", "")
            if text.strip() == "/news":
                if chat_id == owner_chat_id:
                    logger.info("Detected /news command (update_id=%d)", update_id)
                    found_trigger = True
                    break
                logger.warning(
                    "Ignoring /news from non-owner chat %s (update_id=%d)", chat_id, update_id
                )

        # Check for button callback (callback_data="news_command", only from the owner's chat)
        callback = update.get("callback_query", {})
        if callback:
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            callback_data = callback.get("data", "")
            if callback_data == "news_command":
                if chat_id == owner_chat_id:
                    logger.info("Detected news button tap (update_id=%d)", update_id)
                    callback_query_id = callback.get("id")
                    found_trigger = True
                    break
                logger.warning(
                    "Ignoring button tap from non-owner chat %s (update_id=%d)", chat_id, update_id
                )

    if not found_trigger:
        logger.info("No /news command or button tap found in updates.")
        return

    # Answer the callback query if it was a button tap (dismiss loading state)
    if callback_query_id:
        _answer_callback_query(callback_query_id, "Getting latest news...")

    # Run the pipeline (update_id was already persisted above)
    run_on_demand_pipeline()


if __name__ == "__main__":
    poll()

