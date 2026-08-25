"""The overlay-music scrape: sound dedup, its own ledger, the sheet, the layout.

Driven through a fake ClipSource rather than TikTok. What is worth checking
here is not that yt-dlp works but that the branches around it do — and three of
them fail silently:

  * sound dedup collapsing a creator's own audio, which shares ONE name across
    hundreds of different recordings;
  * the dedup ledger being keyed on the account alone, so a music scrape of an
    account already scraped for videos finds everything "already seen" and
    pulls nothing at all — a successful-looking job producing zero files;
  * music landing in a Drive folder called dump_<date> beside the video dumps,
    where a later Drive-link paste sends MP3s to a video pool.
"""
import os
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="musicscrape_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import subprocess

import pandas as pd

from jobs import store
from jobs.runners import scrape as runner
from scrapers import tiktok
from video_generator import find_ffmpeg

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="musicsrc_"))
store.init_db()


# ------------------------------------------------------------------ sound_key
#
# The whole dedup rests on this telling a NAMED sound from a placeholder one.
def clip(**kw) -> tiktok.ClipInfo:
    base = dict(video_id="1", url="u", uploader="somebody")
    return tiktok.ClipInfo(**{**base, **kw})


sound_key_named = tiktok.sound_key(clip(track="Lehanga", artists="Ysrbeats"))
assert sound_key_named
# Two posts using the same released track collapse...
assert tiktok.sound_key(clip(video_id="2", track="Lehanga", artists="Ysrbeats")) \
    == sound_key_named
# ...but differ from another track by the same artist.
assert tiktok.sound_key(clip(track="Other", artists="Ysrbeats")) != sound_key_named
# A creator's own audio must NOT collapse: yt-dlp normalises the English form to
# a bare "original sound", and the localised forms keep the handle suffix.
assert tiktok.sound_key(clip(track="original sound")) == ""
assert tiktok.sound_key(clip(track="son original - somebody")) == ""
assert tiktok.sound_key(clip(track="Sonido original - SOMEBODY",
                             uploader="somebody")) == ""
assert tiktok.sound_key(clip()) == ""
# Whitespace and case are noise, not identity.
assert tiktok.sound_key(clip(track="  LEHANGA ", artists="ysrbeats ")) == sound_key_named
print("ok: sound_key names real sounds and refuses placeholder ones")


# ---------------------------------------------------------------- a fake source
def mp3(path: Path, freq: int, seconds: float) -> Path:
    subprocess.run(
        [FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=44100",
         str(path)], check=True, capture_output=True)
    return path


# Six posts: two share a named sound, two share a DIFFERENT named sound but with
# different byte content (each post clips it to its own length — exactly the
# case a content hash cannot collapse), one is a creator original, one is a
# photo carousel.
POSTS = [
    ("101", "Lehanga", "Ysrbeats", 300, 2.0),
    ("102", "Lehanga", "Ysrbeats", 300, 3.5),      # same sound, different bytes
    ("103", "Watching The Stars", "Oneheart", 400, 2.0),
    ("104", "Watching The Stars", "Oneheart", 400, 4.0),   # same again
    ("105", "original sound", "", 500, 2.0),
    # A photo carousel: flat enumeration reports no duration for it (see
    # ClipInfo.likely_photo), but its sound downloads perfectly well.
    ("106", "Carousel Sound", "Someone", 600, 2.0),
]


class FakeSource:
    """Stands in for YtDlpSource: enumeration is flat (no music info at all,
    exactly as TikTok's is) and the sound only appears on download."""

    def __init__(self):
        self.audio_calls = 0

    def enumerate_clips(self, account_url, limit):
        return [tiktok.ClipInfo(video_id=vid, url=f"https://tiktok.com/@a/video/{vid}",
                                duration=None if vid == "106" else dur,
                                view_count=int(vid), uploader="somebody")
                for vid, _t, _a, _f, dur in POSTS]

    def download_audio(self, info, dest_dir):
        self.audio_calls += 1
        vid, track, artist, freq, dur = next(
            p for p in POSTS if p[0] == info.video_id)
        path = mp3(Path(dest_dir) / f"{vid}.mp3", freq, dur)
        enriched = tiktok.ClipInfo(
            video_id=vid, url=info.url, duration=dur, uploader="somebody",
            view_count=info.view_count, track=track, artists=artist)
        return path, enriched


def run_music(job_label: str, **params) -> tuple[dict, str]:
    job_id = store.new_job_id()
    store.make_job_dirs(job_id)
    store.create_job(kind=store.KIND_SCRAPE, params={}, label=job_label,
                     job_id=job_id)
    fake = FakeSource()
    real = tiktok.YtDlpSource
    tiktok.YtDlpSource = lambda *a, **k: fake
    try:
        result = runner.run({"id": job_id, "params": {
            "account": "https://www.tiktok.com/@somebody",
            "asset": "music", "limit": 50, "upload": False, **params}})
    finally:
        tiktok.YtDlpSource = real
    return result, job_id


# ------------------------------------------------------- 1. dedup by sound name
result, job_id = run_music("music-1")
# Photo carousels are NOT pre-filtered here (post 106 enumerates with no
# duration, the flat-mode hint for one). They are the one post type whose
# ORIGINAL sound is fetchable, so dropping them would discard the best audio on
# the profile -- and the hint is unreliable enough to drop real videos too.
assert result["photos_skipped"] == 2, ("only the sound duplicates, no "
                                       "pre-filtered carousels", result)
assert result["asset"] == "music"
assert result["downloaded"] == 6, result
# 6 posts, 2 named sounds x2 collapsed + 1 original + 1 carousel = 4 kept.
assert result["trimmed"] == 4, result
assert result["photos_skipped"] == 2, result       # the two collapsed duplicates

files = sorted(p.name for p in (store.job_dir(job_id) / "clips").iterdir()
               if p.is_file())
assert files == ["101.mp3", "103.mp3", "105.mp3", "106.mp3"], files
print(f"ok: {len(POSTS)} posts, 2 shared sounds -> {len(files)} tracks kept")

# The sheet leads with the sound, and drops the columns that mean nothing here.
frame = pd.read_excel(store.job_dir(job_id) / "metadata.xlsx", engine="openpyxl")
assert list(frame.columns)[:5] == ["Video_ID", "Track", "Artist", "Album",
                                   "Track_Duration_s"], list(frame.columns)
assert "Audio" not in frame.columns and "Segments" not in frame.columns
kept = frame[frame["Status"] == "done"]
assert set(kept["Track"]) == {"Lehanga", "Watching The Stars",
                             "original sound", "Carousel Sound"}
assert all(kept["Track_Duration_s"] > 0)
print("ok: the sheet leads with Track/Artist/Album and drops the video columns")


# ------------------------------------------------ 2. its own dedup history
#
# The ledger is keyed on the account, so without a separate key a music scrape
# of an account already scraped for videos would report "nothing new" and
# produce an empty folder while looking entirely successful.
# A DIFFERENT account, scraped for videos and never for music: every post must
# still be fetched.
store.remember_clips("freshie", [{"video_id": vid, "content_hash": f"vid-{vid}",
                                  "duration": 1.0} for vid, *_ in POSTS])
result2, _ = run_music("music-2", account="https://www.tiktok.com/@freshie")
assert result2["downloaded"] == 6, (
    "a video scrape's history must not suppress a music scrape", result2)
print("ok: music keeps its own dedup history, separate from the video scrape's")

# ...while a repeat MUSIC scrape of an account does skip what it already took.
result3, _ = run_music("music-3")
assert result3.get("downloaded", 0) == 0, (
    "a post skipped as a duplicate SOUND must be remembered, or every "
    "re-scrape re-downloads it to rediscover the same verdict", result3)
print("ok: a repeat music scrape downloads nothing at all, dup sounds included")

# The two histories really are separate rows.
accounts = {row["account"] for row in store.scraped_accounts()}
assert {"freshie", "somebody#music", "freshie#music"} <= accounts, accounts


# ------------------------------------------------------ 3. layout is forced flat
#
# Music has no slot semantics; asking for batches must not produce them.
# upload=True only so the ZIP gets built; Drive is unconfigured here, so the
# upload itself is a no-op.
result4, job4 = run_music("music-4", account="https://www.tiktok.com/@layout",
                          mode="batch", batches=4, upload=True)
assert result4["batches"] == 0, result4
zip_path = result4.get("zip_path")
assert zip_path, result4
import zipfile

with zipfile.ZipFile(zip_path) as zf:
    names = zf.namelist()
assert not any("batch_" in n for n in names), names
assert any(n.endswith(".xlsx") for n in names), names
print("ok: music ignores the batch layout and packages one flat folder")


# ------------------------------------------------- 4. the Drive folder is named
# for music, so MP3s and clip dumps never share a folder name.
import time as _time

stamp = _time.strftime("%Y-%m-%d")
folders = []


class FakeDrive:
    DriveError = RuntimeError

    @staticmethod
    def set_target(v): pass

    @staticmethod
    def ensure_path(parts, parent_id=None):
        folders.append("/".join(parts))
        return "id-" + "/".join(parts)

    @staticmethod
    def folder_link(fid): return f"https://drive/{fid}"

    @staticmethod
    def upload_many(queue, folder_id): return len(queue), 0

    @staticmethod
    def upload_file(path, parent): return {}


import config as settings

_configured = settings.drive_configured
settings.drive_configured = lambda dest="": True
sys.modules["integrations.drive"] = FakeDrive
try:
    runner._upload({"id": job_id, "params": {}}, "somebody", "dump",
                   tiktok.plan_dump(["101.mp3"]), {},
                   store.job_dir(job_id) / "clips",
                   store.job_dir(job_id) / "metadata.xlsx", is_music=True)
finally:
    settings.drive_configured = _configured
assert f"music_{stamp}" in folders, folders
assert not any(f.startswith("dump_") for f in folders), folders
print(f"ok: music uploads to music_{stamp}, not dump_{stamp}")

print("\nall music-scrape checks passed")
