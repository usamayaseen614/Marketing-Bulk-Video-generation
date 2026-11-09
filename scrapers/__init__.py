"""Clip sources for the CTA-video slot bank: TikTok, Instagram and YouTube.

Each platform module exposes the same handful of names — account_name,
profile_url, is_video_url, fetch_single_video and make_source — so the scrape
runner and the scraper page never branch on the platform beyond picking one.
The trim / split / hash / batch-planning helpers are platform-neutral and live
in scrapers/tiktok.py, where they started.
"""

from __future__ import annotations

import importlib

PLATFORMS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}


def module(platform: str):
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform {platform!r}")
    return importlib.import_module(f"scrapers.{platform}")


def detect(raw: str, default: str = "tiktok") -> str:
    """Which platform a pasted link belongs to. A bare @handle says nothing, so
    it gets `default` — the caller's explicit choice."""
    text = (raw or "").lower()
    if "instagram.com" in text or "instagr.am" in text:
        return "instagram"
    if "youtube.com" in text or "youtu.be" in text:
        return "youtube"
    if "tiktok.com" in text:
        return "tiktok"
    return default


def not_an_account(platform: str, raw: str) -> str:
    """Why `raw` cannot be scraped as an account, or "" when it can.

    A pasted reel or Short is the commonest slip, and without this it resolves
    to SOME handle — Instagram's parser finds no user in /reel/<code>/ — so the
    job would quietly scrape a stranger's profile. TikTok is exempt: its video
    links carry the @handle, and the scrape has always accepted them."""
    mod = module(platform)
    text = (raw or "").strip()
    one_video = (platform != "tiktok" and "/" in text and "list=" not in text
                 and mod.is_video_url(text))
    if one_video or (text and not mod.account_name(text)):
        return ("That's a link to one video, not an account — paste the "
                "profile link, or use the Single video tab for just this one.")
    return ""


def ledger_key(platform: str, account: str) -> str:
    """The dedup-history key. TikTok keeps the bare handle it always had, so
    existing histories still match; the others are prefixed because creators
    reuse one handle everywhere, and "Forget @bob" on the scraper page must not
    wipe bob's TikTok history along with his Instagram one."""
    return account if platform == "tiktok" else f"{platform}:{account}"
