"""
Measures summary faithfulness with an LLM judge: does a digest summary claim
anything the source article doesn't support?

Works off the archive (real delivered summaries), re-scraping each source
URL and asking Gemini to compare. Articles that no longer scrape are skipped.

Run:  python evals/eval_faithfulness.py [N]     (default: 10 latest articles)
"""

import os
import sys
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import gemini
import scrape
import seen

JUDGE_PROMPT = """You are checking a news summary for faithfulness.

Article content:
{content}

Summary to check:
{summary}

Does the summary contain any claim that is NOT supported by the article
content? Ignore the "Why it matters" line's interpretive framing — flag it
only if it states unsupported facts. Reply with exactly one word, FAITHFUL,
if every claim is supported. Otherwise reply UNFAITHFUL followed by the
unsupported claims, one per line.
"""


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    conn = sqlite3.connect(seen.DB_PATH)
    rows = conn.execute(
        "SELECT title, url, summary FROM archive "
        "WHERE summary != '' ORDER BY id DESC LIMIT ?", (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        print("Archive is empty — request some news first (/news), then rerun.")
        return

    faithful = unfaithful = skipped = 0
    for title, url, summary in rows:
        candidate = scrape.fetch_article({"url": url, "title": title, "snippet": ""})
        if candidate["content_source"] != "scraped":
            skipped += 1
            print(f"  skip (won't scrape): {title[:60]}")
            continue

        verdict = gemini.generate(
            JUDGE_PROMPT.format(content=candidate["content"], summary=summary)
        )
        if verdict.strip().upper().startswith("FAITHFUL"):
            faithful += 1
            print(f"  FAITHFUL   {title[:60]}")
        else:
            unfaithful += 1
            print(f"  UNFAITHFUL {title[:60]}")
            print(f"             {verdict[:300]}")

    judged = faithful + unfaithful
    if judged:
        print(f"\nFaithful: {faithful}/{judged} = {faithful / judged:.0%} "
              f"({skipped} skipped, could not re-scrape)")


if __name__ == "__main__":
    main()
