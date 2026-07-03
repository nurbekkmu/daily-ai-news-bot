# On-Demand News Feature: Implementation Summary

## Overview
Added serverless, free on-demand news trigger via Telegram. Users can now:
- Send `/news` command to the bot
- Tap a "🔄 Get Latest News" button on article messages
- Receive fresh (deduplicated) articles within ~5 minutes

## Architecture
**Polling-based (not webhook):** GitHub Actions runs every 5 minutes, checks for new commands/button taps via `getUpdates`, and triggers the pipeline if found. This trades ~5-minute latency for zero server costs.

---

## Files Modified

### 1. `seen.py` — Extended database with Telegram state tracking
**Changes:**
- Added `telegram_state` table to `init_db()` for persistent polling state
- Added `get_last_telegram_update_id()` → retrieve last processed update ID (default 0)
- Added `set_last_telegram_update_id(update_id)` → store processed ID to avoid duplicates

**Why:** Ensures each `/news` command or button tap is processed exactly once across 5-minute polling intervals.

---

### 2. `telegram_sender.py` — Added inline button to digest messages
**Changes:**
- Imported `json` module
- Added `_get_news_button_markup()` → returns JSON keyboard with "🔄 Get Latest News" button
  - Button callback_data: `"news_command"` (detected by poll_telegram.py)
- Modified `_send_message(text, reply_markup=None)` → accepts optional inline keyboard
  - Safely adds `reply_markup` to API payload if provided
- Updated `send_digest()` → attaches button to every article message

**Why:** Users can trigger news refresh directly from any article, or send `/news` command.

---

### 3. `poll_telegram.py` ← NEW — On-demand polling orchestrator
**Key functions:**

1. **`_get_updates(offset=None)`** — Fetch Telegram updates via getUpdates API
   - Uses short polling timeout (5s) to stay responsive
   - Returns empty list on error (graceful degradation)

2. **`_answer_callback_query(callback_query_id, text)`** — Dismiss button tap loading state
   - Shows user "Getting latest news..." popup on button tap

3. **`_select_top_per_topic(articles)`** — Local copy of article selection logic
   - Reuses existing selection criteria (prefer scraped > snippet)

4. **`run_on_demand_pipeline()`** — Execute the full pipeline
   - **Reuses existing functions** (no duplication):
     - `search.gather_candidates()`
     - `scrape.fetch_all(candidates)`
     - `seen.filter_articles()` [deduplication via seen.db]
     - `summarize.summarize_all()`
     - `telegram_sender.send_digest()`
     - `seen.mark_seen()` [updates article tracking]
   - Returns `True` if articles sent, else `False`

5. **`poll()`** — Main polling loop
   - Initialize DB
   - Retrieve last processed update_id from DB
   - Fetch new updates from Telegram API (offset = last_update_id + 1)
   - Scan for:
     - Text message with text == "/news"
     - Callback query with data == "news_command"
   - On match:
     - Answer callback query (if button tap)
     - Run full pipeline
     - Update last_update_id in DB (prevent reprocessing)
   - Exit cleanly if no trigger found

**Tradeoff (noted in docstring):** Polling every 5 minutes means ~5-minute max latency between `/news` command and response. Benefit: no server, pure GitHub Actions, free forever.

---

### 4. `.github/workflows/poll.yml` ← NEW — Polling trigger workflow
**Schedule:** Runs every 5 minutes via cron `*/5 * * * *`

**Steps:**
1. Check out repo
2. Set up Python 3.11
3. Install dependencies from requirements.txt
4. Configure git (for state commits)
5. Run `python poll_telegram.py` (with secrets: GEMINI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
6. Commit & push `seen.db` updates (tracks last update_id + article dedup state)
   - Uses `[skip ci]` to prevent infinite workflow loops

---

## Hard Constraints Met ✅

| Constraint | Implementation |
|-----------|-----------------|
| **Free & serverless** | GitHub Actions cron + polling (no server) |
| **Use existing search.py, scrape.py, summarize.py as-is** | ✅ `poll_telegram.py` only calls their functions, no modifications |
| **Reuse pipeline functions** | ✅ `search.gather_candidates()`, `scrape.fetch_all()`, `seen.filter_articles()`, `summarize.summarize_all()`, `telegram_sender.send_digest()` |
| **Persist update_id to avoid reprocessing** | ✅ Stored in `seen.db` `telegram_state` table |
| **Use plain `requests` for polling** | ✅ No heavy dependencies added |
| **Don't modify search.py, scrape.py, summarize.py** | ✅ Zero changes to those modules |
| **Single DB file** | ✅ Both URL dedup (`seen` table) and polling state (`telegram_state` table) in `seen.db` |

---

## User Flow

### On-Demand Trigger (User Action)
```
User sends "/news" or taps button
           ↓
(up to 5 min)
GitHub Actions runs: poll_telegram.py
           ↓
Detects /news command or button tap
           ↓
Run pipeline: search → scrape → dedup → summarize → send
           ↓
User receives fresh articles with buttons attached
```

### Polling Workflow
```
Every 5 minutes:
  1. Check for new Telegram updates (after last_update_id)
  2. If no /news or button → exit (cost-free)
  3. If /news or button detected → run pipeline → commit state
```

---

## Testing

### Verify syntax & imports
```bash
python -m py_compile seen.py telegram_sender.py poll_telegram.py
python -c "import seen; import telegram_sender; import poll_telegram; print('OK')"
```

### Manual test (once deployed to GitHub)
1. Push code to GitHub repo with poll.yml
2. Set GitHub secrets: GEMINI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
3. Send `/news` to your bot in Telegram
4. Within ~5 minutes, receive fresh articles with buttons
5. Tap button on any article to trigger another run

### Expected behavior
- ✅ Articles have inline "🔄 Get Latest News" button
- ✅ Tapping button shows "Getting latest news..." popup
- ✅ Fresh articles arrive (no duplicates due to seen.db dedup)
- ✅ Same `/news` command never processed twice (update_id tracking)
- ✅ Workflow logs show polling activity

---

## Notes
- **No breaking changes** to existing daily cron workflow (daily.yml still runs independently)
- **Polling schedule** (5 min) can be adjusted in `.github/workflows/poll.yml` cron expression
- **Button delay** between send and response is network latency (usually <1s per message)
- **Latency to trigger** is up to 5 minutes (next polling interval)

---

## File Structure
```
daily-ai-news-bot/
├── config.py           (no changes)
├── main.py             (no changes, daily workflow)
├── search.py           (no changes)
├── scrape.py           (no changes)
├── summarize.py        (no changes)
├── seen.py             ✏️ Modified: added telegram_state table & functions
├── telegram_sender.py  ✏️ Modified: added button markup & reply_markup param
├── poll_telegram.py    ✨ NEW: on-demand polling orchestrator
├── .github/workflows/
│   ├── daily.yml       (no changes)
│   └── poll.yml        ✨ NEW: polling trigger (every 5 min)
└── seen.db             (persists: URL dedup + last_update_id)
```

