"""
captions/assign.py — attaching pooled captions to a batch.

Runs **before** rendering, writing `Caption` and `Hashtags` columns into the
sheet. That ordering is what removes two steps from the old manual process: the
renderer names each file from its caption, so there is nothing left to rename
afterwards and no reason to pull the videos down locally.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import pandas as pd

from jobs import store

logger = logging.getLogger(__name__)

CAPTION_COLUMN = "Caption"
HASHTAG_COLUMN = "Hashtags"

# Filesystem-hostile characters. Windows is the strictest of the platforms the
# outputs travel through, so its rules win.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_for_filename(text: str, max_length: int = 60) -> str:
    """Reduce a caption to something safe as a filename component.

    Emoji, punctuation and control characters go; the result is truncated well
    inside the 255-character path limit, since the row prefix and extension are
    added afterwards."""
    text = _ILLEGAL.sub("", str(text or ""))
    # Keep letters/digits/underscore (Unicode-aware), spaces and hyphens.
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text.strip())
    text = text[:max_length].strip("_.-")
    if text.upper() in _RESERVED:
        text = f"{text}_"
    return text


def apply_to_frame(df: pd.DataFrame, pool_id: Optional[str] = None) -> tuple[pd.DataFrame, dict]:
    """Add Caption and Hashtags columns to every row that lacks them.

    Rows that already carry a caption are left alone, so a sheet the user wrote
    by hand always wins over the generated pool.

    Returns (frame, summary). If no pool exists the frame comes back unchanged —
    captions are an enhancement, never a reason to fail a batch."""
    pool = store.active_pool() if pool_id is None else None
    if pool_id is not None:
        pools = {p["id"]: p for p in store.list_pools(limit=50)}
        if pool_id not in pools:
            return df, {"applied": 0, "reason": f"No caption pool {pool_id!r}"}
        pool = store.active_pool()

    if not pool:
        return df, {"applied": 0,
                    "reason": "No caption pool has been generated yet."}

    out = df.copy()
    for column in (CAPTION_COLUMN, HASHTAG_COLUMN):
        if column not in out.columns:
            out[column] = ""
        out[column] = out[column].astype(object)

    def _blank(value) -> bool:
        return value is None or str(value).strip().lower() in {"", "nan", "none"}

    targets = [i for i in out.index if _blank(out.at[i, CAPTION_COLUMN])]
    if not targets:
        return out, {"applied": 0, "reason": "Every row already has a caption.",
                     "pool_id": pool["id"]}

    pairs = store.take_combinations(pool["id"], len(targets))
    for index, (caption, hashtags) in zip(targets, pairs):
        out.at[index, CAPTION_COLUMN] = caption
        if _blank(out.at[index, HASHTAG_COLUMN]):
            out.at[index, HASHTAG_COLUMN] = hashtags

    logger.info("Applied %d caption/hashtag pairs from pool %s",
                len(targets), pool["id"])
    return out, {
        "applied": len(targets),
        "pool_id": pool["id"],
        "theme": pool.get("theme"),
        "combinations": pool.get("combinations"),
    }


def write_columns_to_workbook(excel_bytes: bytes, df: pd.DataFrame) -> bytes:
    """Write the Caption and Hashtags columns back into the original workbook,
    preserving its formatting — the sheet stays a real artifact the user can
    open, not a regenerated copy."""
    import io

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(excel_bytes))
    sheet = workbook.worksheets[0]
    headers = {
        str(cell.value).strip(): cell.column
        for cell in sheet[1] if cell.value is not None
    }
    next_column = (max(headers.values()) + 1) if headers else 1

    for column in (CAPTION_COLUMN, HASHTAG_COLUMN):
        if column not in headers:
            sheet.cell(row=1, column=next_column, value=column)
            headers[column] = next_column
            next_column += 1

    for offset, (_, row) in enumerate(df.iterrows(), start=2):
        for column in (CAPTION_COLUMN, HASHTAG_COLUMN):
            value = row.get(column)
            if value is not None and str(value).strip():
                sheet.cell(row=offset, column=headers[column], value=str(value))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
