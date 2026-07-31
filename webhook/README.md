# Optional: instant triggers via webhook

By default the bot polls Telegram from a GitHub Actions cron. The workflow asks
for a run every 5 minutes, but GitHub deprioritizes frequent schedules on free
runners — measured cadence on this repo is **one run every 1-3 hours**, so a
`/news` can sit unanswered for most of an afternoon. This optional component
replaces that wait with seconds:

```
Telegram --(webhook)--> Cloudflare Worker --(repository_dispatch)--> GitHub Actions pipeline
```

The Worker (free tier) validates the request, answers button taps instantly,
and forwards the intent to GitHub. All pipeline logic stays in this repo.

**What still runs on the cron:** the 15-minute auto-push. Only *commands* move
to the webhook. Once a webhook is registered Telegram refuses `getUpdates` with
a 409, which `poll_telegram.py` treats as the expected steady state and logs at
INFO — `auto_check()` continues to run on every scheduled invocation.

## Setup

You need a Cloudflare account and a GitHub PAT. Both are yours to create —
every step below runs under your own credentials.

**1. Create a fine-grained GitHub PAT** scoped to this repository only, with
**Contents: Read and write** (that is the permission `repository_dispatch`
requires). Copy it; GitHub shows it once.

**2. Generate a webhook secret.** Any long random string; it is how the Worker
proves a request really came from Telegram. Generate one and keep it — you
will paste the same value twice, in steps 3 and 4:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**3. Deploy the Worker.** From the `webhook/` directory:

```bash
npx wrangler login
```

```bash
npx wrangler deploy
```

Then set the five secrets (each prompts for the value, nothing is echoed):

```bash
npx wrangler secret put TELEGRAM_SECRET
```

```bash
npx wrangler secret put TELEGRAM_TOKEN
```

```bash
npx wrangler secret put OWNER_CHAT_ID
```

```bash
npx wrangler secret put GITHUB_TOKEN
```

```bash
npx wrangler secret put GITHUB_REPO
```

`TELEGRAM_TOKEN` and `OWNER_CHAT_ID` are the same values as `TELEGRAM_TOKEN`
and `TELEGRAM_CHAT_ID` in your `.env`. `GITHUB_REPO` is `nurbekkmu/daily-ai-news-bot`.

`wrangler deploy` prints the Worker URL — something like
`https://news-bot-webhook.<your-subdomain>.workers.dev`. You need it next.

**4. Point Telegram at the Worker**, using the secret from step 2:

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" -d "url=<WORKER_URL>" -d "secret_token=<TELEGRAM_SECRET>"
```

**5. Verify.** Send `/news` in Telegram. Within seconds the Actions tab should
show a run triggered by `repository_dispatch`, not `schedule`. To confirm the
registration itself:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

`pending_update_count` should be 0 and `last_error_message` absent. If
`last_error_message` says something about the secret token, step 2's value does
not match on both sides.

## If something breaks

The Worker reports a failed dispatch back to you over Telegram rather than
dropping the command silently — an expired PAT is the usual cause and is
otherwise invisible from the phone. Live logs:

```bash
npx wrangler tail
```

## To undo

```bash
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```

Polling takes over again on the next scheduled run, with its 1-3 hour latency.
The Worker can be left deployed; with no webhook registered it receives nothing.
