"""Exercise store.outputs_published — the gate that decides whether a finished
job's folder is deleted or kept.

The asymmetry under test: keeping something that could have gone costs disk the
reaper reclaims in a week; deleting something that should have stayed destroys a
night of rendering. So nearly every case below asserts KEEP, and the handful of
True rows are the ones that must be provable.
"""
import os, sys, tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="purgetest_"))
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = str(SCRATCH)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from jobs import store

assert config.JOBS_ROOT == SCRATCH.resolve(), config.JOBS_ROOT

RENDER = {"id": "j1", "kind": store.KIND_RENDER, "params": {}}

# A complete, two-platform, fully-verified zip-mode run — the one shape that
# earns a purge.
FULL = {"drive_link": "https://drive/f", "upload_platforms": ["yt", "tk"],
        "videos_freed": True, "rendered": 10, "uploaded": 10, "upload_failed": 0}

CASES = [
    # (label, job, result, expected_ok)
    ("runner raised, no result",        RENDER, None, False),
    ("drive unconfigured",              RENDER, {}, False),
    ("drive folder prep failed",        RENDER, {"drive_error": "boom"}, False),
    ("full two-platform success",       RENDER, FULL, True),
    ("some uploads failed",             RENDER, {**FULL, "upload_failed": 3}, False),
    ("upload_failures listed",          RENDER, {**FULL, "upload_failures": ["x"]}, False),
    ("one platform still pending",      RENDER,
     {**FULL, "videos_kept": 4, "platforms_pending": ["yt"]}, False),
    # free_local off means `videos_kept` is never set, so the platform list is
    # the only thing left that can catch a half-published night.
    ("single platform, nothing kept",   RENDER, {**FULL, "upload_platforms": ["tk"]}, False),
    ("fewer uploaded than rendered",    RENDER, {**FULL, "uploaded": 7}, False),
    ("operator asked to keep MP4s",
     {**RENDER, "params": {"free_local_videos": False}}, FULL, False),
    ("caption pool lives in the db",
     {"id": "j2", "kind": store.KIND_CAPTIONS, "params": {}}, {}, True),
    ("scrape uploaded to drive",
     {"id": "j3", "kind": store.KIND_SCRAPE, "params": {}},
     {"drive_link": "https://drive/s", "trimmed": 50, "uploaded": 120}, True),
    ("scrape clips never uploaded",
     {"id": "j3", "kind": store.KIND_SCRAPE, "params": {}},
     {"drive_link": "https://drive/s", "trimmed": 50, "uploaded": 0}, False),
]

for label, job, result, expected in CASES:
    ok, reason = store.outputs_published(job, result)
    assert ok is expected, f"{label}: expected {expected}, got {ok} ({reason})"
    print(f"{'PURGE' if ok else 'KEEP ':5} {label:32} — {reason}")

# --- the master switch overrides every True above -------------------------
config.JOB_PURGE_ON_FINISH = False
try:
    for label, job, result, expected in CASES:
        ok, reason = store.outputs_published(job, result)
        assert ok is False, f"{label}: purge ran with the switch off"
    print("BVG_JOB_PURGE_ON_FINISH=false keeps everything")
finally:
    config.JOB_PURGE_ON_FINISH = True

print("\nOK — outputs_published fails closed on every unproven case")
