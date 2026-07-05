"""
Poll-based on-demand news trigger via Telegram commands/buttons.

TRADEOFF: This uses polling (5-minute intervals) instead of a live webhook listener.
This means there's up to ~5 minutes of delay between when you send /news or tap
the button and when you receive the latest articles. The upside: no server needed,
stays serverless and free using GitHub Actions.

Flow:
  1. Check for new Telegram updates (getUpdates) since last processed update_id
  2. If update contains "/news" command or "news_command" callback_query, trigger run
  3. Run the shared pipeline (pipeline.py): search → dedup → scrape → summarize → send
  4. Update the stored update_id so the same command is never processed twice
  5. Exit cleanly if nothing new found
"""

# Load environment variables FIRST, before any other imports that read os.environ
from dotenv import load_dotenv
load_dotenv()

import logging
import requests

import config
import pipeline
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
    sent = pipeline.run()
    if not sent:
        # The request was heard but there's nothing to deliver — say so,
        # otherwise a button tap that finds no news looks like a dead bot.
        telegram_sender.send_notice(
            "Nothing new since the last digest — all current articles were "
            "already sent. Try again in a few hours."
        )


if __name__ == "__main__":
    poll()

