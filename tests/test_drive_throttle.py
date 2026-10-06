"""Riding out Drive's 403 rate limits without losing a 30 GB upload.

Drive answers a sustained transfer with `403 userRateLimitExceeded`, which
reads exactly like a permission error and is not one. Treating it as fatal
throws away hours of work; retrying it blindly duplicates non-idempotent
writes. The line between those two is what these tests pin down.

The 750 GB per rolling 24 hours that one user (a service account included) may
move into Drive arrives as the same 403 — uploads AND server-side copies both
count against it."""
import os, sys, tempfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="throttle_")
os.environ["BVG_DRIVE_SHARED_DRIVE_ID"] = "0AFakeSharedDrive"
os.environ["BVG_DRIVE_RETRY_ATTEMPTS"] = "4"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from integrations import drive

slept: list[float] = []
drive._sleep = slept.append          # never actually wait in a test


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "Forbidden"


class _HttpError(Exception):
    """Shaped like googleapiclient's HttpError: a .resp with a status, and a
    str() carrying the JSON body — which is where the reason lives."""

    def __init__(self, status, reason=""):
        self.resp = _Resp(status)
        body = (f'{{"error": {{"errors": [{{"domain": "usageLimits", '
                f'"reason": "{reason}"}}], "code": {status}}}}}')
        super().__init__(f"<HttpError {status} when requesting None returned "
                         f"\"{reason}\". Details: {body}>")


RATE_LIMITED = lambda: _HttpError(403, "userRateLimitExceeded")
FORBIDDEN = lambda: _HttpError(403, "insufficientFilePermissions")


# ---- what counts as "slow down" vs "no" -------------------------------------
assert drive._is_throttled(RATE_LIMITED())
assert drive._is_throttled(_HttpError(429, "rateLimitExceeded"))
assert drive._is_throttled(_HttpError(503, ""))
assert not drive._is_throttled(FORBIDDEN()), \
    "a real permission error must NOT be mistaken for throttling"
assert not drive._is_throttled(_HttpError(404, "notFound"))
assert not drive._is_throttled(ValueError("something else entirely"))
print("classification: rate limits are retryable, permission errors are not")


# ---- a throttled call is retried, with growing backoff ----------------------
slept.clear()
attempts = {"n": 0}


def _flaky():
    attempts["n"] += 1
    if attempts["n"] < 3:
        raise RATE_LIMITED()
    return "landed"


assert drive._retry_call(_flaky, "upload of tk.zip") == "landed"
assert attempts["n"] == 3, attempts
assert len(slept) == 2 and slept[1] > slept[0], slept
print(f"throttled call: retried and succeeded, backing off {slept[0]:.0f}s "
      f"then {slept[1]:.0f}s")

# A permission error is not retried at all — waiting would never fix it.
slept.clear()
attempts["n"] = 0


def _denied():
    attempts["n"] += 1
    raise FORBIDDEN()


try:
    drive._retry_call(_denied, "upload of tk.zip")
    raise AssertionError("a permission error was retried")
except Exception as exc:
    assert isinstance(exc, _HttpError), type(exc)
assert attempts["n"] == 1 and not slept
print("permission error: raised immediately, unretried")

# Giving up says what to do about it, rather than surfacing a bare 403.
slept.clear()


def _always_limited():
    raise RATE_LIMITED()


try:
    drive._retry_call(_always_limited, "upload of tk.zip")
    raise AssertionError("throttling was retried forever")
except drive.DriveError as exc:
    text = str(exc)
    assert "750 GB" in text and "24 hours" in text, text
    assert "resumes rather than restarting" in text, text
assert len(slept) == 3, slept          # BVG_DRIVE_RETRY_ATTEMPTS=4
print("exhausted: fails with the 750 GB/24h explanation, not a bare 403")


# ---- the budget is per chunk, not per file ----------------------------------
# A 30 GB upload will be throttled repeatedly; as long as chunks keep landing
# it must never be abandoned for taking a long time.
class _Request:
    """Stands in for a resumable upload: throttled twice before every chunk."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.done = 0
        self.stalls = 0
        self.sent = 0

    def next_chunk(self, num_retries=0):
        self.stalls += 1
        if self.stalls <= 2:
            raise RATE_LIMITED()
        self.stalls = 0
        self.done += 1
        self.sent += 1
        if self.done >= self.chunks:
            return None, {"id": "zip-1", "name": "tk.zip", "size": "42"}
        return None, None


slept.clear()
request = _Request(chunks=6)
response = drive._run_resumable(request, "upload of tk.zip")
assert response["id"] == "zip-1"
assert request.sent == 6, "chunks were re-sent instead of resuming"
assert len(slept) == 12, len(slept)     # two stalls before each of six chunks
print("long upload: 12 throttles survived across 6 chunks — the budget resets "
      "every time one lands")


# ---- files.copy: refused is retried, ambiguous is not -----------------------
# A 403 is Drive saying it did NOT act, so repeating is safe. A 5xx or a
# dropped connection may have committed the copy and lost the answer, and
# repeating THAT is how one file becomes two with the same name in one folder.
slept.clear()
copies = {"n": 0}


def _throttled_copy():
    copies["n"] += 1
    if copies["n"] < 3:
        raise RATE_LIMITED()
    return {"id": "cp-1"}


assert drive._retry_call_refused(_throttled_copy, "copy to x.mp4") == {"id": "cp-1"}
assert copies["n"] == 3
print("copy: an explicit 403 refusal is safe to repeat, and is repeated")

for ambiguous in (_HttpError(503, ""), ConnectionResetError("socket reset")):
    slept.clear()
    tries = {"n": 0}

    def _ambiguous_copy(err=ambiguous):
        tries["n"] += 1
        raise err

    try:
        drive._retry_call_refused(_ambiguous_copy, "copy to x.mp4")
        raise AssertionError(f"an ambiguous failure was retried: {ambiguous!r}")
    except (_HttpError, ConnectionResetError):
        pass
    assert tries["n"] == 1 and not slept, \
        f"{type(ambiguous).__name__} was retried — that duplicates the copy"
print("copy: a 5xx or a dropped connection is NOT repeated — it may have landed")


# ---- chunk size is legal for Drive's resumable protocol ---------------------
import config

assert config.DRIVE_UPLOAD_CHUNK_BYTES % (256 * 1024) == 0, \
    "a resumable chunk size must be a multiple of 256 KB"
assert config.DRIVE_UPLOAD_CHUNK_BYTES >= config.DRIVE_CHUNK_BYTES
print(f"upload chunk: {config.DRIVE_UPLOAD_CHUNK_BYTES // 1024 ** 2} MB — "
      f"a 30 GB archive is {30 * 1024 ** 3 // config.DRIVE_UPLOAD_CHUNK_BYTES:,} "
      f"requests, not {30 * 1024 ** 3 // (8 * 1024 ** 2):,}")

print("\nALL DRIVE THROTTLE TESTS PASSED")
