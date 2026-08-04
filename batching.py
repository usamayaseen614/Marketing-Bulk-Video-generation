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
