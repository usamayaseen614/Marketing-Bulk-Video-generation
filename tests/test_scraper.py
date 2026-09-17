"""Scraper planning, dedup, trimming and account parsing."""
import os, shutil, sys, tempfile
from collections import Counter
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="scrapetest_")
sys.path.insert(0, str(PROJ))

import config
from jobs import store
from scrapers import tiktok

# ---------- account parsing ----------
for raw, want in [
    ("https://www.tiktok.com/@someaccount", "someaccount"),
    ("https://www.tiktok.com/@some.account?lang=en", "some.account"),
    ("@handle_1", "handle_1"),
    ("handle_1", "handle_1"),
]:
    got = tiktok.account_name(raw)
    assert got == want, f"{raw!r} -> {got!r}, wanted {want!r}"
assert tiktok.profile_url("@bob") == "https://www.tiktok.com/@bob"
assert tiktok.profile_url("https://www.tiktok.com/@bob?x=1") == "https://www.tiktok.com/@bob"
print("account parsing ok")

# ---------- single-video link parsing ----------
# The share button's tracking blob must not make two links to one video differ.
assert tiktok.video_url(
    "https://www.tiktok.com/@bob/video/7231234567890123456?is_from_webapp=1&x=2"
) == "https://www.tiktok.com/@bob/video/7231234567890123456"
assert tiktok.video_url("www.tiktok.com/@bob/video/7231234567890123456/") == \
       "https://www.tiktok.com/@bob/video/7231234567890123456"
# A bare id is what the metadata sheet's Video_ID column gives you.
assert tiktok.video_url("7231234567890123456") == \
       "https://www.tiktok.com/@_/video/7231234567890123456"
assert tiktok.video_url("") == ""

for good in [
    "https://www.tiktok.com/@bob/video/7231234567890123456",
    "https://www.tiktok.com/@bob/video/7231234567890123456?is_from_webapp=1",
    "https://vm.tiktok.com/ZMabc123/",
    "https://vt.tiktok.com/ZSabc123",
    "https://www.tiktok.com/t/ZTabc123/",
    "https://m.tiktok.com/v/7231234567890123456.html",
    "7231234567890123456",
]:
    assert tiktok.is_video_url(good), f"{good!r} should be a single-video link"

# The guard that matters: a profile URL handed to yt-dlp walks the whole
# account, so it must never reach the single-video path.
for bad in [
    "https://www.tiktok.com/@bob",
    "https://www.tiktok.com/@bob?lang=en",
    "@bob",
    "bob",
    "",
    "   ",
]:
    assert not tiktok.is_video_url(bad), f"{bad!r} must not pass as one video"

assert tiktok.is_photo_url("https://www.tiktok.com/@bob/photo/7231234567890123456")
assert not tiktok.is_photo_url("https://www.tiktok.com/@bob/video/7231234567890123456")
assert tiktok.video_id_from_url(
    "https://www.tiktok.com/@bob/video/7231234567890123456?x=1") == "7231234567890123456"
assert tiktok.video_id_from_url("https://vm.tiktok.com/ZMabc123/") == ""
print("single-video link parsing ok (profile links rejected, share links accepted)")

# ---------- error translation ----------
assert tiktok.explain_failure(
    "ERROR: Unable to extract universal data for rehydration") == \
    "Not a downloadable video (photo carousel or removed post)"
assert tiktok.explain_failure("HTTP Error 404") == "HTTP Error 404"
print("error translation ok")

# ---------- round-robin: 50 clips, 5 slots -> exactly 10 per slot ----------
ids = [f"v{i}" for i in range(200)]
plan = tiktok.plan_batches(ids, n_batches=4, batch_size=50, slots=5)
assert len(plan) == 200, len(plan)

for b in range(1, 5):
    batch = [p for p in plan if p.batch == b]
    assert len(batch) == 50, f"batch {b} has {len(batch)}"
    per_slot = Counter(p.slot for p in batch)
    assert set(per_slot) == {1, 2, 3, 4, 5}, per_slot
    assert all(v == 10 for v in per_slot.values()), per_slot
print("round-robin: 4 batches x 50 clips -> 10 per slot in every batch")

# the documented mapping: 1->1, 2->2 ... 5->5, 6->1
first = sorted([p for p in plan if p.batch == 1], key=lambda p: p.position)
assert [p.slot for p in first[:7]] == [1, 2, 3, 4, 5, 1, 2], [p.slot for p in first[:7]]
print("slot mapping 1,2,3,4,5,1,2 confirmed")

# with 200 unique ids and 200 needed, no clip repeats
assert len({p.video_id for p in plan}) == 200

# ---------- wrap-around must reshuffle, not clone ----------
small = [f"v{i}" for i in range(50)]          # exactly one batch worth
wrapped = tiktok.plan_batches(small, n_batches=3, batch_size=50, slots=5)
b1 = [p.video_id for p in sorted([x for x in wrapped if x.batch == 1], key=lambda p: p.position)]
b2 = [p.video_id for p in sorted([x for x in wrapped if x.batch == 2], key=lambda p: p.position)]
b3 = [p.video_id for p in sorted([x for x in wrapped if x.batch == 3], key=lambda p: p.position)]
assert set(b1) == set(b2) == set(b3), "same pool expected"
assert b1 != b2, "batch 2 is a clone of batch 1 — wrap did not reshuffle"
assert b2 != b3, "batch 3 is a clone of batch 2"
print("wrap-around reshuffles: batch2 != batch1 != batch3 (same pool, new order)")

# and the slot groupings genuinely differ
slots_b1 = {p.video_id: p.slot for p in wrapped if p.batch == 1}
slots_b2 = {p.video_id: p.slot for p in wrapped if p.batch == 2}
moved = sum(1 for v in slots_b1 if slots_b1[v] != slots_b2[v])
assert moved > 20, f"only {moved}/50 clips changed slot on wrap"
print(f"  {moved}/50 clips landed in a different slot on the wrap")

# ---------- determinism: same inputs -> same plan ----------
again = tiktok.plan_batches(small, n_batches=3, batch_size=50, slots=5)
assert [(p.video_id, p.batch, p.slot) for p in wrapped] == \
       [(p.video_id, p.batch, p.slot) for p in again], "plan is not reproducible"
print("planning is deterministic across runs")

# ---------- edge cases ----------
assert tiktok.plan_batches([], 3) == []
assert tiktok.plan_batches(ids, 0) == []
dump = tiktok.plan_dump(["a", "b", "c"])
assert [d.position for d in dump] == [1, 2, 3]
assert all(d.batch == 0 and d.slot == 0 for d in dump)
print("edge cases ok")

# ---------- dedup store ----------
store.init_db()
assert store.known_clip_ids("acct") == set()
store.remember_clips("acct", [{"video_id": "v1", "content_hash": "h1", "duration": 10.0},
                              {"video_id": "v2", "content_hash": "h2", "duration": 9.5}],
                     job_id="j1")
assert store.known_clip_ids("acct") == {"v1", "v2"}
assert store.known_content_hashes("acct") == {"h1", "h2"}
# other accounts are isolated
assert store.known_clip_ids("other") == set()
# re-remembering is harmless
store.remember_clips("acct", [{"video_id": "v1", "content_hash": "h1"}], job_id="j2")
assert len(store.known_clip_ids("acct")) == 2
accounts = store.scraped_accounts()
assert accounts[0]["account"] == "acct" and accounts[0]["clips"] == 2, accounts
assert store.forget_account("acct") == 2
assert store.known_clip_ids("acct") == set()
print("dedup store ok (isolated per account, idempotent, forgettable)")

# ---------- trimming ----------
import subprocess
SA = PROJ / "sample_assets"
TMP = Path(os.environ["BVG_JOBS_ROOT"])
ffmpeg = tiktok.find_ffmpeg()
short_src = SA / "cta_video_1.mp4"
print(f"short sample duration: {tiktok.probe_duration(short_src)}s")

# A real TikTok clip is 20-60s; the samples are 2s, so build a 20s stand-in.
long_src = TMP / "long.mp4"
subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-stream_loop", "20",
                "-i", str(short_src), "-t", "20", "-c:v", "libx264",
                "-preset", "veryfast", "-pix_fmt", "yuv420p", str(long_src)],
               check=True, timeout=180)
print(f"built a {tiktok.probe_duration(long_src)}s source to trim")

# The whole point of re-encoding: an EXACT window, not keyframe-snapped.
out = TMP / "trimmed.mp4"
tiktok.trim_clip(long_src, out, start=1.0, duration=10.0)
got = tiktok.probe_duration(out)
assert out.is_file() and out.stat().st_size > 0
assert abs(got - 10.0) < 0.25, f"expected ~10.0s, got {got}s"
print(f"trim 1s->11s produced {got}s (exact, not the 8-12s a stream copy gives)")

# Trim, not filter: a clip shorter than the window is kept at its own length.
out2 = TMP / "shorter.mp4"
tiktok.trim_clip(short_src, out2, start=1.0, duration=10.0)
got2 = tiktok.probe_duration(out2)
assert out2.is_file() and got2 > 0, "short clip must still be kept"
assert got2 < 2.0, got2
print(f"2s clip with a 1s->11s window kept {got2}s — trimmed, not dropped")

# A clip shorter than the START offset would otherwise produce an empty file.
tiny = TMP / "tiny.mp4"
tiktok.trim_clip(short_src, tiny, start=5.0, duration=10.0)   # start past the end
assert tiny.is_file() and tiny.stat().st_size > 0, "start past the end made an empty file"
assert tiktok.probe_duration(tiny) > 0
print(f"start past clip end slid back to 0 -> {tiktok.probe_duration(tiny)}s, not empty")

# ---------- audio detection ----------
# TikTok advertises acodec=aac on renditions that arrive with no audio stream
# at all, so the file itself is the only trustworthy source.
with_audio = TMP / "with_audio.mp4"
subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(short_src),
                "-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-shortest", "-c:v", "copy", "-c:a", "aac", str(with_audio)],
               check=True, timeout=120)
no_audio = TMP / "no_audio.mp4"
subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(with_audio),
                "-an", "-c:v", "copy", str(no_audio)], check=True, timeout=120)

assert tiktok.has_audio(with_audio), "a file with an aac stream read as silent"
assert not tiktok.has_audio(no_audio), "a file with no audio stream read as audible"
assert not tiktok.has_audio(TMP / "does-not-exist.mp4")
print("has_audio distinguishes a real audio stream from none")

# The trim re-encodes; -c:a aac has to actually carry the sound across, or the
# scrape would strip the audio it just went to the trouble of fetching.
kept = TMP / "kept_audio.mp4"
tiktok.trim_clip(with_audio, kept, start=0.5, duration=1.0)
assert tiktok.has_audio(kept), "trim_clip dropped the audio track"
print("trim_clip preserves audio through the re-encode")


# ---------- silent-rendition recovery ----------
class StubYdl:
    """A YtDlpSource whose downloads are files we choose, so the recovery
    logic can be tested without TikTok's cooperation."""

    def __init__(self, *served):
        self.served = list(served)
        self.formats = []

    def _ydl(self, extra):
        src = self.served[min(len(self.formats), len(self.served) - 1)]
        self.formats.append(extra.get("format"))
        out = Path(str(extra["outtmpl"]).replace("%(ext)s", "mp4"))

        class _Ctx:
            def __enter__(inner):
                return inner

            def __exit__(inner, *a):
                return False

            def download(inner, urls):
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, out)

        return _Ctx()


def recover(landed, *on_retry):
    """`landed` is what the first download produced; `on_retry` is what the
    recovery fetch would return. _recover_audio is called directly, so its
    first _ydl call IS the retry."""
    stub = StubYdl(*(on_retry or (landed,)))
    stub.cookies_file = None
    work = TMP / "recover"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    first = work / "vid.mp4"
    shutil.copyfile(landed, first)
    got = tiktok.YtDlpSource._recover_audio(stub, "https://x", first, "vid")
    return stub, got


# The real case: TikTok's H.265 rendition lands mute, H.264 has the sound.
stub, got = recover(no_audio, with_audio)
assert tiktok.has_audio(got), "recovery did not produce an audible file"
assert len(stub.formats) == 1 and "h265" in stub.formats[0], stub.formats
print(f"silent rendition recovered via {stub.formats[0]!r}")

# A post that genuinely has no sound keeps its original, higher-quality file
# rather than being swapped for an equally silent fallback.
stub, got = recover(no_audio, no_audio)
assert got.name == "vid.mp4", got
assert not tiktok.has_audio(got)
assert len(stub.formats) == 1, stub.formats
print("a genuinely silent post keeps its original file, and is not re-fetched twice")

# An audible download must not cost a second request at all.
stub, got = recover(with_audio, with_audio)
assert stub.formats == [], "a file that already had audio triggered a re-download"
assert tiktok.has_audio(got)
print("an audible download costs no extra fetch")

# ---------- content hash ----------
h1 = tiktok.content_hash(short_src)
h2 = tiktok.content_hash(short_src)
assert h1 == h2 and len(h1) == 64
assert tiktok.content_hash(out) != h1
print("content hash stable and discriminating")

# ---------- download filenames ----------
# A single fetch lands in the user's Downloads, not in a folder that already
# says which account it came from — so the handle has to be in the name.
assert tiktok.clip_stem(tiktok.ClipInfo(video_id="99", url="", uploader="bob")) == "bob_99"
assert tiktok.clip_stem(tiktok.ClipInfo(video_id="42", url="", uploader="")) == "42"
hostile = tiktok.clip_stem(
    tiktok.ClipInfo(video_id="7", url="", uploader="../../etc/passwd"))
assert "/" not in hostile and "\\" not in hostile and hostile.endswith("_7"), hostile
print(f"clip stems sanitised ('../../etc/passwd' -> {hostile!r})")

# ---------- single-video fetch ----------
import time as _t

VIDEO_LINK ="https://www.tiktok.com/@bob/video/7231234567890123456"


class FakeSource:
    """Stands in for yt-dlp — no network, same contract as fetch_one.

    `fail_first` reproduces the flakiness measured against live TikTok: the
    same link that downloads fine one second raises "Unable to extract
    universal data for rehydration" the next."""

    FLAKE = ("ERROR: [TikTok] 7231234567890123456: Unable to extract universal "
             "data for rehydration")

    def __init__(self, src, clip, fail_first=0):
        self.src, self.clip, self.calls = src, clip, 0
        self.fail_first = fail_first

    def fetch_one(self, url, dest_dir):
        self.calls += 1
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if self.calls <= self.fail_first:
            # yt-dlp leaves scratch behind on a failed attempt.
            (dest_dir / f"{self.clip.video_id}.mp4.part").write_bytes(b"junk")
            raise RuntimeError(self.FLAKE)
        produced = dest_dir / f"{self.clip.video_id}.mp4"
        shutil.copyfile(self.src, produced)
        return self.clip, produced


fake = FakeSource(long_src, tiktok.ClipInfo(
    video_id="7231234567890123456", url=VIDEO_LINK, uploader="bob",
    duration=20.0, view_count=1234))
DEST = TMP / "single" / "sess1"

# Untrimmed is the default: a one-off grab usually wants the whole video.
whole = tiktok.fetch_single_video(VIDEO_LINK, DEST, source=fake)
assert whole.trimmed is False
assert [p.name for p in whole.files] == ["bob_7231234567890123456.mp4"]
assert whole.files[0].is_file() and whole.total_bytes > 0
assert not (DEST / "raw").exists(), "the raw download folder must not be left behind"
assert abs(tiktok.probe_duration(whole.files[0]) - 20.0) < 0.5
print(f"untrimmed fetch -> {whole.files[0].name}, {tiktok.probe_duration(whole.files[0])}s intact")

# Each fetch replaces the last, so a session cannot accumulate videos.
stray = DEST / "stray.mp4"
stray.write_bytes(b"x")
cut = tiktok.fetch_single_video(VIDEO_LINK, DEST, trim_start=1.0,
                                trim_duration=10.0, source=fake)
assert not stray.exists(), "dest_dir must be emptied — each fetch replaces the last"
assert cut.trimmed is True
assert [p.name for p in cut.files] == ["bob_7231234567890123456.mp4",
                                       "bob_7231234567890123456_2.mp4"], \
    [p.name for p in cut.files]
assert abs(tiktok.probe_duration(cut.files[0]) - 10.0) < 0.25
assert 8.0 < tiktok.probe_duration(cut.files[1]) < 10.0
print(f"trimmed fetch split 20s into {len(cut.files)} segments, "
      f"{[round(tiktok.probe_duration(p), 1) for p in cut.files]}s")

# The guard that matters: a profile link handed to yt-dlp walks the whole
# account, so it must be refused BEFORE anything reaches the network.
before = fake.calls
for refused, expect in [
    ("https://www.tiktok.com/@bob", "single video"),
    ("https://www.tiktok.com/@bob/photo/7231234567890123456", "photo"),
]:
    try:
        tiktok.fetch_single_video(refused, DEST, source=fake)
    except tiktok.ScrapeError as exc:
        assert expect in str(exc).lower(), str(exc)
    else:
        raise AssertionError(f"{refused!r} should have been refused")
assert fake.calls == before, "a refused link must not reach the source"
assert cut.files[0].is_file(), "a refused link must not delete the previous fetch"
print("profile and photo links refused without touching the network, "
      "and without eating the previous fetch")

# ---------- flaky extraction is retried ----------
# Measured live: a link downloaded fine, then failed "rehydration" seconds
# later on the very next request. A scrape shrugs that off (failures are never
# remembered, so next month's run picks the clip up); one link gets one chance,
# and the message it would otherwise report is a confident lie.
flaky = FakeSource(long_src, fake.clip, fail_first=2)
recovered = tiktok.fetch_single_video(VIDEO_LINK, DEST, source=flaky,
                                      attempts=3, retry_delay=0)
assert flaky.calls == 3, flaky.calls
assert recovered.files[0].is_file()
assert not list(DEST.glob("*.part")), "a failed attempt's scratch leaked into the result"
print(f"flaky extraction recovered on attempt {flaky.calls} of 3")

# ...but a link that never works still fails, and says so honestly.
dead = FakeSource(long_src, fake.clip, fail_first=99)
said = ""
try:
    tiktok.fetch_single_video(VIDEO_LINK, DEST, source=dead,
                              attempts=3, retry_delay=0)
except tiktok.ScrapeError as exc:
    said = str(exc)
else:
    raise AssertionError("a permanently dead link should have raised")
assert dead.calls == 3, dead.calls
assert "3 tries" in said and "photo carousel" in said, said
print(f"exhausted retries report honestly: {said[:88]}…")

# One attempt is still one attempt — nothing about the retry is mandatory.
solo = FakeSource(long_src, fake.clip, fail_first=1)
said = ""
try:
    tiktok.fetch_single_video(VIDEO_LINK, DEST, source=solo, attempts=1)
except tiktok.ScrapeError as exc:
    said = str(exc)
else:
    raise AssertionError("attempts=1 should not retry")
assert solo.calls == 1 and "tries" not in said, (solo.calls, said)
print("attempts=1 makes exactly one request")

# ---------- the single-fetch reaper ----------
# These live under a `_`-prefixed path precisely so store.reap_old_jobs() leaves
# them alone, which means nothing else would ever clean them up.
PRUNE = TMP / "singleroot"
(PRUNE / "old").mkdir(parents=True)
(PRUNE / "fresh").mkdir(parents=True)
(PRUNE / "notadir.txt").write_bytes(b"x")
os.utime(PRUNE / "old", (_t.time() - 7 * 3600,) * 2)
assert tiktok.prune_fetch_dirs(PRUNE, max_age_hours=6.0) == 1
assert not (PRUNE / "old").exists()
assert (PRUNE / "fresh").is_dir() and (PRUNE / "notadir.txt").is_file()
assert tiktok.prune_fetch_dirs(TMP / "never-existed", 6.0) == 0
print("single-fetch reaper drops stale session folders, keeps fresh ones")

print("\nALL SCRAPER TESTS PASSED")
