"""Instagram and YouTube scraping, and the Decodo proxy — no network.

The live paths were measured on 2026-10-07 (see the module docstrings); what
is checked here is everything around them that fails quietly:

  * a profile link passing as "one video" and being walked inside a page render;
  * Instagram's reels-tab gate reported as anything but the IP problem it is;
  * the clips query sent the post query's X-FB-Friendly-Name header, which
    makes Instagram answer `null` — an empty scrape that looks successful;
  * YouTube Shorts (no duration in the listing) thrown away as "likely photos";
  * an Instagram scrape of @bob sharing TikTok @bob's dedup history and Drive
    folder.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="platformscrape_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import scrapers
from jobs import store
from jobs.runners import scrape as runner
from scrapers import instagram as ig
from scrapers import proxy, tiktok
from scrapers import youtube as yt

store.init_db()

# ------------------------------------------------------------------ registry
assert scrapers.detect("https://www.instagram.com/bob/") == "instagram"
assert scrapers.detect("https://youtu.be/BfEVQW9Uc7I") == "youtube"
assert scrapers.detect("https://www.tiktok.com/@bob", default="youtube") == "tiktok"
assert scrapers.detect("@bob", default="instagram") == "instagram"
assert scrapers.ledger_key("tiktok", "bob") == "bob", "TikTok histories must keep matching"
assert scrapers.ledger_key("instagram", "bob") == "instagram:bob"
print("ok: platform detection and ledger keys")

# --------------------------------------------------------------------- proxy
url = proxy.build("spUSER", "p@ss:word", country="US", session="abc123", minutes=30)
assert url == ("http://user-spUSER-country-us-session-abc123-sessionduration-30"
               ":p%40ss%3Aword@gate.decodo.com:7000"), url
assert proxy.build("spUSER", "pw") == "http://spUSER:pw@gate.decodo.com:7000"
assert proxy.mask(url) == "http://***:***@gate.decodo.com:7000"

saved = (config.PROXY_URL, config.DECODO_USER, config.DECODO_PASS, config.PROXY_PLATFORMS)
try:
    config.PROXY_URL, config.DECODO_USER, config.DECODO_PASS = "", "spUSER", "pw"
    config.PROXY_PLATFORMS = {"instagram"}
    assert proxy.url_for("tiktok", "bob") == "", "a platform off the list must go direct"
    a, b = proxy.url_for("instagram", "bob"), proxy.url_for("instagram", "alice")
    assert a and a == proxy.url_for("instagram", "bob"), "one account must keep one exit"
    assert a != b, "different accounts must not share a sticky session"
    config.PROXY_URL = "http://u:p@host:1"
    assert proxy.url_for("instagram", "bob") == "http://u:p@host:1", "a full URL wins as-is"
    assert ig.InstagramSource(session_key="bob").proxy == "http://u:p@host:1"
    assert yt.YouTubeSource().proxy == "", "YouTube is off the default list"
    assert ig.InstagramSource(proxy="").proxy == "", '"" must mean direct'
finally:
    config.PROXY_URL, config.DECODO_USER, config.DECODO_PASS, config.PROXY_PLATFORMS = saved
print("ok: Decodo URL grammar, per-platform gating, sticky session per account")

# ------------------------------------------------------------ Instagram links
for raw, want in [("https://www.instagram.com/Some.One/reels/", "some.one"),
                  ("instagram.com/bob?igsh=x", "bob"), ("@Bob", "bob"), ("bob", "bob"),
                  ("https://www.instagram.com/reel/DeMhkqrijKx/", "")]:
    assert ig.account_name(raw) == want, (raw, ig.account_name(raw))
assert ig.profile_url("@bob") == "https://www.instagram.com/bob/reels/"

for good in ["https://www.instagram.com/reel/DeMhkqrijKx/",
             "https://www.instagram.com/reels/DeMhkqrijKx/?igsh=abc",
             "https://www.instagram.com/p/DeMhkqrijKx/",
             "instagram.com/natgeo/reel/DeMhkqrijKx",
             "3997147758863208946"]:
    assert ig.is_video_url(good), good
for bad in ["https://www.instagram.com/natgeo/", "https://www.instagram.com/natgeo/reels/",
            "@natgeo", "natgeo", "", "https://www.tiktok.com/@bob/video/7231234567890123456",
            # An opaque share token and a sound page are not reel codes.
            "https://www.instagram.com/share/reel/BAbc123xyz/",
            "https://www.instagram.com/reels/audio/1234567890/"]:
    assert not ig.is_video_url(bad), f"{bad!r} must not pass as one reel"

# Real pairs, read off live reels on 2026-10-07.
for code, pk in [("Dd4uLV5ylHy", "3997147758863208946"),
                 ("DZ-dSUgSRiy", "3926704727429552306")]:
    assert ig.shortcode_to_pk(code) == pk and ig.pk_to_code(pk) == code, code
assert ig.video_url("3997147758863208946") == "https://www.instagram.com/reel/Dd4uLV5ylHy/"
assert ig.video_url("https://www.instagram.com/natgeo/reel/Dd4uLV5ylHy/?x=1") == \
    "https://www.instagram.com/reel/Dd4uLV5ylHy/"
ts = ig._posted_ts("3997147758863208946")
assert ts and 1790000000 < ts < 1800000000, ts
assert ig._posted_ts("1") is None, "a pk decoding before 2019 is a decode failure"
print("ok: Instagram links, shortcode <-> pk, pk timestamps")

# ------------------------------------------------- Instagram walk, faked server
SJS = '<script type="application/json" data-sjs>%s</script>'
TOKENS = '"csrf_token":"CSRF1" "LSD",[],{"token":"LSD1"}'


class Resp:
    def __init__(self, status=200, text="", body=None, content=None):
        self.status_code, self.text, self._body = status, text, body
        self.content = content if content is not None else text.encode()

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def reel(pk, plays, hidden=False):
    return {"node": {"media": {"pk": str(pk), "code": ig.pk_to_code(pk), "play_count": plays,
                               "like_count": 5, "comment_count": 1,
                               "like_and_view_counts_disabled": hidden}}}


class FakeIG:
    """Serves the reels page, then clips pages, then post queries."""

    def __init__(self, page_html, pages, post=None):
        self.page_html, self.pages, self.post_body = page_html, list(pages), post
        self.posts_sent = []

    def get(self, url, headers=None, timeout=None):
        return Resp(text=self.page_html)

    def post(self, endpoint, data=None, headers=None):
        self.posts_sent.append((endpoint, data, headers))
        if "PostRootContent" in data["fb_api_req_friendly_name"]:
            return Resp(body={"data": {"xig_polaris_media": {"if_not_gated_logged_out": self.post_body}}})
        assert "X-FB-Friendly-Name" not in headers, \
            "the clips query answers null when sent X-FB-Friendly-Name"
        edges, cursor, more = self.pages.pop(0)
        return Resp(body={"data": {"xdt_api__v1__clips__user__connection_v2": {
            "edges": edges, "page_info": {"end_cursor": cursor, "has_next_page": more}}}})


PAGE = TOKENS + SJS % json.dumps({"x": {"pk": "777", "polaris_clips_connection": {"edges": []}}})
P1 = 3997147758863208946
config.SCRAPE_MIN_DELAY = config.SCRAPE_MAX_DELAY = 0.0


def source_for(fake):
    src = ig.InstagramSource(proxy="")
    src._session = fake
    return src


fake = FakeIG(PAGE, [([reel(P1, 100), reel(P1 - 1, 50, hidden=True)], "c1", True),
                     ([reel(P1 - 2, 30)], "c2", True),
                     ([reel(P1 - 3, 9)], None, False)])
clips = source_for(fake).enumerate_clips("https://www.instagram.com/bob/", 3)
assert [c.video_id for c in clips] == [str(P1), str(P1 - 1), str(P1 - 2)], clips
assert clips[0].view_count == 100 and clips[0].url.endswith(f"/reel/{ig.pk_to_code(P1)}/")
assert clips[1].view_count is None and clips[1].like_count is None, "hidden counts are null"
assert clips[0].uploader == "bob" and clips[0].timestamp
assert len(fake.pages) == 1, "the walk must stop at the limit, not page to the end"
sent = fake.posts_sent[1][1]
assert json.loads(sent["variables"])["data"]["max_id"] == "c1", "cursor not followed"
assert json.loads(sent["variables"])["data"]["target_user_id"] == "777"
print("ok: clips walk follows the cursor, stops at the limit, keeps hidden counts null")

# A cursor that hands back a page already seen must end the walk, not loop on it.
loop = FakeIG(PAGE, [([reel(P1, 1)], "same", True)] * 50)
assert len(source_for(loop).enumerate_clips("bob", 500)) == 1
assert len(loop.pages) == 48, "a stuck cursor was paged more than once"
print("ok: a stuck cursor ends the walk")

# The gate is an HTTP 200 of ~500 KB with no data. It must name the proxy fix.
stripped = FakeIG("x" * 500_000, [])
try:
    source_for(stripped).enumerate_clips("bob", 10)
except tiktok.ScrapeError as exc:
    assert "BVG_DECODO_USER" in str(exc) and "Retrying from this IP" in str(exc), exc
else:
    raise AssertionError("a stripped page must raise")
via_proxy = ig.InstagramSource(proxy="http://u:p@h:1")
via_proxy._session = stripped
try:
    via_proxy.enumerate_clips("bob", 10)
except tiktok.ScrapeError as exc:
    assert "even through the proxy" in str(exc), exc
gone = FakeIG(TOKENS + '"pageID":"httpErrorPage"' + "x" * 700_000, [])
try:
    source_for(gone).enumerate_clips("ghost", 10)
except tiktok.ScrapeError as exc:
    assert "no page" in str(exc), exc
print("ok: the IP gate names the Decodo fix; a missing profile says so")

# Download: the post query fills in what the listing lacks, the CDN gets fetched.
POST = {"taken_at": 1791381675, "caption": {"text": "hello"}, "user": {"username": "bob"},
        "video_versions": [{"type": 101, "url": "https://cdn.example/v.mp4"}],
        "clips_metadata": {"music_info": None, "original_sound_info": {
            "original_audio_title": "Original audio", "audio_asset_id": "555",
            "ig_artist": {"username": "bob"}}}}
FF = tiktok.find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="platformsrc_"))
SAMPLE = TMP / "sample.mp4"
subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=180x320:rate=15",
                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(SAMPLE)], check=True, timeout=120)

import curl_cffi.requests as cr

real_get = cr.get
routes = []


def cdn_get(url, **kw):
    routes.append("direct")
    return Resp(status=403, content=b"")


cr.get = cdn_get
try:
    dl = FakeIG(PAGE, [], post=POST)
    dl.get = lambda url, headers=None, timeout=None: (
        routes.append("proxy"), Resp(content=SAMPLE.read_bytes()))[1]
    src = ig.InstagramSource(proxy="http://u:p@h:1")
    src._session, src._tokens = dl, ("CSRF1", "LSD1")
    clip = tiktok.ClipInfo(video_id=str(P1), url="https://www.instagram.com/reel/x/")
    path = src.download(clip, TMP / "dl")
finally:
    cr.get = real_get
assert routes == ["direct", "proxy"], f"the CDN must be tried direct first: {routes}"
assert path.read_bytes() == SAMPLE.read_bytes()
assert clip.title == "hello" and clip.timestamp == 1791381675 and clip.duration
assert clip.sound_id == "555" and tiktok.sound_key(clip) == "id:555"
print("ok: video bytes go direct first and through the proxy only when refused")

# Own audio has one id per recording, so it must not collapse the way a shared
# name like "Original audio" would.
other = tiktok.ClipInfo(video_id="2", url="u", track="Original audio",
                        artists="bob", sound_id="556")
assert tiktok.sound_key(other) != tiktok.sound_key(clip)
print("ok: Instagram sounds dedupe by asset id, not by the shared 'Original audio' name")

photo = FakeIG(PAGE, [], post={**POST, "video_versions": None})
src = source_for(photo)
src._tokens = ("CSRF1", "LSD1")
try:
    src.download(tiktok.ClipInfo(video_id="1", url="u"), TMP / "photo")
except tiktok.ScrapeError as exc:
    assert "photo" in str(exc), exc
else:
    raise AssertionError("a photo post must raise, so the runner counts a skip")
print("ok: a photo post is a skip, not a crash")

# --------------------------------------------------------------- YouTube links
for raw, want in [("https://www.youtube.com/@Zach.Choi/shorts", "zach.choi"),
                  ("@MrBeast", "mrbeast"),
                  ("youtube.com/channel/UCI78AdiI6f7VKhqW1i4B3Rw", "UCI78AdiI6f7VKhqW1i4B3Rw"),
                  ("https://www.youtube.com/playlist?list=PLabc_123", "PLabc_123"),
                  ("@bob", "bob")]:
    assert yt.account_name(raw) == want, (raw, yt.account_name(raw))
for raw, want in [("@bob", "https://www.youtube.com/@bob/shorts"),
                  ("https://www.youtube.com/@bob", "https://www.youtube.com/@bob/shorts"),
                  ("https://www.youtube.com/@bob/featured", "https://www.youtube.com/@bob/shorts"),
                  ("https://www.youtube.com/@bob/Videos?view=0", "https://www.youtube.com/@bob/videos"),
                  ("UCI78AdiI6f7VKhqW1i4B3Rw",
                   "https://www.youtube.com/channel/UCI78AdiI6f7VKhqW1i4B3Rw/shorts"),
                  ("https://www.youtube.com/playlist?list=PLx", "https://www.youtube.com/playlist?list=PLx"),
                  ("youtube.com/playlist?list=PLx", "https://youtube.com/playlist?list=PLx")]:
    assert yt.profile_url(raw) == want, (raw, yt.profile_url(raw))

for good in ["https://www.youtube.com/shorts/BfEVQW9Uc7I?feature=share",
             "https://youtu.be/BfEVQW9Uc7I", "https://m.youtube.com/watch?v=BfEVQW9Uc7I&t=3",
             "BfEVQW9Uc7I"]:
    assert yt.is_video_url(good), good
for bad in ["https://www.youtube.com/@bob", "https://www.youtube.com/@bob/shorts",
            "https://www.youtube.com/playlist?list=PLabc", "@bob", ""]:
    assert not yt.is_video_url(bad), f"{bad!r} must not pass as one video"
# &list= would turn a single fetch into a playlist walk.
assert yt.video_url("https://www.youtube.com/watch?v=BfEVQW9Uc7I&list=PLx&index=3") == \
    "https://www.youtube.com/watch?v=BfEVQW9Uc7I"
assert yt.video_url("https://youtube.com/shorts/BfEVQW9Uc7I?si=x") == \
    "https://www.youtube.com/shorts/BfEVQW9Uc7I"
print("ok: YouTube links (Shorts by default, list= stripped from one video)")


class FakeYdl:
    def __init__(self, info):
        self.info, self.opts = info, None

    def __call__(self, extra):
        self.opts = extra
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        self.url = url
        return self.info


src = yt.YouTubeSource(proxy="")
fake_ydl = FakeYdl({"entries": [
    {"id": "aaaaaaaaaaa", "url": "https://www.youtube.com/shorts/aaaaaaaaaaa", "view_count": 9},
    {"id": "bbbbbbbbbbb", "duration": 3600, "view_count": 1},
    {"id": "ccccccccccc", "duration": 40, "view_count": 2}, None, {"id": ""}]})
src._ydl = fake_ydl
got = src.enumerate_clips("@bob", 10)
assert fake_ydl.url == "https://www.youtube.com/@bob/shorts"
assert [c.video_id for c in got] == ["aaaaaaaaaaa", "ccccccccccc"], got
assert got[1].url == "https://www.youtube.com/watch?v=ccccccccccc"
assert got[0].uploader == "bob" and got[0].view_count == 9
opts = yt.YouTubeSource(proxy="")._video_opts()
assert opts["merge_output_format"] == "mp4" and opts["ffmpeg_location"], \
    "without ffmpeg_location yt-dlp settles for the 360p file"
print("ok: YouTube listing skips over-long videos and keeps Shorts with no duration")

# ------------------------------------------------- the runner, per platform
class FakeSource:
    """A listing with no durations — exactly what Instagram and Shorts give."""

    def enumerate_clips(self, account_url, limit):
        return [tiktok.ClipInfo(video_id=v, url=f"https://x/{v}", view_count=n, uploader="bob")
                for v, n in (("101", 5), ("102", 7))]

    def download(self, clip, dest_dir):
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        out = Path(dest_dir) / f"{clip.video_id}.mp4"
        # Different bytes per clip, or the content-hash dedup would collapse them.
        subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(SAMPLE), "-metadata",
                        f"title={clip.video_id}", "-c", "copy", str(out)], check=True)
        return out


store.remember_clips("bob", [{"video_id": "101"}], job_id="tiktok-job")
job_id = store.new_job_id()
store.make_job_dirs(job_id)
store.create_job(kind=store.KIND_SCRAPE, params={}, label="t", job_id=job_id)
real_make = ig.make_source
ig.make_source = lambda account="": FakeSource()
try:
    result = runner.run({"id": job_id, "params": {
        "account": "https://www.instagram.com/bob/", "mode": "dump",
        "limit": 10, "upload": False}})
finally:
    ig.make_source = real_make
assert result["platform"] == "instagram", "the link alone must pick the platform"
assert result["videos_found"] == 2 and result["photos_skipped"] == 0, \
    f"clips with no listed duration were dropped as photos: {result}"
assert result["downloaded"] == 2, f"TikTok @bob's history leaked into Instagram: {result}"
assert store.known_clip_ids("instagram:bob") == {"101", "102"}
assert store.known_clip_ids("bob") == {"101"}, "the TikTok history must be untouched"
print("ok: runner keeps per-platform histories and does not photo-filter non-TikTok")

folders = []


class FakeDrive:
    @staticmethod
    def set_target(v): pass

    @staticmethod
    def ensure_path(parts, parent_id=None):
        folders.append("/".join(parts))
        return "id"

    @staticmethod
    def folder_link(fid): return "https://drive/x"

    @staticmethod
    def upload_many(queue, folder_id): return len(queue), 0

    @staticmethod
    def upload_file(path, parent): return {}


real_configured = config.drive_configured
config.drive_configured = lambda dest="": True
sys.modules["integrations.drive"] = FakeDrive
try:
    for plat in ("tiktok", "youtube"):
        runner._upload({"id": job_id, "params": {}}, "bob", "dump", [], {},
                       TMP, TMP / "none.xlsx", platform=plat)
finally:
    config.drive_configured = real_configured
    sys.modules.pop("integrations.drive", None)
assert folders[0] == "scrapes/bob", "TikTok's Drive layout must not move"
assert "scrapes/youtube/bob" in folders, folders
print("ok: Drive folders are per platform, TikTok's unchanged")

# ------------------------------------------------- found in review, 2026-10-07
# A pasted reel or Short is not an account. Instagram's parser finds no user in
# /reel/<code>/, and it used to fall back to the handle "unknown" — a real,
# stranger's profile that the job would then scrape.
for plat, raw in [("instagram", "https://www.instagram.com/reel/DeMhkqrijKx/"),
                  ("instagram", "https://www.instagram.com/p/DeMhkqrijKx/"),
                  ("youtube", "https://www.youtube.com/shorts/BfEVQW9Uc7I"),
                  ("youtube", "https://youtu.be/BfEVQW9Uc7I")]:
    assert scrapers.not_an_account(plat, raw), f"{raw} must be refused as an account"
for plat, raw in [("instagram", "https://www.instagram.com/natgeo/"), ("instagram", "@natgeo"),
                  ("youtube", "https://www.youtube.com/@bob"), ("youtube", "BfEVQW9Uc7I"),
                  ("youtube", "https://www.youtube.com/playlist?list=PLabc"),
                  ("tiktok", "https://www.tiktok.com/@bob/video/7231234567890123456"),
                  ("instagram", "")]:
    assert not scrapers.not_an_account(plat, raw), f"{raw} must be accepted as an account"
ig.make_source = lambda account="": (_ for _ in ()).throw(AssertionError("walked a profile"))
try:
    runner.run({"id": job_id, "params": {"account": "https://www.instagram.com/reel/DeMhkqrijKx/",
                                         "upload": False}})
except tiktok.ScrapeError as exc:
    assert "not an account" in str(exc), exc
else:
    raise AssertionError("the runner must refuse a reel link as an account")
finally:
    ig.make_source = real_make
print("ok: a reel/Short link is refused as an account instead of scraping @unknown")


# One flaky page in a 125-page walk must not throw away what was already listed.
class Flaky(FakeIG):
    def __init__(self, *a, fail_on=(), **k):
        super().__init__(*a, **k)
        self.calls, self.fail_on = 0, set(fail_on)

    def post(self, endpoint, data=None, headers=None):
        self.calls += 1
        if self.calls in self.fail_on:
            raise TimeoutError("proxy timed out")
        return super().post(endpoint, data, headers)


real_sleep = ig.time.sleep
ig.time.sleep = lambda s: None
try:
    once = Flaky(PAGE, [([reel(P1, 1)], "c1", True), ([reel(P1 - 1, 1)], None, False)],
                 fail_on={2})
    assert [c.video_id for c in source_for(once).enumerate_clips("bob", 10)] == \
        [str(P1), str(P1 - 1)], "a page that failed once was not retried"
    dead = Flaky(PAGE, [([reel(P1, 1)], "c1", True)], fail_on={2, 3, 4})
    kept = source_for(dead).enumerate_clips("bob", 10)
    assert [c.video_id for c in kept] == [str(P1)], "reels listed before the failure were lost"
    first = Flaky(PAGE, [], fail_on={1, 2, 3})
    try:
        source_for(first).enumerate_clips("bob", 10)
    except TimeoutError:
        pass
    else:
        raise AssertionError("a walk that never got a page must still fail loudly")
finally:
    ig.time.sleep = real_sleep
print("ok: the reels walk retries a page, and keeps what it has when it cannot go on")

# A stream on air lists with no duration; it must not be downloaded forever.
src = yt.YouTubeSource(proxy="")
src._ydl = FakeYdl({"entries": [{"id": "lllllllllll", "live_status": "is_live"},
                                {"id": "uuuuuuuuuuu", "live_status": "is_upcoming"},
                                {"id": "ddddddddddd", "live_status": "was_live", "duration": 60}]})
assert [c.video_id for c in src.enumerate_clips("@bob", 10)] == ["ddddddddddd"]
refuse = yt.YouTubeSource(proxy="")._video_opts()["match_filter"]
try:
    refuse({"id": "x", "live_status": "is_live"}, incomplete=False)
except tiktok.ScrapeError as exc:
    assert "live stream" in str(exc)
else:
    raise AssertionError("a live stream must be refused before it is recorded")
assert refuse({"id": "x", "live_status": "not_live"}) is None
print("ok: live and upcoming streams are skipped and refused")

# A malformed BVG_PROXY_URL is quoted verbatim by curl_cffi and yt-dlp, so it
# must be stopped — masked — before it reaches either.
saved = (config.PROXY_URL, config.PROXY_PLATFORMS)
try:
    config.PROXY_PLATFORMS = {"instagram"}
    config.PROXY_URL = "http://user:s3cr/et#zq@gate.decodo.com:7000"
    try:
        proxy.url_for("instagram", "bob")
    except ValueError as exc:
        assert "s3cr" not in str(exc) and "et#zq" not in str(exc), exc
    else:
        raise AssertionError("a malformed proxy URL must be refused")
finally:
    config.PROXY_URL, config.PROXY_PLATFORMS = saved
assert proxy.mask("http://u:p@ss@host:1") == "http://***:***@host:1"
print("ok: a malformed proxy URL is refused without echoing the password")

print("\nALL PLATFORM SCRAPER TESTS PASSED")
