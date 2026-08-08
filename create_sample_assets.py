"""
create_sample_assets.py — Generate a sample input set for trying out the app.

Produces in ./sample_assets:
    data.xlsx        — 5 demo rows (row 2 references a missing background on purpose,
                       to demonstrate per-row error handling)
    backgrounds.zip  — two gradient background images (1080x1920)
    promo.mp4        — 5-second 16:9 test video with a tone (FFmpeg testsrc)
    promo_2.mp4 / promo_3.mp4 — two more promos, visibly different, so a
                       multi-batch render and the per-promo text sheets below
                       have something to rotate through
    cta.png          — a "SHOP NOW" call-to-action button with transparency
                       (the CTA image is optional — omit it to skip that layer)
    cta_video_1..5.mp4 — five short clips to try as the optional CTA videos
                       (they play back-to-back in a shuffled order)
    headline_by_promo.xlsx / subheading_by_promo.xlsx / footer_by_promo.xlsx
                     — the optional per-promo text sheets: one column per promo
                       above, one row per row of data.xlsx, so the same row says
                       something different on each promo

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


def make_video(path: Path, source: str = "testsrc", tone: int = 440) -> None:
    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"{source}=size=1280x720:rate=30:duration=5",
        "-f", "lavfi", "-i", f"sine=frequency={tone}:duration=5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True)


# The promo videos, in upload order. `promo.mp4` keeps its name and its source
# so everything that already refers to it is unaffected; the other two exist so
# a multi-batch render has something to rotate, and so the per-promo text
# sheets have more than one column to be interesting.
PROMOS = [("promo.mp4", "testsrc", 440),
          ("promo_2.mp4", "smptebars", 330),
          ("promo_3.mp4", "rgbtestsrc", 550)]


def make_cta_videos(out_dir: Path, count: int = 5) -> None:
    """A few short, silent clips for the optional CTA-video slot — distinct
    sources so the shuffled back-to-back sequence is easy to see."""
    ffmpeg = find_ffmpeg()
    sources = ["testsrc2", "smptebars", "rgbtestsrc", "testsrc"]
    for i in range(count):
        src = sources[i % len(sources)]
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"{src}=size=600x600:rate=30:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
            str(out_dir / f"cta_video_{i + 1}.mp4"),
        ]
        subprocess.run(cmd, check=True)


# The optional per-promo text sheets, one per role. A column header is a promo
# video's filename and a row lines up with a row of the main sheet, so the video
# made from row 3 on promo_2.mp4 reads "Just dropped" rather than whatever the
# main sheet's row 3 says.
#
# Only the words live here. Size, font, colour, position and background box all
# still come from the main sheet, which is the point: one design, different
# wording per promo. Each column is written in a different voice so the effect
# is obvious the moment you watch two videos from the same row.
#
# Row 4 of promo.mp4 in the Headline sheet is BLANK on purpose: a blank cell
# falls back to the main Excel, so you can override only the cells you care
# about. Everything is deliberately generic, because these five rows have to
# read sensibly against whichever 5-row sheet you pair them with.
TEXT_BY_PROMO = {
    "Headline": {
        # Voice: plain and direct.
        "promo.mp4": [
            "Summer Mega Sale", "New Arrivals", "Flash Deal Today", "",
            "These deals are unreal this weekend"],
        # Voice: loud and urgent.
        "promo_2.mp4": [
            "Biggest Summer Blowout", "Just Dropped", "Today Only",
            "Big Weekend Savings", "You are not ready for this drop"],
        # Voice: calm and premium.
        "promo_3.mp4": [
            "Summer Clearance Is Live", "Fresh In This Week", "While It Lasts",
            "The Weekend Edit", "Everyone is talking about these deals"],
    },
    "Subheading": {
        "promo.mp4": [
            "Up to 50% off everything", "Fresh styles every week",
            "While stocks last", "Everything must go",
            "Tap through and see the full range"],
        "promo_2.mp4": [
            "Half price, this week only", "New in every Thursday",
            "Gone by tonight", "Nothing is staying on the shelf",
            "Grab yours before every size sells out"],
        "promo_3.mp4": [
            "Selected lines reduced", "Restocked and ready",
            "Limited quantities", "Final reductions now on",
            "Take a look before the weekend is out"],
    },
    "Footer": {
        "promo.mp4": [
            "Offer ends June 30", "www.example.com", "Limited time only",
            "Shop now before it ends", "Follow us for daily drops"],
        "promo_2.mp4": [
            "Ends Sunday at midnight", "shop.example.com", "Today only",
            "Last chance this weekend", "New videos every single day"],
        "promo_3.mp4": [
            "While stocks last", "example.com/sale", "Until sold out",
            "Closing soon", "Hit follow so you don't miss one"],
    },
}


def make_text_grids(out_dir: Path, n_rows: int) -> list[tuple[str, Path]]:
    """Write one per-promo text sheet per role, trimmed to the sheet's rows.

    The row count has to match data.xlsx exactly — the app refuses a sheet that
    disagrees, because row 1 here is row 1 there and a silent off-by-one would
    put every video's text on the wrong video."""
    written = []
    for role, columns in TEXT_BY_PROMO.items():
        frame = pd.DataFrame({name: texts[:n_rows]
                              for name, texts in columns.items()})
        path = out_dir / f"{role.lower()}_by_promo.xlsx"
        frame.to_excel(path, sheet_name=role, index=False)
        written.append((role, path))
    return written


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
    for name, source, tone in PROMOS:
        make_video(OUT / name, source=source, tone=tone)
    make_cta_videos(OUT, count=5)

    rows = [
        {
            # Explicit per-row video box (smaller, pushed down) and a CTA
            # moved to the bottom-left instead of the default spot. Showcases an
            # outlined headline on a highlight box, a drop-shadow subheading, and
            # the optional CTA video placed bottom-right with a custom fade.
            "BG_Image": "bg_blue.png",
            "Video_X": 140, "Video_Y": 420, "Video_Width": 800, "Video_Height": 800,
            "CTA_X": 60, "CTA_Y": 1680, "CTA_Width": None, "CTA_Height": None,
            "CTA_Video_X": 720, "CTA_Video_Y": 1560, "CTA_Video_Width": 300,
            "CTA_Video_Height": 300, "CTA_Video_Fade_Start": 1.0,
            "CTA_Video_Fade_Duration": 0.8,
            # Per-clip playback speeds: clip 1 fast, clip 2 normal, clip 3 slow,
            # clip 4 brisk. (CTA_Video_Speed would set them all at once instead.)
            "CTA_Video_Speed_1": 2.0, "CTA_Video_Speed_2": 1.0,
            "CTA_Video_Speed_3": 0.5, "CTA_Video_Speed_4": 1.5,
            "Headline": "Summer Mega Sale", "Headline_Size": 72,
            "Headline_Color": "#FFFFFF", "Headline_X": 540, "Headline_Y": 160,
            "Headline_Font": "Impact (Bebas Neue)", "Headline_BgColor": "#FF2D55",
            # Translucent highlight box under solid text — the caption look:
            # the box tints the video instead of hiding it.
            "Headline_BgOpacity": "55%",
            "Headline_Style": "outline",
            "Subheading": "Up to 50% off everything", "Subheading_Size": 44,
            "Subheading_Color": "#FFD700", "Subheading_X": 540, "Subheading_Y": 245,
            "Subheading_Style": "shadow",
            "Footer": "Offer ends June 30", "Footer_Size": 32,
            "Footer_Color": "#CCCCCC", "Footer_X": 540, "Footer_Y": 1850,
            # Experimental: split this footer across frames (persistence of
            # vision) — no single frame shows the whole line. See the sidebar's
            # "Subliminal text" section and the README caveats.
            "Footer_Subliminal": "yes",
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
            # Neon headline in a script font; the others left to auto-styling.
            "Video_X": None, "Video_Y": None, "Video_Width": 700, "Video_Height": 700,
            "CTA_X": None, "CTA_Y": None, "CTA_Width": 320, "CTA_Height": 128,
            "Headline": "New Arrivals", "Headline_Size": 80,
            "Headline_Color": "#00F5D4", "Headline_X": 540, "Headline_Y": 180,
            "Headline_Font": "Script (Pacifico)", "Headline_Style": "neon",
            "Subheading": "Fresh styles every week", "Subheading_Size": 40,
            "Subheading_Color": "lightyellow", "Subheading_X": 540, "Subheading_Y": 270,
            # Ghosted text: 60% opaque, so the background reads through it.
            "Subheading_Opacity": "60%",
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
    # randomly assigned from the ZIP; sizes, colors, positions, fonts, and
    # styles are all chosen automatically; the video and CTA boxes use the
    # sidebar settings.
    auto = df.drop(columns=["BG_Image"] + [
        c for c in df.columns
        if c.startswith(("Video_", "CTA_"))
        or c.endswith(("_X", "_Y", "_Size", "_Color", "_Font", "_BgColor", "_Style",
                       "_Opacity", "_BgOpacity"))
    ])
    auto.to_excel(OUT / "data_auto.xlsx", index=False)

    grids = make_text_grids(OUT, len(df))

    # Written and then checked with the app's own rules, so a sample that the
    # Check button would reject can never be shipped: the demo of a strict
    # feature has to survive the strictness.
    import text_grids

    promo_names = [name for name, _s, _t in PROMOS]
    for role, path in grids:
        report = text_grids.check_grid(
            text_grids.read_grid(path, role), promo_names, len(df))
        if report.errors:
            raise SystemExit(f"{path.name} would be rejected by the app:\n  "
                             + "\n  ".join(report.errors))

    print(f"Sample assets written to {OUT}")
    print(f"  promos: {', '.join(promo_names)}")
    print(f"  per-promo text: {', '.join(p.name for _r, p in grids)} "
          f"({len(df)} rows x {len(PROMOS)} promos each)")


if __name__ == "__main__":
    main()
