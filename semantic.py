"""
Semantic (embedding-based) deduplication of news candidates.

Exact-URL dedup can't see that Reuters and TechCrunch are covering the same
OpenAI announcement — those are different URLs, so both used to arrive in
the digest. This module embeds each candidate's title + snippet with Gemini,
compares cosine similarity, and keeps one article per story cluster,
preferring trusted outlets.

Runs BEFORE scraping (on title + snippet), so duplicates are dropped before
we spend time fetching them.

Fail-open by design: if the embedding call errors out, all candidates pass
through and the digest still goes out — a duplicate story is a better
failure mode than no news at all.
"""

import logging
import math

import config
import gemini

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return dot / norm if norm else 0.0


def _priority(candidate: dict) -> int:
    """Lower = keep first. Trusted outlets win their cluster."""
    return 0 if config.domain_matches(candidate["domain"], config.TRUSTED_DOMAINS) else 1


def dedupe(candidates: list[dict]) -> list[dict]:
    """Drop candidates that are semantically the same story as one we're
    already keeping. Comparison is global across topics, since the same
    announcement often surfaces under both the AI and ML queries."""
    if not config.SEMANTIC_DEDUP_ENABLED or len(candidates) < 2:
        return candidates

    texts = [f"{c['title']}. {c.get('snippet', '')}".strip() for c in candidates]
    try:
        vectors = gemini.embed(texts)
    except Exception as e:  # noqa: BLE001 - fail open, see module docstring
        logger.warning("Embedding failed (%s) — skipping semantic dedup this run", e)
        return candidates

    # Greedy clustering: visit candidates best-source-first; keep one only if
    # it isn't too similar to anything already kept.
    order = sorted(range(len(candidates)), key=lambda i: _priority(candidates[i]))
    kept: list[int] = []
    dropped = 0

    for i in order:
        duplicate_of = next(
            (j for j in kept
             if _cosine(vectors[i], vectors[j]) >= config.SEMANTIC_SIM_THRESHOLD),
            None,
        )
        if duplicate_of is None:
            kept.append(i)
        else:
            dropped += 1
            logger.info(
                "Semantic duplicate: '%s' (%s) ~ '%s' (%s)",
                candidates[i]["title"], candidates[i]["domain"],
                candidates[duplicate_of]["title"], candidates[duplicate_of]["domain"],
            )

    if dropped:
        logger.info("Semantic dedup: dropped %d duplicate stories of %d candidates",
                    dropped, len(candidates))

    kept.sort()  # restore original (search-ranking) order
    return [candidates[i] for i in kept]
