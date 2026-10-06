"""The stage column must ALTER onto a database created before it existed.

CREATE TABLE IF NOT EXISTS does nothing to an existing table, so without a real
migration every deployed VM would break on upgrade with "no such column: stage".
This builds a pre-migration database on purpose and upgrades it.
"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="migtest_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from jobs import store

# ---------- build a database the OLD way: job_items with no `stage` ----------
config.ensure_dirs()
old = sqlite3.connect(config.DB_PATH)
old.executescript("""
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '', params_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT, error TEXT, stage TEXT NOT NULL DEFAULT '',
    notify_email TEXT NOT NULL DEFAULT '', submitted_by TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
    started_at REAL, finished_at REAL, heartbeat_at REAL
);
CREATE TABLE job_items (
    job_id TEXT NOT NULL, idx INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '',
    render_status TEXT NOT NULL DEFAULT 'pending', render_error TEXT,
    render_attempts INTEGER NOT NULL DEFAULT 0,
    upload_status TEXT NOT NULL DEFAULT 'pending', upload_error TEXT,
    upload_attempts INTEGER NOT NULL DEFAULT 0,
    drive_file_id TEXT, drive_link TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]', meta_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY (job_id, idx)
);
""")
now = time.time()
old.execute("INSERT INTO jobs (id, kind, status, label, created_at) VALUES (?,?,?,?,?)",
            ("oldjob", "render", "succeeded", "pre-migration batch", now))
for i in (1, 2, 3):
    old.execute(
        "INSERT INTO job_items (job_id, idx, name, render_status, updated_at) "
        "VALUES (?,?,?,?,?)", ("oldjob", i, f"{i:03d}_old.mp4", "done", now))
old.commit()

cols = {r[1] for r in old.execute("PRAGMA table_info(job_items)").fetchall()}
assert "stage" not in cols, "test setup wrong — column already present"
old.close()
print("built a pre-migration database: job_items has no `stage` column")

# ---------- upgrade ----------
store.init_db()

with store._conn() as c:
    cols = {r["name"] for r in c.execute("PRAGMA table_info(job_items)").fetchall()}
assert "stage" in cols, cols
print("after init_db(): `stage` column exists")

# ---------- existing rows must still be readable and default sensibly ----------
items = store.list_items("oldjob")
assert len(items) == 3, items
assert all(i["stage"] == "render" for i in items), [i["stage"] for i in items]
assert all(i["render_status"] == "done" for i in items)
print(f"existing {len(items)} rows survived and defaulted to stage='render'")

counts = store.item_counts("oldjob")
assert counts["total"] == 3 and counts["rendered"] == 3, counts
print("item_counts still correct on migrated rows:", counts)

# ---------- running it twice must not fail ----------
store.init_db()
store.init_db()
print("init_db() is idempotent — safe to run on every worker/app start")

# ---------- the two stages stay separate ----------
store.add_items("oldjob", [{"idx": 500, "name": "clip.mp4", "stage": store.STAGE_SCRAPE}])
render_items = store.list_items("oldjob", stage=store.STAGE_RENDER)
scrape_items = store.list_items("oldjob", stage=store.STAGE_SCRAPE)
every = store.list_items("oldjob", stage=None)
assert len(render_items) == 3, render_items
assert len(scrape_items) == 1, scrape_items
assert len(every) == 4
print(f"stages isolated: {len(render_items)} render + {len(scrape_items)} scrape = {len(every)} total")

assert store.item_counts("oldjob", stage=store.STAGE_SCRAPE)["total"] == 1
assert store.item_counts("oldjob", stage=store.STAGE_RENDER)["total"] == 3
print("item_counts is per-stage, so a pipeline's clips don't inflate its video count")

# a scrape item must not appear in the render queue
pending = store.pending_render_items("oldjob", stage=store.STAGE_RENDER)
assert all(p["idx"] != 500 for p in pending), pending
print("pending_render_items ignores the other stage")

print("\nALL MIGRATION TESTS PASSED")
