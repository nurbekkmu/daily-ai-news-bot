# Optional: instant triggers via webhook

By default the bot polls Telegram every 5 minutes from GitHub Actions, so a
`/news` can take 5–20 minutes to answer. This optional component replaces
that wait with seconds:

```
Telegram --(webhook)--> Cloudflare Worker --(repository_dispatch)--> GitHub Actions pipeline
```

The Worker (free tier) validates the request, answers button taps instantly,
and forwards the intent to GitHub. All pipeline logic stays in this repo.

**Note:** once a webhook is set, Telegram disables `getUpdates` — the 5-minute
polling runs will simply find nothing, which is harmless. Delete the webhook
(step 5) to go back to pure polling.

## Setup

1. Create a fine-grained GitHub PAT for this repository with
   **Contents: Read and write** permission (that's what `repository_dispatch`
   needs).

2. Deploy the worker:

   ```bash
   cd webhook
   npx wrangler deploy worker.js --name news-bot-webhook
   npx wrangler secret put TELEGRAM_SECRET   # invent a long random string
   npx wrangler secret put TELEGRAM_TOKEN
   npx wrangler secret put OWNER_CHAT_ID
   npx wrangler secret put GITHUB_TOKEN
   npx wrangler secret put GITHUB_REPO      # e.g. nurbekkmu/daily-ai-news-bot
   ```

3. Point Telegram at the worker (use the same secret string):

   ```
   https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WORKER_URL>&secret_token=<TELEGRAM_SECRET>
   ```

4. Test: send `/news` — the Actions run should start within seconds
   (event: `repository_dispatch`).

5. To undo: `https://api.telegram.org/bot<TOKEN>/deleteWebhook` — polling
   takes over again on the next 5-minute cycle.
