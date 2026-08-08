"""End-to-end per-promo text: one sheet row rendered against three promo
videos says three different things, and the filenames follow the words that
were actually drawn.

Runs with no caption pool on purpose — that makes the output filename fall back
to the Headline, which is the cheapest way to read the text a video was
rendered with back out of the job store."""
import io
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="tge_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

import batching
import text_grids as tg
from jobs import store, worker
from video_generator import RenderConfig
from workspace import stage_uploads, workspace_from_dir

SA = PROJ / "sample_assets"
N_BATCHES = 3
PROMO_NAMES = ["alpha promo.mp4", "Beta-Promo.MP4", "gamma promo.mp4"]

# Distinct, filename-safe single words, so the text a video was rendered with
# can be read straight back off its filename.
GRID = {
    "alpha promo.mp4": ["alphaone", "alphatwo"],
    # Spelled differently from the uploaded filename on purpose — the match
    # ignores case, separators and the extension.
    "beta_promo": ["betaone", ""],            # blank cell -> main sheet
    "gamma promo": ["gammaone", "gammatwo"],
}
# Batch b uses promo (b-1) % n_promos, so batch 1/2/3 -> alpha/beta/gamma.
EXPECTED = {
    (1, 1): "alphaone", (1, 2): "alphatwo",
    (2, 1): "betaone", (2, 2): "main two",    # the blank cell's fallback
    (3, 1): "gammaone", (3, 2): "gammatwo",
}


class Fake:
    def __init__(self, p, name=None):
        self.name = name or Path(p).name
        self._d = Path(p).read_bytes()
        self.size = len(self._d)

    def getvalue(self):
        return self._d


store.init_db()
assert store.active_pool() is None, "this test needs no caption pool"

job_id = store.new_job_id()
store.make_job_dirs(job_id)
assets = store.assets_dir(job_id)

promos = [Fake(SA / "promo.mp4", PROMO_NAMES[0]),
          Fake(SA / "cta_video_1.mp4", PROMO_NAMES[1]),
          Fake(SA / "cta_video_5.mp4", PROMO_NAMES[2])]
stage_uploads(assets, promos, Fake(SA / "backgrounds.zip"), None, None)

# Two rows keeps the render short; the claim does not need more.
df = pd.read_excel(SA / "sample_5_videos.xlsx", engine="openpyxl").head(2).copy()
df["Headline"] = ["main one", "main two"]
df["Subheading"] = ["sub one", "sub two"]
df.to_excel(assets / "input.xlsx", index=False, engine="openpyxl")
N_ROWS = len(df)

# ---------- what the UI does at submit time ----------
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="openpyxl") as writer:
    pd.DataFrame(GRID).to_excel(writer, index=False)

grid = tg.read_grid(buf.getvalue(), "Headline")
report = tg.check_grid(grid, PROMO_NAMES, N_ROWS)
assert report.ok, report.errors
assert report.matched == 3, report.pairs
assert report.blank_cells == 1, report.blank_cells
print(f"check: {report.matched}/3 promos matched despite mixed spelling, "
      f"{report.blank_cells} blank cell")

tg.write_overrides(assets, {"Headline": tg.resolve(grid, report)})
assert (assets / tg.OVERRIDES_FILENAME).is_file()

# The staged mapping is keyed by promo INDEX — which is what survives
# stage_uploads renaming every promo to input.mp4 / input_N.mp4.
ws = workspace_from_dir(assets, assets.parent / "work")
assert [p.name for p in ws.video_paths] == ["input.mp4", "input_2.mp4", "input_3.mp4"]
staged = tg.read_overrides(assets)
assert staged["Headline"]["0"] == ["alphaone", "alphatwo"], staged
assert staged["Headline"]["1"] == ["betaone", ""], staged
assert staged["Headline"]["2"] == ["gammaone", "gammatwo"], staged
print(f"staged {tg.OVERRIDES_FILENAME} keyed by promo index: "
      f"{ {k: v for k, v in sorted(staged['Headline'].items())} }")

# ---------- render ----------
store.create_job(
    kind=store.KIND_RENDER,
    params={"render_config": asdict(RenderConfig(crf=32, preset="ultrafast")),
            "workers": 4, "batches": N_BATCHES, "folders": N_BATCHES,
            "make_zip": False, "promo_names": PROMO_NAMES,
            "text_grids": ["Headline"]},
    label="text-grid-test",
    job_id=job_id,
)
worker.main(["--once"])

job = store.get_job(job_id)
res = job["result"]
assert job["status"] == store.STATUS_SUCCEEDED, job.get("error")
print(f"\nrendered {res['rendered']}/{res['total']} "
      f"({N_ROWS} rows x {N_BATCHES} batches, {res['promo_videos']} promos)")
assert res["total"] == N_ROWS * N_BATCHES
assert res["rendered"] == res["total"], res.get("failures")

# ---------- THE KEY CLAIM: same row, different words per promo ----------
items = {}
for item in store.list_items(job_id):
    batch, row = batching.split_index(item["idx"], N_ROWS)
    items[(batch, row)] = item

print("\nfilenames (no caption pool, so the name IS the headline):")
for slot in sorted(items):
    name = items[slot]["name"]
    want = EXPECTED[slot]
    print(f"  batch {slot[0]} row {slot[1]}: {name}")
    assert want in name, f"batch {slot[0]} row {slot[1]}: {want!r} not in {name!r}"
    # The main sheet's text must NOT have leaked through where a grid cell
    # supplied one — that would mean the override never reached the renderer.
    if want != "main two":
        assert "main" not in name, f"main sheet text leaked into {name!r}"

row1 = {slot[0]: items[slot]["name"] for slot in items if slot[1] == 1}
assert len(set(row1.values())) == 3, row1
print(f"\nrow 1 rendered 3 different headlines across 3 promos: "
      f"{sorted(w for w in ('alphaone', 'betaone', 'gammaone'))}")

# ---------- the manifest records what each video actually said ----------
manifest = pd.read_excel(Path(res["sheet_path"]), engine="openpyxl")
assert "Headline" in manifest.columns, list(manifest.columns)
# Subheading had no grid, so it must NOT have gained a column.
assert "Subheading" not in manifest.columns, list(manifest.columns)
seen = {(int(r["Source_Batch"]), int(r["Sheet_Row"])): r["Headline"]
        for _i, r in manifest.iterrows()}
assert seen == EXPECTED, (seen, EXPECTED)
print(f"render_manifest.xlsx Headline column matches all "
      f"{len(EXPECTED)} videos, including the blank cell's fallback")

# ---------- resume must not redraw the text ----------
victim = (2, 1)
videos = Path(res["videos_dir"])
target = videos / batching.source_folder_name(victim[0]) / items[victim]["name"]
target.unlink()
store.update_item(job_id, batching.item_index(victim[0], victim[1], N_ROWS),
                  render_status=store.ITEM_PENDING)
stage_uploads(assets, promos, Fake(SA / "backgrounds.zip"), None, None)
df.to_excel(assets / "input.xlsx", index=False, engine="openpyxl")
tg.write_overrides(assets, {"Headline": tg.resolve(grid, report)})
with store._conn() as c:
    c.execute("UPDATE jobs SET status=? WHERE id=?", (store.STATUS_QUEUED, job_id))

worker.main(["--once"])
again = {}
for item in store.list_items(job_id):
    batch, row = batching.split_index(item["idx"], N_ROWS)
    again[(batch, row)] = item["name"]
assert target.is_file(), target
assert again == {s: i["name"] for s, i in items.items()}, (again, items)
print(f"resume: re-rendered batch {victim[0]} row {victim[1]}, every name unchanged")

print("\nALL PER-PROMO TEXT E2E TESTS PASSED")
