# daily-ai-news-bot

A Telegram bot that fetches, filters, deduplicates and summarizes AI/ML news
— running entirely on GitHub Actions. No server, no hosting bill.

New stories are pushed automatically as they appear (checked every 15
minutes, quiet at night so overnight news arrives as a morning briefing),
and `/news` fetches on demand at any moment. Every article is its own
message with 👍/👎 buttons that teach the bot your taste:

> **AI**
> **DeepMind's new model solves protein interactions**
> _nature.com_
>
> A 4–6 sentence factual summary of the article, written by Gemini from the
> scraped article text. No hype, no invented details.
>
> Why it matters: one grounded sentence on the practical significance.
>
> #AI #DeepMind #Research
>
> [Read more](…)

All commands (owner-only):

| Command | What it does |
|---|---|
| `/news` | run the digest pipeline now |
| `/auto` | auto-push status; `/auto on`, `/auto off` |
| `/weekly` | synthesized roundup of the last 7 days, from the archive |
| `/stats` | delivery counts, top sources, feedback tally |
| `/topics` | list / `add <name>` / `remove <name>` search topics |
| 👍 / 👎 | feedback that personalizes future ranking |

Auto-push piggybacks on the 5-minute poll: when a check is due (every 15
min — the max sane frequency from the Gemini free-tier quota math, since
each story is embedded and summarized exactly once), new articles are
delivered without being asked. Two deliberate differences from `/news`:
empty checks stay **silent** (a channel that says "nothing happened" every
15 minutes trains you to mute it), and pushes are capped at the best 6
articles (trusted sources first, then your feedback preference). Quiet
hours 23:00–07:00 Tashkent time.

## How it works

```
/news in Telegram
      │   (GitHub Actions cron polls getUpdates every 5 min,
      │    or instantly via the optional webhook — see webhook/)
      ▼
gather      DuckDuckGo news index (one query per topic, last 24h)
            + RSS feeds (arXiv, lab blogs) for primary sources
      ▼
filter      blocklist (Wikipedia/Medium/Reddit/...) → already-sent URLs dropped
      ▼
dedup       semantic: Gemini embeddings + cosine similarity catch the same
            story from different outlets; the trusted outlet wins
      ▼
personalize score candidates against the 👍/👎 embedding centroids —
            liked-similar stories rank up, disliked-similar rank down
      ▼
scrape      full article text in parallel; snippet fallback when a site blocks
      ▼
rank        trusted outlets first, then preference score
      ▼
summarize   Gemini 2.5 Flash — also a quality gate: replies SKIP for SEO junk,
            homepages, and off-topic pages, which are then dropped
      ▼
send        individual Telegram messages, marked as seen one by one
```

State lives in a SQLite file (`seen.db`) that the workflow commits back to
the repo after every run — that's how a stateless CI runner remembers what
it already sent you. Sent-article hashes are kept for 30 days for dedup, and
every delivered article (date, topic, title, source, summary, hashtags) is
also appended to a permanent `archive` table in the same file — a growing,
queryable history of everything the bot ever sent:

```bash
sqlite3 seen.db "SELECT date_sent, topic, title FROM archive ORDER BY id DESC LIMIT 10"
```

Two layers of dedup:

- **URL level** — SHA-256 of the *normalized* URL (tracking params, `www.`,
  trailing slashes and `http/https` differences stripped), so the same link
  arriving dressed differently doesn't repeat.
- **Story level** — titles + snippets are embedded with Gemini and clustered
  by cosine similarity, so Reuters and TechCrunch covering the same
  announcement produce one message, not two. Fails open: if the embedding
  call errors, the digest still goes out.

## Measured, not vibes

The AI components are evaluated, not assumed to work (`evals/`):

- **Semantic dedup** (`eval_dedup.py`): on a labeled set of same-story
  paraphrases vs. hard negatives (same company, different event), the
  threshold sweep showed different-story pairs scoring below 0.75 and
  same-story paraphrases at 0.78+. The configured threshold (0.78) scores
  **1.00 precision / 1.00 recall** on that set — and it was moved from an
  initial guess of 0.80 *because* the eval showed two missed duplicates.
- **SKIP quality gate** (`eval_skip_gate.py`): **16/16 (100%)** on labeled
  cases — real news vs. tutorials, homepages, listicles, and off-topic
  articles the digest must reject.
- **Summary faithfulness** (`eval_faithfulness.py`): LLM-as-judge that
  re-scrapes delivered articles and checks the summary for unsupported
  claims. Runs against the live archive as it grows.

The datasets are small and hand-labeled — the point is that every prompt or
threshold change can be re-checked in seconds with `python evals/eval_*.py`.

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
├── pipeline.py           the digest pipeline (gather → dedup → personalize → summarize → send)
├── main.py               entry point for manual runs
├── poll_telegram.py      command router: /news /weekly /stats /topics + 👍/👎
├── search.py             DuckDuckGo queries + domain blocklist
├── rss.py                RSS/Atom primary sources (stdlib parser, no deps)
├── semantic.py           embedding-based same-story dedup
├── personalize.py        feedback-centroid ranking
├── scrape.py             parallel article fetching with snippet fallback
├── summarize.py          Gemini summaries + SKIP quality gate
├── roundup.py            /weekly synthesis from the archive
├── telegram_sender.py    message formatting, sending, crash-safe seen-marking
├── seen.py               SQLite state: dedup, archive, feedback, topics
├── gemini.py             shared Gemini client with API-key rotation
├── config.py             topics, feeds, domains, thresholds, prompts
├── evals/                measured accuracy of the AI parts (see above)
├── tests/                unit tests for the pure logic (pytest)
├── webhook/              optional Cloudflare Worker for instant triggers
└── .github/workflows     poll (5-min cron + dispatch), manual run, tests, keep-alive
```

## Configuration

Everything tunable is in `config.py`: topics and their search queries,
articles per topic, trusted/blocked domains, the semantic similarity
threshold (0.80), scrape timeouts, and the summarization prompt.

MIT licensed.
