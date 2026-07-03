"""
Sends scraped/snippet article content to Gemini (free tier) and returns a
short summary + "why it matters" line + relevant hashtags for each article.
"""

import time
import logging
import re

from google import genai

import config

logger = logging.getLogger(__name__)

# One client per API key, created lazily; _key_cursor round-robins over the
# configured keys so requests are spread evenly and per-key rate limits are
# hit as late as possible.
_clients: dict[str, genai.Client] = {}
_key_cursor = 0


def _next_client() -> tuple[genai.Client, int]:
    """Return the next client in the round-robin rotation and its key index."""
    global _key_cursor
    keys = config.GEMINI_API_KEYS
    if not keys:
        raise RuntimeError(
            "No Gemini API key set — configure GEMINI_API_KEYS (comma-separated) "
            "or GEMINI_API_KEY (check your .env / GitHub secret)."
        )
    key_idx = _key_cursor % len(keys)
    _key_cursor += 1
    key = keys[key_idx]
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key], key_idx


def _is_rate_limit_error(err: Exception) -> bool:
    msg = str(err)
    return (
        "429" in msg
        or "RESOURCE_EXHAUSTED" in msg
        or "quota" in msg.lower()
        or "rate limit" in msg.lower()
    )


def _call_gemini(prompt: str) -> str:
    # Try each key at least once; keep normal retry behavior for transient errors.
    keys = config.GEMINI_API_KEYS
    max_attempts = max(config.MAX_RETRIES, len(keys))
    last_err = None
    for attempt in range(1, max_attempts + 1):
        client, key_idx = _next_client()
        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
            )
            return (response.text or "").strip()
        except Exception as e:  # noqa: BLE001 - retry on rate limits/transient errors
            last_err = e
            logger.warning(
                "Gemini call failed on key #%d (attempt %d/%d): %s",
                key_idx + 1, attempt, max_attempts, e,
            )
            if attempt < max_attempts:
                if _is_rate_limit_error(e) and len(keys) > 1:
                    # Rate-limited: next attempt already uses the next key — no need to wait
                    continue
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    raise last_err


def summarize_article(article: dict) -> dict:
     """Summarize a single article dict (from scrape.py). Adds 'summary', 'hashtags', and 'summarization_failed' keys."""
     prompt = config.SUMMARY_PROMPT_TEMPLATE.format(
         title=article["title"],
         source=article["domain"],
         content=article["content"],
     )
     try:
         response = _call_gemini(prompt)
         if not response:
             raise ValueError("Empty response from Gemini")

         # Reliability screen: the prompt asks Gemini to reply SKIP when the
         # content is not a genuine current news article.
         if response.strip().upper().startswith("SKIP"):
             logger.info("Flagged as not real news, skipping: '%s'", article["title"])
             article["summary"] = None
             article["hashtags"] = []
             article["summarization_failed"] = True
             article["unreliable"] = True
             return article

         # Parse response: extract summary and hashtags
         # Expected format:
         # [Summary text with "Why it matters:" line]
         # [Hashtags on separate line]
         lines = response.strip().split("\n")
         
         # Find hashtags (line starting with # or containing multiple #)
         hashtags = []
         summary_lines = []
         for line in lines:
             if line.strip().startswith("#") or (line.count("#") >= 2):
                 # This is likely the hashtag line
                 hashtags_found = re.findall(r"#\w+", line)
                 hashtags.extend(hashtags_found)
             else:
                 summary_lines.append(line)
         
         article["summary"] = "\n".join(summary_lines).strip()
         article["hashtags"] = hashtags[:3] if hashtags else []  # Keep up to 3 hashtags
         article["summarization_failed"] = False
         
     except Exception as e:
         logger.error("Summarization failed for '%s': %s", article["title"], e)
         # Mark as failed so caller can decide whether to send it
         article["summary"] = None
         article["hashtags"] = []
         article["summarization_failed"] = True
     return article


def summarize_all(articles: list[dict]) -> list[dict]:
    summarized = []
    for a in articles:
        summarized.append(summarize_article(a))
    return summarized


if __name__ == "__main__":
    import search
    import scrape

    logging.basicConfig(level=logging.INFO)
    cands = search.gather_candidates()
    enriched = scrape.fetch_all(cands)
    for a in summarize_all(enriched):
        print(f"\n[{a['topic']}] {a['title']}\n{a['summary']}")
