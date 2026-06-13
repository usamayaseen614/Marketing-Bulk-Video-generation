"""
create_sample_assets.py — Generate a sample input set for trying out the app.

Produces in ./sample_assets:
    data.xlsx        — 5 demo rows (row 2 references a missing background on purpose,
                       to demonstrate per-row error handling)
    backgrounds.zip  — two gradient background images (1080x1920)
    promo.mp4        — 5-second 16:9 test video with a tone (FFmpeg testsrc)
    cta.png          — a "SHOP NOW" call-to-action button with transparency

Usage:  python create_sample_assets.py
"""

import subprocess
import zipfile
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from video_generator import find_default_font, find_ffmpeg

OUT = Path(__file__).parent / "sample_assets"
OUT.mkdir(exist_ok=True)


def make_gradient(path: Path, top: tuple, bottom: tuple) -> None:
    img = Image.new("RGB", (1080, 1920))
    px = img.load()
    for y in range(1920):
        t = y / 1919
        color = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom))
        for x in range(1080):
            px[x, y] = color
    img.save(path)


def make_cta(path: Path) -> None:
    img = Image.new("RGBA", (400, 160), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, 399, 159], radius=40, fill=(255, 87, 51, 255))
    font_path = find_default_font()
    font = ImageFont.truetype(font_path, 56) if font_path else ImageFont.load_default(size=56)
    draw.text((200, 80), "SHOP NOW", font=font, fill="white", anchor="mm")
    img.save(path)


def make_video(path: Path) -> None:
    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    bg1 = OUT / "bg_blue.png"
    bg2 = OUT / "bg_sunset.png"
    make_gradient(bg1, (20, 30, 90), (90, 40, 140))
    make_gradient(bg2, (250, 120, 40), (140, 20, 60))

    with zipfile.ZipFile(OUT / "backgrounds.zip", "w") as zf:
        zf.write(bg1, "bg_blue.png")
        zf.write(bg2, "bg_sunset.png")
    bg1.unlink()
    bg2.unlink()

    make_cta(OUT / "cta.png")
    make_video(OUT / "promo.mp4")

    rows = [
        {
            # Explicit per-row video box (smaller, pushed down) and a CTA
            # moved to the bottom-left instead of the default spot.
            "BG_Image": "bg_blue.png",
            "Video_X": 140, "Video_Y": 420, "Video_Width": 800, "Video_Height": 800,
            "CTA_X": 60, "CTA_Y": 1680, "CTA_Width": None, "CTA_Height": None,
            "Headline": "Summer Mega Sale", "Headline_Size": 72,
            "Headline_Color": "#FFD700", "Headline_X": 540, "Headline_Y": 160,
            "Subheading": "Up to 50% off everything", "Subheading_Size": 44,
            "Subheading_Color": "#FFFFFF", "Subheading_X": 540, "Subheading_Y": 245,
            "Footer": "Offer ends June 30", "Footer_Size": 32,
            "Footer_Color": "#CCCCCC", "Footer_X": 540, "Footer_Y": 1850,
        },
        {
            "BG_Image": "does_not_exist.png",  # intentional failure demo
            "Video_X": None, "Video_Y": None, "Video_Width": None, "Video_Height": None,
            "CTA_X": None, "CTA_Y": None, "CTA_Width": None, "CTA_Height": None,
            "Headline": "Broken Row Example", "Headline_Size": 72,
            "Headline_Color": "#FFFFFF", "Headline_X": 540, "Headline_Y": 160,
            "Subheading": "", "Subheading_Size": 44,
            "Subheading_Color": "", "Subheading_X": 540, "Subheading_Y": 245,
            "Footer": "", "Footer_Size": 32,
            "Footer_Color": "", "Footer_X": 540, "Footer_Y": 1850,
        },
        {
            "BG_Image": "BG_SUNSET.png",  # case-insensitive lookup demo
            # Size-only overrides: positions stay at the sidebar defaults.
            "Video_X": None, "Video_Y": None, "Video_Width": 700, "Video_Height": 700,
            "CTA_X": None, "CTA_Y": None, "CTA_Width": 320, "CTA_Height": 128,
            "Headline": "New Arrivals", "Headline_Size": 80,
            "Headline_Color": "white", "Headline_X": 540, "Headline_Y": 180,
            "Subheading": "Fresh styles every week", "Subheading_Size": 40,
            "Subheading_Color": "lightyellow", "Subheading_X": 540, "Subheading_Y": 270,
            "Footer": "www.example.com", "Footer_Size": 30,
            "Footer_Color": "#EEEEEE", "Footer_X": 540, "Footer_Y": 1860,
        },
        {
            # Auto-placement demo: blank X/Y cells get random, non-overlapping
            # spots; the empty Footer is simply skipped.
            "BG_Image": "bg_blue.png",
            "Video_X": None, "Video_Y": None, "Video_Width": None, "Video_Height": None,
            "CTA_X": None, "CTA_Y": None, "CTA_Width": None, "CTA_Height": None,
            "Headline": "Auto-Placed Headline", "Headline_Size": 64,
            "Headline_Color": "#00FFCC", "Headline_X": None, "Headline_Y": None,
            "Subheading": "This text found its own spot", "Subheading_Size": 40,
            "Subheading_Color": "white", "Subheading_X": None, "Subheading_Y": None,
            "Footer": "", "Footer_Size": 30,
            "Footer_Color": "", "Footer_X": None, "Footer_Y": None,
        },
        {
            # Long-text demo: headline/subheading auto-wrap to stay on the
            # canvas, the footer is balanced onto 3 lines, blank BG_Image is
            # randomly assigned, and all styling is randomized.
            "BG_Image": "",
            "Video_X": None, "Video_Y": None, "Video_Width": None, "Video_Height": None,
            "CTA_X": None, "CTA_Y": None, "CTA_Width": None, "CTA_Height": None,
            "Headline": "OMG, Usama is sooooo good at dancing",
            "Headline_Size": None, "Headline_Color": None,
            "Headline_X": None, "Headline_Y": None,
            "Subheading": "Watch the full video and try to keep up with every move",
            "Subheading_Size": None, "Subheading_Color": None,
            "Subheading_X": None, "Subheading_Y": None,
            "Footer": "Follow us for daily dance tutorials and behind the scenes fun",
            "Footer_Size": None, "Footer_Color": None,
            "Footer_X": None, "Footer_Y": None,
        },
    ]
    df = pd.DataFrame(rows)
    df.to_excel(OUT / "data.xlsx", index=False)

    # Fully-automatic variant: only the text columns remain. Backgrounds are
    # randomly assigned from the ZIP; sizes, colors, and positions are all
    # randomized per row; the video and CTA boxes use the sidebar settings.
    auto = df.drop(columns=["BG_Image"] + [
        c for c in df.columns
        if c.startswith(("Video_", "CTA_")) or c.endswith(("_X", "_Y", "_Size", "_Color"))
    ])
    auto.to_excel(OUT / "data_auto.xlsx", index=False)

    print(f"Sample assets written to {OUT}")


if __name__ == "__main__":
    main()
