"""Pool generation with the chunks running concurrently.

Gemini is stubbed — what is under test is the harness around it, which is where
the 30-minute build actually lived. The properties that matter:

  * chunks really do overlap (the whole point), and stay within the cap
  * de-duplication still holds across concurrent chunks
  * the exact count is met, and never exceeded
  * a throttled chunk is retried instead of taking the run down with it
  * a chunk that is beyond saving costs one chunk, not the pool...
  * ...unless nothing at all got through, which must be raised, not returned
    as a silently empty pool
"""
import json, os, sys, tempfile, threading, time
from pathlib import Path

os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="poolpar_")
os.environ["BVG_GCP_PROJECT"] = "fake-project"
os.environ["BVG_CAPTION_CONCURRENCY"] = "8"

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from captions import pool as pool_module

assert config.CAPTION_CONCURRENCY == 8, config.CAPTION_CONCURRENCY

# ---- a stub Gemini that takes time, so overlap is observable ----------------
CALL_MS = 0.05          # "latency" of one chunk
state = {"calls": 0, "live": 0, "peak": 0, "n": 0}
lock = threading.Lock()
fail_plan: dict[int, list] = {}      # chunk index -> exceptions to raise in turn


class FakeAPIError(Exception):
    """Shaped like google.genai.errors.APIError: carries an HTTP code."""

    def __init__(self, code, message="boom"):
        super().__init__(f"{code} {message}")
        self.code = code


def fake_generate(client, prompt, schema, model=None, attempts=None):
    with lock:
        state["calls"] += 1
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        mine = state["calls"]
    try:
        time.sleep(CALL_MS)
        planned = fail_plan.get(mine)
        if planned:
            raise planned.pop(0)
        with lock:
            start, state["n"] = state["n"], state["n"] + 100
        # 100 strings, of which the first 10 repeat earlier ones — real models
        # do exactly this, and it is what forces a second round.
        items = ([f"caption number {start - 10 + i}" for i in range(10)]
                 + [f"caption number {start + i}" for i in range(10, 100)])
        key = "captions" if "captions" in schema["properties"] else "hashtag_sets"
        return {key: items}
    finally:
        with lock:
            state["live"] -= 1


real_generate = pool_module._generate
pool_module._generate = fake_generate
pool_module._client = lambda: "fake-client"
pool_module._thread_client = lambda: "fake-client"

# ---- 500 captions: concurrent, deduped, exact ------------------------------
started = time.time()
caps = pool_module.generate_captions("test theme", 500)
elapsed = time.time() - started

assert len(caps) == 500, len(caps)
assert len(set(caps)) == 500, "duplicates survived across concurrent chunks"
print(f"500 captions in {state['calls']} chunk calls, {elapsed:.2f}s")

# Overlap actually happened, and stayed inside the cap.
assert state["peak"] > 1, "chunks did not overlap — still sequential"
assert state["peak"] <= config.CAPTION_CONCURRENCY, state["peak"]
print(f"peak concurrent chunks: {state['peak']} (cap {config.CAPTION_CONCURRENCY})")

# Sequential would be calls x CALL_MS; concurrent must beat that clearly.
sequential = state["calls"] * CALL_MS
assert elapsed < sequential * 0.6, (elapsed, sequential)
print(f"took {elapsed:.2f}s vs {sequential:.2f}s if run one at a time")

# ---- the count is a ceiling as well as a target -----------------------------
state.update(calls=0, live=0, peak=0, n=0)
assert len(pool_module.generate_captions("t", 250)) == 250
assert len(pool_module.generate_hashtag_sets("t", 40)) == 40
print("odd counts land exactly: 250 and 40, no overshoot")

# ---- progress is monotonic and never exceeds the target ---------------------
state.update(calls=0, live=0, peak=0, n=0)
seen_progress = []
pool_module.generate_captions("t", 300, progress=lambda d, t: seen_progress.append((d, t)))
assert seen_progress and seen_progress[-1][0] == 300, seen_progress[-5:]
assert all(d <= t == 300 for d, t in seen_progress), seen_progress
assert seen_progress == sorted(seen_progress), "progress went backwards"
print(f"progress reported {len(seen_progress)} times, ending {seen_progress[-1]}")

# ---- a 429 is retried, not fatal -------------------------------------------
# Retry classification is the part worth testing directly.
assert pool_module._is_transient(FakeAPIError(429)) is True
assert pool_module._is_transient(FakeAPIError(503)) is True
assert pool_module._is_transient(FakeAPIError(403)) is False, \
    "a 403 must fail fast — retrying a permission error just makes it slow"
assert pool_module._is_transient(FakeAPIError(400)) is False
assert pool_module._is_transient(RuntimeError("deadline exceeded")) is True
assert pool_module._is_transient(RuntimeError("nonsense")) is False
print("retry classification: 429/503/timeouts retried, 403/400 fail fast")

# The real _generate, against a client that throttles twice then answers. This
# is the loop that stands between a transient 429 and a thrown-away pool.
inner = {"calls": 0}


class _FlakyModels:
    def generate_content(self, **kw):
        inner["calls"] += 1
        if inner["calls"] <= 2:
            raise FakeAPIError(429, "quota exceeded")
        return type("R", (), {"text": json.dumps(
            {"captions": [f"survived {i}" for i in range(5)]})})()


class _FlakyClient:
    models = _FlakyModels()


slept: list[float] = []
real_time = pool_module.time
pool_module.time = type("T", (), {"sleep": staticmethod(slept.append)})
pool_module._generate = real_generate
pool_module._thread_client = lambda: _FlakyClient()
state.update(calls=0, live=0, peak=0, n=0)

out = pool_module.generate_captions("t", 5)
assert out == [f"survived {i}" for i in range(5)], out
assert inner["calls"] == 3, inner
assert len(slept) == 2, slept
# Ranges, not slept[1] > slept[0]: the jitter windows overlap by design (2**n
# scaled by 0.5–1.5), so a strict ordering assertion would fail at random.
assert 1.0 <= slept[0] <= 3.0, slept        # 2 ** 1, jittered
assert 2.0 <= slept[1] <= 6.0, slept        # 2 ** 2, jittered
print(f"chunk retried past two 429s and delivered ({inner['calls']} attempts, "
      f"backoff {slept[0]:.1f}s then {slept[1]:.1f}s)")

# A 403 must NOT be retried — it costs one call, not four.
inner["calls"] = 0
slept.clear()


class _DeniedModels:
    def generate_content(self, **kw):
        inner["calls"] += 1
        raise FakeAPIError(403, "permission denied")


pool_module._thread_client = lambda: type("C", (), {"models": _DeniedModels()})()
try:
    pool_module._generate(pool_module._thread_client(), "p",
                          pool_module.CAPTION_SCHEMA)
    raise AssertionError("a 403 should propagate")
except FakeAPIError:
    assert inner["calls"] == 1, f"a 403 was retried {inner['calls']} times"
    assert not slept, slept
print("a 403 costs exactly one call and no backoff")

pool_module.time = real_time
pool_module._thread_client = lambda: "fake-client"

# ---- one dead chunk costs one chunk, not the pool ---------------------------
pool_module._generate = fake_generate
state.update(calls=0, live=0, peak=0, n=0)
fail_plan.clear()
fail_plan[2] = [FakeAPIError(403, "permission denied")]   # chunk 2 is beyond saving
caps = pool_module.generate_captions("t", 300)
assert len(caps) == 300, len(caps)
assert len(set(caps)) == 300
print(f"one permanently failing chunk did not stop the pool ({len(caps)} captions)")

# ---- but if NOTHING got through, that is an error, not an empty pool --------
state.update(calls=0, live=0, peak=0, n=0)
pool_module._generate = lambda *a, **k: (_ for _ in ()).throw(
    FakeAPIError(403, "Vertex AI API has not been used in project"))
try:
    pool_module.generate_captions("t", 200)
    raise AssertionError("an all-failed round must raise")
except pool_module.CaptionError as exc:
    assert "403" in str(exc) and "Vertex AI API" in str(exc), exc
    print("every chunk failing raises with the real reason:", str(exc)[:60], "…")

# ---- a model that has run dry stops instead of looping ----------------------
pool_module._generate = lambda *a, **k: {"captions": ["same one"] * 100,
                                         "hashtag_sets": ["#same"] * 100}
state.update(calls=0, live=0, peak=0, n=0)
dry = pool_module.generate_captions("t", 1000)
assert dry == ["same one"], dry
print("a model repeating itself stops after one round with an honest 1 caption")

# ---- misconfiguration is reported as itself --------------------------------
pool_module._generate = fake_generate
real_client = pool_module._client
pool_module._client = lambda: (_ for _ in ()).throw(
    pool_module.CaptionError("No GCP project set. Set BVG_GCP_PROJECT ..."))
try:
    pool_module.generate_captions("t", 100)
    raise AssertionError("a missing project should raise")
except pool_module.CaptionError as exc:
    assert "BVG_GCP_PROJECT" in str(exc), exc
    assert "Every" not in str(exc), \
        "config errors must not be buried under 'every request failed'"
    print("missing config surfaces directly:", str(exc)[:45], "…")
pool_module._client = real_client

print("\nALL CAPTION POOL PARALLEL TESTS PASSED")
