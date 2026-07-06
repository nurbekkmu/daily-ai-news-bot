"""
Measures the semantic dedup: at which cosine-similarity threshold do we
best separate "same story from two outlets" from "two different stories"?

Data: evals/data/dedup_pairs.json — labeled title pairs, half same-story
paraphrases, half hard negatives (same company, different event). Grow it
over time with real pairs from the archive.

Run:  python evals/eval_dedup.py     (needs Gemini keys in .env)
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import config
import gemini
from semantic import _cosine

DATA = os.path.join(os.path.dirname(__file__), "data", "dedup_pairs.json")


def main():
    with open(DATA, encoding="utf-8") as f:
        pairs = json.load(f)

    print(f"{len(pairs)} labeled pairs "
          f"({sum(p['same_story'] for p in pairs)} same-story, "
          f"{sum(1 - p['same_story'] for p in pairs)} different)")

    # One batched call: embed all left sides then all right sides
    texts = [p["a"] for p in pairs] + [p["b"] for p in pairs]
    vectors = gemini.embed(texts)
    n = len(pairs)

    sims = [_cosine(vectors[i], vectors[n + i]) for i in range(n)]

    print(f"\n{'threshold':>9} {'precision':>9} {'recall':>7} {'F1':>6}")
    best = (0.0, None)
    for t in [x / 100 for x in range(50, 96, 5)]:
        tp = sum(1 for s, p in zip(sims, pairs) if s >= t and p["same_story"])
        fp = sum(1 for s, p in zip(sims, pairs) if s >= t and not p["same_story"])
        fn = sum(1 for s, p in zip(sims, pairs) if s < t and p["same_story"])
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        marker = "  <- config" if abs(t - config.SEMANTIC_SIM_THRESHOLD) < 0.001 else ""
        print(f"{t:>9.2f} {precision:>9.2f} {recall:>7.2f} {f1:>6.2f}{marker}")
        if f1 > best[0]:
            best = (f1, t)

    print(f"\nBest F1 = {best[0]:.2f} at threshold {best[1]:.2f} "
          f"(config uses {config.SEMANTIC_SIM_THRESHOLD})")

    # Show the mistakes at the configured threshold — these are what to fix
    t = config.SEMANTIC_SIM_THRESHOLD
    print(f"\nErrors at configured threshold {t}:")
    clean = True
    for s, p in zip(sims, pairs):
        predicted = s >= t
        if predicted != bool(p["same_story"]):
            clean = False
            kind = "FALSE MERGE" if predicted else "MISSED DUP "
            print(f"  {kind} (sim={s:.3f}): '{p['a'][:50]}' vs '{p['b'][:50]}'")
    if clean:
        print("  none")


if __name__ == "__main__":
    main()
