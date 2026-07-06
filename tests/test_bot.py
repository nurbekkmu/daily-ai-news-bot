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
import personalize
import pipeline
import rss
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


# ---- RSS parsing ----

_RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed</title>
<item><title>Story one</title><link>https://ex.com/1</link>
<description>&lt;p&gt;Some &lt;b&gt;html&lt;/b&gt; text&lt;/p&gt;</description>
<pubDate>Mon, 06 Jul 2026 08:00:00 GMT</pubDate></item>
<item><title></title><link>https://ex.com/skipme</link></item>
</channel></rss>"""

_ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title>
<entry><title>Atom story</title>
<link rel="alternate" href="https://ex.com/atom1"/>
<summary>Atom snippet</summary>
<published>2026-07-06T08:00:00Z</published></entry>
</feed>"""


def test_parse_feed_rss():
    entries = rss.parse_feed(_RSS_SAMPLE)
    assert len(entries) == 1  # titleless entry dropped
    assert entries[0]["title"] == "Story one"
    assert entries[0]["url"] == "https://ex.com/1"
    assert entries[0]["snippet"] == "Some html text"


def test_parse_feed_atom():
    entries = rss.parse_feed(_ATOM_SAMPLE)
    assert entries == [{
        "title": "Atom story", "url": "https://ex.com/atom1",
        "snippet": "Atom snippet", "published": "2026-07-06T08:00:00Z",
    }]


def test_is_recent_handles_unparseable_and_old():
    assert rss._is_recent("not a date")          # unknown dates pass through
    assert not rss._is_recent("Mon, 01 Jan 2001 00:00:00 GMT")


# ---- personalization ----

def test_attach_scores_no_feedback_is_neutral(monkeypatch):
    monkeypatch.setattr(personalize.seen, "get_feedback_embeddings", lambda: [])
    candidates = [_candidate("AI", "a.com", "scraped", "x")]
    personalize.attach_scores(candidates)
    assert candidates[0]["_pref"] == 0.0


def test_attach_scores_prefers_liked_direction(monkeypatch):
    monkeypatch.setattr(config, "PERSONALIZATION_ENABLED", True)
    monkeypatch.setattr(
        personalize.seen, "get_feedback_embeddings",
        lambda: [("up", [1.0, 0.0]), ("down", [0.0, 1.0])],
    )
    liked_like = _candidate("AI", "a.com", "scraped", "liked")
    liked_like["_embedding"] = [0.9, 0.1]
    disliked_like = _candidate("AI", "b.com", "scraped", "disliked")
    disliked_like["_embedding"] = [0.1, 0.9]
    no_embedding = _candidate("AI", "c.com", "scraped", "none")

    personalize.attach_scores([liked_like, disliked_like, no_embedding])
    assert liked_like["_pref"] > 0 > disliked_like["_pref"]
    assert no_embedding["_pref"] == 0.0


# ---- topics / feedback / stats storage ----

def test_topics_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "t.db"))
    seen.init_db()
    assert seen.get_topics() == config.TOPICS  # seeded from defaults

    seen.add_topic("Robotics", "robotics")
    assert seen.get_topics()["Robotics"] == "robotics"
    assert seen.remove_topic("Robotics") is True
    assert seen.remove_topic("Robotics") is False
    assert "Robotics" not in seen.get_topics()


def test_feedback_and_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "t.db"))
    seen.init_db()
    article = {"title": "T", "domain": "reuters.com", "url": "https://r.com/a",
               "summary": "S", "hashtags": ["#AI"], "_embedding": [0.5, 0.5]}
    seen.archive_article(article, topic="AI")
    seen.record_feedback(seen.url_short_hash("https://r.com/a"), "up")

    rows = seen.get_feedback_embeddings()
    assert rows == [("up", [0.5, 0.5])]

    stats = seen.get_stats()
    assert stats["total"] == 1
    assert stats["thumbs_up"] == 1
    assert stats["top_domains"][0][0] == "reuters.com"
