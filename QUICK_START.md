# Quick Start: Deploy On-Demand News Feature

## What You Got ✅

A serverless Telegram bot that:
- **Sends a daily digest** at 1:00 UTC (8 articles)
- **Responds to `/news` command** within ~5 minutes (fresh articles)
- **Has a refresh button** on each article (users can tap to get more)
- **Costs $0/month** (GitHub Actions free tier)

---

## 3-Step Deployment

### 1️⃣ Push to GitHub
```bash
cd "D:\Telegram News\daily-ai-news-bot"
git add -A
git commit -m "Add on-demand news feature via polling"
git push origin main
```

### 2️⃣ Set GitHub Secrets
Go to your GitHub repo:
- **Settings** → **Secrets and variables** → **Actions**
- Add these 3 secrets (if not already set):
  - `GEMINI_API_KEY` — from https://aistudio.google.com/app/apikey
  - `TELEGRAM_TOKEN` — from @BotFather
  - `TELEGRAM_CHAT_ID` — numeric ID from `/getUpdates`

### 3️⃣ Test It
- Go to **Actions** tab
- Click **"Poll for On-Demand News Commands"**
- Click **"Run workflow"** → **Run workflow**
- Wait ~30 seconds, check logs for "Polling for new Telegram updates..."

---

## Using It

### Send `/news` to get fresh articles
```
You: /news
Bot (within 5 min): [8 fresh articles with buttons]
```

### Tap button on any article
```
You: [Tap "🔄 Get Latest News" button]
Bot (shows "Getting latest news...")
Bot (within 5 min): [8 more fresh articles]
```

### Get daily digest automatically
```
Every day at 1:00 UTC:
Bot: [8 articles with buttons]
```

---

## Documentation

- **`IMPLEMENTATION_SUMMARY.md`** — What was built and how
- **`ON_DEMAND_FEATURE.md`** — Technical deep-dive
- **`DEPLOYMENT.md`** — Full deployment guide with troubleshooting

---

## How It Works (30-second version)

```
Every 5 minutes: GitHub Actions runs poll_telegram.py
                  ↓
                  Checks: Any /news commands or button taps?
                  ↓
                  If YES: Run full pipeline → send fresh articles
                  If NO: Exit (free, no cost)
```

**Latency:** Up to 5 minutes (next polling interval)
**Cost:** Free (GitHub Actions free tier)
**State:** Persists in `seen.db` (committed to GitHub)

---

## Key Files

### New
- `poll_telegram.py` — Polls for commands, runs pipeline
- `.github/workflows/poll.yml` — Runs poll_telegram.py every 5 min

### Modified
- `seen.py` — Tracks last Telegram update (avoid duplicates)
- `telegram_sender.py` — Adds "🔄 Get Latest News" button
- `README.md` — Documents on-demand feature

### Unchanged
- `config.py`, `main.py`, `search.py`, `scrape.py`, `summarize.py`
- `.github/workflows/daily.yml` (daily digest still works)

---

## Verify It Worked

✅ **Check in 5 minutes:**

1. Go to Actions tab
2. See "Poll for On-Demand News Commands" workflow
3. Check recent run logs:
   ```
   "Polling for new Telegram updates..."
   "No new updates found."  ← Normal if no commands
   ```

✅ **Test end-to-end:**

1. Send `/news` to your bot
2. Wait up to 5 min
3. Receive fresh articles
4. Articles have "🔄 Get Latest News" button

---

## Customization (Optional)

### Change poll frequency (example: every 10 min)
Edit `.github/workflows/poll.yml`:
```yaml
schedule:
  - cron: "*/10 * * * *"  # Change 5 to 10
```

### Change button text
Edit `telegram_sender.py`:
```python
"text": "🔄 Get Latest News",  # Change to whatever
```

### Change articles per run
Edit `config.py`:
```python
ITEMS_PER_TOPIC = 2  # Change to 1, 3, etc.
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Poll workflow doesn't show in Actions | Enable Actions in Settings |
| `/news` not detected | Check TELEGRAM_TOKEN in secrets |
| No articles sent | Check Gemini rate limit; try later |
| Button missing | Ensure latest code pushed |

**Check logs:**
- Actions → Workflow → Latest run → Logs

---

## Done! 🎉

Your bot is now:
- ✅ Sending articles daily
- ✅ Responding to `/news` command
- ✅ Showing refresh button on articles
- ✅ Costing $0/month

Enjoy your AI news digest! 📰

