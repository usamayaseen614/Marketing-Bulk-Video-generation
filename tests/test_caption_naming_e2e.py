"""The Workstream C payoff: with a pool active, rendered files are named from
their Caption, and the sheet saved with the batch carries the columns."""
import os, sys, tempfile
from dataclasses import asdict
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="capname_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from jobs import store, worker
from workspace import stage_uploads
from video_generator import RenderConfig

SA = PROJ / "sample_assets"


class Fake:
    def __init__(self, p):
        self.name = Path(p).name; self._d = Path(p).read_bytes(); self.size = len(self._d)
    def getvalue(self): return self._d


store.init_db()

# A stub pool — no Gemini call needed to prove the naming wiring.
captions = [
    "Your skin will thank you for this one 🔥",
    "The 3-second trick nobody talks about",
    "Why is nobody doing this?!",
    "Stop scrolling — this actually works",
    "POV: you finally found the good stuff",
]
hashtags = [f"#asmr #skincare{i} #fyp" for i in range(5)]
pool_id = store.save_pool("satisfying ASMR skincare", captions, hashtags, model="stub")
print("pool:", pool_id)

job_id = store.new_job_id()
store.make_job_dirs(job_id)
assets = store.assets_dir(job_id)
stage_uploads(assets, Fake(SA / "promo.mp4"), Fake(SA / "backgrounds.zip"),
              None, None, [[Fake(SA / "cta_video_1.mp4")]])

df = pd.read_excel(SA / "sample_5_videos.xlsx", engine="openpyxl")
df.to_excel(assets / "input.xlsx", index=False, engine="openpyxl")
print(f"sheet: {len(df)} rows, headlines = {df['Headline'].tolist()[:3]}...")
assert "Caption" not in df.columns, "sheet should start without captions"

store.create_job(
    kind=store.KIND_RENDER,
    params={"render_config": asdict(RenderConfig(crf=30, preset="veryfast")),
            "workers": 4, "make_zip": False, "captions": True},
    label="caption-naming-test",
    items=[{"idx": i} for i in range(1, len(df) + 1)],
    job_id=job_id,
)
worker.main(["--once"])

job = store.get_job(job_id)
res = job["result"]
assert job["status"] == store.STATUS_SUCCEEDED, job.get("error")
print("\ncaption info:", res.get("captions"))
assert res["captions"]["applied"] == len(df), res["captions"]

files = sorted(p.name for p in Path(res["videos_dir"]).rglob("*.mp4"))
print("\nrendered filenames:")
for f in files:
    print("   ", f)

# The on-disk file is the SHORT name built from that video's caption + 1 hashtag,
# and must not be the headline-derived name it would have had before.
from captions import naming
from video_generator import safe_filename

manifest = pd.read_excel(res["sheet_path"], engine="openpyxl")
headlines = pd.read_excel(SA / "sample_5_videos.xlsx", engine="openpyxl")["Headline"]

for _, row in manifest.iterrows():
    short, long_name = naming.build_names(row["Caption"], row["Hashtags"])
    assert row["Short_Filename"] == short, (row["Short_Filename"], short)
    assert row["Long_Filename"] == long_name
    assert short in files, f"{short} not on disk; got {files}"
    assert len(short) <= naming.MAX_SHORT, len(short)
    assert short.count("#") == 1, short
    would_have_been = safe_filename(int(row["Sheet_Row"]),
                                    headlines.iloc[int(row["Sheet_Row"]) - 1])
    assert short != would_have_been, "still named from the headline"
    print(f"   row {int(row['Sheet_Row'])}: {would_have_been}  ->  {short}")
print("\nevery filename comes from its Caption, not its Headline")

# Paste-ready: no numeric prefix, spaces and # preserved, names unique.
assert not any(f[:3].isdigit() and f[3] == "_" for f in files), \
    "numeric prefix should be gone — names are meant to be pasted as captions"
assert len(set(files)) == len(files), "duplicate filenames!"
assert any(" " in f and "#" in f for f in files)
print("no numeric prefix, all unique, spaces + # preserved")

# Emoji are STRIPPED by default, from the filename and from the caption text.
assert not any("🔥" in f for f in files), f"emoji leaked into a filename: {files}"
s_strip, l_strip = naming.build_names("fire sale 🔥🎉 today", "#deal")
assert "🔥" not in s_strip and "🎉" not in s_strip, s_strip
assert "🔥" not in l_strip, l_strip
s_keep, _ = naming.build_names("fire sale 🔥 today", "#deal", keep_emoji=True)
assert "🔥" in s_keep, s_keep
# Typography a caption legitimately uses must NOT be stripped with the emoji.
s_dash, _ = naming.build_names("stop — really 🔥", "#x")
assert "—" in s_dash and "🔥" not in s_dash, s_dash
# No double spaces left where an emoji was removed.
assert "  " not in s_strip, repr(s_strip)
print(f"emoji stripped by default: {s_strip}")
print(f"  BVG_FILENAME_KEEP_EMOJI=true: {s_keep}")
print(f"  em dash kept, emoji gone: {s_dash}")

# The manifest is the artifact that says what to post where.
for col in ("Caption", "Hashtags", "Short_Filename", "Long_Filename",
            "Folder", "Source_Batch", "Sheet_Row"):
    assert col in manifest.columns, list(manifest.columns)
assert manifest["Caption"].notna().all()
print("\nmanifest:")
print(manifest[["Sheet_Row", "Caption", "Short_Filename"]].to_string(index=False))

# The original sheet is still kept alongside the batch.
batch_sheet = store.job_dir(job_id) / "batch_sheet.xlsx"
assert batch_sheet.is_file(), "original sheet must be preserved"
orig = pd.read_excel(batch_sheet, engine="openpyxl")
assert "Headline" in orig.columns and "BG_Image" in orig.columns
print("\noriginal sheet preserved with columns:", list(orig.columns)[:4], "...")

# Pool cursor advanced by exactly the number of rows.
pool = store.active_pool()
assert pool["cursor"] == len(df), f"cursor={pool['cursor']}, rows={len(df)}"
print(f"\npool cursor advanced to {pool['cursor']} (= {len(df)} rows)")

print("\nCAPTION NAMING E2E PASSED")
