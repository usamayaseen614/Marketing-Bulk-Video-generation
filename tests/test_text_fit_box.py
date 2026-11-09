"""Fixed-size text fit boxes: <Role>_Width/<Role>_Height.

The box drives the type — the text re-wraps to the box width and the font size
is searched so the PAINTED block (glyphs plus the style's outline/shadow/glow
padding) fills the box. Measuring glyphs alone would let a neon glow spill
outside the box the user drew, which is the subtlety worth a test.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="fitbox_")
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from PIL import Image

from video_generator import (ALL_COLUMNS, TEXT_BOX_COLUMNS, TEXT_FIT_MAX_SIZE,
                             TEXT_FIT_MIN_SIZE, TEXT_ROLES, RenderConfig,
                             RowSpec, VideoGenerator, find_ffmpeg)

FF = find_ffmpeg()
TMP = Path(tempfile.mkdtemp(prefix="fitbox_"))
BG = TMP / "bg"
BG.mkdir()
Image.new("RGB", (1080, 1920), (18, 18, 40)).save(BG / "b.png")
PROMO = TMP / "p.mp4"
subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=black:s=64x64:r=30", "-t", "1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(PROMO)],
               check=True, capture_output=True)

assert TEXT_BOX_COLUMNS == ["Headline_Width", "Headline_Height",
                            "Subheading_Width", "Subheading_Height",
                            "Footer_Width", "Footer_Height"], TEXT_BOX_COLUMNS
assert all(c in ALL_COLUMNS for c in TEXT_BOX_COLUMNS)
print(f"columns registered on the sheet template: {', '.join(TEXT_BOX_COLUMNS)}")


def fit(text, w, h, *, style="classic", role="Headline", size=None, cfg=None):
    """Resolve one row and return (element, spec) after the fit runs."""
    gen = VideoGenerator(cfg or RenderConfig(), BG, PROMO, None,
                         TMP / "w", TMP / "o")
    cells = {"BG_Image": "b.png", "Headline": "", "Subheading": "", "Footer": "",
             role: text, f"{role}_X": 540, f"{role}_Y": 900,
             f"{role}_Style": style}
    if w is not None:
        cells[f"{role}_Width"] = w
    if h is not None:
        cells[f"{role}_Height"] = h
    if size is not None:
        cells[f"{role}_Size"] = size
    spec = RowSpec.from_row(pd.Series(cells), 1)
    gen._resolve_positions(spec)
    element = next(e for e in spec.text_elements if e.role == role)
    return gen, element, spec


def painted(gen, element):
    """Block size INCLUDING the style padding — what actually lands on screen."""
    tw, th = gen._measure_text(element)
    pad = gen._style_metrics(element)[5]
    return tw + 2 * pad, th + 2 * pad


# ---------- 1. the box drives the size, up and down ----------
print("\nsame text, three boxes — the size follows the box:")
sizes = []
for bw, bh in ((900, 400), (600, 300), (300, 150)):
    gen, el, spec = fit("Summer Mega Sale", bw, bh)
    pw, ph = painted(gen, el)
    assert pw <= bw and ph <= bh, f"{bw}x{bh}: painted {pw:.0f}x{ph:.0f} overflows"
    assert not spec.warnings, spec.warnings
    sizes.append(el.size)
    print(f"   box {bw}x{bh} -> {el.size}px, {len(el.text.splitlines())} line(s), "
          f"painted {pw:.0f}x{ph:.0f}")
assert sizes == sorted(sizes, reverse=True), sizes
assert sizes[0] > 88, "a big box must GROW the text, not just shrink it"
print("   grows as well as shrinks, and never overflows")

# ---------- 2. style padding is part of the fit ----------
gen_c, el_c, _ = fit("Summer Mega Sale", 300, 150, style="classic")
gen_n, el_n, _ = fit("Summer Mega Sale", 300, 150, style="neon")
pw_n, ph_n = painted(gen_n, el_n)
assert el_n.size < el_c.size, (el_c.size, el_n.size)
assert pw_n <= 300 and ph_n <= 150, f"neon glow escapes the box: {pw_n:.0f}x{ph_n:.0f}"
print(f"\nstyle padding counted: classic {el_c.size}px vs neon {el_n.size}px in the "
      f"same 300x150 box (glow stays inside: {pw_n:.0f}x{ph_n:.0f})")

# ---------- 3. line breaks are recomputed, added and removed ----------
LONG = "Limited Time Only Up To Seventy Percent Off"
_, wide_el, _ = fit(LONG, 1000, 200)
_, narrow_el, _ = fit(LONG, 400, 400)
assert len(narrow_el.text.splitlines()) > len(wide_el.text.splitlines())
print(f"\nline breaks follow the box: {len(wide_el.text.splitlines())} line(s) at "
      f"1000x200, {len(narrow_el.text.splitlines())} at 400x400")

# ---------- 4. a boxed text ignores its *_Size cell ----------
_, sized_el, _ = fit("Summer Mega Sale", 900, 400, size=24)
_, unsized_el, _ = fit("Summer Mega Sale", 900, 400)
assert sized_el.size == unsized_el.size, (sized_el.size, unsized_el.size)
assert sized_el.size != 24
print(f"\n*_Size is superseded by the box: cell said 24, box gave {sized_el.size}px")

# ---------- 5. manual line breaks survive ----------
_, br_el, _ = fit("Summer|Mega Sale", 700, 400)
assert br_el.text.splitlines()[0] == "Summer", br_el.text
print(f"\nmanual '|' break honoured: {br_el.text.splitlines()}")

# ---------- 6. impossible box: floor, warn, overflow — never clip ----------
gen_x, el_x, spec_x = fit("Antidisestablishmentarianism", 200, 200)
pw_x, _ = painted(gen_x, el_x)
assert el_x.size == TEXT_FIT_MIN_SIZE, el_x.size
assert pw_x > 200, "expected this case to overflow rather than fit"
assert any("does not fit" in w for w in spec_x.warnings), spec_x.warnings
assert "Antidisestablishmentarianism" in el_x.text, "text must not be clipped"
print(f"\nimpossible box: floored at {el_x.size}px, overflows to {pw_x:.0f}px wide, "
      f"warned, text intact")
print(f"   warning: {spec_x.warnings[0][:88]}…")

# ---------- 7. opt-in: no box == byte-for-byte the old behaviour ----------
_, plain_el, plain_spec = fit("Summer Mega Sale", None, None, size=72)
assert plain_el.size == 72, plain_el.size
assert plain_el.text == "Summer Mega Sale", repr(plain_el.text)
assert plain_el.box_w in (0, None) and plain_el.box_h in (0, None)
# a width with no height is not a box either
_, half_el, _ = fit("Summer Mega Sale", 300, None, size=72)
assert half_el.size == 72, "width alone must not trigger the fit"
print("\nunboxed text untouched (72px, unwrapped); width without height is ignored")

# ---------- 8. sidebar defaults apply, and a row cell overrides them ----------
cfg = RenderConfig(headline_box_w=600, headline_box_h=300)
_, cfg_el, _ = fit("Summer Mega Sale", None, None, cfg=cfg)
assert cfg_el.box_w == 600 and cfg_el.box_h == 300
_, ovr_el, _ = fit("Summer Mega Sale", 300, 150, cfg=cfg)
assert ovr_el.box_w == 300 and ovr_el.size < cfg_el.size, (cfg_el.size, ovr_el.size)
print(f"\nsidebar default box applies ({cfg_el.size}px); a row cell overrides it "
      f"({ovr_el.size}px)")
# other roles stay unboxed when only the headline default is set
_, foot_el, _ = fit("Terms apply", None, None, role="Footer", cfg=cfg)
assert not (foot_el.box_w and foot_el.box_h), (foot_el.box_w, foot_el.box_h)
print("   a per-role default does not leak to the other roles")

# ---------- 8b. the PAINTED pixels stay inside the box ----------
# The strongest form of the promise, and the one worth asserting: rasterise the
# text layer and measure the alpha bounding box, rather than trusting the
# measurement the fit itself used. Descenders and the style treatments (glow,
# stroke, drop shadow) are all real pixels that a bbox-only check can miss.
PIXEL_CASES = [
    ("Summer Mega Sale", 900, 320, "classic"),
    ("Limited Time Only Up To Seventy Percent Off Everything In Store", 500, 400, "classic"),
    ("Summer Mega Sale", 300, 150, "neon"),
    ("Summer Mega Sale", 400, 200, "outline"),
    ("Deep jygpq descenders", 600, 200, "classic"),
    ("Terms and conditions apply see website", 900, 120, "shadow"),
]
print("\npainted-pixel check (alpha bbox of the rendered text layer):")
for text, bw, bh, style in PIXEL_CASES:
    gen_px = VideoGenerator(RenderConfig(), BG, PROMO, None, TMP / "wp", TMP / "op")
    spec_px = RowSpec.from_row(pd.Series(
        {"BG_Image": "b.png", "Headline": text, "Headline_X": 540,
         "Headline_Y": 900, "Headline_Width": bw, "Headline_Height": bh,
         "Headline_Style": style, "Headline_Color": "#FFFFFF",
         "Subheading": "", "Footer": ""}), 1)
    overlay = gen_px.build_overlay_image(spec_px, include_cta=False)
    bbox = overlay.getbbox()
    pw, ph = bbox[2] - bbox[0], bbox[3] - bbox[1]
    assert pw <= bw and ph <= bh, (
        f"{style} '{text[:20]}' painted {pw}x{ph} outside its {bw}x{bh} box")
    print(f"   {style:8s} {bw:4d}x{bh:<4d} -> {spec_px.headline.size:3d}px, "
          f"painted {pw}x{ph}")
print("   nothing escapes its box")

# ---------- 9. the box is reserved for auto-placement ----------
gen_ap = VideoGenerator(RenderConfig(headline_box_w=500, headline_box_h=260),
                        BG, PROMO, None, TMP / "w2", TMP / "o2")
ap_spec = RowSpec.from_row(pd.Series(
    {"BG_Image": "b.png", "Headline": "Hi", "Subheading": "", "Footer": ""}), 1)
gen_ap._resolve_positions(ap_spec)
hl = ap_spec.headline
assert hl.x is not None and hl.y is not None
# the whole BOX must land on canvas, not just the small fitted block
assert 0 <= hl.x - hl.box_w / 2 and hl.x + hl.box_w / 2 <= 1080, hl.x
assert 0 <= hl.y - hl.box_h / 2 and hl.y + hl.box_h / 2 <= 1920, hl.y
print(f"\nauto-placement reserves the box: centre ({hl.x}, {hl.y}) keeps the "
      f"{hl.box_w}x{hl.box_h} box on canvas")

# ---------- 10. the editor payload carries the box, and it renders ----------
gen_p = VideoGenerator(RenderConfig(headline_box_w=600, headline_box_h=300),
                       BG, PROMO, None, TMP / "w3", TMP / "o3")
row = pd.Series({"BG_Image": "b.png", "Headline": "Summer Mega Sale",
                 "Subheading": "Free shipping", "Footer": ""})
payload = gen_p.build_editor_payload(row, 1)
by_role = {t["role"]: t for t in payload["texts"]}
assert by_role["Headline"]["box_w"] == 600 and by_role["Headline"]["box_h"] == 300
assert by_role["Subheading"]["box_w"] == 0, "unboxed text must report 0"
print(f"\neditor payload: Headline box {by_role['Headline']['box_w']}x"
      f"{by_role['Headline']['box_h']}, Subheading unboxed")

img = gen_p.render_preview(row, 1)
assert img.size == (1080, 1920)
res = gen_p.render_row(1, row, "fitbox.mp4")
assert res.ok, res.error
print(f"preview and render both succeed ({res.filename})")

assert TEXT_ROLES == ["Headline", "Subheading", "Footer"]
assert TEXT_FIT_MIN_SIZE < TEXT_FIT_MAX_SIZE

print()
print("ALL TEXT FIT BOX CHECKS PASSED")
