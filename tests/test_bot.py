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

def test_article_markup_is_feedback_only():
    import json
    markup = json.loads(telegram_sender._get_article_markup(
        {"url": "https://example.com/story"}
    ))
    rows = markup["inline_keyboard"]
    assert len(rows) == 1  # no refresh button row
    assert [b["text"] for b in rows[0]] == ["👍", "👎"]
    assert all(b["callback_data"].startswith("fb:") for b in rows[0])


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
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    monkeypatch.setattr(config, "PERSONALIZATION_ENABLED", True)
    monkeypatch.setattr(
        personalize.seen, "get_feedback_embeddings",
        lambda: [("up", [1.0, 0.0], today), ("down", [0.0, 1.0], today)],
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


def test_record_feedback_replaces_instead_of_accumulating(tmp_path, monkeypatch):
    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "t.db"))
    seen.init_db()
    seen.record_feedback("abc123", "up")
    seen.record_feedback("abc123", "up")      # impatient re-tap
    seen.record_feedback("abc123", "down")    # changed their mind

    import sqlite3
    rows = sqlite3.connect(seen.DB_PATH).execute(
        "SELECT url_hash, verdict FROM feedback"
    ).fetchall()
    assert rows == [("abc123", "down")]       # one row, latest verdict wins


def test_decay_weight_halves_at_half_life():
    assert personalize._decay_weight(0) == 1.0
    assert abs(personalize._decay_weight(config.PERSONALIZE_HALF_LIFE_DAYS) - 0.5) < 1e-9
    assert personalize._decay_weight(config.PERSONALIZE_HALF_LIFE_DAYS * 10) < 0.01


def test_weighted_centroid():
    c = personalize._weighted_centroid([[1.0, 0.0], [0.0, 1.0]], [3.0, 1.0])
    assert c == [0.75, 0.25]
    assert personalize._weighted_centroid([[1.0]], [0.0]) is None


def test_archive_embedding_rounded_and_purged(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "t.db"))
    seen.init_db()
    seen.archive_article(
        {"title": "T", "domain": "d.com", "url": "https://d.com/a",
         "summary": "S", "hashtags": [], "_embedding": [0.123456789, 1.0]},
        topic="AI",
    )
    stored = sqlite3.connect(seen.DB_PATH).execute(
        "SELECT embedding FROM archive"
    ).fetchone()[0]
    assert stored == "[0.12346, 1.0]"  # rounded to 5 decimals

    # Backdate the row past the retention window, then purge
    conn = sqlite3.connect(seen.DB_PATH)
    conn.execute("UPDATE archive SET date_sent = '2020-01-01 00:00'")
    conn.commit(); conn.close()
    seen.purge_old_embeddings()
    remaining = sqlite3.connect(seen.DB_PATH).execute(
        "SELECT embedding, summary FROM archive"
    ).fetchone()
    assert remaining == (None, "S")  # vector gone, article text kept


def test_state_kv_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "t.db"))
    seen.init_db()
    assert seen.get_state("missing", "fallback") == "fallback"
    seen.set_state("auto_enabled", "0")
    assert seen.get_state("auto_enabled", "1") == "0"


# ---- auto-push scheduling ----

def test_quiet_hours_tashkent_window():
    import poll_telegram
    # Quiet 23:00-07:00 UTC+5  ->  18:00-02:00 UTC
    assert poll_telegram._is_quiet_hour(18)      # 23:00 Tashkent
    assert poll_telegram._is_quiet_hour(1)       # 06:00 Tashkent
    assert not poll_telegram._is_quiet_hour(2)   # 07:00 Tashkent — morning briefing
    assert not poll_telegram._is_quiet_hour(12)  # 17:00 Tashkent


def test_auto_due_threshold():
    import poll_telegram
    interval = config.AUTO_INTERVAL_MINUTES * 60
    assert poll_telegram._auto_due(1000.0 + interval, 1000.0)
    assert not poll_telegram._auto_due(1000.0 + interval - 1, 1000.0)


def test_cap_for_auto_keeps_best_across_topics():
    def art(domain, pref):
        return {"domain": domain, "_pref": pref, "content_source": "scraped"}

    selected = {
        "AI": [art("reuters.com", 0.1), art("random.net", 0.9)],
        "ML": [art("nature.com", 0.5), art("blog.example", 0.0)],
    }
    capped = pipeline.cap_for_auto(selected, limit=2)
    flat = [a for items in capped.values() for a in items]
    assert len(flat) == 2
    # Both trusted articles win over untrusted, regardless of _pref
    assert {a["domain"] for a in flat} == {"reuters.com", "nature.com"}


def test_cap_for_auto_no_cap_needed():
    selected = {"AI": [{"domain": "reuters.com", "_pref": 0.0}]}
    assert pipeline.cap_for_auto(selected, limit=6) == selected


# ---- the command router, end to end with everything external mocked ----

def _run_poll(monkeypatch, tmp_path, updates):
    """Drive poll_telegram.poll() against fake Telegram updates.
    Returns (pipeline_runs, notices, callback_answers)."""
    import poll_telegram

    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "111")

    pipeline_runs = []
    notices = []
    answers = []

    monkeypatch.setattr(poll_telegram, "_get_updates", lambda offset=None: updates)
    monkeypatch.setattr(poll_telegram, "_answer_callback_query",
                        lambda cid, text: answers.append(text))
    monkeypatch.setattr(
        poll_telegram.pipeline, "run",
        lambda auto=False: (pipeline_runs.append(auto),
                            {"outcome": "sent", "counts": {}})[1],
    )
    monkeypatch.setattr(poll_telegram.telegram_sender, "send_notice",
                        lambda text: notices.append(text))

    poll_telegram.poll()
    return pipeline_runs, notices, answers


def _msg(update_id, chat_id, text):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text}}


def test_router_news_command_runs_pipeline_once(tmp_path, monkeypatch):
    runs, notices, _ = _run_poll(monkeypatch, tmp_path, [
        _msg(1, 111, "/news"),
        _msg(2, 111, "/news"),   # queued twice -> still one run
    ])
    assert runs == [False]       # one run, not auto mode
    assert notices == []         # outcome 'sent' -> no notice
    assert seen.get_last_telegram_update_id() == 2


def test_router_unknown_command_gets_help_once(tmp_path, monkeypatch):
    runs, notices, _ = _run_poll(monkeypatch, tmp_path, [
        _msg(5, 111, "/new"),
        _msg(6, 111, "/nwes"),
    ])
    assert runs == []
    assert len(notices) == 1 and notices[0].startswith("Commands:")


def test_router_ignores_non_owner(tmp_path, monkeypatch):
    runs, notices, answers = _run_poll(monkeypatch, tmp_path, [
        _msg(7, 999, "/news"),
        {"update_id": 8, "callback_query": {
            "id": "cb1", "data": "fb:up:deadbeef",
            "message": {"chat": {"id": 999}}}},
    ])
    assert runs == [] and notices == [] and answers == []
    assert seen.get_last_telegram_update_id() == 8  # still consumed


def test_router_feedback_recorded_and_answered(tmp_path, monkeypatch):
    runs, notices, answers = _run_poll(monkeypatch, tmp_path, [
        {"update_id": 9, "callback_query": {
            "id": "cb2", "data": "fb:down:cafe1234",
            "message": {"chat": {"id": 111}}}},
    ])
    assert runs == []
    assert answers and "👎" in answers[0]
    import sqlite3
    rows = sqlite3.connect(seen.DB_PATH).execute(
        "SELECT url_hash, verdict FROM feedback").fetchall()
    assert rows == [("cafe1234", "down")]


def test_router_topics_guard_last_topic(tmp_path, monkeypatch):
    import poll_telegram
    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "t.db"))
    notices = []
    monkeypatch.setattr(poll_telegram.telegram_sender, "send_notice",
                        lambda text: notices.append(text))
    seen.init_db()
    for label in list(seen.get_topics())[1:]:
        seen.remove_topic(label)
    only = list(seen.get_topics())[0]

    poll_telegram.handle_topics_command(f"/topics remove {only}")
    assert "only topic left" in notices[-1]
    assert only in seen.get_topics()  # still there


def test_feedback_and_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(seen, "DB_PATH", str(tmp_path / "t.db"))
    seen.init_db()
    article = {"title": "T", "domain": "reuters.com", "url": "https://r.com/a",
               "summary": "S", "hashtags": ["#AI"], "_embedding": [0.5, 0.5]}
    seen.archive_article(article, topic="AI")
    seen.record_feedback(seen.url_short_hash("https://r.com/a"), "up")

    rows = seen.get_feedback_embeddings()
    assert len(rows) == 1
    verdict, emb, date = rows[0]
    assert (verdict, emb) == ("up", [0.5, 0.5])

    stats = seen.get_stats()
    assert stats["total"] == 1
    assert stats["thumbs_up"] == 1
    assert stats["top_domains"][0][0] == "reuters.com"
