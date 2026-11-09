"""Saved jobs (presets.py) and staging files from them (workspace.py).

What has to hold, and what breaks if it doesn't:

  * a saved job round-trips: every field value, every file, byte for byte —
    or "Load" quietly renders something other than what was queued
  * identical files are stored once, however many saved jobs use them — the
    same 18 GB promo set saved nightly would otherwise fill the disk
  * deleting one saved job never takes a file another one still uses
  * staging a saved file COPIES it — the voiceover stage rewrites the staged
    sheet in place, and through a hard link that would corrupt the saved copy
  * progress callbacks add up to exactly the size the progress bar was given,
    or the bar stalls short of 100% or runs past it
  * ids are filenames, so anything that could escape the folder is refused
"""
import io, os, sys, tempfile, zipfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="presettest_"))
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = str(SCRATCH / "jobs")
os.environ["BVG_PRESETS_MAX"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import presets
from workspace import stage_uploads, staged_bytes, workspace_from_dir

assert config.PRESETS_ROOT == (SCRATCH / "jobs" / "_presets").resolve(), config.PRESETS_ROOT


class Upload(io.BytesIO):
    """What Streamlit hands app.py: a BytesIO with a name and a size."""
    def __init__(self, name: str, data: bytes):
        super().__init__(data)
        self.name = name
        self.size = len(data)


def blobs() -> set[str]:
    folder = config.PRESETS_ROOT / "blobs"
    return {p.name for p in folder.iterdir()} if folder.is_dir() else set()


class Counter:
    def __init__(self):
        self.total = 0
        self.calls = 0

    def __call__(self, n):
        self.total += n
        self.calls += 1


# ---------------------------------------------------------------- save + read back
promo = os.urandom(3 * 1024 * 1024 + 17)          # under one 8 MB chunk
big = os.urandom(9 * 1024 * 1024 + 5)              # two chunks, so progress fires twice
sheet = b"PK fake xlsx bytes"
files = {
    "excel": [Upload("data.xlsx", sheet)],
    "promos": [Upload("promo_a.mp4", promo), Upload("promo_b.mp4", big)],
    "cta_image": [None],                           # empty slots are skipped
    "gif_pool": [],
}
values = {"music_volume": 25, "layout_mode": "split", "voice_set_kokoro": ["af_heart"],
          "range": (1, 2), "not_json": object()}

want = presets.bytes_to_save(files)
assert want == len(sheet) + len(promo) + len(big), want
progress = Counter()
first = presets.save("Week 32 set 1", values, files,
                     summary={"rows": 5, "batches": 2, "videos": 10},
                     job_id="job123", progress=progress)
assert progress.total == want, (progress.total, want)
assert progress.calls >= 4, "a 9 MB file should report more than once"
assert first["missing"] == []
assert first["values"]["range"] == [1, 2], "tuples come back as lists"
assert "not_json" not in first["values"], "unserialisable values are dropped, not fatal"
assert set(first["files"]) == {"excel", "promos"}, first["files"]
assert len(blobs()) == 3, blobs()

listed = presets.list_presets()
assert [r["id"] for r in listed] == [first["id"]]
again = presets.get(first["id"])
assert again["values"]["music_volume"] == 25
assert again["summary"]["videos"] == 10 and again["job_id"] == "job123"

loaded, missing = presets.load(again)
assert missing == []
assert [f.name for f in loaded["promos"]] == ["promo_a.mp4", "promo_b.mp4"]
assert loaded["promos"][1].getvalue() == big
assert loaded["promos"][1].size == len(big)
assert loaded["excel"][0].getvalue() == sheet
print("round trip ok:", presets.file_count(again), "files,",
      presets.total_size(again), "bytes")

# ---------------------------------------------------------------- dedup
# The same promos uploaded again under new names: stored once.
before = blobs()
second = presets.save("Week 33", {}, {"promos": [Upload("renamed.mp4", big)]})
assert blobs() == before, "an identical re-upload must not add a blob"
assert presets.disk_usage() == len(sheet) + len(promo) + len(big)

# Re-saving files that came FROM a saved job writes nothing and reports nothing.
reuse = {"promos": loaded["promos"], "excel": loaded["excel"]}
assert presets.bytes_to_save(reuse) == 0
progress = Counter()
third = presets.save("Week 33 again", {}, reuse, progress=progress)
assert progress.total == 0 and progress.calls == 0
assert blobs() == before
print("dedup ok:", len(blobs()), "blobs for 3 saved jobs")

# ---------------------------------------------------------------- delete + collect
# Deleting the first leaves its files: the third still uses all of them except
# the sheet... which it also uses. Nothing may go.
assert presets.delete(first["id"]) is True
assert presets.get(first["id"]) is None
assert presets.gc_blobs(grace_seconds=0) == []
assert blobs() == before

# Delete the third: the sheet and promo_a are now unreferenced, but young —
# a session that loaded them a moment ago may still be using them.
assert presets.delete(third["id"])
assert blobs() == before, "unreferenced files survive the grace period"
gone = presets.gc_blobs(grace_seconds=0)
assert len(gone) == 2, gone
assert len(blobs()) == 1, "the file Week 33 still uses must survive"
assert presets.load(presets.get(second["id"]))[0]["promos"][0].getvalue() == big
assert presets.delete(third["id"]) is False, "deleting twice reports nothing deleted"
print("delete ok: shared file kept, orphans collected")

# A saved job whose file vanished from disk loads what it can and says what not.
presets.save("half gone", {}, {"excel": [Upload("x.xlsx", b"x")]})
victim = presets.list_presets()[0]
presets.blob_path(victim["files"]["excel"][0]["sha256"]).unlink()
files_now, missing = presets.load(victim)
assert files_now == {} and missing == ["x.xlsx"], (files_now, missing)
stale = presets.StoredFile("x.xlsx", 1, victim["files"]["excel"][0]["sha256"],
                           presets.blob_path(victim["files"]["excel"][0]["sha256"]))
resaved = presets.save("from a stale load", {}, {"excel": [stale]})
assert resaved["missing"] == ["x.xlsx"] and resaved["files"] == {}
print("missing files reported, not fatal")

# ---------------------------------------------------------------- re-runs
# The same job queued again replaces its older copy; any difference keeps both.
again_files = lambda: {"excel": [Upload("same.xlsx", b"same")]}
r1 = presets.save("nightly", {"crf": 18}, again_files())
r2 = presets.save("nightly", {"crf": 18}, again_files())
assert presets.get(r1["id"]) is None and presets.get(r2["id"]), "identical re-save must replace"
r3 = presets.save("nightly", {"crf": 20}, again_files())
assert presets.get(r2["id"]) and presets.get(r3["id"]), "a changed field is a different job"
r4 = presets.save("nightly", {"crf": 20}, {"excel": [Upload("same.xlsx", b"other")]})
assert presets.get(r3["id"]) and presets.get(r4["id"]), "a changed file is a different job"
print("re-runs ok: identical copies collapse, real changes do not")

# ---------------------------------------------------------------- cap
config.PRESETS_MAX = 2
for name in ("a", "b", "c"):
    presets.save(name, {}, {})
names = [r["name"] for r in presets.list_presets()]
assert names == ["c", "b"], names
config.PRESETS_MAX = 0
print("cap ok:", names)

# ---------------------------------------------------------------- hostile input
for bad in ("../../etc/passwd", "a/b", "", "x" * 200, "..", "a.json"):
    assert presets.get(bad) is None, bad
    assert presets.delete(bad) is False, bad
for bad in ("../x", "abc", "0" * 63):
    try:
        presets.blob_path(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"blob_path accepted {bad!r}")
(config.PRESETS_ROOT / "presets" / "broken.json").write_text("{not json")
(config.PRESETS_ROOT / "presets" / "other.json").write_text('{"id": "mismatch"}')
assert all(r["id"] not in ("broken", "mismatch") for r in presets.list_presets())
print("hostile input refused")

# ---------------------------------------------------------------- staging
# One job's worth of uploads, half fresh and half loaded from a saved job.
zbuf = io.BytesIO()
with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("bg1.png", os.urandom(4000))
    zf.writestr("bg2.jpg", b"\0" * 50_000)         # compresses well
bg_zip = Upload("backgrounds.zip", zbuf.getvalue())
cta = Upload("cta.png", b"cta-bytes")
saved = presets.save("staging source", {}, {
    "promos": [Upload("p1.mp4", promo)], "bg_zip": [bg_zip],
    "gif_pool": [Upload("g1.mp4", b"gif-one"), Upload("g2.mp4", b"gif-two")],
})
stored, _ = presets.load(saved)

args = (stored["promos"] + [Upload("p2.mp4", big)], stored["bg_zip"][0], cta, None,
        [[Upload("clip.mp4", b"clip")], []], stored["gif_pool"], None, None, None)
dest = SCRATCH / "job" / "assets"
progress = Counter()
stage_uploads(dest, *args, progress=progress)
assert progress.total == staged_bytes(*args), (progress.total, staged_bytes(*args))

ws = workspace_from_dir(dest, SCRATCH / "job" / "work")
assert [p.name for p in ws.video_paths] == ["input.mp4", "input_2.mp4"]
assert ws.video_paths[0].read_bytes() == promo and ws.video_paths[1].read_bytes() == big
assert sorted(p.name for p in ws.bg_dir.iterdir()) == ["bg1.png", "bg2.jpg"]
assert [p.name for p in ws.gif_paths] == ["g1.mp4", "g2.mp4"]
assert ws.cta_path.read_bytes() == b"cta-bytes"
assert [len(s) for s in ws.cta_video_slots] == [1, 0]

# Rewriting a staged file in place (as the voiceover stage does to the sheet)
# must leave the saved copy alone.
saved_promo = stored["promos"][0].path
assert os.stat(ws.video_paths[0]).st_ino != os.stat(saved_promo).st_ino, \
    "staged file shares an inode with the saved one"
ws.video_paths[0].write_bytes(b"rewritten in place")
assert saved_promo.read_bytes() == promo
print("staging ok:", progress.calls, "progress calls,", progress.total, "bytes")

# No progress callback: the old positional call shape still works unchanged.
stage_uploads(SCRATCH / "job2", stored["promos"][0], None, None, None)
assert (SCRATCH / "job2" / "input.mp4").read_bytes() == promo

print("\nALL PRESET TESTS PASSED")
