# daily-ai-news-bot

A Telegram bot that fetches, filters, deduplicates and summarizes AI/ML news
on demand — running entirely on GitHub Actions. No server, no hosting bill.

Send `/news` (or tap the button under any digest message) and a few minutes
later you get up to 8 fresh articles, two per topic (AI, ML, deep learning,
LLMs), each as its own message:

> **AI**
> **DeepMind's new model solves protein interactions**
> _nature.com_
>
> A 4–6 sentence factual summary of the article, written by Gemini from the
> scraped article text. No hype, no invented details.
>
> #AI #DeepMind #Research
>
> [Read more](…)

## How it works

```
/news in Telegram
      │   (GitHub Actions cron polls getUpdates every 5 min)
      ▼
search    DuckDuckGo news index, one query per topic, last 24h only
      ▼
filter    blocklist (Wikipedia/Medium/Reddit/...) → already-sent URLs dropped
      ▼
dedup     semantic: Gemini embeddings + cosine similarity catch the same
          story from different outlets; the trusted outlet wins
      ▼
scrape    full article text in parallel; snippet fallback when a site blocks
      ▼
rank      trusted outlets (Reuters, Nature, arXiv, TechCrunch, ...) first
      ▼
summarize Gemini 2.5 Flash — also a quality gate: replies SKIP for SEO junk,
          homepages, and off-topic pages, which are then dropped
      ▼
send      individual Telegram messages, marked as seen one by one
```

State lives in a SQLite file (`seen.db`) that the workflow commits back to
the repo after every run — that's how a stateless CI runner remembers what
it already sent you. Sent-article hashes are kept for 30 days.

Two layers of dedup:

- **URL level** — SHA-256 of the *normalized* URL (tracking params, `www.`,
  trailing slashes and `http/https` differences stripped), so the same link
  arriving dressed differently doesn't repeat.
- **Story level** — titles + snippets are embedded with Gemini and clustered
  by cosine similarity, so Reuters and TechCrunch covering the same
  announcement produce one message, not two. Fails open: if the embedding
  call errors, the digest still goes out.

## Design decisions

**Polling instead of a webhook.** A webhook needs a server listening 24/7;
this bot instead lets a GitHub Actions cron check for new `/news` commands
every 5 minutes. Honest trade-off: GitHub's cron isn't punctual under load,
so real-world latency is 5–20 minutes. Fine for a news digest; the upgrade
path if I ever want instant delivery is a Telegram webhook triggering
`repository_dispatch`.

**Failure modes are handled, not hoped away:**

- The Telegram `update_id` is persisted *before* the pipeline runs, so a
  crashing run can't re-trigger the same `/news` every 5 minutes forever.
- Each article is marked seen immediately after *its own* send succeeds — a
  crash mid-digest never causes resent messages.
- Gemini free-tier keys rotate round-robin; on a 429 the next key is tried
  immediately instead of sleeping.
- Scraped content is untrusted: the prompt tells Gemini to treat embedded
  "ignore previous instructions" text as article content, never as commands.
- Bot tokens are redacted from logs (Actions logs on a public repo are
  public too).
- Telegram rejecting malformed Markdown degrades to plain text instead of
  dropping the article.
- `/news` from anyone but the owner's chat is ignored.
- A monthly keep-alive commit stops GitHub from auto-disabling the cron
  after 60 days of repo inactivity.

## Setup

You need: a Telegram bot token, your chat id, and at least one Gemini API key.

1. Create a bot with [@BotFather](https://t.me/BotFather) → get `TELEGRAM_TOKEN`.
2. Message your bot once, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — your `chat.id` is
   `TELEGRAM_CHAT_ID`.
3. Get a free Gemini key at [aistudio.google.com](https://aistudio.google.com/apikey).
   Several keys from different Google accounts raise your effective rate
   limit — set them comma-separated in `GEMINI_API_KEYS`.
4. Fork this repo and add those three values as GitHub Actions secrets
   (Settings → Secrets and variables → Actions).
5. Enable workflows in the Actions tab. Send `/news` to your bot.

For local runs, copy `.env.example` to `.env`, fill in the same values, and:

```bash
pip install -r requirements.txt
python main.py
```

Never commit your local `seen.db` — the copy in the repo is written by the
workflows, and a manual push can conflict with theirs.

## Repository layout

```
├── pipeline.py           the digest pipeline (search → dedup → scrape → summarize → send)
├── main.py               entry point for manual runs
├── poll_telegram.py      on-demand trigger: polls for /news, runs pipeline
├── search.py             DuckDuckGo queries + domain blocklist
├── semantic.py           embedding-based same-story dedup
├── scrape.py             parallel article fetching with snippet fallback
├── summarize.py          Gemini summaries + SKIP quality gate
├── telegram_sender.py    message formatting, sending, crash-safe seen-marking
├── seen.py               SQLite dedup state + URL normalization
├── gemini.py             shared Gemini client with API-key rotation
├── config.py             topics, domains, thresholds, prompt
├── tests/                unit tests for the pure logic (pytest)
└── .github/workflows     poll (5-min cron), manual run, tests, keep-alive
```

## Configuration

Everything tunable is in `config.py`: topics and their search queries,
articles per topic, trusted/blocked domains, the semantic similarity
threshold (0.80), scrape timeouts, and the summarization prompt.

MIT licensed.
