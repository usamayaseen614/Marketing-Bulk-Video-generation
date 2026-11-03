"""
scrapers/instagram.py — pulling reels from an Instagram profile, logged out.

yt-dlp cannot list a profile (its instagram:user extractor is marked broken),
so the listing is ported from the social-media dashboard's adapter
(app/adapters/instagram.py), which has walked profiles in production since
2026-09. Measured live 2026-10-07 from a residential IP, no cookies:

  * **Listing.** GET /<user>/reels/ for the csrf/lsd tokens and the profile's
    pk, then the clips GraphQL, 12 reels a page at 3-7 s a page. It carries
    exact play/like/comment counts and each reel's code — but no video link,
    duration, date, caption or sound.

  * **So one more query per reel**, at download time: the logged-out post
    query yt-dlp also uses. It carries video_versions, taken_at, the caption
    and the sound. Asking at download time (not during the listing) keeps the
    CDN link fresh — it expires ~48 h after it is minted, and a resumed job
    must never hold a stale one.

  * **video_versions is a progressive H.264 720x1280 MP4 with AAC audio**, and
    the CDN serves it to a cookieless request. The 1080x1920 rendition exists
    only as VP9 DASH; clips are re-encoded anyway, and a one-off download
    should play on any phone, so 720p H.264 is the trade taken.

  * **The datacenter-IP gate.** From a cloud IP Instagram serves the reels tab
    stripped of its data — an HTTP 200 of ~500 KB instead of ~700 KB. That is
    a gate, not a throttle, and retrying from the same IP never clears it, so
    Instagram's page and API calls go through Decodo (scrapers/proxy.py). The
    video bytes try the CDN directly first: residential traffic is billed per
    GB, and those bytes are nearly all of it.
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import config
from scrapers import tiktok
from scrapers.proxy import url_for as proxy_url_for
from scrapers.tiktok import ClipInfo, ScrapeError, SingleFetch

logger = logging.getLogger(__name__)

# Copied from the dashboard adapter, where each one is load-bearing: a wrong
# APP_ID is an instant 403, and without the document headers Instagram serves
# a payload-free page even to a residential IP.
APP_ID = "936619743392459"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
DOC_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
SJS_RE = re.compile(r'<script type="application/json"[^>]*data-sjs>(.*?)</script>', re.S)
CSRF_RE = re.compile(r'"csrf_token":"([^"]+)"')
LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
# A served-but-stripped page is the IP gate; ~700 KB is the real thing.
GATED_PAGE_MAX = 620_000
# On every "no such profile" page and on none of the real ones. Never loosen
# this to the bare word "httpErrorPage", which is on all of them.
PROFILE_404 = '"pageID":"httpErrorPage"'
# Media pks encode creation time in their high bits — the only timestamp the
# listing has. Gated to a plausible window rather than trusted outright.
PK_EPOCH_MS = 1314220021721
PLAUSIBLE = (1546300800, 4102444800)          # 2019-01-01 .. 2100-01-01

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
# /reel/<code>, /reels/<code>, /p/<code>, /tv/<code>, optionally after /<user>.
# A profile's own /<user>/reels/ tab has no code after it, so it never matches.
# Anchored, so /share/reel/<token>/ (an opaque token, not a code) and the
# /reels/audio/<id>/ sound page are not decoded into a bogus pk.
_CODE_RE = re.compile(r"^(?:/(?!share/)[^/]+)?/(?:reels?|p|tv)/(?!audio/)([A-Za-z0-9_-]{5,})")
_BARE_PK_RE = re.compile(r"^\d{8,}$")
_NOT_USERS = {"", "p", "reel", "reels", "tv", "explore", "stories", "accounts"}


# --------------------------------------------------------------------- links

def account_name(raw: str) -> str:
    """`https://www.instagram.com/Some.One/reels/` -> `some.one`; "" for a link
    with no account in it (a reel, a post), never a made-up handle."""
    text = (raw or "").strip()
    if "instagram.com" in text.lower() or "instagr.am" in text.lower():
        path = urlparse(text if "://" in text else "https://" + text).path
        first = path.strip("/").split("/")[0]
        return first.lower() if first.lower() not in _NOT_USERS else ""
    return text.lstrip("@").strip("/").lower() or "unknown"


def profile_url(raw: str) -> str:
    return f"https://www.instagram.com/{account_name(raw)}/reels/"


def shortcode_to_pk(code: str) -> str:
    """A reel's URL code is its media pk in URL-safe base64 (first 11 chars)."""
    number = 0
    for char in code[:11]:
        number = number * 64 + _ALPHABET.index(char)
    return str(number)


def pk_to_code(pk: str) -> str:
    number, chars = int(pk), []
    while number:
        number, rest = divmod(number, 64)
        chars.append(_ALPHABET[rest])
    return "".join(reversed(chars)) or "A"


def video_url(raw: str) -> str:
    """A reel's canonical link. A bare numeric id — the metadata sheet's
    Video_ID column — is turned back into one."""
    text = (raw or "").strip()
    if _BARE_PK_RE.match(text):
        return f"https://www.instagram.com/reel/{pk_to_code(text)}/"
    match = _CODE_RE.search(urlparse(text if "://" in text else "https://" + text).path)
    return f"https://www.instagram.com/reel/{match.group(1)}/" if match else text


def is_video_url(raw: str) -> bool:
    text = (raw or "").strip()
    if _BARE_PK_RE.match(text):
        return True
    if "instagram.com" not in text.lower() and "instagr.am" not in text.lower():
        return False
    return bool(_CODE_RE.search(urlparse(text if "://" in text else "https://" + text).path))


def _posted_ts(pk) -> Optional[int]:
    try:
        ts = ((int(pk) >> 23) + PK_EPOCH_MS) // 1000
    except (TypeError, ValueError):
        return None
    return ts if PLAUSIBLE[0] < ts < PLAUSIBLE[1] else None


def _int(value) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _find_clips(html: str) -> dict:
    """The SSR object holding polaris_clips_connection — the profile itself."""
    for match in SJS_RE.finditer(html):
        chunk = match.group(1)
        if "polaris_clips_connection" not in chunk:
            continue
        try:
            stack = [json.loads(chunk)]
        except ValueError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "polaris_clips_connection" in node:
                    return node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return {}


def _gated(size: int, proxied: bool) -> ScrapeError:
    if proxied:
        return ScrapeError(
            f"Instagram served a stripped page ({size:,} bytes, no data) even "
            "through the proxy. Try another exit country (BVG_PROXY_COUNTRY) "
            "or try again later — the same exit will keep getting the same page.")
    return ScrapeError(
        f"Instagram served a stripped page ({size:,} bytes, no data). That is "
        "its block on cloud-server IPs, not a rate limit: set BVG_DECODO_USER "
        "and BVG_DECODO_PASS (the dashboard's Decodo account) so Instagram goes "
        "through a residential proxy. Retrying from this IP will not help.")


def _enrich(clip: ClipInfo, post: dict) -> None:
    """Fill in what only the post query knows. In place, on purpose: the scrape
    runner's metadata sheet is built from these same objects."""
    clip.timestamp = _int(post.get("taken_at")) or clip.timestamp
    caption = post.get("caption")
    if isinstance(caption, dict) and not clip.title:
        clip.title = caption.get("text") or ""
    clip.uploader = (post.get("user") or {}).get("username") or clip.uploader
    clip.like_count = _int(post.get("like_count")) if clip.like_count is None else clip.like_count
    clip.comment_count = (_int(post.get("comment_count"))
                          if clip.comment_count is None else clip.comment_count)

    sounds = post.get("clips_metadata") or {}
    music = (sounds.get("music_info") or {}).get("music_asset_info") or {}
    own = sounds.get("original_sound_info") or {}
    if music:
        clip.track = music.get("title") or ""
        clip.artists = music.get("display_artist") or ""
        clip.sound_id = str(music.get("audio_asset_id") or music.get("id") or "")
    elif own:
        clip.track = own.get("original_audio_title") or ""
        clip.artists = (own.get("ig_artist") or {}).get("username") or ""
        clip.sound_id = str(own.get("audio_asset_id") or "")


# --------------------------------------------------------------------- source

class InstagramSource:
    """ClipSource for Instagram reels — curl_cffi, not yt-dlp (see above)."""

    NAME = "Instagram"

    def __init__(self, proxy: Optional[str] = None, session_key: str = ""):
        # None = whatever config says; "" = go direct. session_key pins one
        # sticky exit for the whole walk (see scrapers/proxy.py).
        self.proxy = proxy_url_for("instagram", session_key) if proxy is None else proxy
        self._session = None
        self._tokens: Optional[tuple[str, str]] = None   # (csrf, lsd): good for a whole walk

    @property
    def session(self):
        if self._session is None:
            self._session = _new_session(self.proxy)
        return self._session

    def _bootstrap(self, page_url: str) -> str:
        """GET an Instagram page for the csrf/lsd tokens every API call needs."""
        response = self.session.get(page_url, headers=DOC_HEADERS)
        html = response.text
        csrf, lsd = CSRF_RE.search(html), LSD_RE.search(html)
        if PROFILE_404 in html:
            raise ScrapeError(f"Instagram has no page at {page_url} — deleted, "
                              "banned, renamed or private. Check the link.")
        if response.status_code != 200 or not (csrf and lsd):
            if len(html) < GATED_PAGE_MAX:
                raise _gated(len(html), bool(self.proxy))
            raise ScrapeError(
                f"Instagram's page has no API tokens (HTTP {response.status_code}, "
                f"{len(html):,} bytes) — its layout may have changed.")
        self._tokens = (csrf.group(1), lsd.group(1))
        return html

    def enumerate_clips(self, account_url: str, limit: int) -> list[ClipInfo]:
        """Walk the reels tab, pinned reels first and then newest-first."""
        user = account_name(account_url)
        html = self._bootstrap(profile_url(user))
        owner = _find_clips(html).get("pk")
        if not owner:
            if len(html) < GATED_PAGE_MAX:
                raise _gated(len(html), bool(self.proxy))
            raise ScrapeError(f"No reels data on @{user}'s page ({len(html):,} "
                              "bytes) — Instagram's layout may have changed.")

        logger.info("Enumerating instagram @%s (cap %d)", user, limit)
        found: dict[str, ClipInfo] = {}
        cursor = None
        while len(found) < limit:
            # A long walk is ~125 proxied requests; one flaky one must not
            # throw away the 900 reels already listed. The cursor only moves
            # on success, so a retry re-asks for the same page.
            for attempt in range(3):
                try:
                    page, cursor, more = self._clips_page(user, str(owner), cursor)
                    break
                except Exception as exc:  # noqa: BLE001 — timeouts, proxy drops, 429/5xx
                    if attempt < 2:
                        time.sleep(5 * (attempt + 1))
                        continue
                    if not found:
                        raise
                    logger.warning("@%s: listing stopped at %d reels: %s",
                                   user, len(found), str(exc)[:200])
                    return list(found.values())[:int(limit)]
            fresh = [c for c in page if c.video_id not in found]
            found.update((c.video_id, c) for c in fresh)
            # `not fresh` guards against a cursor that hands back the same page.
            if not (more and cursor and fresh):
                break
            time.sleep(random.uniform(config.SCRAPE_MIN_DELAY, config.SCRAPE_MAX_DELAY))
        logger.info("Enumerated %d reels from @%s", len(found), user)
        return list(found.values())[:int(limit)]

    def _clips_page(self, user: str, owner: str, cursor: Optional[str]):
        data ={"include_feed_video": True, "page_size": 12, "target_user_id": owner}
        if cursor:
            data["max_id"] = cursor
        body = self._graphql("https://www.instagram.com/graphql/query", {
            "fb_api_req_friendly_name": "PolarisProfileReelsTabContentQuery",
            "doc_id": config.SCRAPE_IG_CLIPS_DOC_ID,
            "variables": json.dumps({"data": data, "first": 12}),
        }, referer=f"https://www.instagram.com/{user}/reels/")
        conn = (body.get("data") or {}).get("xdt_api__v1__clips__user__connection_v2")
        if conn is None:
            raise ScrapeError("Instagram's reels list came back without its data "
                              "— the query may have changed.")
        clips = []
        for edge in conn.get("edges") or []:
            node = (edge.get("node") or {}).get("media") or {}
            pk = node.get("pk") or node.get("id")
            if not pk:
                continue
            hidden = bool(node.get("like_and_view_counts_disabled"))
            code = node.get("code") or pk_to_code(pk)
            clips.append(ClipInfo(
                video_id=str(pk),
                url=f"https://www.instagram.com/reel/{code}/",
                view_count=None if hidden else _int(node.get("play_count")),
                like_count=None if hidden else _int(node.get("like_count")),
                comment_count=None if hidden else _int(node.get("comment_count")),
                timestamp=_posted_ts(pk),
                uploader=user,
            ))
        info = conn.get("page_info") or {}
        return clips, info.get("end_cursor"), bool(info.get("has_next_page"))

    def _graphql(self, endpoint: str, form: dict, referer: str,
                 extra_headers: Optional[dict] = None) -> dict:
        """POST one GraphQL query. Each caller sends exactly the header set
        verified for its query — they are not interchangeable: measured
        2026-10-07, the clips query answers `null` to the X-FB-Friendly-Name
        header that the post query was verified with."""
        csrf, lsd = self._tokens
        response = self.session.post(endpoint, data={
            "av": "0", "__d": "www", "__user": "0", "__a": "1", "lsd": lsd,
            "fb_api_caller_class": "RelayModern", "server_timestamps": "true",
            **form,
        }, headers={
            "X-CSRFToken": csrf, "X-IG-App-ID": APP_ID, "X-FB-LSD": lsd,
            "Origin": "https://www.instagram.com", "Referer": referer,
            "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty", "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            **(extra_headers or {}),
        })
        if response.status_code != 200:
            raise ScrapeError(f"Instagram answered HTTP {response.status_code} "
                              f"to {form['fb_api_req_friendly_name']}.")
        try:
            body = response.json()
        except ValueError:
            raise ScrapeError("Instagram answered with a page instead of data — "
                              "this session has been gated.") from None
        if body.get("errors"):
            # The documented doc_id-rotation signature: HTTP 200 with errors.
            raise ScrapeError(
                f"Instagram rejected {form['fb_api_req_friendly_name']} (doc_id "
                f"{form['doc_id']} may have rotated — see BVG_IG_*_DOC_ID): "
                + json.dumps(body["errors"])[:200])
        return body

    def _post(self, clip: ClipInfo) -> dict:
        """The logged-out post query: everything the listing does not carry."""
        body = self._graphql("https://www.instagram.com/api/graphql", {
            "fb_api_req_friendly_name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
            "doc_id": config.SCRAPE_IG_POST_DOC_ID,
            "variables": json.dumps({"media_id": clip.video_id}, separators=(",", ":")),
        }, referer=clip.url, extra_headers={
            "X-FB-Friendly-Name": "PolarisLoggedOutDesktopWWWPostRootContentQuery",
            "X-ASBD-ID": "359341", "X-IG-WWW-Claim": "0",
            "X-Requested-With": "XMLHttpRequest", "Accept": "*/*",
        })
        media = (body.get("data") or {}).get("xig_polaris_media") or {}
        post = media.get("if_not_gated_logged_out")
        if not post:
            raise ScrapeError("Instagram will not show this post logged out — it "
                              "is private, age-restricted or removed.")
        return post

    def _save(self, url: str, dest: Path) -> None:
        """CDN bytes, direct first: the proxy bills per GB and this is the
        bulk of a scrape. Through the proxy only if the CDN refuses us."""
        from curl_cffi import requests as cr

        status = "no response"
        for session in ([None, self.session] if self.proxy else [None]):
            try:
                response = (cr.get(url, impersonate="chrome", timeout=120)
                            if session is None else session.get(url, timeout=120))
            except Exception as exc:  # noqa: BLE001 — try the next route
                status = str(exc)[:120]
                continue
            if response.status_code == 200 and response.content:
                dest.write_bytes(response.content)
                return
            status = f"HTTP {response.status_code}"
        raise ScrapeError(f"Instagram's CDN would not serve the video ({status}).")

    def download(self, clip: ClipInfo, dest_dir: Path) -> Path:
        """Ask for the post, then fetch its MP4. Raises on photos and on posts
        Instagram will not show logged out — the runner counts those as skips."""
        if self._tokens is None:
            self._bootstrap(clip.url)
        post = self._post(clip)
        _enrich(clip, post)
        versions = post.get("video_versions") or []
        if not versions:
            raise ScrapeError("Not a video — a photo or carousel post.")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{clip.video_id}.mp4"
        self._save(versions[0]["url"], dest)
        clip.duration = clip.duration or tiktok.probe_duration(dest)
        return dest

    def download_audio(self, clip: ClipInfo, dest_dir: Path) -> tuple[Path, ClipInfo]:
        """The sound behind a reel, as MP3. Instagram offers no separate audio
        file logged out, so it comes out of the video — the reel's full mix,
        exactly as with TikTok's music scrape."""
        scratch = Path(dest_dir) / f"_{clip.video_id}"
        try:
            video = self.download(clip, scratch)
            if not tiktok.has_audio(video):
                raise ScrapeError("This reel has no audio.")
            # The audio knobs mean what they mean to yt-dlp's FFmpegExtractAudio
            # (the TikTok/YouTube path): a codec name, and a quality that is a
            # VBR level below 10 and a bitrate in kbps above it.
            codec = config.SCRAPE_AUDIO_CODEC
            ext = {"best": "m4a", "vorbis": "ogg", "alac": "m4a"}.get(codec, codec)
            quality = config.SCRAPE_AUDIO_QUALITY.rstrip("kK")
            level = (["-q:a", quality] if float(quality) < 10
                     else ["-b:a", f"{quality}k"])
            out = Path(dest_dir) / f"{clip.video_id}.{ext}"
            proc = subprocess.run(
                [tiktok.find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(video),
                 "-vn", *level, str(out)],
                **tiktok._ff_capture(), timeout=180)
            if proc.returncode != 0 or not out.is_file():
                raise ScrapeError(f"Could not extract the audio: {(proc.stderr or '')[-300:]}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return out, clip

    def fetch_one(self, url: str, dest_dir: Path) -> tuple[ClipInfo, Path]:
        target = video_url(url)
        match = _CODE_RE.search(urlparse(target).path)
        if not match:
            raise ScrapeError("No reel in that link.")
        clip = ClipInfo(video_id=shortcode_to_pk(match.group(1)), url=target)
        return clip, self.download(clip, dest_dir)


def _new_session(proxy: str):
    """curl_cffi as Chrome: the only Python client with a real browser TLS
    fingerprint, which is what Instagram's gate looks at. Its UA matches the
    impersonation, so headers stay coherent."""
    try:
        from curl_cffi import requests as cr
    except ImportError as exc:  # pragma: no cover
        raise ScrapeError("curl_cffi is not installed. Install it with:  "
                          "pip install -r requirements.txt") from exc
    session = cr.Session(impersonate="chrome", timeout=25)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.headers.update({"Accept-Encoding": "gzip, br"})
    return session


def make_source(account: str = "") -> InstagramSource:
    return InstagramSource(session_key=account)


def fetch_single_video(url: str, dest_dir: Path,
                       trim_start: Optional[float] = None,
                       trim_duration: Optional[float] = None,
                       source: Optional[InstagramSource] = None,
                       attempts: Optional[int] = None,
                       retry_delay: Optional[float] = None) -> SingleFetch:
    """One reel, whole or split — see tiktok.fetch_single_video."""
    if not is_video_url(url):
        raise ScrapeError(
            "That does not look like a link to one reel. Paste its link "
            "(…instagram.com/reel/…) — a profile link belongs in the account "
            "scraper."
        )
    source = source or InstagramSource(session_key=url)
    return tiktok.fetch_and_trim(url, dest_dir, source, trim_start, trim_duration,
                                 attempts, retry_delay, who="Instagram")
