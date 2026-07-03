# Deployment Checklist: On-Demand News Feature

## Pre-Deployment ✅ (Completed)

- [x] `seen.py` extended with `telegram_state` table and update_id functions
- [x] `telegram_sender.py` updated with inline button markup and reply_markup support
- [x] `poll_telegram.py` created with full polling logic
- [x] `.github/workflows/poll.yml` created with 5-minute schedule
- [x] `README.md` updated with on-demand feature documentation
- [x] `ON_DEMAND_FEATURE.md` created with technical details
- [x] All Python modules pass syntax compilation and import tests
- [x] All workflow YAML files are valid and parseable
- [x] Integration tests pass (DB operations, button markup)

---

## Deployment Steps (Do This)

### 1. Push code to GitHub
```bash
git add -A
git commit -m "Add on-demand news feature via polling"
git push origin main
```

### 2. Verify GitHub Actions recognizes workflows
- Go to your repo → **Actions** tab
- You should see two workflows listed:
  - "Daily AI News Digest" (daily.yml) — runs at 1:00 UTC daily
  - "Poll for On-Demand News Commands" (poll.yml) — runs every 5 minutes

### 3. Test manual trigger
- Click "Poll for On-Demand News Commands" workflow
- Click **"Run workflow"** → **Run workflow** (green button)
- Wait ~30 seconds for the run to complete
- Check **Logs** for any errors
- If successful, you'll see log entries like:
  ```
  Polling for new Telegram updates...
  Last processed update_id: 0
  No new updates found.
  ```

### 4. Test the feature end-to-end
- Open Telegram
- Send `/news` to your bot
- Wait up to 5 minutes (next poll interval)
- You should receive fresh articles with "🔄 Get Latest News" buttons
- Tap the button on any article
- Wait up to 5 minutes again
- You should get another batch of fresh articles

---

## Expected Behavior

### Daily Digest (existing, unchanged)
- **When:** 1:00 UTC every day
- **What:** 8 articles (2 per topic: AI, ML, DL, NLP)
- **How:** `main.py` → full pipeline → send articles
- **Buttons:** Each article has "🔄 Get Latest News" button

### On-Demand Trigger (new)
- **How to trigger:**
  - Send `/news` command to bot
  - Tap "🔄 Get Latest News" button on any article
- **Response time:** Within ~5 minutes at most (polling interval)
- **What you get:** Fresh articles (deduplicated, newest first)
- **Cost:** Still free (GitHub Actions)

---

## Monitoring & Troubleshooting

### Check polling status
- Go to repo → **Actions** → "Poll for On-Demand News Commands"
- Review execution history and logs
- Look for:
  - ✅ "Polling for new Telegram updates..."
  - ✅ "No new updates found." (normal if no commands)
  - ✅ "Detected /news command" (when you send /news)
  - ❌ Any ERROR messages

### Common issues

| Issue | Solution |
|-------|----------|
| Poll workflow doesn't run | Check Actions are **enabled** in repo Settings |
| `/news` command not detected | Verify TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are set correctly in GitHub Secrets |
| No articles sent | Check Gemini rate limit hasn't been hit; verify all seen articles before (check dedup is working) |
| Button not showing | Ensure you're using the latest version; Telegram should render inline keyboard |

### View workflow runs
```
Repo → Actions → Select workflow → View latest run
```

---

## Optional Customizations

### Adjust polling frequency
Edit `.github/workflows/poll.yml`:
```yaml
schedule:
  - cron: "*/10 * * * *"   # Change 5 (*/5) to 10 (*/10) for every 10 minutes
```

### Adjust articles per topic on-demand
Edit `config.py`:
```python
ITEMS_PER_TOPIC = 2   # Change to 1, 3, etc. for on-demand runs
```

### Change button text
Edit `telegram_sender.py`:
```python
"text": "🔄 Get Latest News",   # Change to any text you want
```

---

## What to Share with Users

When you share this bot with others:

1. They create their own bot with @BotFather
2. They get their TELEGRAM_CHAT_ID via `/getUpdates`
3. They fork/clone this repo
4. They push to their own GitHub repo
5. They set secrets: GEMINI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
6. Workflows run automatically!

**Users can then:**
- Wait for daily digest at scheduled time
- Send `/news` anytime for fresh articles
- Tap button on articles to refresh

---

## Files Changed/Created

```
Modified:
  - seen.py                          (+2 functions, +1 table)
  - telegram_sender.py               (+1 function, +1 parameter)
  - README.md                        (added on-demand section)
  - .env                             (created if not existed)

Created:
  - poll_telegram.py                 (new polling orchestrator)
  - .github/workflows/poll.yml       (new polling workflow)
  - ON_DEMAND_FEATURE.md             (technical documentation)

Unchanged:
  - config.py
  - main.py
  - search.py
  - scrape.py
  - summarize.py
  - .github/workflows/daily.yml
```

---

## Post-Deployment

After deployment, monitor for:
- ✅ Both workflows appear in Actions tab
- ✅ Poll workflow runs every 5 minutes (check recent runs)
- ✅ `/news` command triggers article delivery within 5 min
- ✅ Buttons appear on articles
- ✅ No duplicate articles across runs (dedup working)
- ✅ seen.db grows over time (state persists)

---

## Rollback (if needed)

If something goes wrong:

1. Disable the poll workflow:
   - Actions → "Poll for On-Demand News Commands" → ⋯ → Disable workflow

2. Revert last commit:
   ```bash
   git revert HEAD
   git push
   ```

3. The daily digest (daily.yml) will still run untouched

---

## Success! 🎉

Once deployed and tested:
- You have a fully free, serverless news bot
- Fresh articles every day at scheduled time
- Plus on-demand updates anytime
- No servers, no monthly bills
- All state persists in Git (seen.db)

Enjoy your AI news digest! 📰

