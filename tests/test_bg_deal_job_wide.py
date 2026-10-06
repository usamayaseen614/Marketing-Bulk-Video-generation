"""Blank BG_Image cells are dealt across the WHOLE job, not per batch.

render.py calls assign_backgrounds once per batch with the same sheet. It used
to restart the deal every time, so 140 batches of 100 rows used the same 100
images 140 times and left the rest of a 13,000-image pool untouched. These
checks pin the job-wide deal: no image repeats until the pool is used up.
"""
import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from video_generator import VideoGenerator


def generator(n_images: int) -> VideoGenerator:
    """A generator over a pool of `n_images` names, without touching disk or
    probing a promo with FFmpeg — assign_backgrounds only reads the index."""
    gen = VideoGenerator.__new__(VideoGenerator)
    gen._bg_names = [f"img_{i:05d}.png" for i in range(n_images)]
    gen._bg_index = {n: Path(n) for n in gen._bg_names}
    return gen


def run_job(gen, df, n_batches):
    picks, warnings = [], set()
    for batch in range(1, n_batches + 1):
        out, warn = gen.assign_backgrounds(df, batch=batch)
        picks.append(list(out["BG_Image"]))
        warnings.update(warn)
    return picks, warnings


SHEET = pd.DataFrame({"Headline": [f"row {i}" for i in range(100)]})   # no BG_Image

# 140 promos x 100 rows from 14,000 images: every video its own image.
picks, warnings = run_job(generator(14_000), SHEET, 140)
flat = [p for batch in picks for p in batch]
assert len(flat) == 14_000 and len(set(flat)) == 14_000, len(set(flat))
assert not warnings, warnings
print("ok: 14,000 videos from 14,000 images use every image exactly once")

# ...from 13,000: all 13,000 used before any repeat, then one warning per job.
picks, warnings = run_job(generator(13_000), SHEET, 140)
flat = [p for batch in picks for p in batch]
assert len(set(flat[:13_000])) == 13_000
assert len(set(flat)) == 13_000
assert len(warnings) == 1 and "13,000" in next(iter(warnings)), warnings
print("ok: 13,000 images are all used before any repeats, with one warning")

# Never a repeat inside one batch while the pool covers it.
assert all(len(set(batch)) == 100 for batch in picks[:130])
print("ok: no batch repeats an image while the pool lasts")

# Deterministic: a resumed job (or the preview) re-derives the same deal.
again, _ = run_job(generator(13_000), SHEET, 140)
assert again == picks
gen = generator(13_000)
assert list(gen.assign_backgrounds(SHEET, batch=57)[0]["BG_Image"]) == picks[56]
print("ok: the deal is reproducible, batch by batch")

# Filled cells are kept, and the images they name are left out of the deal.
pinned = SHEET.assign(BG_Image=[""] * 99 + ["img_00007.png"])
picks, _ = run_job(generator(500), pinned, 5)
assert all(batch[-1] == "img_00007.png" for batch in picks)
dealt = [p for batch in picks for p in batch[:-1]]
assert "img_00007.png" not in dealt and len(set(dealt)) == 495, len(set(dealt))
print("ok: a named image stays on its row and is never dealt to another")

# Pools that don't divide into whole batches: the batch straddling two decks
# must still show 100 different images, and usage stays even (within one).
for n_images, sheet in ((1_050, SHEET), (150, SHEET), (1_001, SHEET),
                        (1_000, SHEET.assign(BG_Image=[""] * 99 + ["img_00003.png"]))):
    picks, _ = run_job(generator(n_images), sheet, 140)
    dealt = [batch[:99] if len(sheet.columns) > 1 else batch for batch in picks]
    dupes = [b for b, batch in enumerate(dealt, 1) if len(set(batch)) != len(batch)]
    assert not dupes, (n_images, dupes[:5])
    from collections import Counter
    uses = Counter(p for batch in dealt for p in batch)
    assert max(uses.values()) - min(uses.values()) <= 1, (n_images, uses.most_common(2))
print("ok: no batch repeats an image when the pool doesn't divide evenly")

# A resumed Drive job whose pool grew (3 downloads failed the first time) must
# not reshuffle the batches still to render into thousands of repeats.
first, _ = run_job(generator(13_997), SHEET, 70)
gen = generator(14_000)
rest = [list(gen.assign_backgrounds(SHEET, batch=b)[0]["BG_Image"]) for b in range(71, 141)]
flat = [p for batch in first + rest for p in batch]
assert len(set(flat)) >= 14_000 - 10, len(set(flat))
print(f"ok: a pool that grew on resume costs {14_000 - len(set(flat))} repeats, not thousands")

# The index the deal draws from must hold one name per file. A sub-folder's
# bare name used to shadow a root file's own path, and Linux case twins were
# listed twice — either way one image was dealt twice and its twin never.
import tempfile
from pathlib import PurePosixPath

tmp = Path(tempfile.mkdtemp(prefix="bgindex_"))
(tmp / "sub").mkdir()
for rel in ("sub/x.png", "x.png", "y.png"):
    (tmp / rel).write_bytes(b"x")
index, names = VideoGenerator._build_bg_index(tmp)
assert sorted(names) == ["sub/x.png", "x.png", "y.png"], names
assert len({index[n] for n in names}) == 3
assert index["x.png"] == tmp / "x.png" and index["sub/x.png"] == tmp / "sub" / "x.png"
print("ok: a root x.png is not shadowed by sub/x.png")


class _LinuxFile(PurePosixPath):
    def is_file(self):
        return True


class _LinuxDir(PurePosixPath):
    """What the Linux VM holds after extracting a ZIP with Sky.png and sky.png.
    A Windows disk cannot hold both, so the listing is faked."""
    def rglob(self, _pattern):
        return [_LinuxFile(self, n) for n in ("Sky.png", "sea.png", "sky.png")]


index, names = VideoGenerator._build_bg_index(_LinuxDir("/bg"))
assert len(names) == len(set(names)) == 2, names
assert len({index[n] for n in names}) == 2, names
print("ok: Linux case twins are one card, not two cards for one file")

# A pool smaller than one batch still fills every row (repeats, with warning).
out, warn = generator(30).assign_backgrounds(SHEET)
assert out["BG_Image"].map(bool).all() and len(set(out["BG_Image"])) == 30 and warn
print("ok: a pool smaller than a batch repeats, with a warning")

print("\nall job-wide background-deal checks passed")
