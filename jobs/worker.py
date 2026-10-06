"""
jobs/worker.py — the background job runner.

Run alongside Streamlit:

    python -m jobs.worker

One job at a time, on purpose: a render batch already saturates the CPU with
its own internal FFmpeg parallelism, so running two concurrently would just
make both slower.

Crash recovery is deliberately blunt and relies on nothing staying resident.
If this process is killed mid-batch — `docker stop`, an OOM, a VM restart —
the job stays `running` with a heartbeat that stops advancing. On the next
start, requeue_stale_jobs() puts it back in the queue and the runner resumes
from per-item state instead of re-rendering what already finished. That means
there is no graceful-shutdown path to get right, which is the point.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from typing import Callable, Optional

import config
from jobs import store

logger = logging.getLogger("worker")

# How often the reaper runs, in seconds. Retention is measured in days, so
# checking a few times an hour is ample.
_REAP_INTERVAL = 900.0


class _Heartbeat:
    """Pings the job row while work is in progress.

    Without this, a batch that spends 12 minutes inside FFmpeg would look dead
    to requeue_stale_jobs() and get yanked out from under itself."""

    def __init__(self, job_id: str, interval: Optional[float] = None):
        self.job_id = job_id
        self.interval = interval or config.JOB_HEARTBEAT_SECONDS
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                store.heartbeat(self.job_id)
            except Exception:  # noqa: BLE001 — a failed ping must not kill the job
                logger.warning("heartbeat failed for job %s", self.job_id, exc_info=True)

    def __enter__(self) -> "_Heartbeat":
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self.job_id}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def _get_runner(kind: str) -> Callable[[dict], dict]:
    """Imported lazily so the worker starts even while a runner is still being
    built, and so a broken scraper import can't stop renders from running."""
    if kind == store.KIND_RENDER:
        from jobs.runners import render
        return render.run
    if kind == store.KIND_SCRAPE:
        from jobs.runners import scrape
        return scrape.run
    if kind == store.KIND_CAPTIONS:
        from jobs.runners import captions
        return captions.run
    if kind == store.KIND_PIPELINE:
        from jobs.runners import pipeline
        return pipeline.run
    raise ValueError(f"No runner registered for job kind {kind!r}")


def _notify(job: dict, status: str, result: Optional[dict], error: Optional[str]) -> None:
    """Send the completion email. Sent on success AND failure — a job that dies
    silently is the worst outcome. Never raises: a mail problem must not turn a
    successful batch into a failed one."""
    try:
        from integrations import mailer
        mailer.send_job_notification(job, status=status, result=result, error=error)
    except Exception:  # noqa: BLE001
        logger.exception("Notification failed for job %s", job["id"])


def run_job(job: dict) -> None:
    """Execute one claimed job and record its outcome."""
    job_id = job["id"]
    logger.info("Starting job %s (%s) — %s", job_id, job["kind"], job.get("label") or "")
    started = time.time()

    result: Optional[dict] = None
    error: Optional[str] = None
    status = store.STATUS_SUCCEEDED

    try:
        with _Heartbeat(job_id):
            runner = _get_runner(job["kind"])
            result = runner(job)
    except Exception as exc:  # noqa: BLE001 — one bad job must not stop the worker
        logger.exception("Job %s failed", job_id)
        status = store.STATUS_FAILED
        error = f"{type(exc).__name__}: {exc}"

    store.finish_job(job_id, status, error=error, result=result)
    logger.info("Job %s %s in %.0fs", job_id, status, time.time() - started)

    _notify(job, status, result, error)

    # The job's bytes leave the machine here. The default is to take the whole
    # folder — staged uploads, scratch, rendered MP4s, archives and the local
    # ZIP — keeping only a few kilobytes of manifests; the keep branch exists
    # for the job whose output could not be proven to be anywhere else.
    #
    # This must stay AFTER _notify: the notification reads result["sheet_path"]
    # straight off disk to attach it. _KEEP_AFTER_PURGE retains that file
    # anyway, but the ordering should not be the thing that is relied on.
    published, reason = store.outputs_published(job, result)
    try:
        store.cleanup_job_dir(job_id, keep_outputs=not published)
    except Exception:  # noqa: BLE001
        logger.warning("Cleanup failed for job %s", job_id, exc_info=True)
    else:
        if published:
            logger.info("Job %s: folder purged — %s", job_id, reason)
        else:
            logger.info(
                "Job %s: keeping this job's videos and reports on the VM — %s "
                "(the retention reaper takes them after %d days)",
                job_id, reason, config.JOB_RETENTION_DAYS)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Background job worker.")
    parser.add_argument("--once", action="store_true",
                        help="Run at most one job, then exit (for testing).")
    parser.add_argument("--kinds", nargs="*", default=None,
                        help="Only claim these job kinds.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config.ensure_dirs()
    store.init_db()
    logger.info("Worker starting — jobs root %s", config.JOBS_ROOT)

    stop = threading.Event()

    def _signal(signum, _frame):
        logger.info("Signal %s received — finishing current job, then exiting.", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, OSError):  # not the main thread / unsupported
            pass

    # Anything left `running` from a previous process is dead by definition —
    # this process is the only worker. Recover it before claiming new work.
    recovered = store.requeue_stale_jobs(stale_seconds=0)
    if recovered:
        logger.info("Requeued %d job(s) abandoned by a previous worker: %s",
                    len(recovered), ", ".join(recovered))

    last_reap = 0.0
    while not stop.is_set():
        try:
            if time.time() - last_reap > _REAP_INTERVAL:
                last_reap = time.time()
                removed = store.reap_old_jobs()
                if removed:
                    logger.info("Reaped %d job folder(s) past retention", len(removed))

            store.requeue_stale_jobs()
            job = store.claim_next_job(kinds=args.kinds)
            if job is None:
                if args.once:
                    logger.info("No queued jobs; --once exiting.")
                    break
                stop.wait(config.WORKER_POLL_SECONDS)
                continue

            run_job(job)
            if args.once:
                break
        except Exception:  # noqa: BLE001 — the loop must survive anything
            logger.exception("Worker loop error")
            stop.wait(config.WORKER_POLL_SECONDS)

    logger.info("Worker stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
