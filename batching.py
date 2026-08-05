"""
batching.py — how a multi-batch render is laid out and then mixed.

A render turns ONE sheet into several batches: the same rows are rendered once
per batch, each pass using a different promo video and a different variant salt
so the ASMR clip picks differ. That gives `batches x rows` videos in total.

Rendering them is only half of it. If batch 1 went to folder 1 unchanged, every
video in that folder would share a promo video — so the folders are **mixed**
before upload: each output folder ends up with an even share drawn from every
source batch.

The deal is stratified rather than a plain shuffle. A shuffle gives roughly
even representation; stratifying guarantees it, which matters when a folder is
a posting queue and you do not want one promo video clustered in it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class Slot:
    """One video's identity within a render.

    Use item_index()/split_index() to convert to and from the job_items key —
    that needs the row count, which a Slot does not carry."""
    batch: int      # 1-based source batch (which promo video / variant salt)
    row: int        # 1-based sheet row


def item_index(batch: int, row: int, rows_per_batch: int) -> int:
    """Flatten (batch, row) to the 1-based idx used by job_items."""
    return (int(batch) - 1) * int(rows_per_batch) + int(row)


def split_index(idx: int, rows_per_batch: int) -> tuple[int, int]:
    """Inverse of item_index."""
    idx = int(idx) - 1
    return idx // int(rows_per_batch) + 1, idx % int(rows_per_batch) + 1


def plan_render(n_batches: int, n_rows: int) -> list[Slot]:
    """Every (batch, row) a render will produce, in render order."""
    return [Slot(batch=b, row=r)
            for b in range(1, int(n_batches) + 1)
            for r in range(1, int(n_rows) + 1)]


def mix_into_folders(slots: Iterable[Slot], n_folders: int,
                     seed: str = "mix") -> dict[Slot, int]:
    """Assign each rendered video an output folder, 1-based.

    Every source batch is shuffled and then dealt round-robin across the
    folders, so each folder receives an equal share of every batch — and
    therefore a spread of promo videos rather than a single one.

    Seeded, so a resumed job re-derives exactly the same layout instead of
    reshuffling and scattering files that were already uploaded."""
    n_folders = max(1, int(n_folders))
    by_batch: dict[int, list[Slot]] = {}
    for slot in slots:
        by_batch.setdefault(slot.batch, []).append(slot)

    placement: dict[Slot, int] = {}
    # The round-robin continues across batches instead of restarting at folder
    # 1 each time. Restarting would hand every batch's remainder to the same
    # folder: 3 batches of 7 into 3 folders came out 9/6/6 instead of 7/7/7.
    # The offset depends only on the batches already dealt, so appending a
    # batch still leaves earlier placements untouched.
    offset = 0
    for batch in sorted(by_batch):
        group = sorted(by_batch[batch], key=lambda s: s.row)
        # Seeded per batch so adding a batch doesn't reshuffle earlier ones.
        random.Random(f"{seed}-{batch}-{len(group)}").shuffle(group)
        for position, slot in enumerate(group):
            placement[slot] = ((offset + position) % n_folders) + 1
        offset += len(group)
    return placement


def assign_promos(slots: list[Slot], n_promos: int,
                  seed: str = "promo") -> dict[Slot, int]:
    """Which promo video (0-based) each video uses: one promo per pass.

    Every row is rendered once per promo, so with 5 rows and 5 promos you get
    all 25 row-by-promo pairings, each exactly once. That complete coverage is
    the point — it is what "every promo goes through every row" means.

    Assigning a promo per *video* instead was tried and is strictly worse: it
    produced only 17 of those 25 pairings, missing 8 entirely and duplicating 6,
    because an even spread is not the same as complete coverage.

    Mixing still happens — just later. `mix_into_folders` deals each pass
    evenly across the output folders, so every folder you receive holds all the
    promos even though each pass used only one. Randomising here as well would
    buy nothing and cost the coverage.

    When there are more passes than promos the list cycles, so each pairing is
    simply produced more than once."""
    n_promos = max(1, int(n_promos))
    return {slot: (slot.batch - 1) % n_promos for slot in slots}


def select_clips(clips: list[dict], slots: int, per_slot: int,
                 strategy: str = "top_views",
                 picked_ids: Optional[Iterable[str]] = None,
                 seed: str = "select") -> dict[int, list[dict]]:
    """Choose which scraped clips to use, and which slot each one fills.

    `clips` are dicts carrying at least `video_id` and `path`, plus whatever
    metadata the scrape recorded (`views` is what the default strategy sorts
    on).

    Strategies:
      top_views  the unattended default — take the best `slots x per_slot`
                 clips by view count. Curation as a rule rather than a chore.
      manual     use exactly the ids given, in the order given.
      all        use everything available.

    Assignment is **shuffle-then-deal**, not positional. Clips arrive sorted
    (newest first from the scrape, by views after curation), so dealing them
    positionally would hand slot 1 the strongest clips every single time.
    Shuffling first makes the slot genuinely random; dealing without
    replacement means no clip is ever used twice and the slots stay evenly
    filled.

    Seeded, so a resumed job rebuilds the identical selection."""
    slots = max(1, int(slots))
    per_slot = max(1, int(per_slot))

    usable = [c for c in clips if c.get("video_id") and c.get("path")]
    if strategy == "manual":
        wanted = list(picked_ids or [])
        by_id = {c["video_id"]: c for c in usable}
        chosen = [by_id[v] for v in wanted if v in by_id]
    elif strategy == "all":
        chosen = list(usable)
    else:
        chosen = sorted(usable, key=lambda c: (c.get("views") or 0), reverse=True)

    if strategy != "all":
        chosen = chosen[:slots * per_slot]

    if not chosen:
        return {}

    order = list(chosen)
    random.Random(f"{seed}-{len(order)}").shuffle(order)

    out: dict[int, list[dict]] = {s: [] for s in range(1, slots + 1)}
    for position, clip in enumerate(order):
        out[(position % slots) + 1].append(clip)
    return out


def materialize_slots(selection: dict[int, list[dict]], assets_dir: Path) -> int:
    """Copy the chosen clips into `assets/cta_slot_N/`.

    This is the seam that makes the whole unified flow work: whatever the clips
    came from — a browser upload, a previous scrape, or one that just ran — the
    end state on disk is identical, so the render runner needs no idea which it
    was. Empty slots still get their folder, or the per-slot playback speeds
    would shift onto the wrong clips."""
    import shutil

    assets_dir = Path(assets_dir)
    copied = 0
    for slot in sorted(selection):
        slot_dir = assets_dir / f"cta_slot_{slot}"
        slot_dir.mkdir(parents=True, exist_ok=True)
        for clip in selection[slot]:
            src = Path(clip["path"])
            if not src.is_file():
                continue
            dest = slot_dir / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
            copied += 1
    return copied


def folder_name(folder_no: int) -> str:
    """An OUTPUT folder — what ends up in Drive, after mixing."""
    return f"batch_{int(folder_no):02d}"


def source_folder_name(batch_no: int) -> str:
    """A SOURCE batch — one render pass, one promo video, on local disk only.

    Deliberately not `batch_NN`: source batches and output folders are
    different groupings that both used to be called that, so `batch_03` on disk
    held completely different videos from `batch_03` in Drive."""
    return f"source_{int(batch_no):02d}"


def summarize(placement: dict[Slot, int], n_folders: int) -> dict[int, dict]:
    """Per-folder counts and the batch spread inside each — used to report the
    mix back, so 'mixed' is a verifiable claim rather than an assertion."""
    out: dict[int, dict] = {
        f: {"total": 0, "from_batch": {}} for f in range(1, int(n_folders) + 1)
    }
    for slot, folder in placement.items():
        entry = out.setdefault(folder, {"total": 0, "from_batch": {}})
        entry["total"] += 1
        entry["from_batch"][slot.batch] = entry["from_batch"].get(slot.batch, 0) + 1
    return out
