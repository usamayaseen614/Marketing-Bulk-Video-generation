"""
scrapers/proxy.py — the Decodo residential proxy, per platform.

The same Decodo account the social-media dashboard uses, and the same URL
grammar (ported from its app/proxy.py, syntax verified against
help.decodo.com 2026-09-14):

    user-<USERNAME>-country-us-session-<ID>-sessionduration-<N>:<PASSWORD>
    @gate.decodo.com:7000

A bare `username:password` is plain rotating; any parameter needs the literal
`user-` prefix and HYPHEN separators — not the colon style other providers use.

Two decisions worth knowing before changing anything here:

  * **Only the platforms in BVG_PROXY_PLATFORMS use it** (Instagram by
    default). Residential traffic is billed per GB and a scrape is mostly video
    bytes, so routing a platform that works fine from the VM through it would
    only cost money.

  * **One sticky exit per account.** Changing exit IP halfway through an
    account's walk, while the TLS fingerprint and headers stay fixed, is itself
    a bot signal; pinning a session per account keeps the identity coherent.
"""

from __future__ import annotations

import hashlib
from urllib.parse import quote, urlsplit

import config

GATE_HOST = "gate.decodo.com"
ROTATING_PORT = 7000
SESSION_MINUTES = 30      # Decodo's default is shorter than a scrape; 24h is its cap


def build(username: str, password: str, *, country: str = "",
          session: str = "", minutes: int = SESSION_MINUTES) -> str:
    """Compose a Decodo URL. http:// even for HTTPS traffic — the hop to the
    proxy is plain HTTP and HTTPS rides a CONNECT tunnel through it."""
    parts = [username]
    if country:
        parts += ["country", country.lower()]
    if session:
        parts += ["session", session, "sessionduration", str(minutes)]
    user = "user-" + "-".join(parts) if len(parts) > 1 else username
    return "http://%s:%s@%s:%d" % (quote(user, safe="-_"), quote(password, safe=""),
                                   GATE_HOST, ROTATING_PORT)


def url_for(platform: str, account: str = "") -> str:
    """The proxy this platform's requests should use, or "" to go direct.

    BVG_PROXY_URL (a full pre-built URL, e.g. the dashboard's PROXY_URL secret)
    wins as-is. Otherwise the URL is composed from the Decodo credentials, with
    a session pinned to `account` so one account keeps one exit IP."""
    if platform not in config.PROXY_PLATFORMS:
        return ""
    if config.PROXY_URL:
        # Checked here because both curl_cffi and yt-dlp quote a malformed
        # proxy URL — password and all — in their error text, which lands in
        # job rows, on the Jobs page and in emails.
        try:
            parts = urlsplit(config.PROXY_URL)
            ok = bool(parts.hostname and parts.port)
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("BVG_PROXY_URL is malformed (%s) — percent-encode "
                             "special characters in the password"
                             % mask(config.PROXY_URL))
        return config.PROXY_URL
    if not (config.DECODO_USER and config.DECODO_PASS):
        return ""
    session = hashlib.sha256(f"{platform}:{account}".encode()).hexdigest()[:16] \
        if account else ""
    return build(config.DECODO_USER, config.DECODO_PASS,
                 country=config.PROXY_COUNTRY, session=session)


def mask(url: str) -> str:
    """Never let credentials reach a log line or the Setup page."""
    if not url or "@" not in url:
        return url or "(none)"
    scheme, _, rest = url.partition("://")
    # rsplit: an unencoded "@" in the password must not leak what follows it.
    return "%s://***:***@%s" % (scheme, rest.rsplit("@", 1)[1])
