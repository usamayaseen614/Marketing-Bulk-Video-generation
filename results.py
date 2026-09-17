"""
results.py — render log and ZIP packaging, built from job-item records.

The interactive path used to build these straight from the in-memory list of
RowResults. A background job can outlive the process that started it, so these
read from the job store instead: the database is the only thing that survives a
worker restart, and it is what a resumed job has.

No streamlit import here — the worker uses this headlessly.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional

from jobs import store


def record_from_row_result(result) -> dict[str, Any]:
    """Normalize a video_generator.RowResult into the shape used below."""
    return {
        "idx": result.row_number,
        "name": result.filename or "",
        "ok": bool(result.ok),
        "error": result.error,
        "warnings": list(result.warnings or []),
    }


def record_from_item(item: dict) -> dict[str, Any]:
    """Normalize a job_items row into the same shape.

    `rel_path` is where the MP4 actually sits under the job's videos/ folder. A
    multi-batch render writes into `batch_NN/` subfolders, so the filename
    alone is no longer enough to find it — or to lay the ZIP out sensibly."""
    name = item.get("name") or ""
    meta = item.get("meta") or {}
    batch = meta.get("batch")
    rel = f"source_{int(batch):02d}/{name}" if batch and name else name
    return {
        "idx": item["idx"],
        "name": name,
        "rel_path": rel,
        "ok": item.get("render_status") == store.ITEM_DONE,
        "error": item.get("render_error"),
        "warnings": list(item.get("warnings") or []),
        "upload_status": item.get("upload_status"),
        "upload_error": item.get("upload_error"),
        "drive_link": item.get("drive_link"),
    }


def write_render_log(path: Path, records: Iterable[dict],
                     elapsed: Optional[float] = None,
                     extra_warnings: Iterable[str] = ()) -> Path:
    """Human-readable per-row log — the same format the ZIP has always carried,
    plus Drive upload state when the job uploaded."""
    records = sorted(records, key=lambda r: r["idx"])
    ok = sum(1 for r in records if r["ok"])
    lines = [
        f"Bulk video render log — {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Total rows: {len(records)} | Succeeded: {ok} | Failed: {len(records) - ok}",
    ]
    if elapsed is not None:
        lines.append(f"Elapsed: {elapsed:.0f}s")
    lines.append("-" * 70)

    for warning in extra_warnings:
        lines.append(f"batch warning: {warning}")
    if extra_warnings:
        lines.append("-" * 70)

    for r in records:
        status = "OK    " if r["ok"] else "FAILED"
        lines.append(f"Row {r['idx']:4d}  {status}  {r['name']}")
        for w in r.get("warnings") or []:
            lines.append(f"           warning: {w}")
        if r.get("error"):
            lines.append(f"           error: {r['error']}")
        if r.get("upload_status") == store.ITEM_FAILED:
            lines.append(f"           drive upload failed: {r.get('upload_error')}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def package_zip(zip_path: Path, videos_dir: Path, records: Iterable[dict],
                log_path: Optional[Path] = None,
                extra_files: Iterable[Path] = ()) -> Path:
    """Bundle the rendered MP4s + the render log.

    ZIP_STORED because MP4s are already compressed — recompressing burns
    minutes for roughly no gain. Kept as a fallback for when someone wants the
    files directly rather than through Drive."""
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    videos_dir = Path(videos_dir)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for r in sorted(records, key=lambda r: r["idx"]):
            if not r["ok"] or not r["name"]:
                continue
            # rel_path keeps the batch_NN/ layout both on disk and in the ZIP.
            rel = r.get("rel_path") or r["name"]
            src = videos_dir / rel
            if src.is_file():
                zf.write(src, rel)
        if log_path and Path(log_path).is_file():
            zf.write(log_path, Path(log_path).name)
        for extra in extra_files:
            if Path(extra).is_file():
                zf.write(extra, Path(extra).name)
    return zip_path


def failure_summary(records: Iterable[dict], limit: int = 25) -> list[str]:
    """One line per failed row, for the notification email. Truncated — a batch
    where every row failed for the same reason should not send a 300-line
    email."""
    failed = [r for r in sorted(records, key=lambda r: r["idx"]) if not r["ok"]]
    lines = [f"Row {r['idx']}: {r.get('error') or 'unknown error'}" for r in failed[:limit]]
    if len(failed) > limit:
        lines.append(f"… and {len(failed) - limit} more")
    return lines
