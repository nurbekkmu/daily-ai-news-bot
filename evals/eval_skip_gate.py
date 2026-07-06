"""
Measures the SKIP quality gate: the summarization prompt doubles as a
classifier that rejects non-news (tutorials, homepages, SEO listicles,
off-topic articles). How accurate is it?

Data: evals/data/skip_cases.json — labeled page contents, half genuine
AI/tech news, half things the digest must reject.

Run:  python evals/eval_skip_gate.py     (needs Gemini keys in .env)
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import config
import gemini

DATA = os.path.join(os.path.dirname(__file__), "data", "skip_cases.json")


def main():
    with open(DATA, encoding="utf-8") as f:
        cases = json.load(f)

    print(f"{len(cases)} labeled cases "
          f"({sum(c['is_news'] for c in cases)} news, "
          f"{sum(1 - c['is_news'] for c in cases)} should-skip)")

    correct = 0
    errors = []
    for case in cases:
        prompt = config.SUMMARY_PROMPT_TEMPLATE.format(
            title=case["title"], source=case["source"], content=case["content"],
        )
        response = gemini.generate(prompt)
        skipped = response.strip().upper().startswith("SKIP")
        predicted_news = not skipped
        if predicted_news == bool(case["is_news"]):
            correct += 1
        else:
            errors.append((case, skipped))
        print(f"  {'ok ' if predicted_news == bool(case['is_news']) else 'ERR'} "
              f"[{'news' if case['is_news'] else 'junk'}] {case['title'][:60]}")

    print(f"\nAccuracy: {correct}/{len(cases)} = {correct / len(cases):.0%}")
    for case, skipped in errors:
        kind = ("REJECTED REAL NEWS" if skipped else "LET JUNK THROUGH")
        print(f"  {kind}: {case['title']}")


if __name__ == "__main__":
    main()
