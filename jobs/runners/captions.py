"""
jobs/runners/captions.py — build a caption/hashtag pool in the background.

A full 2,000-caption pool is ~20 model calls. They run concurrently (see
captions/pool.py), so it is minutes rather than the half-hour it used to be —
but still long enough that holding a browser tab open for it would be wrong.
It runs rarely — weekly, or when the theme changes — so it reuses the same job
machinery as everything else and emails when it's done.
"""

from __future__ import annotations

import logging
import time

import config
from captions import pool as pool_module
from jobs import store

logger = logging.getLogger(__name__)


def run(job: dict) -> dict:
    params = job.get("params") or {}
    started = time.time()
    job_id = job["id"]

    theme = (params.get("theme") or config.CAPTION_THEME or "").strip()
    caption_count = int(params.get("captions") or config.CAPTION_POOL_SIZE)
    hashtag_count = int(params.get("hashtags") or config.HASHTAG_POOL_SIZE)

    store.set_stage(job_id, "generating captions")

    def progress(done: int, total: int) -> None:
        store.heartbeat(job_id, stage=f"generating {done}/{total}")

    info = pool_module.build_pool(
        theme=theme,
        caption_count=caption_count,
        hashtag_count=hashtag_count,
        progress=progress,
    )
    info["elapsed"] = time.time() - started
    logger.info("Job %s built caption pool %s (%s combinations)",
                job_id, info["pool_id"], f"{info['combinations']:,}")
    return info
