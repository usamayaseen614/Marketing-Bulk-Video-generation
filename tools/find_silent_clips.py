"""
tools/find_silent_clips.py — list clips that have no audio track.

Clips scraped before the H.265 fix may be silent: TikTok advertises AAC on
every rendition it offers, including bytevc1 streams that arrive with no audio
stream at all, and the old format selector preferred exactly those. A clip bank
for ASMR full of silent clips is worse than an empty one, so this walks a folder
and tells you which files to re-scrape.

    python tools/find_silent_clips.py <folder> [--delete]

Reports every file, then a summary. `--delete` removes the silent ones, so a
re-scrape with "Skip clips already scraped" unticked refills those slots.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers import tiktok  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="folder of clips to check (walked recursively)")
    parser.add_argument("--delete", action="store_true",
                        help="delete the silent clips instead of only listing them")
    parser.add_argument("--ext", default=".mp4", help="file extension to check")
    args = parser.parse_args()

    root = Path(args.folder)
    if not root.is_dir():
        print(f"not a folder: {root}")
        return 2

    files = sorted(p for p in root.rglob(f"*{args.ext}") if p.is_file())
    if not files:
        print(f"no {args.ext} files under {root}")
        return 0

    ffmpeg = tiktok.find_ffmpeg()
    silent: list[Path] = []
    for n, path in enumerate(files, start=1):
        ok = tiktok.has_audio(path, ffmpeg)
        if not ok:
            silent.append(path)
        print(f"[{n}/{len(files)}] {'ok    ' if ok else 'SILENT'}  "
              f"{path.relative_to(root)}")

    print(f"\n{len(silent)} of {len(files)} clip(s) have no audio.")
    if not silent:
        return 0

    if args.delete:
        freed = 0
        for path in silent:
            freed += path.stat().st_size
            path.unlink(missing_ok=True)
        print(f"Deleted {len(silent)} silent clip(s), freeing "
              f"{freed / 1024 / 1024:.1f} MB.")
        print("Re-scrape those accounts with “Skip clips already scraped” "
              "unticked to pull them again, now with sound.")
    else:
        print("Re-run with --delete to remove them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
