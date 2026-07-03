# On-Demand AI News Digest Bot

Zero-cost, on-demand Telegram digest of AI/ML/DL/NLP news. Send `/news` or tap
the "🔄 Get Latest News" button and the bot searches DuckDuckGo, scrapes full
article text, summarizes with Gemini's free tier (rotating across multiple API
keys), and DMs the articles to your personal chat — all orchestrated by
GitHub Actions (no server). There is no automatic daily schedule: news arrives
only when you ask for it.

## How it works

```
GitHub Actions (poll every 5 min, triggered by /news or button tap)
  -> search.py       DuckDuckGo search, 1 query per topic (AI, ML, DL, NLP)
  -> scrape.py        fetch + parse full article text (snippet fallback if blocked)
  -> poll_telegram.py  keep top 2 articles per topic (8 per request)
  -> summarize.py    Gemini free tier, multi-key rotation -> summary + "why it matters" + hashtags
  -> telegram_sender.py   send individual article messages to your personal chat
```

## On-Demand News

Each article message includes a "🔄 Get Latest News" button. You can also send `/news` to your bot anytime.

Behind the scenes:
- GitHub Actions runs `poll_telegram.py` every 5 minutes
- Detects your `/news` command or button tap
- Triggers the same pipeline above
- Sends fresh (deduplicated) articles directly to you
- Max latency: ~5 minutes (polling interval)
- Cost: still free (just GitHub Actions)

**Tradeoff:** Polling-based (not a live webhook), so there's no instant response. But it stays serverless and costs nothing.

See [`ON_DEMAND_FEATURE.md`](./ON_DEMAND_FEATURE.md) for technical details.

## One-time setup

### 1. Create the Telegram bot
- Message **@BotFather** on Telegram -> `/newbot` -> follow prompts -> save the token.

### 2. Get your personal chat ID
- Open Telegram and send a message to your bot (e.g., `/start`).
- Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
- Look for the **first message** in the response — copy the numeric `"id"` field from `"chat"`.
- Example: `"chat":{"id": 123456789}` → save `123456789` as your chat ID.
- This allows the bot to send you direct messages.

### 3. Get free Gemini API keys (several!)
- Go to [Google AI Studio](https://aistudio.google.com/app/apikey) -> create an API key.
- The free tier is rate-limited **per Google Cloud project**, so create 2-4 keys
  from different Google accounts (or different Cloud projects) and combine them
  comma-separated: `key1,key2,key3`. The bot rotates between them automatically.

### 4. Push this repo to GitHub, add secrets
In your repo: **Settings -> Secrets and variables -> Actions -> New repository secret**, add:
- `GEMINI_API_KEYS` (comma-separated list of keys)
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`

### 5. Test it
Go to the **Actions** tab -> "AI News Digest (manual only)" -> **Run workflow**
(manual trigger) to confirm everything works, then send `/news` to your bot.

## Local testing (optional)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your keys (GEMINI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
python main.py         # load_dotenv() automatically loads variables from .env
```

## Notes & known limitations

- **Scraping is best-effort.** Paywalled or Cloudflare-protected sites will fail
  to scrape and fall back to the DuckDuckGo search snippet instead — the pipeline
  never crashes because of one bad source.
- **Gemini free tier has rate limits.** At 8 articles/day this is well within
  free-tier limits, but if you raise `ITEMS_PER_TOPIC` a lot, watch for 429s.
- **No Prefect.** GitHub Actions' cron + `workflow_dispatch` covers scheduling,
  and each module has its own lightweight retry loop — Prefect would be
  redundant infrastructure for a job this small.
- Tune topics, item counts, and prompt wording in `config.py` — no need to
  touch pipeline logic in the other files.
