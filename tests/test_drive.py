"""Drive helpers that don't need credentials, plus the unconfigured-skip path."""
import os, sys, tempfile
from pathlib import Path

# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="drivetest_")
os.environ.pop("BVG_DRIVE_SHARED_DRIVE_ID", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from integrations import drive

# ---- unique_names: the whole point is repeated names, which a dict would collapse
got = drive.unique_names(["001_Sale.mp4", "001_Sale.mp4", "001_Sale.mp4", "002_New.mp4"])
assert got == ["001_Sale.mp4", "001_Sale_2.mp4", "001_Sale_3.mp4", "002_New.mp4"], got
print("duplicates separated:", got)

# parallel to input, so zip() against the item list stays aligned
src = ["a.mp4", "b.mp4", "a.mp4"]
assert len(drive.unique_names(src)) == len(src)

# names without an extension still work
assert drive.unique_names(["noext", "noext"]) == ["noext", "noext_2"]
# a dotfile-ish name doesn't lose its stem
assert drive.unique_names(["x.tar.gz", "x.tar.gz"]) == ["x.tar.gz", "x.tar_2.gz"]
print("edge cases ok")

# empty input
assert drive.unique_names([]) == []

# ---- query escaping: a folder name with an apostrophe must not break the query
assert drive._escape("Usama's clips") == "Usama\\'s clips"
print("query escaping ok:", drive._escape("Usama's clips"))

# ---- unconfigured behaviour is a clear message, not a crash
assert config.drive_configured() is False
ok, msg = drive.check_access()
assert ok is False and "BVG_DRIVE_SHARED_DRIVE_ID" in msg, msg
print("unconfigured check_access:", msg[:70])

try:
    drive._require_config()
    raise AssertionError("should have raised")
except drive.DriveError as exc:
    assert "BVG_DRIVE_SHARED_DRIVE_ID" in str(exc)
    print("_require_config raises DriveError as expected")

# ---- upload_many with nothing to do is a no-op, not an API call
assert drive.upload_many([], "parent") == (0, 0)
print("empty upload_many ok")

print("\nALL DRIVE UNIT TESTS PASSED")
