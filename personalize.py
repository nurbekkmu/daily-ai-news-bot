"""
Feedback-driven ranking.

Every digest message carries 👍/👎 buttons; reactions land in the feedback
table alongside the article's embedding. Future candidates are scored by
how close their embedding sits to the liked-articles centroid, minus how
close it sits to the disliked centroid:

    preference = cos(candidate, liked_centroid) - cos(candidate, disliked_centroid)

The score is a tie-breaker within the existing ranking (trusted outlets
still come first) — feedback nudges which stories win, it doesn't override
source quality. With no feedback recorded yet, every score is 0 and the
ranking is exactly what it was before this feature existed.
"""

import logging

import config
import seen
from semantic import _cosine

logger = logging.getLogger(__name__)


def _centroid(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    return [sum(v[i] for v in vectors) / n for i in range(len(vectors[0]))]


def attach_scores(candidates: list[dict]) -> None:
    """Set c['_pref'] on every candidate. Zero when personalization is off,
    there's no feedback yet, or the candidate has no embedding."""
    for c in candidates:
        c["_pref"] = 0.0

    if not config.PERSONALIZATION_ENABLED:
        return

    reactions = seen.get_feedback_embeddings()
    liked = [emb for verdict, emb in reactions if verdict == "up"]
    disliked = [emb for verdict, emb in reactions if verdict == "down"]
    if not liked and not disliked:
        return

    liked_c = _centroid(liked) if liked else None
    disliked_c = _centroid(disliked) if disliked else None

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
