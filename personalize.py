"""
Feedback-driven ranking.

Every digest message carries 👍/👎 buttons; reactions land in the feedback
table alongside the article's embedding. Future candidates are scored by
how close their embedding sits to the liked-articles centroid, minus how
close it sits to the disliked centroid:

    preference = cos(candidate, liked_centroid) - cos(candidate, disliked_centroid)

Centroids are recency-weighted: a reaction's influence halves every
PERSONALIZE_HALF_LIFE_DAYS, so first-week taste doesn't dominate forever.

The score is a tie-breaker within the existing ranking (trusted outlets
still come first) — feedback nudges which stories win, it doesn't override
source quality. With no feedback recorded yet, every score is 0 and the
ranking is exactly what it was before this feature existed.
"""

import logging
from datetime import datetime

import config
import seen
from semantic import _cosine

logger = logging.getLogger(__name__)


def _decay_weight(age_days: float) -> float:
    """Exponential decay: weight halves every PERSONALIZE_HALF_LIFE_DAYS."""
    return 0.5 ** (max(age_days, 0.0) / config.PERSONALIZE_HALF_LIFE_DAYS)


def _age_days(date_str: str) -> float:
    try:
        then = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        return (datetime.now() - then).total_seconds() / 86400
    except ValueError:
        return 0.0  # unparseable date -> treat as fresh


def _weighted_centroid(vectors: list[list[float]], weights: list[float]) -> list[float] | None:
    total = sum(weights)
    if not total:
        return None
    return [
        sum(w * v[i] for v, w in zip(vectors, weights)) / total
        for i in range(len(vectors[0]))
    ]


def attach_scores(candidates: list[dict]) -> None:
    """Set c['_pref'] on every candidate. Zero when personalization is off,
    there's no feedback yet, or the candidate has no embedding."""
    for c in candidates:
        c["_pref"] = 0.0

    if not config.PERSONALIZATION_ENABLED:
        return

    reactions = seen.get_feedback_embeddings()
    liked = [(emb, _decay_weight(_age_days(date)))
             for verdict, emb, date in reactions if verdict == "up"]
    disliked = [(emb, _decay_weight(_age_days(date)))
                for verdict, emb, date in reactions if verdict == "down"]
    if not liked and not disliked:
        return

    liked_c = _weighted_centroid(*zip(*liked)) if liked else None
    disliked_c = _weighted_centroid(*zip(*disliked)) if disliked else None

    scored = 0
    for c in candidates:
        vec = c.get("_embedding")
        if not vec:
            continue
        score = 0.0
        if liked_c:
            score += _cosine(vec, liked_c)
        if disliked_c:
            score -= _cosine(vec, disliked_c)
        c["_pref"] = score
        scored += 1

    logger.info(
        "Personalization: scored %d/%d candidates from %d reactions (%d up, %d down)",
        scored, len(candidates), len(reactions), len(liked), len(disliked),
    )
