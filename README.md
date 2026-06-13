# 🎬 Bulk Marketing Video Generator

A Streamlit app that mass-produces vertical **1080x1920 (9:16)** marketing videos for
**Instagram Reels, TikTok, Facebook Reels, and YouTube Shorts** — one MP4 per Excel row.

Each output video is composed of:

1. A **background image** (per row, from a ZIP) filling the whole canvas
2. Your **promo video** (one MP4, reused in every output) scaled into a configurable box —
   aspect ratio preserved, the background shows through any leftover area. The box's
   size and position can be set **per row** via the `Video_*` Excel columns
3. **Headline / Subheading / Footer** text with per-row size, color, and position
4. A **CTA image** (PNG with transparency supported) at a configurable position and size,
   overridable **per row** via the `CTA_*` Excel columns

Output: H.264 MP4, 30 fps, `yuv420p`, AAC audio, `+faststart` — upload-ready for social platforms.

## Setup

Requires **Python 3.10+**. FFmpeg is **not** required to be installed — a static binary is
bundled via the `imageio-ffmpeg` package (a system FFmpeg on PATH is used if present).

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
streamlit run app.py
```

The app opens in your browser at `http://localhost:8501`.

### Try it with sample data

```bash
python create_sample_assets.py
```

This creates a `sample_assets/` folder with a demo `data.xlsx`, `backgrounds.zip`,
`promo.mp4`, and `cta.png` you can upload straight into the app. (Row 2 intentionally
references a missing background to demonstrate per-row error handling.) There is also
`data_auto.xlsx` — just the three text columns, nothing else — to try the fully
automatic mode: random backgrounds, sizes, colors, and positions.

## Excel format

One row = one video. **Every column is optional** — any column may be left blank
*or omitted from the sheet entirely* (extra columns are ignored). The number of
rows alone determines how many videos are generated:

| Column | Meaning | Example |
|---|---|---|
| `BG_Image` | Background filename inside the ZIP (case-insensitive, subfolders OK). **Blank/absent = randomly assigned** from the ZIP — no image repeats until all have been used | `summer_bg.jpg` |
| `Video_X` / `Video_Y` | **Top-left corner** of the box the promo video is placed into, per row. **Blank/absent = the sidebar default** (or a random per-row spot when *Randomize position per video* is on) | `90` / `300` |
| `Video_Width` / `Video_Height` | Size of the video box, per row — the video is scaled to fit inside it, aspect ratio preserved. **Blank/absent = the sidebar default** | `900` / `900` |
| `CTA_X` / `CTA_Y` | **Top-left corner** of the CTA image, per row. **Blank/absent = the sidebar default** | `340` / `1600` |
| `CTA_Width` / `CTA_Height` | Size the CTA image is resized to, per row. **Blank/absent = the sidebar default** | `400` / `160` |
| `Headline` | Headline text (empty = skipped) | `Summer Mega Sale` |
| `Headline_Size` | Font size in px. **Blank/absent = random** within a sensible range per element (headline 56–88, subheading 34–52, footer 24–36) | `72` |
| `Headline_Color` | Hex (`#FFD700`), CSS color name (`yellow`, `blue`, `lightyellow`…), or `rgb(...)`. **Blank/absent = random** vivid palette color, never repeated within one video | `gold` |
| `Headline_X` / `Headline_Y` | **Center point** of the text, in canvas pixels | `540` / `160` |
| `Subheading`, `Subheading_Size`, `Subheading_Color`, `Subheading_X`, `Subheading_Y` | Same scheme | |
| `Footer`, `Footer_Size`, `Footer_Color`, `Footer_X`, `Footer_Y` | Same scheme. The footer is always laid out on **3 balanced lines** (fewer if it has fewer words) | |

Notes:

- The canvas is **1080 wide x 1920 tall**; `X=540` horizontally centers any text.
- Text coordinates are the **center** of the text block (easiest for marketers to reason
  about); `Video_*` and `CTA_*` coordinates are the **top-left corner** of their box,
  matching the sidebar values.
- `Video_*` and `CTA_*` cells override the sidebar per row — even with *Randomize position
  per video* on, a filled `Video_X`/`Video_Y` pins that axis (fill one to randomize only
  the other). Auto-placed texts avoid the row's actual video and CTA boxes.
- Positions may go **off-canvas** (negative, or past the edges) — anything outside the
  1080x1920 frame is simply clipped, handy for bleed effects like a half-visible video.
  The preview editor lets you drag elements out too; a small sliver always stays on-canvas
  so you can grab them back.
- **Any text may be left empty** — that element is simply skipped for that video.
- **Blank X/Y cells trigger auto-placement**: the text gets a random position that
  avoids the video box, the CTA, and the other texts. Placement is seeded per row,
  so the preview matches the final render and re-runs reproduce the same layout.
  You can also blank just one axis (e.g. fix Y, let X be chosen).
- Blank sizes and colors are randomized (per the ranges/palette above); invalid values
  produce a warning and a random fallback. All randomness is seeded per row, so the
  preview matches the final render and re-runs reproduce identical videos.
- **Long texts never run off the canvas**: headlines and subheadings automatically wrap
  onto extra lines when they would exceed the canvas width, and a single over-long word
  shrinks the font until it fits. The auto-placer reserves space for the wrapped block.
- **Not sure which numbers to use?** Preview a row and drag things around — the preview
  editor shows the exact column values and can save them back to the sheet for you.
- **The CTA fades in**: it's invisible for the first second of every video, then fades
  to fully visible at 1.5s (the preview shows its final, fully visible state).
- Output files are named `001_Headline_Text.mp4` (row number + sanitized headline).

## Sidebar settings

| Setting | Purpose |
|---|---|
| Randomize position per video | Each video gets its own random spot per row (avoids the CTA and explicitly positioned texts; auto-placed texts then avoid the video). Seeded per row, so previews and re-runs are reproducible |
| Video X/Y/W/H | Default box the promo video is fitted into (aspect ratio preserved, centered). X/Y are hidden when randomize is on. A row's `Video_X`/`Video_Y`/`Video_Width`/`Video_Height` cells override these per video |
| CTA X/Y/W/H | Default position (top-left corner) and size of the CTA image. A row's `CTA_X`/`CTA_Y`/`CTA_Width`/`CTA_Height` cells override these per video |
| Quality (CRF) | 16 = near-lossless, 28 = small files. 18 is great for social media |
| Encoder speed | x264 preset; `medium` balances speed and file size |
| Parallel renders | Concurrent FFmpeg processes — raise on strong multi-core machines |
| Custom font | Optional TTF/OTF used for all text (defaults to Arial / system font) |

## Workflow

1. Upload the four files. The Excel is validated immediately — missing columns are listed.
2. Pick a row number and click **👁️ Preview Row** — an interactive preview opens.
   **Drag** the video box, the CTA, or any text to reposition it (a dotted line shows
   when an element is centered on the canvas, and it gently snaps there), **resize**
   anything with its corner handle (texts resize their font size around their center),
   and **recolor** texts with the color swatches. The side panel live-updates the
   matching Excel values (`Video_X`, `Headline_Size`, `Headline_Color`, …) and
   highlights what changed. Click **💾 Save to Excel** to apply the changes to that
   row in one go — they're used by subsequent previews and generation, and
   **⬇️ Download updated Excel** gives you the sheet with the edits written in
   (formatting preserved) so your file stays the source of truth. A copy button is
   still there if you prefer pasting values by hand. (Text resizing scales the preview
   proportionally; a long text may re-wrap slightly in the final render at the new
   size.)
3. Click **🚀 Generate All Videos** — a progress bar shows live success/failure counts.
4. Failed rows are listed with their error (e.g., a background missing from the ZIP);
   one bad row never stops the batch.
5. Download the ZIP — it contains every rendered MP4 plus `render_log.txt`.

Temporary working files are cleaned automatically after every run. Rendered output is kept
in your system temp folder (path shown under the download button) until the next run.

## Deployment

For team use, deploy on a single GCP Compute Engine VM (Docker image included) —
see [DEPLOYMENT.md](DEPLOYMENT.md) for copy-paste instructions, secure access
options (IAP tunnel or HTTPS + basic auth via Caddy), and cost controls.

## How rendering works (for developers)

`video_generator.py` pre-renders the static layers with Pillow — `base.png` (cover-cropped
background), `overlay.png` (transparent layer with texts), and `cta.png` (the resized CTA) —
then FFmpeg composites everything in a single pass per row:

```
[1:v]scale=W:H:force_original_aspect_ratio=decrease[vid]   # fit video in box, no distortion
[0:v][vid]overlay=x='X+(W-w)/2':y='Y+(H-h)/2':shortest=1   # center in box over background
[bgvid][2:v]overlay=0:0[txt]                               # stamp text layer on top
[3:v]format=rgba,fade=t=in:st=1:d=0.5:alpha=1[cta]         # CTA: alpha fade-in 1.0s -> 1.5s
[txt][cta]overlay=CTA_X:CTA_Y,format=yuv420p               # place CTA above everything
```

This is much faster than FFmpeg `drawtext` (text is rasterized once per row, not per frame)
and sidesteps Windows font-path escaping. See the docstrings in
[video_generator.py](video_generator.py) for the full explanation.

## Troubleshooting

- **"Background image 'x' not found in the ZIP"** — a filled `BG_Image` cell must match a
  file name inside the ZIP (matching is case-insensitive and ignores folder paths). Leave
  the cell blank to have a background assigned automatically.
- **Text looks wrong / boxes instead of letters** — upload a TTF font in the sidebar that
  supports your language's characters.
- **Renders are slow** — increase *Parallel renders*, choose a faster *Encoder speed*, or
  raise CRF a little (e.g. 21).
- **Audio missing** — the output simply has no audio track if the uploaded MP4 has none.
