"""
packing.py — the ZIPs that get published to Drive.

A render's output used to be uploaded a file at a time: 16,000 videos became
32,000 Drive files, which is slow to browse, slow to hand to anyone, and slow
to upload (every file costs its own resumable session). Each output folder is
published as two archives instead — `yt.zip` holding the short single-hashtag
names, `tk.zip` the long all-hashtags ones.

The one thing this costs is the server-side copy. Uploading a video once and
letting Drive clone it with `files.copy` only works while the two names are two
*files*; inside an archive they are two entries in two different ZIPs, so the
bytes have to cross the network twice. That is the trade being made here, and
it is deliberate: roughly double the upload in exchange for two archives per
folder instead of two thousand files.

Everything is ZIP_STORED. MP4s are already compressed, so deflating them burns
hours of CPU to save a percent or two, and a stored archive is written at disk
speed.

Entries are added one at a time rather than through a single
`shutil.make_archive`, because callers need to interleave. The repack tool
downloads a video, adds it, and deletes it again — so its peak disk usage is
the archive plus a handful of videos, not a second copy of everything.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

# See the module docstring: MP4s do not deflate.
COMPRESSION = zipfile.ZIP_STORED


class PackError(RuntimeError):
    """A ZIP did not come out the way it was asked for."""


def _numbered(name: str, n: int) -> str:
    """`clip.mp4` -> `clip_2.mp4`, keeping the extension where it belongs."""
    stem, dot, ext = name.rpartition(".")
    return f"{stem}_{n}.{ext}" if dot else f"{name}_{n}"


class ZipWriter:
    """A ZIP built one entry at a time, so sources can be freed as they land.

    Written to `<name>.part` and renamed into place on close. A ZIP's central
    directory is only written when the archive is closed, so a half-built one
    is not a smaller archive — it is an unreadable file. Building under `.part`
    means an interrupted run can never leave behind something that *looks*
    finished, either to a resumed run or to whoever downloads it.
    """

    def __init__(self, path: Path, *, dedupe: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._part = self.path.with_name(self.path.name + ".part")
        self._zf = zipfile.ZipFile(self._part, "w", COMPRESSION, allowZip64=True)
        self._dedupe = dedupe
        self._names: set[str] = set()
        self.files = 0
        self.bytes = 0
        self.missing: list[str] = []
        self.closed = False

    # -- entries

    def _unique(self, name: str) -> str:
        """A ZIP will happily hold two entries with the same name, and most
        extractors then silently overwrite one with the other. Same rule as
        drive.unique_names(), for the same reason."""
        if not self._dedupe or name not in self._names:
            self._names.add(name)
            return name
        n = 2
        while _numbered(name, n) in self._names:
            n += 1
        renamed = _numbered(name, n)
        self._names.add(renamed)
        return renamed

    def add(self, src: Path, arcname: Optional[str] = None, *,
            delete_source: bool = False) -> Optional[str]:
        """Add one file. Returns the name it went in under, or None if the
        source was missing — a single absent video should not throw away an
        archive of a thousand that are present."""
        src = Path(src)
        name = str(arcname or src.name)
        if not src.is_file():
            self.missing.append(name)
            return None
        stored = self._unique(name)
        size = src.stat().st_size
        self._zf.write(src, stored)
        self.files += 1
        self.bytes += size
        if delete_source:
            self._free(src)
        return stored

    @staticmethod
    def _free(src: Path) -> None:
        try:
            src.unlink()
        except OSError:  # noqa: BLE001 — a file we cannot delete is litter, not a failure
            logger.warning("Could not delete %s after packing it", src)

    # -- lifecycle

    def close(self) -> Path:
        if self.closed:
            return self.path
        self._zf.close()
        self.closed = True
        self._part.replace(self.path)
        return self.path

    def abort(self) -> None:
        """Throw the part-file away. Called when the build raised, so a failed
        attempt leaves no debris for the next one to trip over."""
        try:
            self._zf.close()
        except Exception:  # noqa: BLE001 — already failing; this must not mask it
            pass
        self.closed = True
        try:
            self._part.unlink()
        except OSError:
            pass

    def __enter__(self) -> "ZipWriter":
        return self

    def __exit__(self, exc_type, _exc, _tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


def verify(path: Path, expected_files: Optional[int] = None,
           expected_bytes: Optional[int] = None, deep: bool = False) -> dict:
    """Re-open a finished ZIP and check it says what it should.

    Reading the central directory is cheap — it is a few kilobytes at the end
    of the file — and it proves the archive was closed properly and holds the
    entries we think it does. `deep` additionally re-reads every byte to check
    each CRC, which for a 30 GB archive means a 30 GB read, so it is opt-in.

    Returns {files, bytes, size}."""
    path = Path(path)
    if not path.is_file():
        raise PackError(f"ZIP not found: {path}")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if deep:
                bad = zf.testzip()
                if bad:
                    raise PackError(f"{path.name}: entry {bad!r} failed its CRC check")
    except zipfile.BadZipFile as exc:
        raise PackError(f"{path.name} is not a readable ZIP: {exc}") from exc

    report = {"files": len(infos), "bytes": sum(i.file_size for i in infos),
              "size": path.stat().st_size}
    if expected_files is not None and report["files"] != expected_files:
        raise PackError(
            f"{path.name} holds {report['files']:,} entries, expected "
            f"{expected_files:,}")
    if expected_bytes is not None and report["bytes"] != expected_bytes:
        raise PackError(
            f"{path.name} holds {report['bytes']:,} bytes of video, expected "
            f"{expected_bytes:,}")
    return report


def build_platform_zips(
    entries: Sequence[tuple[Path, dict[str, str]]],
    outputs: dict[str, Path],
    *,
    delete_sources: bool = False,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Write one set of videos into several archives, under different names.

    `entries` are (source_path, {platform: name_in_that_platform's_zip}) and
    `outputs` maps each platform to the ZIP being built for it. One pass over
    the videos fills both archives, which matters because the alternative —
    building yt.zip and then tk.zip — walks the same 30 GB twice.

    A source is only deleted once it is inside *every* archive, so
    `delete_sources` can never leave a folder half-packed with nothing to
    re-pack from.

    Returns {platforms: {name: {path, files, bytes, size}}, files, missing}."""
    writers = {platform: ZipWriter(Path(path)) for platform, path in outputs.items()}
    total = len(entries)
    written = 0
    missing: list[str] = []

    try:
        for src, names in entries:
            src = Path(src)
            if not src.is_file():
                missing.append(src.name)
                continue
            for platform, writer in writers.items():
                writer.add(src, names.get(platform) or src.name)
            written += 1
            if delete_sources:
                ZipWriter._free(src)
            if on_progress:
                on_progress(written, total)
    except BaseException:
        # Includes KeyboardInterrupt and the worker being killed: a part-file
        # left on a full disk is the last thing a re-run needs.
        for writer in writers.values():
            writer.abort()
        raise

    built = {}
    for platform, writer in writers.items():
        path = writer.close()
        built[platform] = {"path": path, "files": writer.files,
                           "bytes": writer.bytes, "size": path.stat().st_size}
    return {"platforms": built, "files": written, "missing": missing}


def free_files(paths: Iterable[Path]) -> tuple[int, int]:
    """Delete files, returning (count, bytes) actually removed.

    Used to reclaim an output folder's MP4s once its archives are safely in
    Drive. Failures are logged and skipped — reclaiming disk is housekeeping,
    and a file that will not delete is not a reason to fail a job."""
    freed = count = 0
    for path in paths:
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not free %s", path)
            continue
        count += 1
        freed += size
    return count, freed
