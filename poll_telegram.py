"""
Poll-based on-demand trigger via Telegram commands/buttons.

TRADEOFF: This uses polling (5-minute intervals) instead of a live webhook
listener, so there's up to ~5 minutes (in practice 5-20, GitHub cron is not
punctual) between a command and its response. The upside: no server, stays
serverless and free on GitHub Actions. For instant delivery there's an
optional webhook mode — see webhook/README.md — which forwards Telegram
updates through a Cloudflare Worker to a repository_dispatch event; this
script then runs in dispatch mode and skips getUpdates entirely.

Commands (owner's chat only):
  /news              run the digest pipeline now
  /weekly            synthesized roundup of the last 7 days from the archive
  /stats             delivery + feedback statistics
  /topics            list active search topics
  /topics add X      add a topic (label = query = X)
  /topics remove X   remove a topic
  👍/👎 buttons       record feedback that personalizes future ranking
"""

# Load environment variables FIRST, before any other imports that read os.environ
from dotenv import load_dotenv
load_dotenv()

import json
import logging
import os

import requests

import config
import pipeline
import roundup
import seen
import telegram_sender


logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

HELP_TEXT = (
    "Commands:\n"
    "  /news — get the latest digest\n"
    "  /weekly — roundup of the last 7 days\n"
    "  /stats — delivery and feedback statistics\n"
    "  /topics — list topics; /topics add <name>, /topics remove <name>\n\n"
    "Replies can take a few minutes — the bot checks for commands every "
    "5 minutes."
)


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


def _answer_callback_query(callback_query_id: str, text: str) -> None:
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
    except Exception as e:
        logger.warning("Error answering callback query: %s", telegram_sender._redact(str(e)))


def run_news() -> None:
    """Run the digest pipeline; always reply with something — and when the
    run produced nothing, say WHY, with the stage counts, so a failure is
    diagnosable straight from the phone."""
    result = pipeline.run()
    if result["outcome"] == "sent":
        return

    c = result["counts"]
    trace = (f"\n\n(search {c['search']}, feeds {c['rss']}, new {c['unseen']}, "
             f"after dedup {c['deduped']}, selected {c['selected']}, delivered {c['sent']})")

    reasons = {
        "no_candidates":
            "Found no articles at all — the search backend is probably "
            "blocking this runner and the feeds had nothing fresh.",
        "all_seen":
            "Nothing new since the last digest — everything found right now "
            "was already sent. Try again in a few hours.",
        "nothing_sent":
            "Found new articles, but none survived summarizing/sending — "
            "check the Actions log for this run.",
    }
    telegram_sender.send_notice(reasons[result["outcome"]] + trace)


def handle_stats() -> None:
    stats = seen.get_stats()
    if not stats:
        telegram_sender.send_notice("Couldn't read the archive.")
        return
    lines = [
        f"Articles delivered: {stats['total']} total, {stats['last_30_days']} in the last 30 days",
        f"Feedback: {stats['thumbs_up']} 👍 / {stats['thumbs_down']} 👎",
    ]
    if stats["top_domains"]:
        lines.append("\nTop sources:")
        lines += [f"  {d or '(unknown)'} — {c}" for d, c in stats["top_domains"]]
    if stats["top_topics"]:
        lines.append("\nBy topic:")
        lines += [f"  {t or '(none)'} — {c}" for t, c in stats["top_topics"]]
    telegram_sender.send_notice("\n".join(lines))


def handle_topics_command(text: str) -> None:
    """Parse and execute '/topics', '/topics add X', '/topics remove X'."""
    parts = text.split(maxsplit=2)
    action = parts[1].lower() if len(parts) > 1 else "list"

    if action == "add" and len(parts) == 3:
        label = parts[2].strip()
        seen.add_topic(label, label.lower())
        telegram_sender.send_notice(f"Added topic: {label}. It's included from the next /news.")
    elif action == "remove" and len(parts) == 3:
        label = parts[2].strip()
        if seen.remove_topic(label):
            telegram_sender.send_notice(f"Removed topic: {label}.")
        else:
            current = ", ".join(seen.get_topics())
            telegram_sender.send_notice(f"No topic '{label}'. Current topics: {current}")
    else:
        topics = seen.get_topics()
        listing = "\n".join(f"  {label} — searches '{query}'" for label, query in topics.items())
        telegram_sender.send_notice(
            "Active topics:\n" + listing +
            "\n\nUse /topics add <name> or /topics remove <name>."
        )


def poll():
    """Poll for new Telegram updates and dispatch commands/feedback."""
    logger.info("Polling for new Telegram updates...")

    seen.init_db()

    last_update_id = seen.get_last_telegram_update_id()
    logger.info("Last processed update_id: %d", last_update_id)

    updates = _get_updates(offset=last_update_id + 1 if last_update_id > 0 else None)
    if not updates:
        logger.info("No new updates found.")
        return

    logger.info("Found %d new update(s)", len(updates))

    # Persist the newest update_id IMMEDIATELY, before doing anything else.
    # If the pipeline below crashes, the same command must not be
    # re-processed on every 5-minute poll forever.
    newest_update_id = max(u.get("update_id", 0) for u in updates)
    if newest_update_id:
        seen.set_last_telegram_update_id(newest_update_id)

    owner_chat_id = str(config.TELEGRAM_CHAT_ID)
    news_requested = False
    commands: list[str] = []

    for update in updates:
        update_id = update.get("update_id")

        message = update.get("message", {})
        if message:
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = message.get("text", "").strip()
            if not text.startswith("/"):
                continue
            if chat_id != owner_chat_id:
                logger.warning("Ignoring command from non-owner chat %s (update_id=%s)",
                               chat_id, update_id)
                continue
            if text == "/news":
                news_requested = True
            elif text in ("/weekly", "/stats") or text.startswith("/topics"):
                commands.append(text)
            else:
                # Typos happen (/new, /nwes...). Never ignore the owner
                # silently — that looks identical to a dead bot.
                commands.append("/help")
            logger.info("Command '%s' (update_id=%s)", text.split()[0], update_id)

        callback = update.get("callback_query", {})
        if callback:
            chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
            data = callback.get("data", "")
            if chat_id != owner_chat_id:
                logger.warning("Ignoring callback from non-owner chat %s (update_id=%s)",
                               chat_id, update_id)
                continue
            if data == "news_command":
                news_requested = True
                _answer_callback_query(callback.get("id"), "Getting latest news...")
            elif data.startswith("fb:"):
                # fb:up:<hash> / fb:down:<hash> — record and thank, no pipeline run
                parts = data.split(":", 2)
                if len(parts) == 3:
                    seen.record_feedback(parts[2], parts[1])
                    _answer_callback_query(
                        callback.get("id"),
                        "Noted 👍 — more like this" if parts[1] == "up"
                        else "Noted 👎 — less like this",
                    )

    # Lightweight commands first (they read the archive, not the news)
    help_sent = False
    for command in commands:
        if command == "/weekly":
            roundup.send_weekly()
        elif command == "/stats":
            handle_stats()
        elif command.startswith("/topics"):
            handle_topics_command(command)
        elif command == "/help" and not help_sent:
            telegram_sender.send_notice(HELP_TEXT)
            help_sent = True

    # The pipeline runs at most once per poll no matter how many taps queued up
    if news_requested:
        run_news()


def handle_dispatch() -> None:
    """Webhook mode: a Cloudflare Worker already read the Telegram update and
    forwarded it as a repository_dispatch event — getUpdates is not involved
    (and wouldn't work: setting a webhook disables polling on Telegram's side).
    The event payload says what to do."""
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        payload = json.load(f).get("client_payload", {})
    action = payload.get("action", "")
    logger.info("repository_dispatch received: action=%s", action)

    seen.init_db()
    if action == "news":
        run_news()
    elif action == "feedback":
        seen.record_feedback(payload.get("hash", ""), payload.get("verdict", ""))
    elif action == "weekly":
        roundup.send_weekly()
    elif action == "stats":
        handle_stats()
    elif action == "topics":
        handle_topics_command(payload.get("text", "/topics"))
    else:
        logger.warning("Unknown dispatch action: %s", action)


if __name__ == "__main__":
    if os.environ.get("GITHUB_EVENT_NAME") == "repository_dispatch":
        handle_dispatch()
    else:
        poll()
