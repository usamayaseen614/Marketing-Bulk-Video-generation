"""Failing over to a shadow service account when the 750 GB/24h allowance runs out.

The trigger is deliberately hard to pull. Drive answers "you are going too fast
right now" and "your allowance for the day is gone" with the SAME 403
userRateLimitExceeded, so the only evidence that separates them is behavioural:
the full retry budget spent, AND a 2-byte test upload also refused. Switching on
anything less would spend the shadow's allowance on a blip — which is the very
resource this exists to protect.
"""
import os, sys, tempfile
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="failover_")

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from integrations import drive

PRIMARY = "video-uploads-2@companion-app-26947.iam.gserviceaccount.com"
SHADOW = "video-uploads-3@companion-app-26947.iam.gserviceaccount.com"


class _Resp:
    def __init__(self, status):
        self.status = status


class _HttpError(Exception):
    """Shaped like googleapiclient's HttpError: a .resp.status and a body that
    renders into str(), which is what _is_throttled actually reads."""
    def __init__(self, status, text):
        super().__init__(text)
        self.resp = _Resp(status)


def _throttle(status=403):
    return _HttpError(status, "User rate limit exceeded. userRateLimitExceeded")


def _reset(fallbacks=(SHADOW,)):
    config.DRIVE_IMPERSONATE = PRIMARY
    config.DRIVE_IMPERSONATE_FALLBACKS = list(fallbacks)
    drive._identity_index = 0
    drive._identities_spent = False
    drive._identity_switched_at = 0.0


drive._sleep = lambda _s: None          # no real backoff in tests
config.DRIVE_RETRY_ATTEMPTS = 3


# ---- the ring ---------------------------------------------------------------
_reset()
assert drive.identity_ring() == [PRIMARY, SHADOW], drive.identity_ring()
assert drive.current_identity() == PRIMARY
# A duplicate address must not become a second hop that publishes nowhere new.
config.DRIVE_IMPERSONATE_FALLBACKS = [PRIMARY, SHADOW]
assert drive.identity_ring() == [PRIMARY, SHADOW], drive.identity_ring()
print("ring ok:", drive.identity_ring())


# ---- only a 403 exhaustion is a QuotaExhausted ------------------------------
_reset()
for status, expect_quota in ((403, True), (429, False), (503, False)):
    try:
        drive._retry_call(lambda: (_ for _ in ()).throw(_throttle(status)), "upload of x")
        raise AssertionError("should have given up")
    except drive.DriveError as exc:
        is_quota = isinstance(exc, drive.QuotaExhausted)
        assert is_quota is expect_quota, f"{status} -> QuotaExhausted={is_quota}"
assert issubclass(drive.QuotaExhausted, drive.DriveError), "must stay catchable as DriveError"
print("403 exhaustion is QuotaExhausted; 429 and 5xx are not")


# ---- the probe decides: a tiny file that lands means throttling, not the wall
_reset()
probe_calls = []


def _probe_says(refused):
    def _probe(parent_id):
        probe_calls.append(parent_id)
        return refused
    return _probe


drive._allowance_probe = _probe_says(False)      # 2 bytes still lands
attempts = []


def _always_exhausted():
    attempts.append(drive.current_identity())
    raise drive.QuotaExhausted("spent")


try:
    drive._with_failover(_always_exhausted, "upload of yt.zip", "parent1")
    raise AssertionError("should have re-raised")
except drive.QuotaExhausted:
    pass
assert attempts == [PRIMARY], attempts
assert drive.current_identity() == PRIMARY, "a landing probe must NOT switch"
assert probe_calls == ["parent1"], probe_calls
print("probe lands -> stays on the primary, re-raises (burst throttling)")


# ---- the real thing: probe refused too -> switch and retry ------------------
_reset()
drive._allowance_probe = _probe_says(True)
seen = []


def _spent_once():
    who = drive.current_identity()
    seen.append(who)
    if who == PRIMARY:
        raise drive.QuotaExhausted("spent")
    return {"id": "ok", "size": 10}


got = drive._with_failover(_spent_once, "upload of yt.zip", "parent1")
assert got == {"id": "ok", "size": 10}, got
assert seen == [PRIMARY, SHADOW], seen
assert drive.current_identity() == SHADOW, drive.current_identity()
assert drive.acting_identity() == SHADOW, "acting_identity must name who is spending NOW"
assert not drive.identities_spent(), "one account left is not 'all spent'"
print("primary spent -> switched to the shadow and the upload completed")


# ---- the switch must invalidate every thread's cached client ----------------
# The whole feature is inert without this: eight pool workers each hold a client
# built from the spent account's credentials and would keep using it.
built = []


class _FakeLocalService:
    pass


drive._import_google = lambda: (None, None,
                                lambda *a, **k: built.append(k.get("credentials")) or object(),
                                None)
drive._credentials = lambda: f"creds-for-{drive.current_identity()}"
drive._local.service = None
first = drive.service()
assert built == [f"creds-for-{SHADOW}"], built
assert drive.service() is first, "same generation must reuse the cached client"
with drive._identity_lock:
    drive._use_identity(0)
second = drive.service()
assert second is not first, "a switch must rebuild this thread's client"
assert built[-1] == f"creds-for-{PRIMARY}", built
print("generation counter rebuilds the per-thread client on a switch")


# ---- the last account running out sets the spent flag ----------------------
_reset(fallbacks=())            # a single-account deployment
drive._allowance_probe = _probe_says(True)
solo = []
try:
    drive._with_failover(lambda: solo.append(1) or (_ for _ in ()).throw(
        drive.QuotaExhausted("spent")), "upload of yt.zip", "parent1")
    raise AssertionError("should have re-raised")
except drive.QuotaExhausted:
    pass
assert solo == [1], "one attempt only — nothing to fail over to"
assert not drive.identities_spent(), "no fallbacks configured is not 'all spent'"
print("single account: re-raises immediately, unchanged behaviour")

_reset()
drive._allowance_probe = _probe_says(True)
try:
    drive._with_failover(lambda: (_ for _ in ()).throw(
        drive.QuotaExhausted("spent")), "upload of yt.zip", "parent1")
    raise AssertionError("should have re-raised")
except drive.QuotaExhausted:
    pass
assert drive.identities_spent(), "every account refused -> spent flag set"
assert drive.current_identity() == SHADOW, "parked on the last one"
print("every account spent -> flag set so the runner can stop early")


# ---- begin_run clears the flag and returns to the head after 24h -----------
import time as _time

drive.begin_run()
assert not drive.identities_spent(), "begin_run must clear last run's flag"
assert drive.current_identity() == SHADOW, "a recent switch must be kept"

drive._identity_switched_at = _time.time() - (25 * 3600)
drive.begin_run()
assert drive.current_identity() == PRIMARY, "a day-old switch returns to the primary"
print("begin_run: clears the flag, and rewinds to the primary after 24h")

print("\nALL DRIVE FAILOVER TESTS PASSED")
