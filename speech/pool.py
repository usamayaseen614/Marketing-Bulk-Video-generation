"""
speech/pool.py — dealing uploaded scripts and voices across a batch.

Deliberately the same shape as `captions/assign.py`: it runs BEFORE rendering,
writes plain columns into the sheet, leaves any cell the user filled in alone,
and returns a summary instead of raising. A missing script pool is not an error
— those rows simply render silent, the way a row with no music renders with no
bed.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

VOICEOVER_COLUMN = "Voiceover"
SCREEN_TEXT_COLUMN = "Screen_Text"
VOICE_COLUMN = "Voiceover_Voice"


def _blank(value) -> bool:
    return value is None or str(value).strip().lower() in {"", "nan", "none"}


def with_item_script(row: pd.Series, meta: Optional[dict]) -> pd.Series:
    """`row` as one particular video narrates it.

    A generated script belongs to a (batch, row) item, not to the sheet row —
    every rendered video gets its own — so it lives on the item's meta and is
    laid over the row here. The voice stage and the renderer BOTH go through
    this, which is what keeps the cache key the stage synthesizes under
    identical to the one the renderer looks up."""
    meta = meta or {}
    if not meta.get("voiceover"):
        return row
    row = row.copy()
    row[VOICEOVER_COLUMN] = meta["voiceover"]
    if meta.get("voice"):
        row[VOICE_COLUMN] = meta["voice"]
    return row


def parse_scripts(data: bytes, filename: str = "") -> list[str]:
    """Read an uploaded script pool into a list of scripts.

    .txt / .md — blank-line-separated paragraphs, falling back to one per line
    when the file has no blank lines at all (the common case for a quick list).
    .xlsx / .csv — the first column, or a 'Voiceover'/'Script' column if present.
    Never raises: an unreadable file yields an empty pool, which the caller
    reports as "no scripts" rather than failing the batch."""
    suffix = Path(filename or "").suffix.lower()
    try:
        if suffix in {".xlsx", ".xlsm", ".csv"}:
            frame = (pd.read_csv(io.BytesIO(data)) if suffix == ".csv"
                     else pd.read_excel(io.BytesIO(data), engine="openpyxl"))
            for name in (VOICEOVER_COLUMN, "Script", "script", "voiceover"):
                if name in frame.columns:
                    column = frame[name]
                    break
            else:
                column = frame.iloc[:, 0] if len(frame.columns) else []
            return [s for s in (str(v).strip() for v in column) if not _blank(s)]

        text = data.decode("utf-8", errors="replace")
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text)]
        if len(blocks) <= 1:
            blocks = [line.strip() for line in text.splitlines()]
        return [re.sub(r"\s+", " ", b) for b in blocks if b.strip()]
    except Exception as exc:                            # noqa: BLE001
        logger.error("Could not read the script pool %r: %s", filename, exc)
        return []


def apply_to_frame(df: pd.DataFrame, scripts: Sequence[str],
                   voices: Optional[Sequence[str]] = None) -> tuple[pd.DataFrame, dict]:
    """Fill blank Voiceover / Voiceover_Voice cells.

    A row with a Screen_Text but no Voiceover is skipped: writing the on-screen
    words yourself and leaving the spoken ones empty is how you ask for captions
    with no narration, and the pool must not override that.

    Scripts are dealt round-robin rather than at random, for the same reason the
    promo mix is stratified: an even spread across a posting queue is the point,
    and a deterministic deal means a re-run reproduces the batch.

    Voices rotate on (global position + how many times this script has been
    used). Both halves are needed, and each alone is wrong:

      * On the row index alone, the two pools correlate whenever their sizes
        share a factor — scripts are dealt round-robin too, so 60 scripts across
        3 voices would hand script 0 the same voice every single time.
      * On the per-script count alone, a sheet where every script is DISTINCT
        leaves that count at 0 for every row, so the whole batch narrates in one
        voice. Measured exactly that way on a four-row sheet.

    Summing them is even in both directions: unique scripts spread across the
    voices by position, and a repeated script walks the list as it recurs."""
    out = df.copy()
    for column in (VOICEOVER_COLUMN, VOICE_COLUMN):
        if column not in out.columns:
            out[column] = ""
        out[column] = out[column].astype(object)

    scripts = [s for s in (str(x).strip() for x in scripts or ()) if s]
    voices = [v for v in (str(x).strip() for x in voices or ()) if v]

    # A row that already says what it wants on screen, and left Voiceover
    # blank, is deliberately SILENT — the pool leaves it alone. Without this
    # there is no way to express "captions, no narration" once a pool is
    # uploaded, because the pool fills every blank cell it can find.
    silent = (out[SCREEN_TEXT_COLUMN].map(lambda v: not _blank(v))
              if SCREEN_TEXT_COLUMN in out.columns else None)
    targets = [i for i in out.index
               if _blank(out.at[i, VOICEOVER_COLUMN])
               and not (silent is not None and bool(silent.at[i]))]
    if scripts and targets:
        for n, index in enumerate(targets):
            out.at[index, VOICEOVER_COLUMN] = scripts[n % len(scripts)]

    applied = len(targets) if scripts else 0
    kept_silent = 0 if silent is None else sum(
        1 for i in out.index
        if bool(silent.at[i]) and _blank(out.at[i, VOICEOVER_COLUMN]))
    if not scripts and targets:
        reason = "No script pool, so those rows render silent."
    elif not targets:
        reason = "Every row already has a Voiceover."
    else:
        reason = ""

    # Voices are assigned to every row carrying a script, including rows whose
    # Voiceover came from the sheet rather than the pool.
    if voices:
        seen: dict[str, int] = {}
        position = 0
        for index in out.index:
            if _blank(out.at[index, VOICEOVER_COLUMN]):
                continue
            script = str(out.at[index, VOICEOVER_COLUMN])
            k = seen.get(script, 0)
            seen[script] = k + 1
            # `position` advances for every narrated row, including ones whose
            # voice was pinned in the sheet, so a pinned row does not shift the
            # rotation of everything after it.
            position += 1
            if not _blank(out.at[index, VOICE_COLUMN]):
                continue
            out.at[index, VOICE_COLUMN] = voices[(position - 1 + k) % len(voices)]

    logger.info("Applied %d pooled script(s) across %d voice(s)", applied, len(voices))
    return out, {"applied": applied, "scripts": len(scripts),
                 "voices": len(voices), "silent_rows": kept_silent,
                 "reason": reason}
