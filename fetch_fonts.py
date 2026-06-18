"""
fetch_fonts.py — Download the bundled font library into ./fonts.

The app ships a curated set of open-source display fonts (TikTok-style) so the
font picker works identically on Windows, macOS, and Linux/Docker without
relying on whatever happens to be installed. All fonts here are licensed under
the SIL Open Font License (OFL) or Apache 2.0 — free to bundle and redistribute.

Run once after cloning (the .ttf files are also committed, so this is only
needed to refresh them):

    python fetch_fonts.py

Each entry maps a local filename to its path inside the google/fonts GitHub
repo. Two are variable fonts (rendered at their Bold instance by the engine);
the rest are single-weight display faces.
"""

from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import ImageFont

FONTS_DIR = Path(__file__).parent / "fonts"
_RAW = "https://raw.githubusercontent.com/google/fonts/main/"

# local filename -> repo path (license dir + family + file)
FONT_SOURCES: dict[str, str] = {
    "BebasNeue-Regular.ttf": "ofl/bebasneue/BebasNeue-Regular.ttf",
    "Anton-Regular.ttf": "ofl/anton/Anton-Regular.ttf",
    "Montserrat-Variable.ttf": "ofl/montserrat/Montserrat[wght].ttf",
    "PlayfairDisplay-Variable.ttf": "ofl/playfairdisplay/PlayfairDisplay[wght].ttf",
    "Pacifico-Regular.ttf": "ofl/pacifico/Pacifico-Regular.ttf",
    "PermanentMarker-Regular.ttf": "apache/permanentmarker/PermanentMarker-Regular.ttf",
    "SpecialElite-Regular.ttf": "apache/specialelite/SpecialElite-Regular.ttf",
    "Lobster-Regular.ttf": "ofl/lobster/Lobster-Regular.ttf",
    "PressStart2P-Regular.ttf": "ofl/pressstart2p/PressStart2P-Regular.ttf",
    "Bungee-Regular.ttf": "ofl/bungee/Bungee-Regular.ttf",
}


def _download(repo_path: str) -> bytes:
    # The "[wght]" in variable-font filenames must be percent-encoded.
    url = _RAW + urllib.parse.quote(repo_path)
    req = urllib.request.Request(url, headers={"User-Agent": "bvg-fetch-fonts"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — fixed host
        return resp.read()


def main() -> int:
    FONTS_DIR.mkdir(exist_ok=True)
    failures = 0
    for filename, repo_path in FONT_SOURCES.items():
        dest = FONTS_DIR / filename
        try:
            data = _download(repo_path)
            dest.write_bytes(data)
            font = ImageFont.truetype(str(dest), 48)  # verify it loads
            print(f"OK   {filename:32s} {len(data) // 1024:4d} KB  ({font.getname()[0]})")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {filename:32s} {exc}")
    print(f"\nDone — {len(FONT_SOURCES) - failures}/{len(FONT_SOURCES)} fonts in {FONTS_DIR}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
