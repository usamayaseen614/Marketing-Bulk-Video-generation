"""
jobs/runners/voice.py — synthesizing a batch's narration before it renders.

Its own module rather than a stage inside pipeline.py because a plain render
job has to narrate too, and pipeline.py already imports render.py — so the call
has to live on the render side or the import graph turns into a cycle. Calling
it from render.run() covers KIND_RENDER and KIND_PIPELINE alike, exactly once.
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd

import config
from jobs import store

logger = logging.getLogger(__name__)


def run_stage(job: dict, params: dict) -> dict:
    """Synthesize every unique narration BEFORE a single row is rendered.

    Two reasons this is its own stage rather than something render_row does on
    demand:

    MEMORY. Rows render up to sixteen wide in a ThreadPoolExecutor, on a box
    where sixteen parallel FFmpeg processes have already been OOM-killed at
    43-53 GB RSS each. Loading a PyTorch model into every one of those render
    threads is the same mistake with a different library.

    WORK. Synthesis is keyed by content, so a 16,000-row batch dealt from a
    60-script pool does 60 syntheses and 15,940 cache hits. Doing it per row
    would do it 16,000 times, and do it inside the render's critical path.

    Never raises: a voiceover is an enhancement, and a batch that cannot narrate
    must still render. Rows whose synthesis failed simply come out silent."""
    job_id = job["id"]
    render_config = params.get("render_config") or {}
    if not render_config.get("voice_enabled"):
        return {"skipped": "voiceover is off"}

    from speech import pool as script_pool
    from speech import synth

    usable, reason = synth.available()
    if not usable:
        return {"skipped": reason}

    assets = store.assets_dir(job_id)
    sheet = assets / "input.xlsx"
    if not sheet.is_file():
        return {"skipped": "the staged sheet is gone, so there is nothing to read"}

    frame = pd.read_excel(sheet, engine="openpyxl")
    frame.columns = [str(c).strip() for c in frame.columns]

    # The uploaded script pool, staged beside the sheet the way hashtags.xlsx is.
    scripts: list[str] = []
    for candidate in sorted(assets.glob("scripts.*")):
        scripts = script_pool.parse_scripts(candidate.read_bytes(), candidate.name)
        if scripts:
            break

    voices = list(render_config.get("voice_set") or config.VOICE_SET)
    frame, summary = script_pool.apply_to_frame(frame, scripts, voices)

    # Persist the dealt columns back into the workbook, because render.py reads
    # this same file and must see the same assignment. Writing through the
    # captions writer keeps the user's formatting intact.
    try:
        from captions.assign import write_columns_to_workbook
        sheet.write_bytes(write_columns_to_workbook(
            sheet.read_bytes(), frame,
            columns=(script_pool.VOICEOVER_COLUMN, script_pool.VOICE_COLUMN)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Job %s: could not write the Voiceover columns back (%s)",
                       job_id, exc)

    lang = render_config.get("voice_lang") or config.VOICE_LANG
    lead_in = float(render_config.get("voice_lead_in") or config.VOICE_LEAD_IN)
    default_speed = float(render_config.get("voice_speed") or config.VOICE_SPEED)
    default_voice = voices[0] if voices else synth.DEFAULT_VOICE
    cache = assets / "voice"
    cache.mkdir(parents=True, exist_ok=True)

    # Deduplicate by the SAME key the renderer will look up with. That has to be
    # true by CONSTRUCTION, not by two places agreeing to parse a row the same
    # way — so the row goes through RowSpec.from_row here exactly as it will in
    # render_row, and voice/speed are defaulted with the same expressions
    # VideoGenerator._voice_entry uses.
    #
    # Parsing it by hand here was a real bug: float(NaN) does NOT raise, so a
    # sheet carrying a Voiceover_Speed column with any blank cell cached under
    # speed "nan" while the renderer looked up "1.000". Every such row missed
    # the cache and rendered SILENT, with nothing on stderr and no failed item.
    from video_generator import RowSpec

    tasks: dict[str, tuple] = {}
    for _, row in frame.iterrows():
        spec = RowSpec.from_row(row)
        text = synth.normalize(spec.voiceover)
        if not text:
            continue
        voice = spec.voice_name or default_voice
        speed = spec.voice_speed if spec.voice_speed is not None else default_speed
        tasks[synth.cache_key(text, voice, speed, lang)] = (text, voice, speed)

    if not tasks:
        return {"skipped": "no row carries a Voiceover", **summary}

    store.set_stage(job_id, f"generating voiceovers 0/{len(tasks)}")
    workers = max(1, int(params.get("voice_workers") or config.VOICE_WORKERS))
    pending = list(tasks.values())
    done = failed = 0

    def record(result) -> None:
        nonlocal done, failed
        if result:
            done += 1
        else:
            failed += 1
        if (done + failed) % 5 == 0 or (done + failed) == len(pending):
            store.heartbeat(job_id,
                            stage=f"generating voiceovers {done + failed}/{len(pending)}")

    def synthesize_serially() -> None:
        nonlocal done, failed
        done = failed = 0
        synth.configure_threads(0)
        for text, voice, speed in pending:
            try:
                record(synth.synthesize(text, voice, speed, cache, lang, lead_in))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Job %s: a voiceover failed (%s)", job_id, exc)
                record(None)

    pool_failed = False
    try:
        # Separate processes, each pinned to one torch thread: a 112-core box
        # would otherwise have every worker ask for 112 threads. One model per
        # process costs under a gigabyte each, which on this hardware is noise.
        with ProcessPoolExecutor(max_workers=workers,
                                 initializer=synth.configure_threads) as pool:
            futures = [pool.submit(synth.synthesize, text, voice, speed, cache,
                                   lang, lead_in)
                       for text, voice, speed in pending]
            for future in as_completed(futures):
                try:
                    record(future.result())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Job %s: a voiceover failed (%s)", job_id, exc)
                    record(None)
    except Exception as exc:  # noqa: BLE001
        # The pool would not start at all.
        logger.warning("Job %s: voice pool unavailable (%s) - synthesizing serially",
                       job_id, exc)
        pool_failed = True
        synthesize_serially()

    if pending and not done and not pool_failed:
        # The pool started but every worker died - a spawn that cannot re-import
        # __main__, a torch that will not load in a subprocess, an OOM. Caught
        # here rather than inside the loop above, because each future's failure
        # is handled per-item there and so never reaches the handler that was
        # supposed to fall back. Without this the whole batch renders silent
        # with nothing but warnings in the log.
        logger.warning("Job %s: every pooled voiceover failed - retrying serially",
                       job_id)
        synthesize_serially()

    logger.info("Job %s: %d voiceover(s) ready, %d failed, from %d row(s)",
                job_id, done, failed, len(frame))
    return {"unique_scripts": len(tasks), "synthesized": done, "failed": failed,
            "voices": len(voices), **summary}
