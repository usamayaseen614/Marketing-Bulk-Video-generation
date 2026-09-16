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


def run_stage(job: dict, params: dict, slots: list, n_rows: int) -> dict:
    """Synthesize every unique narration BEFORE a single row is rendered.

    `slots` are the videos this job renders; their items must already be
    registered, because a generated script is stored on its item.

    Two reasons this is its own stage rather than something render_row does on
    demand:

    MEMORY. Rows render up to sixteen wide in a ThreadPoolExecutor, on a box
    where sixteen parallel FFmpeg processes have already been OOM-killed at
    43-53 GB RSS each. Loading a PyTorch model into every one of those render
    threads is the same mistake with a different library.

    WORK. Synthesis is keyed by content, so a 16,000-row batch dealt from a
    60-script pool does 60 syntheses and 15,940 cache hits. Doing it per row
    would do it 16,000 times, and do it inside the render's critical path.
    Generated scripts (script_source "generate") are one per video by design,
    so there every video IS a synthesis — still here, still out of the render.

    Never raises: a voiceover is an enhancement, and a batch that cannot narrate
    must still render. Rows whose synthesis failed simply come out silent."""
    job_id = job["id"]
    render_config = params.get("render_config") or {}
    if not render_config.get("voice_enabled"):
        return {"skipped": "voiceover is off"}

    from speech import pool as script_pool
    from speech import synth

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

    # A video that already rendered is never rendered again, so a script or a
    # synthesis for it on a resumed job is paid for and unused — and a script
    # written onto its item would put narration in the manifest its MP4 lacks.
    from batching import item_index

    listed = store.list_items(job_id)
    items = {i["idx"]: i.get("meta") or {} for i in listed}
    done = {i["idx"] for i in listed if i.get("render_status") == store.ITEM_DONE}
    slots = [s for s in slots if item_index(s.batch, s.row, n_rows) not in done]

    # Scripts are written BEFORE the engine check: with no engine on this
    # machine the renderer still paces the words on screen as timed captions,
    # and those words have to exist for it to show anything.
    if params.get("script_source") == "generate":
        summary.update(_generate_scripts(job_id, params, frame, slots, n_rows,
                                         items, voices))

    # The engine this batch was queued with, not the machine's current default:
    # a job sitting in the queue must narrate with what its sidebar selected.
    engine = synth.normalize_engine(render_config.get("voice_engine"))
    usable, reason = synth.available(engine)
    if not usable:
        return {"skipped": reason, **summary}

    lang = render_config.get("voice_lang") or config.VOICE_LANG
    lead_in = float(render_config.get("voice_lead_in") or config.VOICE_LEAD_IN)
    default_speed = float(render_config.get("voice_speed") or config.VOICE_SPEED)
    default_voice = voices[0] if voices else synth.default_voice(engine)
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
    #
    # Walked per video rather than per sheet row, so a generated script is laid
    # over its row by the same with_item_script the renderer calls.
    from video_generator import RowSpec

    tasks: dict[str, tuple] = {}
    for slot in slots:
        row = script_pool.with_item_script(
            frame.iloc[slot.row - 1],
            items.get(item_index(slot.batch, slot.row, n_rows)))
        spec = RowSpec.from_row(row)
        text = synth.normalize(spec.voiceover)
        if not text:
            continue
        voice = spec.voice_name or default_voice
        speed = spec.voice_speed if spec.voice_speed is not None else default_speed
        tasks[synth.cache_key(text, voice, speed, lang, engine)] = (
            text, voice, speed)

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
                record(synth.synthesize(text, voice, speed, cache, lang,
                                        lead_in, engine))
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
                                   lang, lead_in, engine)
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

    logger.info("Job %s: %d voiceover(s) ready, %d failed, from %d row(s) on %s",
                job_id, done, failed, len(frame), synth.ENGINES.get(engine, engine))
    return {"unique_scripts": len(tasks), "synthesized": done, "failed": failed,
            "voices": len(voices), "engine": engine, **summary}


def _generate_scripts(job_id: str, params: dict, frame: pd.DataFrame,
                      slots: list, n_rows: int, items: dict,
                      voices: list) -> dict:
    """Write one Gemini script onto every video that needs one.

    A video needs one when its sheet row left both Voiceover and Screen_Text
    blank — the same two rules the uploaded pool follows — and its item does not
    carry one yet. That last check is what makes a resumed job free: the scripts
    were paid for once, and each video keeps the words it was given.

    `items` (idx -> meta) is updated in place so the synthesis walk after this
    sees the new scripts. A `warning` in the result goes into the render log:
    a batch that quietly rendered mute is the outcome most worth reporting."""
    from batching import item_index
    from captions import pool as gemini
    from speech import pool as script_pool

    targets = []
    for slot in slots:
        idx = item_index(slot.batch, slot.row, n_rows)
        row = frame.iloc[slot.row - 1]
        if (items.get(idx, {}).get("voiceover")
                or not script_pool._blank(row.get(script_pool.VOICEOVER_COLUMN))
                or not script_pool._blank(row.get(script_pool.SCREEN_TEXT_COLUMN))):
            continue
        targets.append((slot, idx, row))
    if not targets:
        return {"generated_scripts": 0, "reason": ""}

    def silent(why: str) -> dict:
        logger.warning("Job %s: %s", job_id, why)
        return {"generated_scripts": 0, "reason": "",
                "warning": f"{why} — {len(targets):,} video(s) render silent."}

    prompt = (params.get("script_prompt") or "").strip()
    if not prompt:
        return silent("Scripts were set to generate, but no prompt was given")

    store.set_stage(job_id, f"writing scripts 0/{len(targets)}")

    def progress(done: int, total: int) -> None:
        store.heartbeat(job_id, stage=f"writing scripts {done}/{total}")

    try:
        scripts = gemini.generate_scripts(prompt, len(targets), progress=progress)
    except Exception as exc:  # noqa: BLE001
        return silent(f"Script generation failed ({exc})")
    if not scripts:
        return silent("Gemini returned no scripts")

    for n, (slot, idx, row) in enumerate(targets):
        meta = {**items.get(idx, {}), "voiceover": scripts[n % len(scripts)]}
        # Row + batch, not position: each batch spreads its rows across the
        # voices, and each row walks the voices from one batch to the next.
        if voices and script_pool._blank(row.get(script_pool.VOICE_COLUMN)):
            meta["voice"] = voices[(slot.row + slot.batch) % len(voices)]
        store.update_item(job_id, idx, meta=meta)
        items[idx] = meta

    result = {"generated_scripts": len(scripts), "reason": ""}
    if len(scripts) < len(targets):
        # Only reachable when the model stops producing new scripts; repeating
        # a few beats rendering those videos mute.
        result["warning"] = (
            f"Gemini wrote {len(scripts):,} distinct script(s) for "
            f"{len(targets):,} videos, so some videos share a script.")
    logger.info("Job %s: wrote %d script(s) across %d video(s)",
                job_id, len(scripts), len(targets))
    return result
