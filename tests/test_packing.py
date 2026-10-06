"""The archive builder: names, dedup, part-files, verification, freeing.

Every assertion here is about something that silently ruins a 30 GB archive if
it goes wrong — two entries with one name, a half-written ZIP that looks
finished, or sources deleted before they were safely inside every archive."""
import os, sys, tempfile, zipfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="packing_")

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import packing

WORK = Path(tempfile.mkdtemp(prefix="packwork_"))


def make_video(name: str, size: int = 1024) -> Path:
    path = WORK / "videos" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(range(256)) * (size // 256))
    return path


# ---- ZipWriter --------------------------------------------------------------

one = make_video("one.mp4")
two = make_video("two.mp4")

with packing.ZipWriter(WORK / "basic.zip") as archive:
    assert (WORK / "basic.zip.part").is_file(), "must build under .part"
    assert not (WORK / "basic.zip").exists(), "a building archive must not look done"
    archive.add(one, "first video #a.mp4")
    archive.add(two, "second video #b.mp4")
    archive.add(WORK / "videos" / "gone.mp4", "gone.mp4")

assert (WORK / "basic.zip").is_file() and not (WORK / "basic.zip.part").exists()
assert archive.files == 2 and archive.missing == ["gone.mp4"]
with zipfile.ZipFile(WORK / "basic.zip") as zf:
    assert zf.namelist() == ["first video #a.mp4", "second video #b.mp4"]
    assert zf.getinfo("first video #a.mp4").compress_type == zipfile.ZIP_STORED
print("ZipWriter: builds under .part, renames on close, skips missing sources")

# A ZIP happily holds two entries with the same name and extractors then
# overwrite one with the other — which is a video silently lost.
with packing.ZipWriter(WORK / "dupes.zip") as archive:
    archive.add(one, "same.mp4")
    archive.add(two, "same.mp4")
    archive.add(one, "same.mp4")
with zipfile.ZipFile(WORK / "dupes.zip") as zf:
    assert zf.namelist() == ["same.mp4", "same_2.mp4", "same_3.mp4"], zf.namelist()
print("ZipWriter: repeated names are numbered, never collapsed")

# An aborted build must leave nothing behind that a re-run could mistake for
# a finished archive.
try:
    with packing.ZipWriter(WORK / "doomed.zip") as archive:
        archive.add(one, "one.mp4")
        raise RuntimeError("worker killed mid-pack")
except RuntimeError:
    pass
assert not (WORK / "doomed.zip").exists(), "a failed build left a finished-looking ZIP"
assert not (WORK / "doomed.zip.part").exists(), "a failed build left its part-file"
print("ZipWriter: an interrupted build leaves no archive and no debris")


# ---- build_platform_zips ----------------------------------------------------

sources = [make_video(f"clip_{n}.mp4") for n in range(4)]
entries = [(src, {"yt": f"short {n} #a.mp4", "tk": f"long {n} #a #b #c.mp4"})
           for n, src in enumerate(sources)]
entries.append((WORK / "videos" / "missing.mp4", {"yt": "x.mp4", "tk": "y.mp4"}))

seen = []
built = packing.build_platform_zips(
    entries,
    {"yt": WORK / "pair" / "yt.zip", "tk": WORK / "pair" / "tk.zip"},
    delete_sources=True,
    on_progress=lambda done, total: seen.append((done, total)),
)

assert built["files"] == 4 and built["missing"] == ["missing.mp4"]
assert seen[-1] == (4, 5), seen
with zipfile.ZipFile(WORK / "pair" / "yt.zip") as zf:
    assert zf.namelist() == [f"short {n} #a.mp4" for n in range(4)]
with zipfile.ZipFile(WORK / "pair" / "tk.zip") as zf:
    assert zf.namelist() == [f"long {n} #a #b #c.mp4" for n in range(4)]
    assert zf.read("long 2 #a #b #c.mp4") == bytes(range(256)) * 4, \
        "the bytes in tk.zip are not the source's"
assert all(not src.exists() for src in sources), "sources were not freed"
print("build_platform_zips: one pass fills both archives, each with its own names")

# The source is only freed once it is inside EVERY archive — otherwise a
# failure on the second one leaves nothing to rebuild from.
survivor = make_video("survivor.mp4")


class _Boom(RuntimeError):
    pass


class _FailingWriter(packing.ZipWriter):
    def add(self, src, arcname=None, *, delete_source=False):
        if self.path.name.startswith("tk"):
            raise _Boom("disk full while writing tk.zip")
        return super().add(src, arcname, delete_source=delete_source)


real_writer = packing.ZipWriter
packing.ZipWriter = _FailingWriter
try:
    packing.build_platform_zips(
        [(survivor, {"yt": "s.mp4", "tk": "s.mp4"})],
        {"yt": WORK / "half" / "yt.zip", "tk": WORK / "half" / "tk.zip"},
        delete_sources=True)
    raise AssertionError("the simulated failure never fired")
except _Boom:
    pass
finally:
    packing.ZipWriter = real_writer

assert survivor.is_file(), "the source was deleted before it was in both archives"
assert not (WORK / "half" / "yt.zip").exists(), "a failed pair left one archive behind"
assert not (WORK / "half" / "yt.zip.part").exists()
print("build_platform_zips: a failure keeps the sources and discards both archives")


# ---- verify -----------------------------------------------------------------

report = packing.verify(WORK / "pair" / "tk.zip", expected_files=4)
assert report["files"] == 4 and report["bytes"] == 4 * 1024
assert report["size"] >= report["bytes"], "a stored ZIP cannot be smaller than its contents"

for kwargs in ({"expected_files": 5}, {"expected_bytes": 999}):
    try:
        packing.verify(WORK / "pair" / "tk.zip", **kwargs)
        raise AssertionError(f"verify accepted a wrong archive: {kwargs}")
    except packing.PackError:
        pass

truncated = WORK / "truncated.zip"
truncated.write_bytes((WORK / "pair" / "tk.zip").read_bytes()[:512])
try:
    packing.verify(truncated)
    raise AssertionError("verify accepted a truncated ZIP")
except packing.PackError:
    pass
print("verify: entry count, byte count and unreadable archives all rejected")

# deep=True re-reads the payload, which catches damage the central directory
# cannot: same length, wrong bytes.
corrupt = WORK / "corrupt.zip"
data = bytearray((WORK / "pair" / "tk.zip").read_bytes())
data[100:110] = b"\x00" * 10
corrupt.write_bytes(bytes(data))
packing.verify(corrupt, expected_files=4)          # the directory still checks out
try:
    packing.verify(corrupt, deep=True)
    raise AssertionError("deep verify accepted corrupted payload bytes")
except packing.PackError:
    pass
print("verify(deep=True): corrupted payload caught by its CRC")


# ---- free_files -------------------------------------------------------------

doomed = [make_video(f"free_{n}.mp4") for n in range(3)]
count, freed = packing.free_files(doomed + [WORK / "videos" / "never.mp4"])
assert count == 3 and freed == 3 * 1024, (count, freed)
assert all(not p.exists() for p in doomed)
print("free_files: reclaims what exists and shrugs at what doesn't")

print("\nALL PACKING TESTS PASSED")
