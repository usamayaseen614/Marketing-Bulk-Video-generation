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
   styles** (outline, drop shadow, neon glow) à la TikTok. Each text can optionally be given
   a **fixed fit box**: the text then re-wraps to the box's width and its font size is chosen
   so the whole block fills the box — line breaks added *and removed*, size grown *and*
   shrunk — so headlines of wildly different lengths come out optically consistent.
   Upload several promo videos and each can be given **its own wording** for the same row
   via an optional per-promo text sheet — same design, different words (see
   *Different text per promo video*)
4. An **optional CTA image** (PNG with transparency supported) at a configurable position and
   size, overridable **per row** via the `CTA_*` Excel columns, with a **configurable fade-in**.
   Leave the upload empty to skip the CTA-image layer entirely
5. An optional **CTA video** — a fixed sequence of up to 5 clips that always play in order
   (1 → 2 → 3 → 4 → 5) in one shared box. Each position is a **pool of sample videos**; one
   sample is **chosen per output video** (pinned by an Excel `CTA_Clip_<n>` cell, otherwise
   at random), with a shared **configurable fade-in** and a **per-clip playback speed**
6. An optional **GIF layer** — a **flat pool** of short looping clips (supplied as MP4) that
   play one after another in their own box. Each gif holds the box for at least a
   **dwell time** (5s by default), repeating **itself** a whole number of times to get
   there — a 3-second gif plays twice, for 6 seconds; it is never cut short. The sequence
   keeps drawing fresh gifs until the promo video ends. Gifs are **contain-fitted**: scaled
   to the largest size that sits inside the box — **enlarged** when smaller than it, shrunk
   when bigger — never cropped or stretched, with whatever is behind showing through the
   space the aspect ratio leaves over
7. An optional **background-video layer** — a **flat pool** of videos (uploaded or from a
   Drive folder) that play **one after another** for the length of each video, exactly like
   the GIF layer: each holds the frame for at least a **dwell time** (10s by default),
   repeating itself if it is shorter, and clips are dealt from a shuffled deck so every one
   is used before any repeats. Drawn **translucent** (opacity slider, 8% by default) in a
   box that defaults to the **full canvas** and is overridable per row via the `BG_Video_*`
   columns. It always sits **directly beneath the texts** and over every other layer at or
   below the texts' z-number — there is no z knob for it

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
three promo videos (`promo.mp4`, `promo_2.mp4`, `promo_3.mp4`), `cta.png`, and five
`cta_video_*.mp4` clips you can upload straight into the app. (Row 2
intentionally references a missing background to demonstrate per-row error handling; other
rows show off custom fonts, background boxes, and the outline/shadow/neon styles.) There is
also `data_auto.xlsx` — just the three text columns, nothing else — to try the fully
automatic mode: random backgrounds, sizes, colors, positions, fonts, and styles.

> ⚠️ It **overwrites** `data.xlsx`, `data_auto.xlsx` and `backgrounds.zip` in that folder.
> Move your own sheet somewhere else first if one is sitting there.

To try **per-promo text**, it also writes `headline_by_promo.xlsx`,
`subheading_by_promo.xlsx` and `footer_by_promo.xlsx` — one column per promo video above,
five rows to line up with the five-row demo sheet (`data.xlsx`, or the `sample_5_videos.xlsx`
already in the folder). Upload all three promo videos, the main sheet, and these three
sheets, then hit **🔍 Check promo names against these sheets**: every column resolves, and
the same row comes out reading *Summer Mega Sale* / *Biggest Summer Blowout* /
*Summer Clearance Is Live* depending on which promo it landed on. Row 4 of `promo.mp4` in the
Headline sheet is blank on purpose, to show a cell falling back to the main Excel. If your own
sheet has a different number of rows, use the **template** buttons in the app instead — they
size themselves to your sheet and your promo filenames.

## Excel format

One row = one video. **Every column is optional** — any column may be left blank
*or omitted from the sheet entirely* (extra columns are ignored). The number of
rows alone determines how many videos are generated:

| Column | Meaning | Example |
|---|---|---|
| `BG_Image` | Background filename inside the ZIP (case-insensitive, subfolders OK). **Blank/absent = randomly assigned** from the ZIP — no image repeats until all have been used. The ZIP itself is **optional**: without one, rows render on the sidebar's solid background color | `summer_bg.jpg` |
| `Video_X` / `Video_Y` | **Top-left corner** of the box the promo video is placed into, per row. **Blank/absent = the sidebar default** (or a random per-row spot when *Randomize position per video* is on) | `90` / `300` |
| `Video_Width` / `Video_Height` | Size of the video box, per row — the video is scaled to fit inside it, aspect ratio preserved. **Blank/absent = the sidebar default** | `900` / `900` |
| `CTA_X` / `CTA_Y` | **Top-left corner** of the CTA image, per row. **Blank/absent = the sidebar default** | `340` / `1600` |
| `CTA_Width` / `CTA_Height` | Size the CTA image is resized to, per row. **Blank/absent = the sidebar default** | `400` / `160` |
| `CTA_Fade_Start` / `CTA_Fade_Duration` | When the CTA image starts fading in and how long it takes, in **seconds**. **Blank/absent = the sidebar default** | `1.0` / `0.5` |
| `CTA_Video_X` / `CTA_Video_Y` | **Top-left corner** of the shared CTA-video box, per row. **Blank/absent = the sidebar default** | `720` / `1560` |
| `CTA_Video_Width` / `CTA_Video_Height` | Size of the CTA-video box (each clip is cover-filled to it). **Blank/absent = the sidebar default** | `300` / `300` |
| `CTA_Video_Fade_Start` / `CTA_Video_Fade_Duration` | Fade-in timing for the CTA-video sequence, in **seconds**. **Blank/absent = the sidebar default. Ignored in split-screen mode**, where the panel is always visible from the first frame | `1.0` / `0.8` |
| `CTA_Video_Speed_1` … `CTA_Video_Speed_10` | Playback speed of clip position 1…N individually (1 = normal, 2 = twice as fast, 0.5 = half). Columns exist up to 10; the sidebar's *Number of clip slots* sets how many are active. **Blank/absent = `CTA_Video_Speed`, then the sidebar's per-clip default** | `2.0` |
| `CTA_Video_Speed` | Playback speed for **every** clip in the row at once — a shortcut for setting all of `CTA_Video_Speed_<n>`. A specific `CTA_Video_Speed_<n>` cell overrides it. Also the speed used by fill clips (see *Keep clips playing to fill the whole video*). **Blank/absent = normal / the sidebar per-clip defaults** | `1.5` |
| `CTA_Clip_1` … `CTA_Clip_10` | Pin which sample plays in clip position 1…N for this video, by file name (with or without extension). **Blank/absent = a random sample from that position's pool** | `intro_a.mp4` |
| `GIF_X` / `GIF_Y` | **Top-left corner** of the gif box, per row. Rounded down to an even pixel so the chroma planes stay aligned. **Blank/absent = the sidebar default** | `60` / `560` |
| `GIF_Width` / `GIF_Height` | Size of the gif box. Each gif is **contain-fitted** into it — scaled up or down until one side touches the edge, never cropped and never stretched, so the box sets the gif's size whatever the source resolution. **Blank/absent = the sidebar default** | `360` / `360` |
| `GIF_Fade_Start` / `GIF_Fade_Duration` | Fade-in timing for the gif layer, in **seconds**. Applies to the first gif only. **Blank/absent = the sidebar default (0/0 = visible immediately)** | `0.5` / `0.5` |
| `BG_Video_X` / `BG_Video_Y` | **Top-left corner** of the translucent background-video box, per row. **Blank/absent = the sidebar default (0/0)** | `0` / `0` |
| `BG_Video_Width` / `BG_Video_Height` | Size of the background-video box (every clip in the row's sequence is cover-filled to it — scaled and center-cropped, never stretched). **Blank/absent = the sidebar default (the full 1080 × 1920 canvas)** | `1080` / `1920` |
| `Headline` | Headline text (empty = skipped) | `Summer Mega Sale` |
| `Headline_Width` / `Headline_Height` | Optional **fit box**, in canvas pixels, **centred on `Headline_X`/`Headline_Y`**. Set both and the box drives the type: the text re-wraps to the width and the font size is chosen so the painted block (glyphs *plus* any outline/shadow/glow) fills the box. `Headline_Size` is then ignored — the box computes it. **Blank/absent, or either one alone = the sidebar default, else the classic behaviour** (`Headline_Size`, wrapped to the canvas). Same columns exist for Subheading and Footer | `800` / `300` |
| `Headline_Size` | Font size in px. **Blank/absent = random** within a sensible range per element (headline 56–88, subheading 34–52, footer 24–36) | `72` |
| `Headline_Color` | Hex (`#FFD700`), CSS color name (`yellow`, `blue`, `lightyellow`…), `rgb(...)`, or an alpha hex (`#FFFFFF80` = half-transparent white). **Blank/absent = random** vivid palette color, never repeated within one video | `gold` |
| `Headline_Opacity` | How solid the text is: `0`–`100` (a `%` is allowed), or a `0`–`1` fraction — `65`, `65%` and `0.65` all mean 65% opaque. The outline, glow and shadow fade with it. **Blank/absent = the sidebar's *Text opacity*** | `65%` |
| `Headline_X` / `Headline_Y` | **Center point** of the text, in canvas pixels | `540` / `160` |
| `Headline_Font` | Font family — a bundled name like `Impact`, `Heavy`, `Script`, `Marker`, `Elegant`, `Typewriter`, `Retro`, `Urban` (or the full `Impact (Bebas Neue)`), `System default`, or `Custom upload`. **Blank/absent = the sidebar default font** | `Impact` |
| `Headline_BgColor` | Color of a **highlight box** drawn behind the text (same color formats as `_Color`). **Blank/absent = no box** | `#FF2D55` |
| `Headline_BgOpacity` | Opacity of that highlight box, same formats as `_Opacity`. A translucent box under solid text is the classic caption look — the box tints the video instead of hiding it. **Blank/absent = the sidebar's *Highlight box opacity*** | `55%` |
| `Headline_Style` | Artistic treatment: `classic`, `outline`, `shadow`, or `neon`. **Blank/absent = the sidebar default style** | `outline` |
| `Headline_Subliminal` / `Subheading_Subliminal` / `Footer_Subliminal` | Turn the **experimental subliminal / persistence-of-vision effect** on or off for that one text (`yes`/`no`), overriding the sidebar's *Apply to* choice for this row. **Blank/absent = on only if the sidebar targets that role.** See *Subliminal text* below | `yes` |
| `Subheading`, `Subheading_*` | Same scheme (`_Size`, `_Color`, `_Opacity`, `_X`, `_Y`, `_Font`, `_BgColor`, `_BgOpacity`, `_Style`, `_Subliminal`) | |
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
- **Manual line breaks**: put a `|` in any text (`Summer Mega | Sale Week`) — or press
  Alt+Enter inside the Excel cell — to break the line exactly there. Works for all three
  texts; a footer with manual breaks skips its automatic 3-line balancing. A manual line
  that is still too wide for the canvas wraps further automatically.
- **Long texts never run off the canvas**: without manual breaks, headlines and subheadings
  automatically wrap onto extra lines when they would exceed the canvas width, and a single
  over-long word shrinks the font until it fits. The auto-placer reserves space for the
  wrapped block.
- **Not sure which numbers to use?** Preview a row and drag things around — the preview
  editor shows the exact column values and can save them back to the sheet for you.
- **The CTA image is optional**: skip the upload to leave the CTA-image layer off entirely
  (no reserved space, no fade). When supplied, it's invisible for the first second of every
  video by default, then fades to fully visible at 1.5s (the preview shows its final, fully
  visible state). The start and duration are configurable in the sidebar and per row
  (`CTA_Fade_*`).
- **Fonts**: choose a bundled family in the sidebar or per text (`*_Font`). The library
  ships TikTok-style faces — `Impact`, `Heavy`, `Clean`, `Elegant`, `Script`, `Marker`,
  `Typewriter`, `Bold Script`, `Retro`, `Urban` — plus `System default` and your own
  `Custom upload`. Run `python fetch_fonts.py` once to populate `./fonts`.
- **Background box & artistic styles**: any text can sit on a colored highlight box
  (`*_BgColor`) and use a `*_Style` of `outline` (contrasting border), `shadow` (drop
  shadow), or `neon` (glow) — combine them freely. `classic` is plain text.
- **Translucent text & boxes**: set `*_Opacity` / `*_BgOpacity` (or the two sidebar
  sliders, or an alpha color like `#FFFFFF80`) to let the video show through. The text
  and its box are independent, so the two staple looks both work: a translucent tint box
  under solid text, and ghosted watermark-style lettering over the footage. An alpha color
  cell and an opacity cell multiply, so `#FFFFFF80` at `50%` lands on 25%. Opacity applies
  to everything the text draws — fill, outline ring, glow and drop shadow fade together —
  and translucent text **veils** whatever is under it rather than cutting a hole in it.
- **CTA videos (optional)**: upload one or more clips in the sidebar to layer them alongside
  the CTA image in a single shared box (`CTA_Video_*`). They play **back-to-back as one clip
  in a shuffled order** — re-shuffled for every output video (so video 1 might run clips
  1,3,4,2 and video 2 runs 3,4,1,2), seeded per row so the preview matches and re-runs are
  reproducible. The box and fade-in are shared by all clips, but **each clip slot has its own
  playback speed** (`CTA_Video_Speed_<n>`, or `CTA_Video_Speed` to set them all at once);
  each clip is cover-filled to the box, and their audio is ignored (the promo video supplies
  the soundtrack). Leave the upload empty to skip the element — output is identical to before.
- **Split-screen layout (sidebar)**: switch *Layout mode* to **Split-screen** to place the
  promo video in one half of the canvas and the CTA-video sequence in the other, as a centered
  band (height set by *Panel height*). *Swap left / right* flips which side is which. In this
  mode the side keeps drawing fresh random clips until the promo ends (never freezing), the
  whole video ends when the promo ends, and per-row `Video_*` / `CTA_Video_*` positions and
  *Randomize position* are ignored (a warning notes any that were set). The promo is fitted
  inside its panel (letterboxed against the background); side clips are cropped to fill theirs.
  **The side panel never fades** in split-screen — it's part of the layout, so it is fully
  visible from the first frame through to the end, and the CTA-video fade settings are ignored.
  *No background — output only the panels* goes further: the finished video is **exactly the two
  panels** (1080 × panel height) with no background at all. Texts and the CTA are drawn **on top
  of the videos**; auto-placed texts are confined to the band, and anything explicitly placed
  outside it triggers a warning (it would be cropped out).
- **Fill the whole video (sidebar / split mode)**: *Keep clips playing to fill the whole video*
  keeps appending random clips from the pools after the fixed slots until the side covers the
  full promo length, so it never holds a last frame. Off = play once, then hold. Split-screen
  turns it on automatically.
- **Subliminal text (experimental)**: pick **one** text in the sidebar's *Apply to* box
  (Headline, Subheading, or Footer — default **Off**) and it renders so **no single frame shows
  the whole thing** — each frame omits ~1/K of the words and the set cycles every K frames
  (K/30 s), so it reads as whole in motion but is only partial when scrubbed frame-by-frame.
  It is a CTA treatment, so it deliberately **never applies to every text at once**; override
  it per text (and per row) with a `<Role>_Subliminal` cell.
  *Effect style* picks how each frame is built. **Hide a slice** (default) draws the whole text
  minus part of the words, so most of it is lit each frame and it stays bright and solid.
  **Show only a slice** draws *only* ~1/K of the words, so every word is lit just 1/K of the
  time and time-averages to roughly **1/K brightness** — faint and ghostly. Raising the frame
  rate shortens the cycle but does **not** change that brightness ratio, because the eye
  averages light over time; it is a duty-cycle limit, not a frame-rate one.
  For **Hide a slice**, *Hidden characters* chooses how the hidden set is picked each frame:
  **Random each cycle** (default) hides a different, evenly-spread subset every frame — balanced
  so no character stays hidden, seeded so the preview still matches the render, and never
  repeating the same comb; **Fixed pattern** hides the same characters in the same frames every
  cycle. In random mode *Hidden per frame (%)* sets how much is hidden: ~33% (≈100/K) keeps
  every character hidden **exactly once per cycle** and stays bright, while higher percentages
  hide more per frame and look progressively fainter. Every setting guarantees no frame is ever
  complete and the whole text reassembles over the cycle — the hidden amount is automatically
  capped so **no word can ever be hidden in every frame**. (Without that cap a high percentage
  against a small K — e.g. 70% at K=3, or 65% at K=2 — leaves some words hidden in *every* frame,
  so they never appear in the video at all.) Raise K if you want to hide more per frame.
  Rendering at **60 fps** (Output → Frame rate) halves the cycle to K/60 s, which blends
  noticeably more smoothly.
  A **highlight box** (`*_BgColor`) works normally with the effect: the box is painted once as
  an always-on layer and the cycling glyphs ride on top of it, so it stays solid and unnotched
  while the letters flicker (only the *letters* are ever hidden, never the box).
  A **hardcoded** rule always applies to whichever text has the effect: the **last 4
  characters** follow their own schedule, independent of the body — they alternate every frame,
  the **1st & 4th together, then the 2nd & 3rd**, so no more than 2 of them are ever visible at
  once. This keeps the tail of the text — e.g. a code or domain — from ever appearing whole. Use
  an **even** Frames-per-cycle (K) for a perfectly regular alternation. **Caveats:** the
  "never whole" guarantee holds on *this* output file (with *Preserve frame-by-frame* on, which
  encodes every frame independently and makes files much larger), but **platform re-encoding
  (TikTok/IG/YouTube) can break it**; the effect reads as a shimmer, not crisp text; and rapid
  flashing can affect photosensitive viewers and may conflict with platform policy. Off by
  default — use deliberately.
- **Text fit boxes.** Setting a text's `*_Width` and `*_Height` turns the box into the
  instruction and the type into the output: the text is re-wrapped to the box's width and
  the font size is searched for the largest value whose block still fits. Three things
  follow from that and are worth knowing:
  - The `*_Size` cell is **ignored** for a boxed text — the box computes the size, growing
    it as readily as shrinking it. "Summer Mega Sale" lands at 58px in a 300×150 box and
    180px in a 900×400 one, both on two lines.
  - The **artistic style is part of the fit**, because its padding scales with the font
    size. The same text in the same 300×150 box comes out at 58px in `classic` but 45px in
    `neon` — the glow is half the font size on every side, and it has to stay inside the box
    you drew rather than spill past it.
  - Text that cannot fit even at **20px** is drawn at 20px, allowed to overflow, and the row
    is **warned**. Clipping mid-word would read as a rendering fault, and shrinking without a
    floor produces text nobody can read and nothing to say why.

  Leave either dimension blank and that text behaves exactly as it always has.
- Output files are named `001_Headline_Text.mp4` (row number + sanitized headline).

### Different text per promo video

A row's `Headline` is one cell, so every promo video rendered from that row says the
same thing. Upload a **per-promo text sheet** to change that: 5 rows × 3 promos becomes
15 videos with 15 different headlines instead of 5 repeated three times.

Under *1. Upload assets* → **Per-promo heading, subheading and footer text (optional)**
there are three separate uploaders, one each for Headline, Subheading and Footer. Each
takes a workbook in this shape — **column headers are your promo video filenames**, rows
line up with the rows of the main Excel:

| video 1.mp4 | video 2.mp4 | video 3.mp4 |
|---|---|---|
| pov | omsdhajkl | askfjnan |
| awsdlan | salfjnal | sakfjn |

So the video made from **row 2** on **video 3.mp4** reads `sakfjn`.

- **Only the words change.** Size, font, colour, opacity, position, background box, fit
  box and every other `<Role>_*` column still come from the main Excel and the sidebar —
  one design, different text. That is the whole point of keeping them in separate sheets.
  (Cells you leave *blank* in the main Excel are randomized per video as they always have
  been — a blank `Headline_Color` still draws a different colour on each promo, because
  every render pass reseeds. Set the value in the main Excel to pin it across all of them.)
  Backgrounds are unaffected: a row keeps the same background image on every promo.
- **Upload only what you need.** A Headline sheet on its own leaves Subheading and Footer
  coming from the main Excel as before.
- **Name matching is forgiving**: case, spaces, separators and the `.mp4` extension are
  all ignored, so `video 1`, `Video_1` and `video-1.mp4` all name `video 1.mp4`.
- **🔍 Check promo names against these sheets** shows a table of which column feeds which
  promo before you commit to a render. A column matching no uploaded promo, two columns
  naming the same promo, or a row count that differs from the main Excel are **errors and
  refuse the batch** — the alternative is thousands of videos carrying the wrong words and
  looking perfectly successful. A promo with *no* column is only a warning: it falls back
  to the main Excel.
- **A blank cell falls back** to the main Excel's text for that row, so you can override
  just the ones you care about.
- **Download a template** (buttons under the uploaders) to get a workbook already headed
  with your uploaded promo filenames and the right number of rows. For a filled-in example
  to look at, run `python create_sample_assets.py` — see *Try it with sample data*.
- Previews and **🎬 Render Row** show the text for the promo picked in *Promo video*, so
  what you see is the pairing the batch will actually produce. `render_manifest.xlsx`
  gains a column per overridden role recording what each finished video actually said.

## Sidebar settings

| Setting | Purpose |
|---|---|
| Layout mode | **Free** = every box placed by its own coordinates. **Split-screen** = promo video on one half, CTA-video sequence on the other (with *Swap left / right* and *Panel height*), ending when the promo ends |
| Background color | Solid canvas color used wherever a row has no background image — e.g. when no background ZIP is uploaded (the ZIP is optional) |
| No background — output only the panels | Split-screen only: crop the output to exactly the two panels (1080 × panel height). No background at all; texts/CTA overlay the videos |
| Number of clip slots | How many CTA-video clip positions play in fixed order (1–10). Each is a pool; one sample is picked per output video |
| Keep clips playing to fill the whole video | After the fixed slots, keep drawing fresh random clips until the side covers the full promo length (never freezes). Split-screen turns this on automatically |
| Subliminal text (experimental) | *Apply to* picks **up to two** texts (Headline / Subheading / Footer, default off) that get split across frames so no single frame shows all of them — never all three at once; override per text via `<Role>_Subliminal`. *Effect style* = **Hide a slice** (bright, recommended) or **Show only a slice** (~1/K brightness, faint). For Hide: *Hidden characters* = **Random each cycle** (balanced, non-repeating; default) or **Fixed pattern**, and *Hidden per frame (%)* sets how much is hidden (≈33% stays bright and hides each character exactly once per cycle; higher is fainter). Tunable K (frames/cycle), word/char granularity, and *Preserve frame-by-frame* (all-intra, larger files). Certain known texts carry a **hand-authored schedule** (see `CUSTOM_SUBLIMINAL_SCHEDULES` in `video_generator.py`) that replaces all of these settings and the last-4 rule. See the caveats above |
| Frame rate (fps) | 30 or 60. 60 doubles the frames to encode but halves the subliminal cycle (K/fps s), so the effect blends more smoothly. Both upload fine to every major platform |
| Randomize position per video | Each video gets its own random spot per row (avoids the CTA and explicitly positioned texts; auto-placed texts then avoid the video). Seeded per row, so previews and re-runs are reproducible. Ignored in split-screen mode |
| Video X/Y/W/H | Default box the promo video is fitted into (aspect ratio preserved, centered). X/Y are hidden when randomize is on. A row's `Video_X`/`Video_Y`/`Video_Width`/`Video_Height` cells override these per video |
| CTA image X/Y/W/H | Default position (top-left corner) and size of the CTA image. A row's `CTA_X`/`CTA_Y`/`CTA_Width`/`CTA_Height` cells override these per video |
| CTA fade-in start / duration | When the CTA image fades in and for how long (seconds). Overridable per row via `CTA_Fade_Start` / `CTA_Fade_Duration` |
| CTA videos + box + fade + per-clip speed | Optional clips layered with the CTA image; they play back-to-back in a shuffled order in one shared box (`CTA_Video_*`), with a shared fade-in and a separate speed per clip slot (overridable per row via `CTA_Video_Speed_<n>`, or `CTA_Video_Speed` for the whole row). Leave the upload empty to skip the whole element |
| GIF clips (MP4) | The gif pool. **Flat, not slots** — a random selection plays in each output video, dealt from a shuffled deck so every gif is used once before any repeats. Leave empty to skip the layer entirely |
| Minimum seconds per gif | The dwell floor (default 5). A gif shorter than this repeats **itself** a whole number of times until it clears the floor — a 3s gif plays twice (6s). A gif already longer plays once, in full. Sidebar-only: it is batch-wide pacing, so there is no per-row column |
| GIF box X/Y/W/H | The box gifs are fitted into. It is an **invisible fit guide**, not a visible panel — nothing is drawn for it. Overridable per row via `GIF_X`/`GIF_Y`/`GIF_Width`/`GIF_Height` |
| GIF fade-in start / duration | When the gif layer fades in and for how long. Defaults to 0/0 (visible from the first frame), which is also exactly what the static preview shows. Applies to the first gif only — the rest of the sequence cuts straight in. Overridable per row via `GIF_Fade_Start` / `GIF_Fade_Duration` |
| Background videos (MP4) | The translucent overlay pool. **Flat, like the gifs** — clips play back-to-back for the length of each video, dealt from a shuffled deck (seeded per row, so previews and re-runs match). Leave empty to skip the layer. Can come from a Drive folder instead — see *Where the background videos come from* |
| Minimum seconds per background video | The dwell floor (default 10, longer than the GIF floor). A clip shorter than this repeats **itself** a whole number of times to clear it; one already longer plays once, in full. Sidebar-only, like the GIF floor — it is batch-wide pacing |
| Background video opacity | How visible the pool's clips are (1–50%, default 8). Keep it low so texts stay legible; 0 would disable the layer, so the slider floors at 1 |
| Background video box | The box the row's clip is **cover-filled** into (scaled + center-cropped). Defaults to the **full canvas**; overridable per row via `BG_Video_X`/`BG_Video_Y`/`BG_Video_Width`/`BG_Video_Height` |
| Layer order (z-index) | Which layer sits on top: promo video (1), **GIFs (2)**, CTA video (3), CTA image (4), texts (5). Higher = nearer the front; the background is always at the back. Raise the gif number above the promo video's to float a gif over the video instead of behind it. The **background videos have no number**: they always sit directly beneath the texts, over any layer numbered at or below the texts — a layer raised *above* the texts also rises above the veil |
| Default font | The font used when a text's `*_Font` cell is blank — a bundled family, the system font, or your uploaded font |
| Default artistic style | The style used when a text's `*_Style` cell is blank — `classic`, `outline`, `shadow`, or `neon` |
| Text fit boxes (per role) | Optional fixed box for the Headline / Subheading / Footer, **0 = off**. With one set, the text re-wraps to the box width and its font size is picked so the block fills the box — line breaks are added *and removed*, and the size **grows as well as shrinks**. The box is centred on the text's X/Y, so switching it on never moves anything. Per-row overrides: `Headline_Width`/`Headline_Height` and the same for the other two |
| Text opacity / Highlight box opacity | Batch defaults for how solid the texts and their `*_BgColor` boxes are (0–100%). Below 100 the video shows through. Overridable per text via `*_Opacity` / `*_BgOpacity`, and per-text sliders in the preview editor |
| Quality (CRF) | 16 = near-lossless, 28 = small files. 18 is great for social media |
| Encoder speed | x264 preset; `medium` balances speed and file size |
| Parallel renders | Concurrent FFmpeg processes (up to min(16, CPU cores)). One render only keeps ~8–10 threads busy, so on a many-core VM set ~1 per 3 cores (e.g. 10 on 32 cores) to saturate the CPU — roughly a 3–5× throughput jump over the old cap of 4 |
| Custom font | Optional TTF/OTF; pick **Custom upload** as the default font (or in a `*_Font` cell) to use it |

## Workflow

1. Upload the Excel sheet and a promo video (background ZIP and CTA image are optional —
   without backgrounds, videos render on the sidebar's background color).
   The Excel is validated immediately — missing columns are listed.
   Optionally open **Per-promo heading, subheading and footer text** and upload a sheet
   per role to give each promo video its own wording, then click **🔍 Check promo names
   against these sheets** to confirm every column lines up with an uploaded promo
   (see *Different text per promo video*).
2. Pick a row number and click **👁️ Preview Row** — an interactive preview opens.
   **Drag** the video box, the CTA image, the CTA video box, the GIF box, the background-video
   box, or any text to reposition it. The background-video box starts full-canvas, where it
   deliberately lets clicks pass through to the boxes beneath (moving a full-canvas box is a
   no-op) — grab its corner handle at the canvas's bottom-right to shrink it first, and it
   becomes draggable like the rest. It shows the row's chosen clip at the render opacity. The GIF box keeps a permanent dashed outline (every other element only
   outlines on hover) because a contain-fitted gif fills the box on one axis only — unless
   its aspect ratio matches the box exactly, the rest stays empty and see-through, so along
   those sides there is nothing to grab. Resizing the box resizes the gif with it, up as
   well as down.
   The box shows the **first** gif of the sequence and cannot show the rotation.
   A text with a **fit box** shows it as a black dashed outline, and its corner handle
   resizes the *box* rather than the font — the type re-fits when you preview again, because
   the wrap-and-shrink search lives in Python and a second copy of it in the browser is how
   the editor and the render start disagreeing. Such a text writes `*_Width`/`*_Height` back
   to the sheet instead of `*_Size`
   (a dotted line shows when an element is centered on the canvas, and it gently snaps
   there), **resize** anything with its corner handle (texts resize their font size around
   their center), **recolor** texts with the color swatches, give any text a
   **background box** with the bg swatch (“none” removes it), and fade a text or its box
   with the **opacity sliders**. Texts render with their actual
   font and artistic style; the side panel live-updates the matching Excel values (`Video_X`,
   `Headline_Size`, `Headline_Color`, `Headline_Opacity`, `Headline_BgColor`, …) and highlights what changed. Click **💾 Save to Excel** to apply the changes to that
   row in one go — they're used by subsequent previews and generation, and
   **⬇️ Download updated Excel** gives you the sheet with the edits written in
   (formatting preserved) so your file stays the source of truth. A copy button is
   still there if you prefer pasting values by hand. (Text resizing scales the preview
   proportionally; a long text may re-wrap slightly in the final render at the new
   size.)
3. Click **🎬 Render Row** to render that one row to a real MP4 — with your saved edits —
   and play it right in the app (with a download button). Slower than the static preview,
   but it's exactly what the batch will produce for that row, including motion-only
   behavior the static preview can't show (side-clip sequencing, fades, the subliminal
   effect).
4. Click **🚀 Generate All Videos** — the batch is **queued** and the click returns
   immediately. **You can close the tab.** Rendering, captioning and uploading happen in
   a background worker process, so a closed tab, a slept laptop or a dropped connection
   no longer kills a batch.
5. Watch it on the **📋 Jobs** page — live progress, per-row failures with their reason
   (e.g. a background missing from the ZIP), and warnings. One bad row never stops the
   batch. An email arrives when it finishes, on success *and* on failure.
6. Collect the output: a **Google Drive** link if Drive is configured (the link never
   expires), or the ZIP from the Jobs page — every rendered MP4 plus `render_log.txt`
   and the batch spreadsheet.

If the worker is interrupted mid-batch, the job resumes rather than restarting: a crash
at video 250 of 300 costs the one video in flight, not the 250 already done.

Uploaded assets are deleted as soon as a job finishes; finished job folders are reaped
after a configurable retention period (7 days by default).

### Where the CTA clips come from

Step **3. CTA clips** on the Generate page picks the source. All four end up in the same
place — `assets/cta_slot_N/` — so the renderer cannot tell which was used.

| Source | What it does |
|---|---|
| **Upload files in the sidebar** | The original path: one uploader per clip slot |
| **Paste a Google Drive folder link** | The server downloads the clips from Drive itself — nothing goes through your browser |
| **Use a previous scrape on this machine** | Clips a TikTok scrape already left on the VM, selected in place |
| **Scrape a TikTok account now** | Scrape and render as one submitted job |

**The Drive option is there because uploading is the slow part.** If the clips already live
in Drive, a browser upload drags them down your home connection and pushes the same bytes
straight back to Google. Pasting a link keeps it Google-to-Google, at the VM's bandwidth
rather than yours, and the batch is queued the instant you click Generate.

Two layouts:

* **One folder per clip slot** — exactly what the sidebar uploaders do, so which clip plays
  in which position stays yours to decide. Leave a slot's link blank to skip it.
* **One folder for everything** — every video in the folder (sub-folders included) is dealt
  evenly across the slots, shuffled, and never used twice.

Notes:

* Share the source folder with the service account first — **Viewer is enough**. Unlike the
  *upload destination*, a source folder does **not** have to be in a Shared Drive: reading
  uses nobody's storage quota, so an ordinary My Drive folder is fine.
* **🔍 Check the folder(s)** reports the clip count and total size before you commit to a
  multi-hour run — one API call instead of finding out from the Jobs page later.
* Sub-folders are searched, shortcuts are followed, and non-videos are ignored. Duplicate
  filenames are made unique so nothing is silently overwritten.
* Downloads resume: a job that dies part-way re-fetches only what is actually missing,
  and a half-written file is never mistaken for a finished clip.

### Where the GIFs come from

The gif pool has its **own** source selector, right below the CTA-clip one, and the two are
independent — you can fetch CTA clips from Drive while uploading gifs, or the other way round.
There are two options rather than four: uploading, or a single pooled Drive folder
(downloaded straight into `assets/gifs/`). "Scrape a TikTok account" is deliberately absent —
it harvests posts by view count, which means nothing for a pool of loops.

### What to expect from the gif layer

Two consequences of the dwell floor are worth knowing before you judge a batch:

* **Not every uploaded gif appears in every video.** At 5 seconds each, a 20-second promo
  shows about 4 gifs, a 60-second promo about 12 — no matter how many you upload. The pool
  is the source of variety *across* videos, not *within* one. Upload 30 gifs and each video
  draws a different handful.
* **The last gif is usually cut mid-animation**, because the output ends exactly when the
  promo does. That is invisible for a true loop, but a gif with a beginning and an end (a
  logo reveal, a text animation) will look clipped in that final slot.

Gifs are supplied as **MP4**, not as `.gif` files — a real `.gif` upload is rejected by the
uploader's file-type filter, and a `.gif` sitting in a Drive folder is skipped rather than
downloaded.

## The other pages

| Page | What it does |
|---|---|
| **📋 Jobs** | Queued / running / finished batches, live progress, per-row errors, downloads |
| **🎵 TikTok Scraper** | Paste an account, get clips downloaded, trimmed to 10s, and dropped into Drive pre-sorted into `batch_NN/slot_N` folders — plus a `metadata.xlsx` of views, likes, duration and post date so curating 500 clips means sorting by views, not scrubbing thumbnails |
| **🔧 Setup** | What's configured (email, Drive, Gemini, worker) with a test button for each, and caption-pool generation |

### Batches, mixing, and the two filenames

One sheet becomes **`batches × rows`** videos. Set *Batches to render* and the sheet is
rendered that many times — each pass uses the **next promo video** (upload up to 20) and
a different variant salt, so every pass picks different CTA clips. The finished videos
are then **mixed evenly across the output folders**, so no folder is just one promo
video, and each folder gets an equal share of every batch.

Every video is published to Drive **twice**, under two names built from its caption:

| | Contents | Limit |
|---|---|---|
| **Short** | caption + **exactly one** hashtag | **max 90 characters** (`.mp4` not counted) |
| **Long** | caption + **one to five** hashtags | **max 90 characters** (`.mp4` not counted) |

```
Your skin will thank you for this one #skincare.mp4
Your skin will thank you for this one #skincare #asmr #fyp #glowup #selfcare.mp4
```

Names are **paste-ready**: spaces and `#` are preserved so the filename reads as the
caption you will actually post. **Emoji are stripped** — from the caption text itself as
well as the filename (`BVG_FILENAME_KEEP_EMOJI=true` keeps them). There is no numeric
prefix; collisions get a ` (2)` suffix that stays inside the cap.

In the long name the **caption never gives way to a hashtag**. Five long tags plus a
caption exceed 90 characters, and cutting the caption is what makes two different
captions collide on one filename — so hashtags are dropped instead, down to a floor of
one. A 46-character caption beside five 12-character tags comes out carrying three:

```
Your evening routine deserves better than this #skincareroutine #asmrsounds #foryoupage.mp4
```

The exception is a caption too long to fit even beside a single hashtag: it is being
truncated either way, so the full set is kept rather than losing tags for nothing.

### A fixed call-to-action instead of hashtags

Tick **End every filename with a fixed call-to-action line** in *4. Captions* and each
video's name ends with one of three lines, drawn at random per video:

```
Chat with your plushie at PlushieFriend.com
Bring your bestie to life at PlushieFriend.com
Join the plushie community at PlushieFriend.com
```

```
Soft little friend for quiet nights Chat with your plushie at PlushieFriend.com.mp4
```

Hashtags are **switched off** while it is on — a name carries one ending, not two, and
both together would not fit inside 90 characters. The short and long names then come out
identical, which is fine because they are published into different folders (so pick one
platform in *Publish which names?* unless you want the video in both).

**The line is never truncated**, including when a collision forces a ` (2)` counter: the
caption is what gives way, and the counter goes *in front of* the line rather than eating
the end of the URL. That matters because duplicate captions are normal here — a `Caption`
you typed into the sheet is reused across batches, as is the `Headline` fallback:

```
Your plushie remembers every single word Bring your bestie to life at PlushieFriend.com.mp4
Your plushie remembers every single (2) Bring your bestie to life at PlushieFriend.com.mp4
```

A caption still has to fit beside it: the longest line is 47 characters, leaving 42 of the
90. Captions generated at the 40-character limit always fit; a **pool generated before that
limit** holds longer ones and they are cut — the app counts them and warns you before you
queue. The lines live in `captions/naming.py` (`FIXED_TAILS`) — edit them there to change
the wording or domain.

Each output folder is published as **two ZIPs**, one per platform, so a 16,000-video night
arrives as ~32 archives instead of ~32,000 files:

```
<your Drive folder>/renders/2026-08-05_14-23-45.123/<batch label>/
├── batch_01/
│   ├── yt.zip                  ← the short name (one hashtag)
│   ├── tk.zip                  ← the long name (up to five)
│   └── batch_01_manifest.xlsx  ← readable without downloading 30 GB
├── batch_02/
│   ├── yt.zip
│   └── tk.zip
└── …
```

The platform is the **archive**, never the filename — nothing is prefixed onto the caption,
so the name stays paste-ready exactly as shown above. Archives are `ZIP_STORED`: MP4s do
not deflate, so recompressing them would burn hours to save a percent.

Zipping costs the one shortcut the per-file layout had. Uploading a video once and letting
Drive clone it with server-side `files.copy` only works while the two names are two
*files* — inside an archive they are entries in two different ZIPs, so the bytes cross the
network twice. That's the trade: **~2× the upload** in exchange for folders you can
actually hand to someone.

Packing runs a folder at a time and each folder's MP4s are deleted **only once both of its
archives are verified in Drive** (Drive reports the stored size; a short upload is
refused). So the disk high-water mark is the rendered videos plus the two archives of the
one folder being packed — and a failure anywhere before that leaves every byte where it
was, ready for the next attempt.

Set `BVG_UPLOAD_MODE=files` (or `upload_mode: files` in a job's params) for the older
behaviour: every video as its own Drive file under `batch_NN/yt/` and `batch_NN/tk/`, the
second made with `files.copy`. Worth it when you need to replace one video without
rebuilding an archive.

#### Drive's 750 GB/day ceiling

Google caps **one user at 750 GB per rolling 24 hours** of data moved into Drive. A
service account is a user, and **server-side copies count against it as well as
uploads**. Past the ceiling every write returns `403 userRateLimitExceeded` — which
reads like a permission error and is not one.

That is a hard planning constraint on a big night, and it applies to *both* publishing
modes:

| Mode | Bytes against the 750 GB allowance, for 16,000 videos (~500 GB of MP4s) |
|---|---|
| `zip` | ~1 TB — each archive is a separate upload |
| `files` | ~1 TB — ~500 GB uploaded, ~500 GB of `files.copy`, and copies count |

At ~31 MB a video that is roughly **12,000 videos a day** under both names — whichever
mode you pick, because copies are not free.

**The way out is to publish one set of names at a time.** *Publish which names?* on the
Generate page (or `upload_platforms` in a job's params, or `BVG_UPLOAD_PLATFORMS`) takes
`yt,tk`, `tk`, or `yt`. One platform halves the day's traffic, so ~24,000 videos fit:

```
Monday:  publish tk  → batch_NN/tk.zip   (~500 GB)   videos KEPT on the VM
Tuesday: requeue the same job with upload_platforms=["yt"]
         → batch_NN/yt.zip (~500 GB), and now the MP4s are freed
```

Each platform's state is recorded separately, so the second run re-packs from the MP4s
still on disk and re-sends nothing. **The videos are deliberately not deleted while a
platform is still outstanding** — `BVG_UPLOAD_FREE_LOCAL` only takes effect once every
platform has an archive, or the second day would have nothing to build from. Budget disk
for that: the MP4s stay put overnight.

The other lever is a **second service account**, since the allowance is charged per
identity. `BVG_DRIVE_IMPERSONATE=other-sa@PROJECT.iam.gserviceaccount.com` publishes as
one: the VM mints short-lived tokens for it rather than holding a second key file, so
there is no long-lived secret on disk. A brand-new account is unspent, which makes this
the only option that unblocks a run *today* rather than tomorrow. Setup — one IAM binding
and adding the account to the Shared Drive — is in DEPLOYMENT.md 5b-bis, and *Test Drive
access* reports which identity it published as so you can see the switch took effect.

Throttling itself is ridden out rather than fatal: every Drive call retries with
exponential backoff and jitter, and **the retry budget resets each time a chunk lands**,
so a 30 GB archive that is throttled repeatedly still finishes. Only a sustained refusal
— which is what the daily ceiling looks like — gives up, and it says so in those terms
instead of surfacing a bare 403. Nothing local is deleted when it does, so requeueing the
job after the window rolls picks up where it stopped.

#### Zipping folders that are already in Drive

Renders published before archives existed can be converted in place:

```bash
python tools/zip_drive_tk.py --link <drive folder url> --dry-run
python tools/zip_drive_tk.py --link <drive folder url> --platform tk
```

It walks the tree, and for each `tk/` (or `yt/`) folder it downloads the videos a handful
at a time, appends each to the archive and deletes it again, then uploads `tk.zip` beside
the original folder. Peak disk is one folder's archive, not the whole night — which is
what lets a 200 GB VM repack a terabyte. **Nothing in Drive is ever deleted**: the source
folder stays exactly as it was, so the videos remain their own backup. Interrupt it and
re-run whenever; finished folders are recorded and skipped, and an archive that is re-made
*replaces* the old one instead of becoming a second file with the same name.

That timestamp is **to the millisecond**, and it is stamped once when the job first
uploads, not re-derived per run. Two batches submitted under the same label never merge
into one folder, a job that uploads past midnight stays in one place, and a job that
crashes and resumes writes back into the folder it started.

Each output folder gets a `batch_NN_manifest.xlsx` listing exactly what landed in it —
caption, hashtags, and both filenames — plus a combined `render_manifest.xlsx`. After
mixing, that manifest is the artifact that tells you what to post; the original sheet is
kept alongside it unchanged.

### Captions

Captions are generic and themed: a pool of ~2,000 captions × ~500 hashtag sets is
generated occasionally (Setup page) and recombined per video, with no model call per
video. A caption identifies one *video*, not one sheet row — ten batches of the same row
are ten separate posts and each draws its own.

**No caption is ever used twice in one job.** That is stricter than it sounds, and it is
the rule the filenames depend on: a short filename is the caption plus *one* hashtag, so
two videos sharing a caption collide on their name however much their hashtag sets
differ. The draw therefore walks the caption list rather than the caption × hashtag grid.

So the pool must hold at least **`batches × rows`** captions — 1,000 rows across 16 promos
is 16,000 videos and therefore 16,000 captions. The Generate page checks this against the
batch count you set and warns before you queue anything. If it slips through, the job
stops before rendering and says by how much — a batch that cannot name its videos apart is
not worth the hours of FFmpeg it would cost. Generate a larger pool, or render fewer
batches. The input's ceiling is `BVG_CAPTION_POOL_MAX` (50,000), which exists to catch a
typo rather than to limit real batches.

**A pool is a consumable, not a cycle.** It is worth exactly as many videos as it holds
captions, and it never starts over: once its captions are used the next job is refused
and you generate a fresh pool. That is what makes "one caption, one video" true *across*
runs and not just inside one. The Setup page shows how many are left.

Hashtags are the opposite — they repeat freely, and the hashtag pool can be small. With
the caption already unique per video, the tags carry no naming duty at all; a handful of
sets is enough. (Keep at least two tags per set, or the short and long names come out
identical.) With the fixed call-to-action line switched on, no hashtags are generated
at all — the line replaces them, so the hashtag half of the pool is skipped.

The one exception is a `Caption` you type into the sheet yourself: that text is reused
across every batch, because you asked for that exact wording. Those are the only files
that can still pick up a ` (2)` suffix.

**How long a pool takes.** It is built in chunks of ~100 captions per model call, so 5,000
captions is ~50 calls at roughly half a minute each. Those calls run **concurrently**
(8 at a time by default, `BVG_CAPTION_CONCURRENCY`), which is what turns half an hour of
waiting into a few minutes — the chunks are independent single-turn requests, so nothing
is lost by overlapping them. Uniqueness is enforced locally by de-duplication, never by
the model, and the work comes in rounds: enough chunks to cover what's missing, then more
if de-duplication left a shortfall. Throttled chunks (429) are retried with jittered
backoff, and a chunk that can't be saved costs that chunk rather than the whole pool.

If it's still slower than you want, the model is a setting — `BVG_GEMINI_POOL_MODEL`.
`gemini-2.5-pro` is the default because a pool is generated rarely and its quality carries
across every video that reuses it; `gemini-2.5-flash` is several times faster and much
cheaper if you'd rather have the throughput.

## Deployment

For team use, deploy on a single GCP Compute Engine VM (Docker image included) —
see [DEPLOYMENT.md](DEPLOYMENT.md) for copy-paste instructions, secure access
options (IAP tunnel or HTTPS + basic auth via Caddy), and cost controls.

## How rendering works (for developers)

`video_generator.py` pre-renders the static layers with Pillow — `base.png` (cover-cropped
background), `overlay.png` (transparent layer with the styled texts: each text painted on its
own layer with its font, optional background box, and outline/shadow/neon decoration), and —
when a CTA image is supplied — `cta.png` (the resized CTA image) — then FFmpeg composites
everything in a single pass per row:

```
[1:v]scale=W:H:force_original_aspect_ratio=decrease[vid]   # fit promo video in box, no distortion
[0:v][vid]overlay=x='X+(W-w)/2':y='Y+(H-h)/2':shortest=1   # center in box over background
[bgvid][2:v]overlay=0:0[txt]                               # stamp text layer on top
# optional CTA videos — cover-filled to the box, each sped up/slowed by its own clip speed,
# concatenated in the row's shuffled order, faded in:
[N:v]...,scale=increase,crop=CVW:CVH,setpts=PTS/SPEED0,format=rgba[cv0]; ... ; [cv0][cv1]...concat=n=N:v=1:a=0[cseq]
[cseq]fade=t=in:st=CVS:d=CVD:alpha=1[ctav]
[txt][ctav]overlay=CVX:CVY[txtv]
# optional GIFs — each claimed with `-stream_loop <repeats-1>` so it replays whole before the
# graph sees it, then contain-fitted and padded transparent to a common size for concat:
[N:v]fps=F,format=rgba,scale=GW:GH:force_original_aspect_ratio=decrease:force_divisible_by=2,
     pad=GW:GH:'trunc((GW-iw)/4)*2':'trunc((GH-ih)/4)*2':color=0x00000000,setsar=1[gv0]; ...
[gv0][gv1]...concat=n=N:v=1:a=0[gseq] ; [gseq]fade=...:alpha=1[gifl]
[txtv][gifl]overlay=GX:GY[withgifs]
# optional CTA image (only when uploaded): configurable alpha fade-in, placed on top
[N:v]format=rgba,fade=t=in:st=CFS:d=CFD:alpha=1[cta]
[withgifs][cta]overlay=CTA_X:CTA_Y,format=yuv420p
```

Layers are stacked in ascending z-index order, so the actual chain depends on the sidebar's
*Layer order* — the sketch above shows the defaults.

Three things about the gif layer are load-bearing and easy to undo by accident:

- **`force_original_aspect_ratio=decrease` is what makes the box authoritative.** It fits the
  gif inside `GW x GH` in *both* directions — shrinking a big gif and enlarging a small one —
  so the box sets the on-screen size regardless of the source resolution. Wrapping the
  dimensions in `min(GW,iw)`/`min(GH,ih)` caps the fit at the source size and silently leaves
  low-res gifs small; that was the old behaviour and it is not what this layer promises.
- **The `pad` is not cosmetic.** `concat` rejects inputs of differing sizes, and contain-fitting
  gifs of assorted shapes produces exactly that. `format=rgba` must come *before* the pad, or
  the transparent colour flattens to opaque black and the box becomes a visible plate.
- **No `setpts` on the gif chain.** `-stream_loop` already emits continuous monotonic PTS and
  `concat` re-stamps the joined timeline; `setpts=N/FRAME_RATE/TB` drops one frame per segment.

Input indices are claimed through a helper that returns the index it took, rather than computed
by summing list lengths, and `_check_filter_inputs` then asserts every input is referenced
exactly once. That guard exists because an off-by-one here does **not** fail — it renders a
different video with exit code 0 and empty stderr.

Gif dwell times are computed from the **video stream's** duration, not the container header:
an MP4 whose audio outlasts its video reports the audio length, which would silently
under-repeat the gif and miss the floor.

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
