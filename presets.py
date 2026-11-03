"""
presets.py — saved job settings and files, so a batch can start from an
earlier one.

Every batch queued from the main page is saved here: the value of every form
field it was queued with, and every file it used. The page lists them newest
first in a dropdown; loading one puts the fields back and stands its files in
for the uploaders, and deleting one removes it for good.

Layout under config.PRESETS_ROOT:

    presets/<id>.json     one per saved job: field values, file list, summary
    blobs/<sha256>        file contents, ONE copy however many saved jobs use it

Files are stored by content because the same promo videos tend to come back
batch after batch. At up to ~18 GB a set, a copy per saved job would fill the
disk long before the count cap (config.PRESETS_MAX) did.

Saved files are COPIED into a job folder when a batch uses them, never
hard-linked: the voiceover stage rewrites the staged input.xlsx in place
(jobs/runners/voice.py), and through a hard link that edit would land in the
saved copy too.

Nothing here imports streamlit, so the tests drive it headlessly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import config

# Read/write granularity. Also how often a progress callback hears about it.
_CHUNK = 8 * 1024 * 1024

# Ids become filenames, so anything else is refused rather than escaped.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PART_PREFIX = ".part-"

# A file no saved job refers to any more is deleted once it has gone this long
# without being written or used. The delay covers the two windows where a blob
# is legitimately unreferenced: a save that has written its files but not yet
# its JSON, and a browser session still using the files of a saved job that
# someone else just deleted (load() refreshes the clock on every file).
GC_GRACE_SECONDS = 3600.0

ProgressFn = Optional[Callable[[int], None]]


# --------------------------------------------------------------------------- files

@dataclass(frozen=True)
class StoredFile:
    """A saved file, shaped like Streamlit's UploadedFile everywhere app.py
    reads one: `.name`, `.size` and `.getvalue()`. `path` is what lets
    workspace.stage_uploads copy it disk-to-disk instead of through memory."""
    name: str
    size: int
    sha256: str
    path: Path

    def getvalue(self) -> bytes:
        return self.path.read_bytes()


@dataclass(frozen=True)
class MemoryFile:
    """Bytes with a filename — for a file the page builds itself, such as the
    sheet with the preview editor's edits written in."""
    name: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    def getvalue(self) -> bytes:
        return self.data


def file_size(f) -> int:
    """Size of an uploaded, stored or in-memory file, without reading it when
    the object already knows."""
    size = getattr(f, "size", None)
    if size is None:
        size = len(f.getvalue())
    return int(size)


def bytes_to_save(files: dict[str, Iterable]) -> int:
    """How many bytes save() will write for these files. Files that came from
    a saved job are already on disk and cost nothing — the number a progress
    bar should be sized to."""
    return sum(file_size(f) for group in files.values() for f in group or []
               if f is not None and not isinstance(f, StoredFile))


# --------------------------------------------------------------------------- paths

def _root() -> Path:
    return Path(config.PRESETS_ROOT)


def _presets_dir() -> Path:
    return _root() / "presets"


def _blobs_dir() -> Path:
    return _root() / "blobs"


def blob_path(sha256: str) -> Path:
    if not _SHA_RE.match(sha256 or ""):
        raise ValueError(f"not a sha256: {sha256!r}")
    return _blobs_dir() / sha256


def _preset_path(preset_id: str) -> Path:
    if not _ID_RE.match(preset_id or ""):
        raise ValueError(f"not a saved-job id: {preset_id!r}")
    return _presets_dir() / f"{preset_id}.json"


def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _touch(path: Path) -> None:
    try:
        os.utime(path)
    except OSError:
        pass


def _write_json(path: Path, data: dict) -> None:
    """Atomic: a reader never sees half a file, and a crash leaves either the
    old version or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{_PART_PREFIX}{uuid.uuid4().hex}.json")
    try:
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- store

def _store_file(f, progress: ProgressFn) -> Optional[dict]:
    """Put one file in the blob store and return its entry for the JSON.

    A StoredFile is already there — it only has its clock refreshed. Anything
    else is streamed to a temporary file while being hashed, then renamed onto
    its content address; if that address already exists the temporary copy is
    dropped, so an identical re-upload costs one write and no disk.

    Returns None for a stored file whose blob has gone missing."""
    name = Path(getattr(f, "name", "") or "file").name
    if isinstance(f, StoredFile):
        if not f.path.is_file():
            return None
        _touch(f.path)
        return {"name": name, "size": int(f.size), "sha256": f.sha256}

    blobs = _blobs_dir()
    blobs.mkdir(parents=True, exist_ok=True)
    tmp = blobs / f"{_PART_PREFIX}{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "wb") as out:
            data = memoryview(f.getvalue())
            for offset in range(0, len(data), _CHUNK):
                piece = data[offset:offset + _CHUNK]
                digest.update(piece)
                out.write(piece)
                size += len(piece)
                if progress:
                    progress(len(piece))
        final = blob_path(digest.hexdigest())
        if final.is_file():
            _touch(final)
        else:
            os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return {"name": name, "size": size, "sha256": digest.hexdigest()}


def _jsonable(values: dict) -> dict:
    """Keep only values that survive a JSON round trip (tuples come back as
    lists, which every widget here accepts). Anything else — an uploaded file
    left in a field by mistake — is dropped rather than failing the save."""
    out: dict[str, Any] = {}
    for key, value in (values or {}).items():
        try:
            out[str(key)] = json.loads(json.dumps(value))
        except (TypeError, ValueError):
            continue
    return out


def save(name: str, values: dict, files: dict[str, Iterable], *,
         summary: Optional[dict] = None, job_id: str = "",
         progress: ProgressFn = None) -> dict:
    """Save one job's fields and files. Returns the saved record, whose
    `missing` lists any reused file that had vanished from disk (and so could
    not be saved again)."""
    stored: dict[str, list[dict]] = {}
    missing: list[str] = []
    for slot, group in (files or {}).items():
        entries = []
        for f in group or []:
            if f is None:
                continue
            entry = _store_file(f, progress)
            if entry is None:
                missing.append(getattr(f, "name", "?"))
            else:
                entries.append(entry)
        if entries:
            stored[str(slot)] = entries

    record = {
        "version": 1,
        "id": _new_id(),
        "name": (name or "").strip() or "Untitled",
        "created_at": time.time(),
        "job_id": job_id,
        "values": _jsonable(values),
        "files": stored,
        "summary": dict(summary or {}),
    }
    record["fingerprint"] = _fingerprint(record)
    _write_json(_preset_path(record["id"]), record)
    # Re-running the same job (Load, then Generate unchanged) replaces its
    # older copy rather than adding another. Otherwise a nightly re-run fills
    # the dropdown with identical entries and pushes distinct ones past the cap.
    for older in list_presets():
        if older["id"] != record["id"] and older.get("fingerprint") == record["fingerprint"]:
            _preset_path(older["id"]).unlink(missing_ok=True)
    prune(config.PRESETS_MAX)
    gc_blobs()
    return {**record, "missing": missing}


def _fingerprint(record: dict) -> str:
    """Same name, same field values, same file contents under the same names
    in the same slots — the same job, whenever it was saved."""
    basis = {"name": record.get("name"), "values": record.get("values"),
             "files": record.get("files")}
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- read

def list_presets() -> list[dict]:
    """Every saved job, newest first. A file that cannot be read is skipped,
    never fatal — one bad JSON must not take the main page down."""
    folder = _presets_dir()
    if not folder.is_dir():
        return []
    out = []
    for path in folder.glob("*.json"):
        if path.name.startswith(_PART_PREFIX):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("id") == path.stem:
            out.append(record)
    out.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
    return out


def get(preset_id: str) -> Optional[dict]:
    try:
        path = _preset_path(preset_id)
    except ValueError:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def load(record: dict) -> tuple[dict[str, list[StoredFile]], list[str]]:
    """A saved job's files as StoredFiles, by slot, plus the names of any whose
    contents are gone. Refreshes each file's clock so the collector leaves it
    alone while this session is using it."""
    files: dict[str, list[StoredFile]] = {}
    missing: list[str] = []
    for slot, entries in (record.get("files") or {}).items():
        for entry in entries or []:
            try:
                path = blob_path(str(entry.get("sha256")))
            except ValueError:
                missing.append(str(entry.get("name")))
                continue
            if not path.is_file():
                missing.append(str(entry.get("name")))
                continue
            _touch(path)
            files.setdefault(slot, []).append(StoredFile(
                name=Path(str(entry.get("name") or "file")).name,
                size=int(entry.get("size") or 0),
                sha256=str(entry["sha256"]),
                path=path,
            ))
    return files, missing


def total_size(record: dict) -> int:
    """Bytes of file content in one saved job (a file used twice counts once)."""
    seen = {}
    for entries in (record.get("files") or {}).values():
        for entry in entries or []:
            seen[entry.get("sha256")] = int(entry.get("size") or 0)
    return sum(seen.values())


def file_count(record: dict) -> int:
    return sum(len(v or []) for v in (record.get("files") or {}).values())


def disk_usage() -> int:
    """Bytes the blob store holds right now, shared files counted once."""
    folder = _blobs_dir()
    if not folder.is_dir():
        return 0
    total = 0
    for path in folder.iterdir():
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


# --------------------------------------------------------------------------- delete

def delete(preset_id: str) -> bool:
    """Delete one saved job, and any file content no other saved job uses
    (subject to GC_GRACE_SECONDS). Returns False when there was nothing to
    delete."""
    try:
        path = _preset_path(preset_id)
    except ValueError:
        return False
    existed = path.is_file()
    path.unlink(missing_ok=True)
    gc_blobs()
    return existed


def prune(keep: int) -> list[str]:
    """Delete all but the `keep` newest saved jobs. 0 keeps everything."""
    if keep <= 0:
        return []
    removed = []
    for record in list_presets()[keep:]:
        try:
            _preset_path(record["id"]).unlink(missing_ok=True)
        except ValueError:
            continue
        removed.append(record["id"])
    return removed


def gc_blobs(grace_seconds: Optional[float] = None) -> list[str]:
    """Delete file content that no saved job refers to and nobody has written
    or used for `grace_seconds`. Also clears temporary files a crashed save
    left behind, on the same clock."""
    grace = GC_GRACE_SECONDS if grace_seconds is None else grace_seconds
    folder = _blobs_dir()
    if not folder.is_dir():
        return []
    referenced = {entry.get("sha256")
                  for record in list_presets()
                  for entries in (record.get("files") or {}).values()
                  for entry in entries or []}
    cutoff = time.time() - grace
    removed = []
    for path in folder.iterdir():
        if path.name in referenced:
            continue
        if not (_SHA_RE.match(path.name) or path.name.startswith(_PART_PREFIX)):
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
        except OSError:
            continue
        removed.append(path.name)
    return removed
