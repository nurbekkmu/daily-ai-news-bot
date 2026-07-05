"""
Shared Gemini access with API-key rotation.

Free-tier rate limits are per Google Cloud project, so the bot accepts
several keys (GEMINI_API_KEYS, comma-separated) and round-robins between
them on every request. When a key is rate-limited we skip straight to the
next one instead of sleeping.

Used by summarize.py (text generation) and semantic.py (embeddings).
"""

import time
import logging

from google import genai

import config

logger = logging.getLogger(__name__)

# One client per API key, created lazily; _key_cursor round-robins over the
# configured keys so requests are spread evenly and per-key rate limits are
# hit as late as possible.
_clients: dict[str, genai.Client] = {}
_key_cursor = 0


def _next_client() -> tuple[genai.Client, int]:
    """Return the next client in the round-robin rotation and its key index."""
    global _key_cursor
    keys = config.GEMINI_API_KEYS
    if not keys:
        raise RuntimeError(
            "No Gemini API key set — configure GEMINI_API_KEYS (comma-separated) "
            "or GEMINI_API_KEY (check your .env / GitHub secret)."
        )
    key_idx = _key_cursor % len(keys)
    _key_cursor += 1
    key = keys[key_idx]
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key], key_idx


def _is_rate_limit_error(err: Exception) -> bool:
    msg = str(err)
    return (
        "429" in msg
        or "RESOURCE_EXHAUSTED" in msg
        or "quota" in msg.lower()
        or "rate limit" in msg.lower()
    )


def _with_rotation(what: str, call):
    """Run `call(client)` with retries, rotating keys on rate limits."""
    keys = config.GEMINI_API_KEYS
    max_attempts = max(config.MAX_RETRIES, len(keys))
    last_err = None
    for attempt in range(1, max_attempts + 1):
        client, key_idx = _next_client()
        try:
            return call(client)
        except Exception as e:  # noqa: BLE001 - retry on rate limits/transient errors
            last_err = e
            logger.warning(
                "Gemini %s failed on key #%d (attempt %d/%d): %s",
                what, key_idx + 1, attempt, max_attempts, e,
            )
            if attempt < max_attempts:
                if _is_rate_limit_error(e) and len(keys) > 1:
                    # Rate-limited: next attempt already uses the next key — no need to wait
                    continue
                time.sleep(config.RETRY_BACKOFF_SECONDS * attempt)
    raise last_err


def generate(prompt: str) -> str:
    """Generate text for a prompt. Returns the stripped response text."""
    def call(client):
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
        )
        return (response.text or "").strip()

    return _with_rotation("generation", call)


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input text."""
    def call(client):
        result = client.models.embed_content(
            model=config.EMBEDDING_MODEL,
            contents=texts,
        )
        return [e.values for e in result.embeddings]

    return _with_rotation("embedding", call)
