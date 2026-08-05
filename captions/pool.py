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
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Optional

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


def _generate(client, prompt: str, schema: dict, model: Optional[str] = None) -> dict:
    from google.genai import types

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
        raise CaptionError(f"Gemini returned unparseable JSON: {text[:200]}") from exc


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


def generate_captions(theme: str, count: int, model: Optional[str] = None,
                      progress=None) -> list[str]:
    """Generate `count` distinct captions on `theme`."""
    client = _client()
    captions: list[str] = []
    seen: set[str] = set()
    chunk_no = 0

    while len(captions) < count:
        angle = _ANGLES[chunk_no % len(_ANGLES)]
        want = min(CHUNK_SIZE, count - len(captions))
        prompt = (
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
            f"- These must be distinct from ordinary phrasing you would repeat; "
            f"set {chunk_no + 1} should not echo earlier sets."
        )
        data = _generate(client, prompt, CAPTION_SCHEMA, model)
        fresh = 0
        for raw in data.get("captions") or []:
            clean = _clean_caption(raw)
            key = clean.lower()
            if clean and key not in seen:
                seen.add(key)
                captions.append(clean)
                fresh += 1
        chunk_no += 1
        if progress:
            progress(len(captions), count)
        logger.info("Caption pool: %d/%d (chunk %d added %d)",
                    len(captions), count, chunk_no, fresh)
        if fresh == 0:
            # The model has stopped producing anything new; better a smaller
            # honest pool than an infinite loop.
            logger.warning("Caption generation stopped early at %d — the model "
                           "returned nothing new.", len(captions))
            break

    return captions[:count]


def generate_hashtag_sets(theme: str, count: int, model: Optional[str] = None,
                          progress=None) -> list[str]:
    """Generate `count` distinct hashtag sets on `theme`."""
    client = _client()
    sets: list[str] = []
    seen: set[str] = set()
    chunk_no = 0

    while len(sets) < count:
        want = min(CHUNK_SIZE, count - len(sets))
        prompt = (
            f"Write {want} hashtag sets for vertical marketing videos "
            f"(TikTok, Instagram Reels, YouTube Shorts).\n\n"
            f"Theme: {theme}\n\n"
            "Rules:\n"
            "- Each set is 5 to 8 hashtags, space separated, each starting '#'.\n"
            "- Mix broad reach tags with narrower niche ones.\n"
            "- Letters, numbers and underscores only — no emoji, no punctuation.\n"
            "- No duplicate tags within a set.\n"
            f"- Set group {chunk_no + 1}: vary the mix from earlier groups."
        )
        data = _generate(client, prompt, HASHTAG_SCHEMA, model)
        fresh = 0
        for raw in data.get("hashtag_sets") or []:
            clean = _clean_hashtags(raw)
            key = clean.lower()
            if clean and key not in seen:
                seen.add(key)
                sets.append(clean)
                fresh += 1
        chunk_no += 1
        if progress:
            progress(len(sets), count)
        logger.info("Hashtag pool: %d/%d (chunk %d added %d)",
                    len(sets), count, chunk_no, fresh)
        if fresh == 0:
            logger.warning("Hashtag generation stopped early at %d.", len(sets))
            break

    return sets[:count]


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
