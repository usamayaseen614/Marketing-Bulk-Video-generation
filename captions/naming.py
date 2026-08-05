"""
captions/naming.py — the two filenames each video is published under.

Every video goes to Drive twice under different names:

  SHORT  caption + exactly ONE hashtag
  LONG   caption + up to FIVE hashtags

Both are capped at **90 characters of name** — the caption and its hashtags.
The ".mp4" is outside that count, so the cap applies to the text you actually
paste as a caption rather than to an implementation detail of the file.

The names are deliberately *paste-ready*: spaces and '#' are preserved so the
filename reads as the caption you will actually post. Only characters that a
filesystem or Drive genuinely rejects are removed. That is the whole point of
naming from the caption — if you have to retype it, nothing was saved.

There is no numeric row prefix. Uniqueness is handled by appending a counter
only when two names actually collide, and that suffix is budgeted inside the
90-character cap rather than pushing past it.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

EXTENSION = ".mp4"

# The hard rule is on the NAME — caption plus hashtags — not on the filename.
# ".mp4" sits outside it, so what you paste as a caption is what is capped.
MAX_STEM = 90

# Full-filename equivalents, used by the length checks below. Both forms share
# the same limit: a long name is "longer" by carrying more hashtags, not by
# being allowed more characters.
MAX_SHORT = MAX_STEM + len(EXTENSION)
MAX_LONG = MAX_STEM + len(EXTENSION)

# Hashtags per name. The short form carries exactly one — the tag doing the
# reach work. The long form carries up to five: enough to matter, few enough
# that the caption is still readable in a file listing.
MAX_SHORT_HASHTAGS = 1
MAX_LONG_HASHTAGS = 5

# Characters no Windows/Drive filename may contain, plus control characters.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Emoji and pictographs, stripped by default. They are legal in filenames on
# NTFS, APFS, ext4 and Drive, but they are not wanted in these captions, so
# they go — here as a safety net, and at generation time in captions/pool.py so
# the stored caption text is clean too.
# Deliberately does NOT cover General Punctuation (U+2000-206F), so typographic
# characters a caption legitimately uses — em dashes, curly quotes — survive.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoticons, pictographs, symbols, transport
    "\U00002600-\U000027BF"   # misc symbols and dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U00002B00-\U00002BFF"   # misc symbols and arrows
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0000200D"              # zero-width joiner (compound emoji)
    "]+",
    flags=re.UNICODE,
)


def _keep_emoji_default() -> bool:
    try:
        import config
        return bool(config.FILENAME_KEEP_EMOJI)
    except Exception:  # noqa: BLE001 — naming must work with no config present
        return True


def strip_emoji(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI.sub("", str(text or ""))).strip()
# Windows refuses these as whole names regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def clean_text(text) -> str:
    """Strip what a filesystem rejects, keep what a human reads."""
    text = _ILLEGAL.sub("", str(text or ""))
    # Collapse whitespace (including newlines pasted in from a sheet).
    return re.sub(r"\s+", " ", text).strip()


def clean_caption(text) -> str:
    """Caption text with any hashtags removed.

    The short name must carry **exactly one** '#'. Hashtags live in their own
    column, but a caption typed by hand can easily contain one too — and then
    the short name would silently end up with two. They are stripped here so
    the hashtag count in a name is always exactly the number of tags supplied,
    which is what makes the rule enforceable."""
    text = clean_text(text)
    text = re.sub(r"\s*#\w+", "", text)
    # A bare '#' with no word after it would still count against the rule.
    return re.sub(r"\s+", " ", text.replace("#", "")).strip()


def parse_hashtags(value) -> list[str]:
    """'#a #b, c' -> ['#a', '#b', '#c']. Order is preserved; duplicates go."""
    tags: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"#?([A-Za-z0-9_]+)", str(value or "")):
        low = raw.lower()
        if low and low not in seen:
            seen.add(low)
            tags.append(f"#{raw}")
    return tags


def _truncate_words(text: str, budget: int) -> str:
    """Trim to `budget` characters, preferring a word boundary.

    Cutting mid-word produces names like 'Your skin will tha', which reads as a
    mistake. Falling back to a hard cut matters for captions with no spaces at
    all (a single long word, or a language that doesn't use them)."""
    if budget <= 0:
        return ""
    text = text.strip()
    if len(text) <= budget:
        return text
    cut = text[:budget]
    space = cut.rfind(" ")
    # Only honour the word boundary if it keeps a reasonable amount of text —
    # otherwise a long first word would leave almost nothing.
    if space >= max(8, budget // 2):
        cut = cut[:space]
    return cut.rstrip(" -_")


def _finalize(stem: str, ext: str = EXTENSION) -> str:
    """Apply the rules that bite at the very end of a name."""
    # Windows rejects trailing dots and spaces on the name itself.
    stem = stem.strip().rstrip(". ")
    if not stem:
        stem = "video"
    if stem.upper() in _RESERVED:
        stem = f"{stem}_"
    return f"{stem}{ext}"


def build_names(caption, hashtags, ext: str = EXTENSION,
                max_short: int = MAX_SHORT,
                max_long: int = MAX_LONG,
                keep_emoji: Optional[bool] = None,
                max_long_tags: int = MAX_LONG_HASHTAGS) -> tuple[str, str]:
    """Return (short_name, long_name) for one video.

    The short name keeps its single hashtag intact and sacrifices caption text
    to stay inside the cap — the hashtag carries the reach, so truncating it
    would be the wrong trade."""
    caption = clean_caption(caption)
    if not (_keep_emoji_default() if keep_emoji is None else keep_emoji):
        caption = strip_emoji(caption)
    tags = parse_hashtags(hashtags)

    # ---- short: caption + exactly one hashtag, <= max_short INCLUDING ext
    first = tags[0] if tags else ""
    if first:
        # budget = cap - extension - the space before the hashtag - the hashtag
        budget = max_short - len(ext) - 1 - len(first)
        if budget < 8:
            # Pathological: a hashtag so long it crowds out the caption. Keep
            # the caption readable and truncate the tag instead.
            first = first[:max(2, max_short - len(ext) - 1 - 20)]
            budget = max_short - len(ext) - 1 - len(first)
        stem = f"{_truncate_words(caption, budget)} {first}".strip()
    else:
        stem = _truncate_words(caption, max_short - len(ext))

    short = _finalize(stem, ext)
    # Belt and braces: the cap is a hard rule, so enforce it on the result.
    if len(short) > max_short:
        short = _finalize(short[: max_short - len(ext)].rstrip(), ext)

    # ---- long: caption + up to max_long_tags hashtags
    tags = tags[:max(0, int(max_long_tags))]
    tail = " ".join(tags)
    if tail:
        budget = max_long - len(ext) - 1 - len(tail)
        if budget < 8:
            # Too many hashtags to fit any caption — drop tags from the end.
            while tags and budget < 8:
                tags.pop()
                tail = " ".join(tags)
                budget = max_long - len(ext) - 1 - len(tail)
        long_stem = f"{_truncate_words(caption, budget)} {tail}".strip()
    else:
        long_stem = _truncate_words(caption, max_long - len(ext))

    long_name = _finalize(long_stem, ext)
    if len(long_name) > max_long:
        long_name = _finalize(long_name[: max_long - len(ext)].rstrip(), ext)

    return short, long_name


def _with_suffix(name: str, n: int, cap: int, ext: str = EXTENSION) -> str:
    """Add a ' (n)' disambiguator, making room for it inside `cap`."""
    suffix = f" ({n})"
    stem = name[: -len(ext)] if name.endswith(ext) else name
    room = cap - len(ext) - len(suffix)
    return _finalize(stem[:room].rstrip() + suffix, ext)


def dedupe(names: Iterable[str], cap: int = MAX_SHORT,
           ext: str = EXTENSION) -> list[str]:
    """Make a list of names unique, returned parallel to the input.

    Drive will happily store a dozen files with the same name, so uniqueness is
    enforced here. The counter is budgeted *inside* the cap, because a name that
    grew to 104 characters to avoid a clash would break the strict rule the
    short name exists to satisfy."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        key = name.lower()
        if key not in seen:
            seen[key] = 1
            out.append(name)
            continue
        # Keep bumping until the suffixed name is genuinely unused.
        n = seen[key] + 1
        candidate = _with_suffix(name, n, cap, ext)
        while candidate.lower() in seen:
            n += 1
            candidate = _with_suffix(name, n, cap, ext)
        seen[key] = n
        seen[candidate.lower()] = 1
        out.append(candidate)
    return out


def names_for_rows(rows: Iterable[tuple], ext: str = EXTENSION) -> list[tuple[str, str]]:
    """Build and de-duplicate both names for a whole batch at once.

    `rows` yields (caption, hashtags) pairs. Both name lists are de-duplicated
    independently, since two rows sharing a caption but differing in hashtags
    collide on the short name and not the long one."""
    pairs = [build_names(caption, tags, ext=ext) for caption, tags in rows]
    shorts = dedupe([p[0] for p in pairs], cap=MAX_SHORT, ext=ext)
    longs = dedupe([p[1] for p in pairs], cap=MAX_LONG, ext=ext)
    return list(zip(shorts, longs))
