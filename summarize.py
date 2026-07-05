"""
Sends scraped/snippet article content to Gemini and returns a short factual
summary + relevant hashtags for each article. Gemini also acts as a quality
gate: it replies SKIP for pages that aren't genuine current AI/tech news.
"""

import logging
import re

import config
import gemini

logger = logging.getLogger(__name__)

# A hashtag line is hashtags and nothing else, e.g. "#NLP #AI #Research"
_HASHTAG_LINE = re.compile(r"^\s*(#\w+[\s,]*)+\s*$")


def parse_response(response: str) -> tuple[str, list[str]]:
    """Split a Gemini response into (summary, hashtags).

    Only the LAST non-empty line is considered as the hashtag line, and only
    if it contains nothing but hashtags — a summary sentence like "the #1
    model beat #2" must never be mistaken for hashtags and dropped.
    """
    lines = response.strip().split("\n")

    # Walk back over trailing blank lines
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1

    hashtags: list[str] = []
    if idx >= 0 and _HASHTAG_LINE.match(lines[idx]):
        hashtags = re.findall(r"#\w+", lines[idx])
        lines = lines[:idx]

    summary = "\n".join(lines).strip()
    return summary, hashtags[:3]


def summarize_article(article: dict) -> dict:
    """Summarize a single article dict (from scrape.py). Adds 'summary',
    'hashtags', and 'summarization_failed' keys."""
    prompt = config.SUMMARY_PROMPT_TEMPLATE.format(
        title=article["title"],
        source=article["domain"],
        content=article["content"],
    )
    try:
        response = gemini.generate(prompt)
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

        summary, hashtags = parse_response(response)
        article["summary"] = summary
        article["hashtags"] = hashtags
        article["summarization_failed"] = False

    except Exception as e:
        logger.error("Summarization failed for '%s': %s", article["title"], e)
        # Mark as failed so caller can decide whether to send it
        article["summary"] = None
        article["hashtags"] = []
        article["summarization_failed"] = True
    return article


def summarize_all(articles: list[dict]) -> list[dict]:
    return [summarize_article(a) for a in articles]


if __name__ == "__main__":
    import search
    import scrape

    logging.basicConfig(level=logging.INFO)
    cands = search.gather_candidates()
    enriched = scrape.fetch_all(cands)
    for a in summarize_all(enriched):
        print(f"\n[{a['topic']}] {a['title']}\n{a['summary']}")
