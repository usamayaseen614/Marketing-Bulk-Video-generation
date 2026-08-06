"""
tools/zip_drive_tk.py — turn folders of videos already in Drive into ZIPs.

Renders published before archives existed left every video as its own Drive
file: `batch_NN/tk/*.mp4` and `batch_NN/yt/*.mp4`, tens of thousands of them.
This walks that tree and publishes each `tk/` (or `yt/`) folder as a single
`tk.zip` beside it.

    python tools/zip_drive_tk.py --link <drive folder url>
    python tools/zip_drive_tk.py --link <url> --platform yt

**Nothing in Drive is ever deleted.** The original folder is left exactly as it
was, so the archive is an addition and the videos remain their own backup. If a
zip turns out wrong, delete it by hand and run this again.

## Working within a small disk

The videos have to come down to be zipped, and a night's output is far larger
than the VM. So the unit of work is ONE folder, and inside that folder the unit
is a handful of files:

    download N videos -> append them to the archive -> delete them -> repeat
    close the archive -> upload it -> verify its size -> delete it -> next folder

Peak disk is therefore one folder's archive plus `--concurrency` videos, not
the whole night. Free space is checked before each folder starts, so it stops
with an explanation rather than filling the disk.

## Re-running it

Safe at any point. Finished folders are recorded in a state file and skipped;
a folder whose archive is already in Drive and at least as large as the videos
it should hold is skipped too. An archive that IS re-made replaces the existing
one (files.update) rather than becoming a second file with the same name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import packing  # noqa: E402
from integrations import drive  # noqa: E402

logger = logging.getLogger("zip_drive")

# Room to leave free beyond what a folder needs, so the machine does not hit a
# zero-byte disk while writing the archive's central directory.
_HEADROOM_BYTES = 5 * 1024 ** 3

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------- output

def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:,.1f} {unit}"
        size /= 1024


def duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def say(message: str = "") -> None:
    """Print and flush — output goes to an SSH session watching a job that
    takes hours, so buffered progress is no progress at all."""
    print(message, flush=True)


def _utf8_console() -> None:
    """Filenames here are captions, and captions are not ASCII. A Windows
    console defaults to cp1252 and would abort the run on the first one."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — not every stream supports it
            pass


# --------------------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"folders": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt state file means "start over", not "crash"
        logger.warning("Could not read %s — starting with an empty state", path)
        return {"folders": {}}
    data.setdefault("folders", {})
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- discovery

def find_targets(root_id: str, root_name: str, platform: str,
                 max_depth: int = 3) -> list[dict]:
    """Every folder in the tree holding a `<platform>` sub-folder.

    Written to accept whatever link is to hand: point it at one `batch_07`, at
    a whole job's folder, or at the `renders/<date>/` root above several jobs,
    and the same walk finds the same work. Descent stops at a match, so a
    `tk/` folder is never itself searched for another `tk/`."""
    targets: list[dict] = []
    stack: list[tuple[str, str, int]] = [(root_id, root_name, 0)]
    seen = {root_id}

    while stack:
        folder_id, path, depth = stack.pop()
        children = drive.list_files(folder_id, include_folders=True)
        subfolders = [c for c in children if c["folder"]]
        match = next((c for c in subfolders if c["name"] == platform), None)
        if match:
            targets.append({"path": path, "parent_id": folder_id,
                            "source_id": match["id"]})
            continue
        if depth < max_depth:
            for child in subfolders:
                if child["id"] not in seen:
                    seen.add(child["id"])
                    stack.append((child["id"], f"{path}/{child['name']}", depth + 1))

    targets.sort(key=lambda t: t["path"])
    if targets or root_name != platform:
        return targets

    # The link points AT the folder to archive — pasting the tk/ folder's own
    # URL is at least as likely as pasting the one above it. Its parent is
    # where the archive belongs.
    parent = _parent_of(root_id)
    if parent:
        return [{"path": parent["name"], "parent_id": parent["id"],
                 "source_id": root_id}]
    return []


def _parent_of(folder_id: str) -> Optional[dict]:
    try:
        meta = drive.service().files().get(
            fileId=folder_id, fields="parents", supportsAllDrives=True).execute()
        parents = meta.get("parents") or []
        if not parents:
            return None
        info = drive.service().files().get(
            fileId=parents[0], fields="id, name", supportsAllDrives=True).execute()
        return {"id": info["id"], "name": info.get("name") or info["id"]}
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the parent of %s", folder_id, exc_info=True)
        return None


def slug(path: str) -> str:
    return _SLUG.sub("_", path).strip("_") or "folder"


# --------------------------------------------------------------------------- one folder

def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(entry: dict, dest: Path, verify: str) -> tuple[dict, Path, Optional[str]]:
    """Download one video and prove it arrived whole."""
    try:
        drive.download_file(entry["id"], dest, entry["size"])
        landed = dest.stat().st_size
        if entry["size"] and landed != entry["size"]:
            return entry, dest, (f"downloaded {landed:,} bytes, Drive says "
                                 f"{entry['size']:,}")
        if verify == "md5" and entry.get("md5"):
            got = _md5(dest)
            if got != entry["md5"]:
                return entry, dest, f"md5 {got} != {entry['md5']}"
        return entry, dest, None
    except Exception as exc:  # noqa: BLE001 — reported per file
        return entry, dest, str(exc)


def repack_folder(target: dict, platform: str, args, shared_drive_id: str) -> dict:
    """Download one folder's videos into an archive and publish it.

    Returns {status, files, bytes, zip_id, error}. `status` is one of
    skipped / done / failed / empty."""
    label = f"{target['path']}/{platform}"
    zip_name = args.zip_name or f"{platform}.zip"

    files = [f for f in drive.list_files(target["source_id"]) if not f["folder"]]
    total_bytes = sum(f["size"] for f in files)
    if not files:
        say(f"  {label}: no files — skipped")
        return {"status": "empty", "files": 0, "bytes": 0}

    # An archive already sitting there is only trustworthy if it is at least as
    # big as the videos it should contain: ZIP_STORED adds a couple of hundred
    # bytes per entry and compresses nothing, so a smaller file is a partial
    # upload from an interrupted run and gets replaced.
    existing = drive.find_file(zip_name, target["parent_id"], drive_id=shared_drive_id)
    if existing and not args.force:
        landed = int(existing.get("size") or 0)
        if landed >= total_bytes:
            say(f"  {label}: {zip_name} already there ({human(landed)}) — skipped")
            return {"status": "skipped", "files": len(files),
                    "bytes": landed, "zip_id": existing["id"]}
        say(f"  {label}: existing {zip_name} is only {human(landed)} of "
            f"{human(total_bytes)} — rebuilding it")

    work = Path(args.work_dir) / slug(target["path"]) / platform
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    free = shutil.disk_usage(work).free
    needed = total_bytes + args.concurrency * 200 * 1024 ** 2 + _HEADROOM_BYTES
    if free < needed:
        raise SystemExit(
            f"\nNot enough disk for {label}.\n"
            f"  the archive will be about {human(total_bytes)}\n"
            f"  free on {args.work_dir}: {human(free)}, needed: {human(needed)}\n\n"
            f"Nothing has been changed in Drive. Grow the disk, or point "
            f"--work-dir at one with room."
        )

    say(f"  {label}: {len(files):,} file(s), {human(total_bytes)}")
    zip_path = work / zip_name
    names = drive.local_names(files)
    started = time.time()
    done = moved = 0
    failures: list[str] = []

    with packing.ZipWriter(zip_path) as archive:
        # A chunk at a time, so at most `concurrency` videos are ever on disk
        # at once alongside the growing archive.
        for start in range(0, len(files), args.concurrency):
            chunk = list(zip(files[start:start + args.concurrency],
                             names[start:start + args.concurrency]))
            fetched = []
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = [pool.submit(_fetch, entry, work / name, args.verify)
                           for entry, name in chunk]
                for future in as_completed(futures):
                    fetched.append(future.result())

            # Added in the folder's own order, not the order they happened to
            # arrive, so the archive's listing matches Drive's.
            by_id = {entry["id"]: (entry, dest, error)
                     for entry, dest, error in fetched}
            for entry, _name in chunk:
                _entry, dest, error = by_id[entry["id"]]
                if error:
                    failures.append(f"{entry['name']}: {error}")
                    dest.unlink(missing_ok=True)
                    continue
                # safe_name, not the raw Drive name: Drive permits characters
                # Windows refuses, and an entry nobody can extract is worse
                # than one whose name lost a colon. These names came from the
                # renderer, so in practice it changes nothing.
                archive.add(dest, drive.safe_name(entry["name"]),
                            delete_source=True)
                moved += entry["size"]
            done += len(chunk)

            elapsed = max(0.001, time.time() - started)
            rate = moved / elapsed
            remaining = (total_bytes - moved) / rate if rate else 0
            say(f"    {done:,}/{len(files):,} files  {human(moved)}"
                f"/{human(total_bytes)}  {human(rate)}/s  "
                f"eta {duration(remaining)}")

    if failures and not args.allow_partial:
        shutil.rmtree(work, ignore_errors=True)
        raise SystemExit(
            f"\n{label}: {len(failures)} file(s) could not be downloaded, so "
            f"the archive would be incomplete.\n  "
            + "\n  ".join(failures[:10])
            + f"\n\nNothing was uploaded and nothing in Drive was touched. "
              f"Re-run to try again, or pass --allow-partial to publish an "
              f"archive without them."
        )

    report = packing.verify(zip_path, expected_files=archive.files)
    say(f"    archive {human(report['size'])} with {report['files']:,} entries "
        f"— uploading")

    response = drive.upload_verified(zip_path, target["parent_id"], zip_name,
                                     drive_id=shared_drive_id)
    if args.keep_zip:
        say(f"    kept the local archive at {zip_path}")
    else:
        shutil.rmtree(work, ignore_errors=True)

    say(f"    published {zip_name} in {duration(time.time() - started)}  "
        f"{response.get('webViewLink') or response.get('id')}")
    return {"status": "done", "files": report["files"], "bytes": report["size"],
            "zip_id": response.get("id"), "failed": failures}


# --------------------------------------------------------------------------- entry

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish folders of videos already in Drive as ZIPs. "
                    "Never deletes anything in Drive.")
    parser.add_argument("--link", required=True,
                        help="Drive URL (or id) of the folder to walk. May be "
                             "one batch_NN, a whole job folder, or the "
                             "renders/<date>/ root above several.")
    parser.add_argument("--platform", default="tk",
                        help="Sub-folder name(s) to archive, comma separated. "
                             "Default: tk")
    parser.add_argument("--work-dir", default=str(config.JOBS_ROOT / "_repack"),
                        help="Scratch space for downloads and archives. Must "
                             "have room for one folder at a time.")
    parser.add_argument("--concurrency", type=int,
                        default=config.DRIVE_DOWNLOAD_CONCURRENCY,
                        help="Parallel downloads, and therefore how many "
                             "videos sit on disk at once.")
    parser.add_argument("--only", default="",
                        help="Comma-separated folder names to include "
                             "(e.g. batch_01,batch_02).")
    parser.add_argument("--max-folders", type=int, default=0,
                        help="Stop after this many folders. Use 1 for a first "
                             "run you want to check before committing hours.")
    parser.add_argument("--depth", type=int, default=3,
                        help="How far below --link to look for the folders.")
    parser.add_argument("--zip-name", default="",
                        help="Override the archive name (default <platform>.zip).")
    parser.add_argument("--verify", choices=("size", "md5"), default="size",
                        help="How hard to check each download. md5 is exact "
                             "but re-reads every file.")
    parser.add_argument("--state", default="",
                        help="Progress file (default <work-dir>/state.json).")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild folders that already have an archive.")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Publish an archive even if some videos could not "
                             "be downloaded.")
    parser.add_argument("--keep-zip", action="store_true",
                        help="Keep each archive on disk after uploading it.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be done and stop.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _utf8_console()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The uploader's own chunk logging would drown the progress lines.
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    args.concurrency = max(1, args.concurrency)
    work_root = Path(args.work_dir)
    work_root.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state) if args.state else work_root / "state.json"
    state = load_state(state_path)

    # The archives are written INTO this tree, so the destination rules apply:
    # set_target resolves the enclosing Shared Drive and refuses a My Drive
    # folder up front rather than at upload time, hours in.
    drive.set_target(args.link)
    root_id = drive.extract_id(args.link)
    info = drive.folder_info(root_id)
    shared_drive_id = drive.resolve_target()[0]

    platforms = [p.strip() for p in args.platform.split(",") if p.strip()]
    only = {p.strip() for p in args.only.split(",") if p.strip()}

    say(f"Root: “{info['name']}”  {drive.folder_link(info['id'])}")
    say(f"Archiving: {', '.join(platforms)}")
    say(f"Work dir:  {work_root}  ({human(shutil.disk_usage(work_root).free)} free)")
    say("")

    plan: list[tuple[dict, str]] = []
    for platform in platforms:
        targets = find_targets(info["id"], info["name"], platform, args.depth)
        if only:
            targets = [t for t in targets
                       if t["path"].rsplit("/", 1)[-1] in only]
        if not targets:
            say(f"No “{platform}” folders found under this link.")
        for target in targets:
            plan.append((target, platform))

    if not plan:
        say("Nothing to do.")
        return 1

    plan.sort(key=lambda pair: (pair[0]["path"], pair[1]))
    if args.max_folders > 0:
        plan = plan[:args.max_folders]

    say(f"{len(plan)} folder(s) to archive:")
    for target, platform in plan:
        key = f"{target['path']}/{platform}"
        mark = " (already recorded done)" if key in state["folders"] else ""
        say(f"  {key}{mark}")
    say("")

    if args.dry_run:
        # One listing call per folder — cheap, and these are the numbers worth
        # knowing before starting something that runs for hours.
        sizes = []
        for target, platform in plan:
            files = [f for f in drive.list_files(target["source_id"])
                     if not f["folder"]]
            size = sum(f["size"] for f in files)
            sizes.append(size)
            say(f"  {target['path']}/{platform}: {len(files):,} files, "
                f"{human(size)}")
        biggest = max(sizes, default=0)
        free = shutil.disk_usage(work_root).free
        say("")
        say(f"Total to move: {human(sum(sizes))} down, then the same back up.")
        say(f"Peak disk needed: about {human(biggest + _HEADROOM_BYTES)} "
            f"(the largest folder's archive) — {human(free)} free now.")
        if biggest + _HEADROOM_BYTES > free:
            say("That does NOT fit. Grow the disk or point --work-dir "
                "somewhere with room.")
        say("Dry run — nothing was downloaded, uploaded or deleted.")
        return 0

    started = time.time()
    published = skipped = failed = 0
    total_bytes = 0

    for position, (target, platform) in enumerate(plan, start=1):
        key = f"{target['path']}/{platform}"
        say(f"[{position}/{len(plan)}] {key}")
        if key in state["folders"] and not args.force:
            say("  recorded as done in the state file — skipped")
            skipped += 1
            continue
        try:
            result = repack_folder(target, platform, args, shared_drive_id)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            say("\nInterrupted. Nothing in Drive was deleted; re-run to carry on.")
            return 130
        except Exception as exc:  # noqa: BLE001 — one folder must not lose the rest
            logger.exception("Failed on %s", key)
            say(f"  FAILED: {exc}")
            failed += 1
            continue

        if result["status"] in {"done", "skipped"}:
            state["folders"][key] = {
                "platform": platform, "status": result["status"],
                "files": result.get("files", 0), "bytes": result.get("bytes", 0),
                "zip_id": result.get("zip_id"), "finished_at": time.time(),
            }
            save_state(state_path, state)
        if result["status"] == "done":
            published += 1
            total_bytes += result.get("bytes", 0)
        elif result["status"] == "skipped":
            skipped += 1

    say("")
    say(f"Done in {duration(time.time() - started)}: {published} archive(s) "
        f"published ({human(total_bytes)}), {skipped} skipped, {failed} failed.")
    say("Nothing was deleted from Google Drive — the original folders are "
        "untouched.")
    if failed:
        say(f"Re-run the same command to retry the {failed} that failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
