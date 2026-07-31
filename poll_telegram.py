"""
Poll-based on-demand trigger via Telegram commands/buttons.

TRADEOFF: This uses polling instead of a live webhook listener, so a command
waits for the next scheduled run. The workflow asks for every 5 minutes, but
that is a request, not a promise: GitHub deprioritizes frequent schedules on
free runners, and measured cadence on this repo is one run every 1-3 hours.
The upside: no server, stays serverless and free. For instant delivery there's
an optional webhook mode — see webhook/README.md — which forwards Telegram
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
import time
from datetime import datetime, timezone

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
    "  /topics — list topics; /topics add <name>, /topics remove <name>\n"
    "  /auto — auto-push status; /auto on, /auto off\n\n"
    "Replies are not instant. The poller is a GitHub Actions cron: it asks "
    "for every 5 minutes, but GitHub deprioritizes frequent schedules and "
    "in practice runs it every 1-3 hours."
)


def _is_quiet_hour(utc_hour: int) -> bool:
    """True during configured quiet hours (local time via fixed UTC offset —
    Tashkent has no DST, so zoneinfo would be overkill)."""
    local = (utc_hour + config.AUTO_TZ_UTC_OFFSET) % 24
    start, end = config.AUTO_QUIET_START_HOUR, config.AUTO_QUIET_END_HOUR
    if start <= end:
        return start <= local < end
    return local >= start or local < end  # window wraps midnight


def _auto_enabled() -> bool:
    default = "1" if config.AUTO_PUSH_DEFAULT else "0"
    return seen.get_state("auto_enabled", default) == "1"


def _auto_due(now_ts: float, last_ts: float) -> bool:
    return now_ts - last_ts >= config.AUTO_INTERVAL_MINUTES * 60


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


# Why a run delivered nothing, in the owner's words rather than a stack trace.
OUTCOME_REASONS = {
    "no_api_key":
        "⚠️ No Gemini API key is configured, so nothing can be summarized. "
        "Set the GEMINI_API_KEYS secret under Settings → Secrets and "
        "variables → Actions in the repo, then try again.",
    "no_candidates":
        "Found no articles at all — the search backend is probably "
        "blocking this runner and the feeds had nothing fresh.",
    "all_seen":
        "Nothing new since the last digest — everything found right now "
        "was already sent. Try again in a few hours.",
    "summarize_failed":
        "⚠️ Found new articles, but every Gemini call failed — the "
        "GEMINI_API_KEYS secret is most likely missing, invalid, or out of "
        "quota. Check the Actions log for this run.",
    "nothing_sent":
        "Found new articles, but none survived summarizing/sending — "
        "check the Actions log for this run.",
}

# Outcomes that mean the BOT is broken rather than the news being quiet.
# Auto-push is silent by design, which is exactly how a missing API key can
# go unnoticed for weeks — these get through anyway (throttled to once a day).
BROKEN_OUTCOMES = ("no_api_key", "summarize_failed")


def _describe(result: dict) -> str:
    c = result["counts"]
    trace = (f"\n\n(search {c['search']}, feeds {c['rss']}, new {c['unseen']}, "
             f"after dedup {c['deduped']}, selected {c['selected']}, "
             f"failed {c['failed']}, delivered {c['sent']})")
    return OUTCOME_REASONS[result["outcome"]] + trace


def run_news() -> str:
    """Run the digest pipeline; always reply with something — and when the
    run produced nothing, say WHY, with the stage counts, so a failure is
    diagnosable straight from the phone. Returns the outcome."""
    result = pipeline.run()
    if result["outcome"] != "sent":
        telegram_sender.send_notice(_describe(result))
    return result["outcome"]


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


def handle_auto_command(text: str) -> None:
    """Parse and execute '/auto', '/auto on', '/auto off'."""
    parts = text.split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""

    if arg == "on":
        seen.set_state("auto_enabled", "1")
        telegram_sender.send_notice(
            f"Auto-push ON — checking every {config.AUTO_INTERVAL_MINUTES} min, "
            f"quiet {config.AUTO_QUIET_START_HOUR}:00–{config.AUTO_QUIET_END_HOUR}:00 "
            "Tashkent time."
        )
    elif arg == "off":
        seen.set_state("auto_enabled", "0")
        telegram_sender.send_notice("Auto-push OFF — news only on /news.")
    else:
        state = "ON" if _auto_enabled() else "OFF"
        telegram_sender.send_notice(
            f"Auto-push is {state} (every {config.AUTO_INTERVAL_MINUTES} min, "
            f"quiet {config.AUTO_QUIET_START_HOUR}:00–{config.AUTO_QUIET_END_HOUR}:00 "
            "Tashkent). Use /auto on or /auto off."
        )


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
        topics = seen.get_topics()
        if label in topics and len(topics) == 1:
            # An empty topics table silently falls back to the config
            # defaults — refusing is less surprising than resurrection.
            telegram_sender.send_notice(
                f"'{label}' is the only topic left — add another before removing it."
            )
        elif seen.remove_topic(label):
            telegram_sender.send_notice(f"Removed topic: {label}.")
        else:
            telegram_sender.send_notice(
                f"No topic '{label}'. Current topics: {', '.join(topics)}"
            )
    else:
        topics = seen.get_topics()
        listing = "\n".join(f"  {label} — searches '{query}'" for label, query in topics.items())
        telegram_sender.send_notice(
            "Active topics:\n" + listing +
            "\n\nUse /topics add <name> or /topics remove <name>."
        )


def poll() -> str | None:
    """Poll for new Telegram updates and dispatch commands/feedback.
    Returns the pipeline outcome if /news ran, else None."""
    logger.info("Polling for new Telegram updates...")

    seen.init_db()

    last_update_id = seen.get_last_telegram_update_id()
    logger.info("Last processed update_id: %d", last_update_id)

    updates = _get_updates(offset=last_update_id + 1 if last_update_id > 0 else None)
    if not updates:
        logger.info("No new updates found.")
        return None

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
            elif (text in ("/weekly", "/stats")
                  or text.startswith("/topics") or text.startswith("/auto")):
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
        elif command.startswith("/auto"):
            handle_auto_command(command)
        elif command == "/help" and not help_sent:
            telegram_sender.send_notice(HELP_TEXT)
            help_sent = True

    # The pipeline runs at most once per poll no matter how many taps queued up
    if news_requested:
        outcome = run_news()
        seen.set_state("last_auto_ts", str(int(time.time())))
        return outcome
    return None


def _alert_once_a_day(outcome: str, text: str) -> None:
    """Send a breakage alert at most once per day per outcome. Auto-push runs
    every 15 minutes; an unthrottled alert would be its own kind of broken."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stamp = f"{outcome}:{today}"
    if seen.get_state("last_alert", "") == stamp:
        logger.info("Auto-push: already alerted about '%s' today.", outcome)
        return
    seen.set_state("last_alert", stamp)
    telegram_sender.send_notice(text)


def auto_check() -> str | None:
    """Scheduled push: when due (and outside quiet hours), run the pipeline
    and deliver whatever is new. Empty outcomes stay SILENT — a push channel
    that says 'nothing happened' every 15 minutes trains you to mute it.
    Outcomes that mean the bot itself is broken DO speak up, once a day.
    Returns the outcome, or None if no run happened."""
    if not _auto_enabled():
        return None
    if _is_quiet_hour(datetime.now(timezone.utc).hour):
        logger.info("Auto-push: quiet hours, skipping.")
        return None
    try:
        last_ts = float(seen.get_state("last_auto_ts", "0"))
    except ValueError:
        last_ts = 0.0
    if not _auto_due(time.time(), last_ts):
        return None

    # Mark the attempt BEFORE running (same crash-safety idea as update_id:
    # a crashing pipeline must not retry on every 5-minute poll)
    seen.set_state("last_auto_ts", str(int(time.time())))
    logger.info("Auto-push: check is due, running pipeline.")
    result = pipeline.run(auto=True)
    logger.info("Auto-push outcome: %s %s", result["outcome"], result["counts"])
    if result["outcome"] in BROKEN_OUTCOMES:
        _alert_once_a_day(result["outcome"], _describe(result))
    return result["outcome"]


def handle_dispatch() -> str | None:
    """Webhook mode: a Cloudflare Worker already read the Telegram update and
    forwarded it as a repository_dispatch event — getUpdates is not involved
    (and wouldn't work: setting a webhook disables polling on Telegram's side).
    The event payload says what to do. Returns the pipeline outcome if one ran."""
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        payload = json.load(f).get("client_payload", {})
    action = payload.get("action", "")
    logger.info("repository_dispatch received: action=%s", action)

    seen.init_db()
    if action == "news":
        return run_news()
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
    return None


if __name__ == "__main__":
    if os.environ.get("GITHUB_EVENT_NAME") == "repository_dispatch":
        outcomes = [handle_dispatch()]
    else:
        # Both may run in one invocation; neither should mask the other.
        outcomes = [poll(), auto_check()]

    # A missing key is a misconfiguration that will never fix itself, so fail
    # the workflow loudly. Everything else — including transient Gemini errors
    # — stays green and is reported over Telegram instead.
    if "no_api_key" in outcomes:
        logger.error("Failing the run: GEMINI_API_KEYS is not configured.")
        raise SystemExit(1)
