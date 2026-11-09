"""Folder-level access to a Shared Drive, as opposed to membership.

A service account is usually given access to a *folder inside* a Shared Drive
rather than being added to the drive. It can then read and write that folder
perfectly well, but two calls that name the driveId come back
`404 Shared drive not found`:

    drives.get(driveId=…)                      → the drive's name
    files.list(corpora='drive', driveId=…)     → any search

The first used to fail the whole setup check, reporting a working destination
as broken. The second is worse: a search that finds nothing makes
ensure_folder() create a duplicate folder beside the real one, every run."""
import os, sys, tempfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="member_")
os.environ["BVG_DRIVE_SHARED_DRIVE_ID"] = "0ASharedDriveId"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from integrations import drive

DRIVE_ID = "0ASharedDriveId"


class _Resp:
    def __init__(self, status):
        self.status = status


class _HttpError(Exception):
    def __init__(self, status, message):
        self.resp = _Resp(status)
        super().__init__(f"<HttpError {status} … \"{message}\">")


NOT_A_MEMBER = lambda: _HttpError(404, f"Shared drive not found: {DRIVE_ID}")

calls = {"scoped": 0, "unscoped": 0, "drives_get": 0, "created": []}
EXISTING = {("renders", "root-folder"): "renders-id"}


class _Files:
    def list(self, **kw):
        scoped = "driveId" in kw
        calls["scoped" if scoped else "unscoped"] += 1
        if scoped:
            # What Drive says to a caller that is not a member of the drive.
            raise NOT_A_MEMBER()
        # The unscoped, parent-filtered query is the one that works.
        found = [{"id": fid, "name": name, "size": "1"}
                 for (name, parent), fid in EXISTING.items()
                 if f"name = '{name}'" in kw["q"] and f"'{parent}' in parents" in kw["q"]]
        return _Exec({"files": found})

    def create(self, body=None, **kw):
        calls["created"].append(body["name"])
        return _Exec({"id": f"new-{body['name']}"})

    def get(self, **kw):
        return _Exec({"id": kw.get("fileId"), "name": "A Folder",
                      "mimeType": drive.FOLDER_MIME, "driveId": DRIVE_ID})

    def copy(self, fileId=None, body=None, **kw):
        return _Exec({"id": f"copy-of-{fileId}", "name": body["name"]})

    def delete(self, **kw):
        return _Exec({})


class _Drives:
    def get(self, **kw):
        calls["drives_get"] += 1
        raise NOT_A_MEMBER()


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Service:
    def files(self):
        return _Files()

    def drives(self):
        return _Drives()


drive.service = _Service
drive._local.target = None
drive._local.no_corpus = set()


# ---- a search must survive the drive being invisible as a corpus ------------
found = drive.find_folder("renders", "root-folder")
assert found == "renders-id", \
    "the folder exists but the scoped search 404'd — ensure_folder would now " \
    "create a SECOND 'renders' beside it"
assert calls["scoped"] == 1 and calls["unscoped"] == 1, calls
print("find_folder: scoped search 404s, unscoped fallback finds the folder")

# ...and the doomed call is not repeated for every lookup afterwards.
drive.find_folder("renders", "root-folder")
drive.find_folder("2026-08-07", "renders-id")
assert calls["scoped"] == 1, "the 404 was re-attempted after we learned better"
assert calls["unscoped"] == 3, calls
print("find_folder: the corpus 404 is learned once, not paid per lookup")

# An existing folder is reused rather than duplicated.
assert drive.ensure_folder("renders", "root-folder") == "renders-id"
assert not calls["created"], f"created a duplicate folder: {calls['created']}"
assert drive.ensure_folder("brand-new", "root-folder") == "new-brand-new"
print("ensure_folder: reuses what the fallback found, creates only what's absent")

# A 404 is specifically what the fallback is for — anything else must surface.
class _Files403(_Files):
    def list(self, **kw):
        raise _HttpError(403, "insufficientFilePermissions")


class _Service403(_Service):
    def files(self):
        return _Files403()


drive.service = _Service403
drive._local.no_corpus = set()
try:
    drive.find_folder("renders", "root-folder")
    raise AssertionError("a permission error was swallowed by the fallback")
except _HttpError as exc:
    assert exc.resp.status == 403
print("find_folder: a 403 still raises — only the corpus 404 is worked around")


# ---- the setup check must not fail on an unreadable drive NAME --------------
drive.service = _Service
drive._local.no_corpus = set()
ok, message = drive.check_access()
assert ok, f"a working destination was reported as broken:\n{message}"
assert calls["drives_get"] >= 1, "the name lookup was skipped entirely"
assert "not a member of the Shared Drive" in message, message
assert DRIVE_ID in message, "with no name to show, the id should stand in"
print("check_access: passes on folder-level access, and says so plainly")


# ---- non-member + aimed at the drive ROOT: name the real fix ----------------
# Creating a folder at a Shared Drive's root is a member's privilege. Told only
# "probably Viewer instead of Content Manager", you go and check permissions
# that are already correct.
class _FilesNoRootWrite(_Files):
    def create(self, body=None, **kw):
        raise _HttpError(403, "insufficientFilePermissions")


class _ServiceNoRootWrite(_Service):
    def files(self):
        return _FilesNoRootWrite()


drive.service = _ServiceNoRootWrite
drive._local.no_corpus = set()
drive._local.target = None
ok, message = drive.check_access()
assert not ok
assert "not a member of that Shared Drive" in message, message
assert "Point at a folder instead of the drive" in message, message
print("check_access: root-write refusal explains membership, not Viewer/Editor")


# ---- out of upload allowance is NOT a permission problem --------------------
# The signature: folders can still be created, but a 2-byte file cannot be
# uploaded. Nothing about roles or request rate can do that — it is the 750 GB
# per rolling 24 hours being spent. Telling someone to check permissions here
# sends them to audit the one thing just proven to work.
def _out_of_quota(detailed: bool):
    """The two shapes this arrives in. The upload endpoint does not always
    include the structured `reason` the metadata API does, so the plain
    message has to be enough on its own."""
    if detailed:
        return _HttpError(403, "User rate limit exceeded.\". Details: \"[{'message': "
                               "'User rate limit exceeded.', 'domain': "
                               "'usageLimits', 'reason': 'userRateLimitExceeded'}]")
    return _HttpError(403, "User rate limit exceeded.")


for detailed in (True, False):
    class _FilesOutOfQuota(_Files):
        def create(self, body=None, media_body=None, _d=detailed, **kw):
            if media_body is not None:
                raise _out_of_quota(_d)
            return super().create(body=body, **kw)

    class _ServiceOutOfQuota(_Service):
        def files(self):
            return _FilesOutOfQuota()

    drive.service = _ServiceOutOfQuota
    drive._local.no_corpus = set()
    drive._local.target = None
    ok, message = drive.check_access()
    assert not ok
    assert "750 GB" in message and "rolling 24 hours" in message, message
    assert "**access is fine**" in message, message
    assert "Content Manager" not in message, \
        "a spent upload allowance was blamed on permissions"
    assert "Viewer and Commenter" not in message, message
print("check_access: an exhausted upload allowance is named, not misread as a "
      "role — with or without the structured reason")

# Running out of STORAGE is the opposite case: waiting never fixes it, so it
# must NOT be reported as an allowance to wait out.
storage_full = _HttpError(403, "The user has exceeded their Drive storage "
                               "quota\". reason: 'storageQuotaExceeded'")
assert not drive._is_throttled(storage_full), \
    "a full destination was mistaken for throttling — it would retry forever"
print("classification: out of storage is not out of allowance")

print("\nALL DRIVE MEMBERSHIP TESTS PASSED")
