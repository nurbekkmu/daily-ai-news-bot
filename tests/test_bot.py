"""
Unit tests for the pure logic: URL normalization, summary parsing, Markdown
escaping, per-topic selection, text extraction and semantic dedup.
Nothing here touches the network, Telegram, or seen.db.

    pip install pytest
    pytest -q
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import pipeline
import scrape
import seen
import semantic
import summarize
import telegram_sender


# ---- seen.normalize_url ----

def test_normalize_url_strips_tracking_params():
    assert seen.normalize_url(
        "https://example.com/story?utm_source=ddg&utm_medium=web"
    ) == "https://example.com/story"


def test_normalize_url_keeps_meaningful_params():
    assert seen.normalize_url(
        "https://example.com/story?id=42&utm_source=x"
    ) == "https://example.com/story?id=42"


def test_normalize_url_variants_hash_identically():
    variants = [
        "https://example.com/story",
        "http://example.com/story",
        "https://www.example.com/story/",
        "HTTPS://EXAMPLE.COM/story#comments",
        "https://example.com/story?fbclid=abc123",
    ]
    normalized = {seen.normalize_url(v) for v in variants}
    assert normalized == {"https://example.com/story"}


def test_normalize_url_distinct_articles_stay_distinct():
    a = seen.normalize_url("https://example.com/story-one")
    b = seen.normalize_url("https://example.com/story-two")
    assert a != b


# ---- summarize.parse_response ----

def test_parse_response_extracts_trailing_hashtags():
    summary, tags = summarize.parse_response(
        "OpenAI released a new model today.\nIt is faster.\n\n#AI #OpenAI"
    )
    assert summary == "OpenAI released a new model today.\nIt is faster."
    assert tags == ["#AI", "#OpenAI"]


def test_parse_response_does_not_eat_summary_with_hash_symbols():
    text = "The #1 model beat the #2 model on every benchmark."
    summary, tags = summarize.parse_response(text)
    assert summary == text
    assert tags == []


def test_parse_response_caps_hashtags_at_three():
    _, tags = summarize.parse_response("News.\n#a #b #c #d #e")
    assert tags == ["#a", "#b", "#c"]


def test_parse_response_no_hashtags():
    summary, tags = summarize.parse_response("Just a summary, nothing else.")
    assert summary == "Just a summary, nothing else."
    assert tags == []


def test_parse_response_keeps_why_it_matters_line():
    summary, tags = summarize.parse_response(
        "A new model was released.\nWhy it matters: it halves inference cost.\n#AI"
    )
    assert "Why it matters: it halves inference cost." in summary
    assert tags == ["#AI"]


# ---- archive ----

def test_archive_article_roundtrip(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "test_seen.db"))
    seen.init_db()
    seen.archive_article(
        {"title": "T", "domain": "reuters.com", "url": "https://r.com/a",
         "summary": "S", "hashtags": ["#AI", "#ML"]},
        topic="AI",
    )
    rows = sqlite3.connect(seen.DB_PATH).execute(
        "SELECT topic, title, domain, url, summary, hashtags FROM archive"
    ).fetchall()
    assert rows == [("AI", "T", "reuters.com", "https://r.com/a", "S", "#AI #ML")]


# ---- telegram_sender._escape_markdown ----

def test_escape_markdown_escapes_specials():
    assert telegram_sender._escape_markdown("a_b*c`d[e") == r"a\_b\*c\`d\[e"


def test_escape_markdown_leaves_plain_text_alone():
    assert telegram_sender._escape_markdown("plain title 123") == "plain title 123"


# ---- pipeline.select_top_per_topic ----

def _candidate(topic, domain, source, title="t"):
    return {"topic": topic, "domain": domain, "content_source": source,
            "title": title, "url": f"https://{domain}/{title}"}


def test_select_top_prefers_trusted_then_scraped():
    articles = [
        _candidate("AI", "random-blog.com", "scraped", "blog"),
        _candidate("AI", "reuters.com", "snippet", "reuters"),
        _candidate("AI", "unknown.net", "snippet", "unknown"),
        _candidate("AI", "techcrunch.com", "scraped", "tc"),
    ]
    selected = pipeline.select_top_per_topic(articles)["AI"]
    assert len(selected) == config.ITEMS_PER_TOPIC
    # Trusted outlets first; among trusted, scraped beats snippet
    assert selected[0]["domain"] == "techcrunch.com"
    assert selected[1]["domain"] == "reuters.com"


def test_select_top_groups_by_topic():
    articles = [
        _candidate("AI", "reuters.com", "scraped", "a"),
        _candidate("ML", "bbc.com", "scraped", "b"),
    ]
    selected = pipeline.select_top_per_topic(articles)
    assert set(selected) == {"AI", "ML"}


# ---- scrape._extract_text ----

def test_extract_text_prefers_article_tag():
    html = """
    <html><body>
      <p>This navigation paragraph is outside the article element entirely, promo promo.</p>
      <article><p>This is the real article body text and it is long enough to keep for sure.</p></article>
    </body></html>
    """
    text = scrape._extract_text(html)
    assert "real article body" in text
    assert "navigation paragraph" not in text


def test_extract_text_drops_short_junk_lines():
    html = "<html><body><p>Menu</p><p>%s</p></body></html>" % ("word " * 20)
    text = scrape._extract_text(html)
    assert "Menu" not in text


# ---- semantic dedup ----

def test_cosine_identical_and_orthogonal():
    assert semantic._cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert semantic._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_semantic_dedupe_keeps_trusted_source(monkeypatch):
    candidates = [
        _candidate("AI", "random-blog.com", "scraped", "OpenAI launches model"),
        _candidate("AI", "reuters.com", "scraped", "OpenAI launches new model"),
        _candidate("ML", "bbc.com", "scraped", "Completely different robotics story"),
    ]
    # First two are the same story, third is unrelated
    vectors = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]]
    monkeypatch.setattr(semantic.gemini, "embed", lambda texts: vectors)
    monkeypatch.setattr(config, "SEMANTIC_DEDUP_ENABLED", True)

    result = semantic.dedupe(candidates)
    domains = {c["domain"] for c in result}
    assert domains == {"reuters.com", "bbc.com"}  # trusted copy of the story survives


def test_semantic_dedupe_fails_open(monkeypatch):
    candidates = [
        _candidate("AI", "a.com", "scraped", "one"),
        _candidate("AI", "b.com", "scraped", "two"),
    ]

    def boom(texts):
        raise RuntimeError("embedding API down")

    monkeypatch.setattr(semantic.gemini, "embed", boom)
    monkeypatch.setattr(config, "SEMANTIC_DEDUP_ENABLED", True)

    assert semantic.dedupe(candidates) == candidates
