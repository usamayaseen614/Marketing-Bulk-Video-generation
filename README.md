# 🎬 Bulk Marketing Video Generator

A Streamlit app that mass-produces vertical **1080x1920 (9:16)** marketing videos for
**Instagram Reels, TikTok, Facebook Reels, and YouTube Shorts** — one MP4 per Excel row.

Each output video is composed of:

1. A **background image** (per row, from a ZIP) filling the whole canvas
2. Your **promo video** (one MP4, reused in every output) scaled into a configurable box —
   aspect ratio preserved, the background shows through any leftover area. The box's
   size and position can be set **per row** via the `Video_*` Excel columns
3. **Headline / Subheading / Footer** text with per-row size, color, and position — plus a
   choice of **bundled fonts**, an optional **background highlight box**, and **artistic
   styles** (outline, drop shadow, neon glow) à la TikTok
4. A **CTA image** (PNG with transparency supported) at a configurable position and size,
   overridable **per row** via the `CTA_*` Excel columns, with a **configurable fade-in**
5. An optional **CTA video** — a fixed sequence of up to 4 clips that always play in order
   (1 → 2 → 3 → 4) in one shared box. Each position is a **pool of sample videos**; one
   sample is **chosen per output video** (pinned by an Excel `CTA_Clip_<n>` cell, otherwise
   at random), with a shared **configurable fade-in** and a **per-clip playback speed**

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

# 3. Download the bundled font library into ./fonts (the .ttf files are also
#    committed, so this is only needed to refresh them)
python fetch_fonts.py

# 4. Run the app
streamlit run app.py
```

The app opens in your browser at `http://localhost:8501`.

### Try it with sample data

```bash
python create_sample_assets.py
```

This creates a `sample_assets/` folder with a demo `data.xlsx`, `backgrounds.zip`,
`promo.mp4`, `cta.png`, and four `cta_video_*.mp4` clips you can upload straight into the app. (Row 2
intentionally references a missing background to demonstrate per-row error handling; other
rows show off custom fonts, background boxes, and the outline/shadow/neon styles.) There is
also `data_auto.xlsx` — just the three text columns, nothing else — to try the fully
automatic mode: random backgrounds, sizes, colors, positions, fonts, and styles.

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
| `CTA_Fade_Start` / `CTA_Fade_Duration` | When the CTA image starts fading in and how long it takes, in **seconds**. **Blank/absent = the sidebar default** | `1.0` / `0.5` |
| `CTA_Video_X` / `CTA_Video_Y` | **Top-left corner** of the shared CTA-video box, per row. **Blank/absent = the sidebar default** | `720` / `1560` |
| `CTA_Video_Width` / `CTA_Video_Height` | Size of the CTA-video box (each clip is cover-filled to it). **Blank/absent = the sidebar default** | `300` / `300` |
| `CTA_Video_Fade_Start` / `CTA_Video_Fade_Duration` | Fade-in timing for the CTA-video sequence, in **seconds**. **Blank/absent = the sidebar default** | `1.0` / `0.8` |
| `CTA_Video_Speed_1` … `CTA_Video_Speed_4` | Playback speed of clip position 1…4 individually (1 = normal, 2 = twice as fast, 0.5 = half). **Blank/absent = `CTA_Video_Speed`, then the sidebar's per-clip default** | `2.0` |
| `CTA_Video_Speed` | Playback speed for **every** clip in the row at once — a shortcut for setting all of `CTA_Video_Speed_<n>`. A specific `CTA_Video_Speed_<n>` cell overrides it. **Blank/absent = the sidebar per-clip defaults** | `1.5` |
| `CTA_Clip_1` … `CTA_Clip_4` | Pin which sample plays in clip position 1…4 for this video, by file name (with or without extension). **Blank/absent = a random sample from that position's pool** | `intro_a.mp4` |
| `Headline` | Headline text (empty = skipped) | `Summer Mega Sale` |
| `Headline_Size` | Font size in px. **Blank/absent = random** within a sensible range per element (headline 56–88, subheading 34–52, footer 24–36) | `72` |
| `Headline_Color` | Hex (`#FFD700`), CSS color name (`yellow`, `blue`, `lightyellow`…), or `rgb(...)`. **Blank/absent = random** vivid palette color, never repeated within one video | `gold` |
| `Headline_X` / `Headline_Y` | **Center point** of the text, in canvas pixels | `540` / `160` |
| `Headline_Font` | Font family — a bundled name like `Impact`, `Heavy`, `Script`, `Marker`, `Elegant`, `Typewriter`, `Retro`, `Urban` (or the full `Impact (Bebas Neue)`), `System default`, or `Custom upload`. **Blank/absent = the sidebar default font** | `Impact` |
| `Headline_BgColor` | Color of a **highlight box** drawn behind the text (same color formats as `_Color`). **Blank/absent = no box** | `#FF2D55` |
| `Headline_Style` | Artistic treatment: `classic`, `outline`, `shadow`, or `neon`. **Blank/absent = the sidebar default style** | `outline` |
| `Subheading`, `Subheading_*` | Same scheme (`_Size`, `_Color`, `_X`, `_Y`, `_Font`, `_BgColor`, `_Style`) | |
| `Footer`, `Footer_*` | Same scheme. The footer is always laid out on **3 balanced lines** (fewer if it has fewer words) | |

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
- **The CTA fades in**: by default it's invisible for the first second of every video, then
  fades to fully visible at 1.5s (the preview shows its final, fully visible state). The
  start and duration are configurable in the sidebar and per row (`CTA_Fade_*`).
- **Fonts**: choose a bundled family in the sidebar or per text (`*_Font`). The library
  ships TikTok-style faces — `Impact`, `Heavy`, `Clean`, `Elegant`, `Script`, `Marker`,
  `Typewriter`, `Bold Script`, `Retro`, `Urban` — plus `System default` and your own
  `Custom upload`. Run `python fetch_fonts.py` once to populate `./fonts`.
- **Background box & artistic styles**: any text can sit on a colored highlight box
  (`*_BgColor`) and use a `*_Style` of `outline` (contrasting border), `shadow` (drop
  shadow), or `neon` (glow) — combine them freely. `classic` is plain text.
- **CTA videos (optional)**: upload one or more clips in the sidebar to layer them alongside
  the CTA image in a single shared box (`CTA_Video_*`). They play **back-to-back as one clip
  in a shuffled order** — re-shuffled for every output video (so video 1 might run clips
  1,3,4,2 and video 2 runs 3,4,1,2), seeded per row so the preview matches and re-runs are
  reproducible. The box and fade-in are shared by all clips, but **each clip slot has its own
  playback speed** (`CTA_Video_Speed_<n>`, or `CTA_Video_Speed` to set them all at once);
  each clip is cover-filled to the box, and their audio is ignored (the promo video supplies
  the soundtrack). Leave the upload empty to skip the element — output is identical to before.
- Output files are named `001_Headline_Text.mp4` (row number + sanitized headline).

## Sidebar settings

| Setting | Purpose |
|---|---|
| Randomize position per video | Each video gets its own random spot per row (avoids the CTA and explicitly positioned texts; auto-placed texts then avoid the video). Seeded per row, so previews and re-runs are reproducible |
| Video X/Y/W/H | Default box the promo video is fitted into (aspect ratio preserved, centered). X/Y are hidden when randomize is on. A row's `Video_X`/`Video_Y`/`Video_Width`/`Video_Height` cells override these per video |
| CTA image X/Y/W/H | Default position (top-left corner) and size of the CTA image. A row's `CTA_X`/`CTA_Y`/`CTA_Width`/`CTA_Height` cells override these per video |
| CTA fade-in start / duration | When the CTA image fades in and for how long (seconds). Overridable per row via `CTA_Fade_Start` / `CTA_Fade_Duration` |
| CTA videos + box + fade + per-clip speed | Optional clips layered with the CTA image; they play back-to-back in a shuffled order in one shared box (`CTA_Video_*`), with a shared fade-in and a separate speed per clip slot (overridable per row via `CTA_Video_Speed_<n>`, or `CTA_Video_Speed` for the whole row). Leave the upload empty to skip the whole element |
| Default font | The font used when a text's `*_Font` cell is blank — a bundled family, the system font, or your uploaded font |
| Default artistic style | The style used when a text's `*_Style` cell is blank — `classic`, `outline`, `shadow`, or `neon` |
| Quality (CRF) | 16 = near-lossless, 28 = small files. 18 is great for social media |
| Encoder speed | x264 preset; `medium` balances speed and file size |
| Parallel renders | Concurrent FFmpeg processes — raise on strong multi-core machines |
| Custom font | Optional TTF/OTF; pick **Custom upload** as the default font (or in a `*_Font` cell) to use it |

## Workflow

1. Upload the four files. The Excel is validated immediately — missing columns are listed.
2. Pick a row number and click **👁️ Preview Row** — an interactive preview opens.
   **Drag** the video box, the CTA image, the CTA video box, or any text to reposition it
   (a dotted line shows when an element is centered on the canvas, and it gently snaps
   there), **resize** anything with its corner handle (texts resize their font size around
   their center), **recolor** texts with the color swatches, and give any text a
   **background box** with the bg swatch (“none” removes it). Texts render with their actual
   font and artistic style; the side panel live-updates the matching Excel values (`Video_X`,
   `Headline_Size`, `Headline_Color`, `Headline_BgColor`, …) and highlights what changed. Click **💾 Save to Excel** to apply the changes to that
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
background), `overlay.png` (transparent layer with the styled texts: each text painted on its
own layer with its font, optional background box, and outline/shadow/neon decoration), and
`cta.png` (the resized CTA image) — then FFmpeg composites everything in a single pass per row:

```
[1:v]scale=W:H:force_original_aspect_ratio=decrease[vid]   # fit promo video in box, no distortion
[0:v][vid]overlay=x='X+(W-w)/2':y='Y+(H-h)/2':shortest=1   # center in box over background
[bgvid][2:v]overlay=0:0[txt]                               # stamp text layer on top
# optional CTA videos (inputs 4..4+N-1) — cover-filled to the box, each sped up/slowed by
# its own clip speed, concatenated in the row's shuffled order, faded in, then over the texts:
[4:v]...,scale=increase,crop=CVW:CVH,setpts=PTS/SPEED0,format=rgba[cv0]; ... ; [cv0][cv1]...concat=n=N:v=1:a=0[cseq]
[cseq]fade=t=in:st=CVS:d=CVD:alpha=1[ctav]
[txt][ctav]overlay=CVX:CVY[txtv]
[3:v]format=rgba,fade=t=in:st=CFS:d=CFD:alpha=1[cta]       # CTA image: configurable alpha fade-in
[txtv][cta]overlay=CTA_X:CTA_Y,format=yuv420p              # place CTA image on top
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
