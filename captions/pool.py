"""
captions/pool.py — periodic caption and hashtag generation with Gemini.

**There is deliberately no per-video model call.** Captions here are generic and
themed, so generating 2,000 genuinely distinct ones a day would be both
expensive and pointless — they would be near-duplicates anyway. Instead a pool
is generated occasionally and recombined per batch:

    2,000 captions x 500 hashtag sets = 1,000,000 unique pairs

At ~2,000 videos a day that is well over a year of output before any pair
repeats, for a handful of model calls. A Pro-tier model is used because this
runs rarely and quality is what carries across every video that reuses it.

Model IDs are configuration, not constants — Google retires them on a schedule.
Checked against Google's docs in August 2026: `gemini-2.5-pro`,
`gemini-2.5-flash` and `gemini-2.5-flash-lite` are the current Vertex AI
generation, with Pro at $1.25/$10 per million input/output tokens. A full
2,000-caption pool costs roughly $2 to generate.

Vertex AI is used rather than the Gemini Developer API so the app keeps using
the credentials it already has — the VM's attached service account — with no
separate key to store or leak.

## Why the chunks run concurrently

A pool is built in chunks of ~100, and every chunk is an independent
single-turn request — nothing is threaded through as conversation history, so
chunk 40 knows nothing about chunk 1 and could not echo it if it tried.
Uniqueness comes from the `seen` set here, not from the model. Running the
chunks one at a time therefore bought nothing at all: 5,000 captions is 50
calls of ~35 seconds, and half an hour of that is a process sitting idle
waiting on HTTP.

They are issued through a bounded pool instead. Bounded rather than all-at-once
because Vertex throttles, and because the shortfall after de-duplication is
only known once the responses are back — so the work naturally comes in rounds:
issue enough chunks to cover what is missing, see what survives dedup, issue
more if short.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Optional

import config

logger = logging.getLogger(__name__)

# Structured output — the model is constrained to this shape, so nothing here
# has to hand-parse prose.
CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "captions": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["captions"],
}

HASHTAG_SCHEMA = {
    "type": "object",
    "properties": {
        "hashtag_sets": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["hashtag_sets"],
}

# One call cannot reliably emit 2,000 varied strings, so the pool is built in
# chunks with a varying angle per chunk to keep them from converging.
CHUNK_SIZE = 100

# A round issues enough chunks to cover the shortfall, then measures what
# survived de-duplication. Normally two or three rounds are enough. The cap
# exists for the pathological case — a model that has run dry and returns a
# trickle of new strings forever — and stopping short is always logged, because
# a pool quietly smaller than asked for is exactly the kind of thing that only
# shows up later as "the caption pool is too small for this job".
MAX_ROUNDS = 8

# HTTP statuses worth trying again. 429 is the one that matters: with chunks in
# flight concurrently it is a normal thing to meet, not an error.
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

_ANGLES = [
    "curiosity gap — imply something the viewer needs to see",
    "direct benefit — say plainly what they get",
    "relatable frustration the product removes",
    "bold claim, confidently stated",
    "question aimed straight at the viewer",
    "before-and-after contrast",
    "urgency and scarcity, without sounding fake",
    "social proof and popularity",
    "insider tip or little-known trick",
    "playful, a bit irreverent",
]


class CaptionError(RuntimeError):
    """Configuration or generation problems worth surfacing verbatim."""


def _client():
    """Vertex AI client via the Google Gen AI SDK."""
    if not config.GCP_PROJECT:
        raise CaptionError(
            "No GCP project set. Set BVG_GCP_PROJECT to the project running "
            "the VM so Vertex AI can be reached with the service account it "
            "already uses."
        )
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover
        raise CaptionError(
            "The Google Gen AI SDK is missing. Install it with:\n"
            "    pip install google-genai"
        ) from exc
    return genai.Client(
        vertexai=True,
        project=config.GCP_PROJECT,
        location=config.VERTEX_LOCATION,
    )


_local = threading.local()


def _thread_client():
    """The Vertex client for the calling thread.

    The SDK's client is believed to be safe to share, but the Drive code
    already keeps a per-thread client because its transport is definitively
    not — and a client is cheap to build. Following the same pattern removes
    the question rather than resting on a belief."""
    existing = getattr(_local, "client", None)
    if existing is None:
        existing = _local.client = _client()
    return existing


def _is_transient(exc: Exception) -> bool:
    """Whether asking again is likely to work.

    A 429 or a 503 is the service saying "not now"; a 403 is it saying "no".
    Retrying the second kind just turns a clear failure into a slow one."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in _RETRY_STATUSES
    text = str(exc).lower()
    return any(word in text for word in
               ("timeout", "timed out", "deadline exceeded", "connection reset",
                "connection aborted", "temporarily unavailable"))


def _generate(client, prompt: str, schema: dict, model: Optional[str] = None,
              attempts: Optional[int] = None) -> dict:
    """One constrained call, retried on the failures that are worth retrying.

    An empty or unparseable response is retried too. With `response_schema`
    set, malformed JSON means the answer was cut off rather than that the model
    misunderstood — so it is a transport-shaped problem, and asking again is
    the right move."""
    from google.genai import types

    attempts = attempts or config.CAPTION_MAX_ATTEMPTS
    for attempt in range(1, max(1, attempts) + 1):
        try:
            response = client.models.generate_content(
                model=model or config.GEMINI_POOL_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=1.0,   # variety is the whole point here
                ),
            )
            text = (response.text or "").strip()
            if not text:
                raise CaptionError("Gemini returned an empty response.")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise CaptionError(
                    f"Gemini returned unparseable JSON: {text[:200]}") from exc
        except Exception as exc:  # noqa: BLE001 — classified immediately below
            retryable = isinstance(exc, CaptionError) or _is_transient(exc)
            if attempt >= attempts or not retryable:
                raise
            # Jittered, so eight throttled chunks don't all come back at once
            # and trip the same limit again in lockstep.
            delay = min(2 ** attempt, 30) * (0.5 + random.random())
            logger.warning("Gemini call failed (attempt %d/%d, retrying in "
                           "%.1fs): %s", attempt, attempts, delay, exc)
            time.sleep(delay)
    raise CaptionError("unreachable")  # pragma: no cover — loop always returns


def _clean_caption(text: str) -> str:
    """Strip the things that make a caption unusable as a filename or a post.

    Emoji are removed here, at the point the caption is stored, rather than
    only when a filename is built — the caption is pasted into the post as
    well, and it should be clean in both places. Models add emoji regardless of
    being told not to, so this is enforced rather than merely requested."""
    from captions.naming import _truncate_words, strip_emoji

    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.strip('"“”\'')
    # Hashtags belong in the hashtag column, not baked into the caption.
    text = re.sub(r"\s*#\w+", "", text)
    text = strip_emoji(text)
    text = re.sub(r"\s+", " ", text).strip()
    # Word-aware: a blunt slice leaves half a word or a dangling space, which
    # then gets stripped later and looks like the caption lost a character.
    return _truncate_words(text, config.CAPTION_MAX_CHARS)


def _clean_hashtags(text: str) -> str:
    """Normalise to a single space-separated '#a #b #c' string."""
    tags = re.findall(r"#?([A-Za-z0-9_]+)", str(text or ""))
    seen, out = set(), []
    for tag in tags:
        low = tag.lower()
        if low and low not in seen:
            seen.add(low)
            out.append(f"#{tag}")
    return " ".join(out[:12])


def _fill(count: int, make_prompt: Callable[[int, int], str], schema: dict,
          key: str, clean: Callable[[str], str], label: str,
          model: Optional[str] = None, progress=None) -> list[str]:
    """Build a list of `count` distinct strings by running chunks concurrently.

    `make_prompt(chunk_no, want)` writes one chunk's prompt; `key` is the field
    to read out of the response; `clean` normalises one string.

    All state — the output list, the `seen` set, the progress callback — is
    touched only on this thread, as futures land. The workers do nothing but
    make a call and hand back raw strings, so there is no shared state to lock
    and no ordering to get wrong."""
    # Built here, on this thread, purely so a misconfiguration (no project, SDK
    # missing) surfaces as itself instead of arriving 50 times over as "every
    # request failed".
    _client()

    out: list[str] = []
    seen: set[str] = set()
    chunk_no = 0
    workers = max(1, config.CAPTION_CONCURRENCY)

    def _one(number: int, want: int) -> list[str]:
        return (_generate(_thread_client(), make_prompt(number, want),
                          schema, model).get(key) or [])

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="caption") as pool:
        for round_no in range(1, MAX_ROUNDS + 1):
            missing = count - len(out)
            if missing <= 0:
                break

            # Enough chunks to cover the shortfall in one go. They queue behind
            # the pool's `workers` slots, so this is pipelined rather than a
            # burst: as one finishes the next starts.
            futures = {}
            for _ in range(math.ceil(missing / CHUNK_SIZE)):
                chunk_no += 1
                want = min(CHUNK_SIZE, missing)
                futures[pool.submit(_one, chunk_no, want)] = chunk_no

            fresh = failures = 0
            first_error: Optional[Exception] = None
            for future in as_completed(futures):
                try:
                    raw_items = future.result()
                except Exception as exc:  # noqa: BLE001 — reported per chunk
                    failures += 1
                    first_error = first_error or exc
                    logger.warning("%s chunk %d gave up: %s",
                                   label, futures[future], exc)
                    continue
                for raw in raw_items:
                    value = clean(raw)
                    marker = value.lower()
                    if value and marker not in seen:
                        seen.add(marker)
                        out.append(value)
                        fresh += 1
                if progress:
                    progress(min(len(out), count), count)

            logger.info("%s: %d/%d after round %d (%d chunk(s), %d new, "
                        "%d failed)", label, len(out), count, round_no,
                        len(futures), fresh, failures)

            if failures == len(futures):
                # Nothing got through at all — a wrong model id, a revoked
                # permission, an exhausted quota. Returning a silently empty
                # pool would hide the one thing worth saying.
                raise CaptionError(
                    f"Every {label} request failed. Last error: {first_error}"
                ) from first_error
            if fresh == 0:
                # The model has stopped producing anything new; better a
                # smaller honest pool than an endless loop.
                logger.warning("%s stopped early at %d of %d — the model "
                               "returned nothing new.", label, len(out), count)
                break
        else:
            if len(out) < count:
                logger.warning(
                    "%s stopped at %d of %d after %d rounds — the model kept "
                    "repeating itself. Using the smaller pool.",
                    label, len(out), count, MAX_ROUNDS)

    return out[:count]


def generate_captions(theme: str, count: int, model: Optional[str] = None,
                      progress=None) -> list[str]:
    """Generate `count` distinct captions on `theme`."""

    def make_prompt(chunk_no: int, want: int) -> str:
        angle = _ANGLES[(chunk_no - 1) % len(_ANGLES)]
        return (
            f"Write {want} short social-media captions for vertical marketing "
            f"videos (TikTok, Instagram Reels, YouTube Shorts).\n\n"
            f"Theme: {theme}\n"
            f"Angle for this set: {angle}\n\n"
            "Rules:\n"
            "- Each caption stands alone; it is not a reply or continuation.\n"
            "- Under 120 characters.\n"
            "- No hashtags — those are handled separately.\n"
            "- No emoji anywhere. Plain text only.\n"
            "- No numbering, quotes, or surrounding punctuation.\n"
            "- Vary sentence shape and length; avoid all of them starting the "
            "same way.\n"
            f"- Set {chunk_no}: reach for phrasing you would not normally "
            "repeat."
            # No "don't echo the earlier sets" instruction: each call is
            # single-turn, so the model has never seen them. The `seen` set in
            # _fill is what actually enforces that, and it always did.
        )

    return _fill(count, make_prompt, CAPTION_SCHEMA, "captions",
                 _clean_caption, "Caption pool", model, progress)


def generate_hashtag_sets(theme: str, count: int, model: Optional[str] = None,
                          progress=None) -> list[str]:
    """Generate `count` distinct hashtag sets on `theme`."""

    def make_prompt(chunk_no: int, want: int) -> str:
        return (
            f"Write {want} hashtag sets for vertical marketing videos "
            f"(TikTok, Instagram Reels, YouTube Shorts).\n\n"
            f"Theme: {theme}\n\n"
            "Rules:\n"
            "- Each set is 5 to 8 hashtags, space separated, each starting '#'.\n"
            "- Mix broad reach tags with narrower niche ones.\n"
            "- Letters, numbers and underscores only — no emoji, no punctuation.\n"
            "- No duplicate tags within a set.\n"
            f"- Set group {chunk_no}: vary the mix."
        )

    return _fill(count, make_prompt, HASHTAG_SCHEMA, "hashtag_sets",
                 _clean_hashtags, "Hashtag pool", model, progress)


def build_pool(theme: Optional[str] = None,
               caption_count: Optional[int] = None,
               hashtag_count: Optional[int] = None,
               model: Optional[str] = None,
               progress=None) -> dict:
    """Generate a full pool and store it as the active one."""
    from jobs import store

    theme = (theme or config.CAPTION_THEME or "").strip()
    if not theme:
        raise CaptionError(
            "No caption theme set. Captions are generic and built around a "
            "theme you choose — set BVG_CAPTION_THEME or pass one in."
        )
    caption_count = caption_count or config.CAPTION_POOL_SIZE
    hashtag_count = hashtag_count or config.HASHTAG_POOL_SIZE
    model = model or config.GEMINI_POOL_MODEL

    captions = generate_captions(theme, caption_count, model, progress)

    # hashtag_count=0 means the hashtags are coming from somewhere else (an
    # uploaded sheet, or none at all). Asking Gemini for sets that will be
    # discarded at naming time is pure waste, so skip the calls entirely and
    # store one empty set to keep the pool's arithmetic valid.
    if hashtag_count > 0:
        hashtags = generate_hashtag_sets(theme, hashtag_count, model, progress)
    else:
        logger.info("Skipping hashtag generation — they come from elsewhere")
        hashtags = [""]

    pool_id = store.save_pool(theme, captions, hashtags, model=model)
    logger.info("Built caption pool %s: %d captions x %d hashtag sets = %s pairs",
                pool_id, len(captions), len(hashtags),
                f"{len(captions) * len(hashtags):,}")
    return {
        "pool_id": pool_id,
        "theme": theme,
        "captions": len(captions),
        "hashtag_sets": len(hashtags),
        "combinations": len(captions) * len(hashtags),
        "model": model,
    }


def check_access() -> tuple[bool, str]:
    """Diagnostic for the setup page."""
    if not config.gemini_configured():
        return False, "No GCP project set — set BVG_GCP_PROJECT."
    try:
        client = _client()
        data = _generate(
            client,
            "Write 2 short social-media captions about a coffee shop.",
            CAPTION_SCHEMA,
        )
        samples = [_clean_caption(c) for c in (data.get("captions") or [])][:2]
        return True, (f"Vertex AI reachable with {config.GEMINI_POOL_MODEL}. "
                      f"Sample: {samples}")
    except CaptionError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "default credentials were not found" in message.lower():
            from integrations.drive import _no_credentials_help
            return False, _no_credentials_help("Vertex AI")
        if "403" in message or "permission" in message.lower():
            return False, (
                "Permission denied. Enable the Vertex AI API on the project and "
                "give the service account the Vertex AI User role.\n\n"
                f"Raw error: {message}")
        return False, f"Vertex AI check failed: {message}"
