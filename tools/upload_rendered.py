"""
tools/upload_rendered.py — publish a dead job's videos without re-rendering.

A render that dies partway (disk full at 18,000 of 21,000) never reaches its
upload phase, and it cannot simply be requeued: worker.run_job's cleanup
deletes `assets/` on the way out, so run() raises on the missing input.xlsx
before it gets anywhere near Drive. The MP4s under `videos/` are untouched
though, and everything the upload needs — each item's short and long name, its
source batch — is in the job database, not in the deleted sheet.

So this skips straight to the upload phase the job never ran:

    python tools/upload_rendered.py <job_id>
    python tools/upload_rendered.py <job_id> --mode files    # no disk headroom
    python tools/upload_rendered.py <job_id> --platforms tk  # half the traffic

It reuses render._upload, so it is the same folder-at-a-time pack, the same
verify-then-free, and the same resume state: re-run it as many times as you
like and folders already in Drive are skipped. Items that never rendered are
ignored — the archives hold what actually exists.

## The catch on a full disk

`zip` mode packs each output folder into two archives before uploading them,
so it needs roughly twice one folder's worth of free space to start. Once the
first folder lands, its MP4s are deleted and the rest cascade. With zero free
space use `--mode files`, which streams each MP4 straight to Drive and needs
no headroom at all — at the cost of tens of thousands of Drive files instead
of a few dozen archives.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batching  # noqa: E402
import config  # noqa: E402
from jobs import store  # noqa: E402
from jobs.runners import render  # noqa: E402


def _gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("job_id", nargs="?", help="Job to publish. Omit to list jobs.")
    p.add_argument("--mode", choices=["zip", "files"],
                   help="Override the job's upload_mode.")
    p.add_argument("--platforms", help="Comma-separated: tk, yt, or tk,yt.")
    p.add_argument("--drive-folder", help="Override the destination Drive folder link.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be published and stop. Touches nothing.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store.init_db()

    if not args.job_id:
        for j in store.list_jobs(limit=25, kinds=[store.KIND_RENDER, store.KIND_PIPELINE]):
            c = store.item_counts(j["id"])
            print(f"{j['id']}  {j['status']:<9} rendered={c['rendered']:>6} "
                  f"uploaded={c['uploaded']:>6}  {j.get('label') or ''}")
        return 0

    job = store.get_job(args.job_id)
    if not job:
        # An id is 16 hex characters and gets truncated by every terminal it
        # passes through, so a prefix is accepted when it names exactly one job.
        known = [j["id"] for j in store.list_jobs(limit=100)]
        hit = [i for i in known if i.startswith(args.job_id)]
        if len(hit) != 1:
            p.error(f"No job matching {args.job_id!r} in {config.DB_PATH}. "
                    "Run without a job id to list them.")
        args.job_id = hit[0]
        job = store.get_job(args.job_id)

    # Overrides go through merge_job_params rather than the in-memory dict, so
    # a re-run after this one keeps the same choice instead of silently
    # reverting to whatever the job was submitted with.
    changes = {}
    if args.mode:
        changes["upload_mode"] = args.mode
    if args.platforms:
        changes["upload_platforms"] = [s.strip().lower()
                                       for s in args.platforms.split(",") if s.strip()]
    if args.drive_folder:
        changes["drive_folder"] = args.drive_folder
    if changes:
        job["params"] = store.merge_job_params(args.job_id, **changes)
    params = job["params"]

    n_batches = max(1, int(params.get("batches") or 1))
    n_folders = max(1, int(params.get("folders") or n_batches))

    # The row count is deliberately NOT read from params: nothing puts it
    # there. run() takes it from the staged sheet, which cleanup deletes the
    # moment a job finishes — so by the time this tool is useful it is gone.
    # The item rows carry the same fact and outlive the sheet. Getting this
    # wrong is not a small error: every idx is (batch - 1) * n_rows + row, so a
    # row count that is off by one sends every video to the wrong output folder
    # under a name drawn from a different row.
    items = store.list_items(args.job_id)
    n_rows = max((int((i.get("meta") or {}).get("row") or 0) for i in items),
                 default=0)
    if n_rows * n_batches != len(items):
        # Items registered before meta carried the row — the flat index layout
        # still holds, so the count divides out.
        n_rows = len(items) // n_batches
    if n_rows < 1:
        p.error(f"Cannot derive the row count from {len(items)} item(s) across "
                f"{n_batches} batch(es).")

    counts = store.item_counts(args.job_id)
    videos = store.videos_dir(args.job_id)
    on_disk = [f for f in videos.rglob("*.mp4")]
    total_bytes = sum(f.stat().st_size for f in on_disk)
    free = shutil.disk_usage(videos if videos.is_dir() else config.JOBS_ROOT).free
    mode = str(params.get("upload_mode") or config.UPLOAD_MODE).lower()

    print(f"\nJob {args.job_id} ({job['status']}) — {job.get('label') or ''}")
    print(f"  items      : {counts['rendered']:,} rendered of {counts['total']:,} "
          f"({counts['render_pending']:,} never started, {counts['uploaded']:,} already in Drive)")
    print(f"  on disk    : {len(on_disk):,} mp4 = {_gb(total_bytes)} in {videos}")
    print(f"  layout     : {n_rows:,} rows x {n_batches} batches -> {n_folders} folders")
    print(f"  free space : {_gb(free)}")
    if mode == "zip":
        need = 2 * total_bytes / n_folders
        print(f"  zip mode needs ~{_gb(need)} free to pack the first folder"
              + ("" if free > need else "  <-- NOT ENOUGH: free space, or use --mode files"))
    print()

    if args.dry_run:
        print("--dry-run: nothing was uploaded.")
        return 0

    if not on_disk:
        # zip mode deletes each folder's MP4s once Drive confirms them, so an
        # empty videos/ is the normal end state of a finished run — not a
        # missing-files problem, and worth saying differently.
        if counts["rendered"] and counts["uploaded"] >= counts["rendered"]:
            print("Every rendered video is already in Drive — nothing left to do.")
            return 0
        print("No .mp4 files under videos/ — nothing to upload.")
        return 1

    placement = batching.mix_into_folders(
        batching.plan_render(n_batches, n_rows), n_folders)
    result = render._upload(job, n_rows, n_folders, placement)

    if not result:
        print("Drive is not configured for this job — nothing was uploaded. "
              "Pass --drive-folder <link>.")
        return 1

    result["rendered"] = counts["rendered"]
    failures = result.get("upload_failures") or []
    status = store.STATUS_FAILED if failures else store.STATUS_SUCCEEDED
    # Merged into whatever the failed run recorded, so the Jobs page keeps the
    # render's own numbers alongside the Drive link this run produced.
    store.finish_job(args.job_id, status,
                     error=job.get("error"), result={**(job.get("result") or {}), **result})

    print(f"\n{result.get('uploaded', 0):,} item(s) published — {result.get('drive_link')}")
    for line in failures:
        print(f"  ! {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
