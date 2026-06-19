"""
preview_editor.py — Interactive HTML preview editor for one Excel row.

Renders the row's composite at reduced scale. The video box, the CTA, and
every text block can be dragged to move and resized with a corner handle
(boxes resize freely; texts scale their font size around their center).
While moving or resizing, a dotted guide appears — and the element snaps —
when its center lands on the canvas's vertical/horizontal centerline. Each
text also has a color picker. A side panel lists the resulting Excel column
values live, highlighting anything that differs from the rendered row.

Two ways to render it:

- preview_editor(payload, nonce): bidirectional Streamlit component (the
  app). "Save to Excel" posts {nonce, token, values} back to Python so the
  app can apply the changed values to the sheet directly.
- render_editor_html(payload): self-contained HTML fragment with the payload
  baked in (used by the static test page); the Save button is hidden because
  there is no app to receive the values.

Everything runs client-side. The payload (see VideoGenerator.
build_editor_payload) ships each layer as a data URI; texts are white
alpha-mask PNGs recolored with CSS mask-image, so moving, resizing, and
recoloring need no server round-trip while keeping PIL's exact glyphs (text
resizing scales the raster — the final render re-rasterizes, and may re-wrap
long texts, at the chosen size).
"""

import json
from pathlib import Path

import streamlit.components.v1 as components

# On-screen width of the 1080px-wide canvas (scale ~= 0.39, height ~= 747px).
STAGE_W = 420
EDITOR_HEIGHT = 790  # iframe height (the component sets it via setFrameHeight)

# Mirrors the engine's clamps: video boxes 50..canvas, CTA 10..canvas
# (see box_dim in VideoGenerator._resolve_positions); text sizes get a
# sensible editor range.
MIN_VIDEO_DIM = 50
MIN_CTA_DIM = 10
MIN_TEXT_SIZE = 10
MAX_TEXT_SIZE = 250

# Center guides: while moving/resizing, an element whose center comes within
# this many canvas px (~3 px on screen) of the canvas's vertical/horizontal
# centerline snaps onto it and shows a dotted line.
SNAP_TOL = 8

_COMPONENT_DIR = Path(__file__).parent / "editor_component"


def _fill_template(payload_js: str) -> str:
    consts = {
        "__STAGE_W__": STAGE_W,
        "__EDITOR_HEIGHT__": EDITOR_HEIGHT,
        "__MIN_VIDEO_DIM__": MIN_VIDEO_DIM,
        "__MIN_CTA_DIM__": MIN_CTA_DIM,
        "__MIN_TEXT_SIZE__": MIN_TEXT_SIZE,
        "__MAX_TEXT_SIZE__": MAX_TEXT_SIZE,
        "__SNAP__": SNAP_TOL,
    }
    html = _TEMPLATE
    for token, value in consts.items():
        html = html.replace(token, str(value))
    return html.replace("__PAYLOAD__", payload_js)


def render_editor_html(payload: dict) -> str:
    # "</" never occurs in base64, but row text can reach future payload
    # fields — escape it so the JSON can't close the <script>.
    data = json.dumps(payload).replace("</", "<\\/")
    return _fill_template(data)


def _write_component_page() -> None:
    """The declared component serves a static page; the payload arrives via
    the streamlit:render message instead of being baked into the HTML."""
    _COMPONENT_DIR.mkdir(exist_ok=True)
    doc = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;background:transparent">'
        + _fill_template("null")
        + "</body></html>"
    )
    (_COMPONENT_DIR / "index.html").write_text(doc, encoding="utf-8")


def preview_editor(payload: dict, nonce: str, key: str = "preview_editor"):
    """Render the editor as a bidirectional component. Returns None until the
    user clicks "Save to Excel", then {nonce, token, values} where values maps
    Excel column names to the edited values. The nonce identifies which
    preview the save belongs to (stale values echo back on reruns); the token
    is unique per click so the app can tell a new save from an echo."""
    return _component(payload=payload, nonce=nonce, key=key, default=None)


_TEMPLATE = r"""
<style>
  #wrap { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start;
          font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  #stage { position: relative; overflow: hidden; border-radius: 8px;
           flex: 0 0 auto; background: #000; user-select: none; }
  #stage img.bg { position: absolute; inset: 0; width: 100%; height: 100%; }
  .el { position: absolute; cursor: move; touch-action: none; }
  .el:hover, .el.drag { outline: 1.5px dashed rgba(255,255,255,.85); outline-offset: 1px; }
  .el > img { width: 100%; height: 100%; display: block; pointer-events: none; }
  #videoBox > img.frame, #ctaVideoBox > img.frame { position: absolute; width: auto; height: auto; }
  /* Texts are layered (bottom to top): the live CSS background box, the baked
     decoration (outline/shadow/neon glow), then the recolorable glyph mask.
     All position:absolute so they paint in DOM order. */
  .bgbox { position: absolute; pointer-events: none; }
  .el > img.deco { position: absolute; inset: 0; pointer-events: none; }
  /* Texts: the mask+color live on an inner layer so the mask can't clip the
     resize handle (a mask on the outer div would hide everything outside the
     glyph pixels, including children). */
  .ink { position: absolute; inset: 0; pointer-events: none;
         mask-size: 100% 100%; -webkit-mask-size: 100% 100%;
         mask-repeat: no-repeat; -webkit-mask-repeat: no-repeat; }
  .handle { position: absolute; right: -6px; bottom: -6px; width: 12px; height: 12px;
            background: #fff; border: 1px solid #333; border-radius: 3px;
            cursor: nwse-resize; opacity: 0; touch-action: none; }
  .el:hover .handle, .el.drag .handle { opacity: 1; }
  .guide { position: absolute; pointer-events: none; display: none; z-index: 1000; }
  #guideV { top: 0; bottom: 0; width: 0; border-left: 2px dotted #00e0ff; }
  #guideH { left: 0; right: 0; height: 0; border-top: 2px dotted #00e0ff; }
  #panel { flex: 1 1 280px; min-width: 260px; background: #262730; color: #fafafa;
           border-radius: 8px; padding: 12px 14px; font-size: 13px;
           max-height: 747px; overflow: auto; box-sizing: border-box; }
  #panel h3 { margin: 0 0 6px; font-size: 14px; }
  .hint { color: #9aa0ab; font-size: 12px; margin: 0 0 10px; }
  table.vals { width: 100%; border-collapse: collapse; }
  table.vals th, table.vals td { padding: 3px 6px; text-align: left; font-size: 12.5px;
                                 border-bottom: 1px solid rgba(255,255,255,.08); }
  table.vals th { color: #a3a8b3; font-weight: 600; }
  tr.changed td { color: #ffd166; font-weight: 600; }
  input[type=color] { width: 26px; height: 18px; border: none; background: none;
                      padding: 0; margin-left: 6px; cursor: pointer; vertical-align: middle; }
  .miniBtn { margin-left: 4px; background: #3b3f4a; color: #fff;
             border: 1px solid rgba(255,255,255,.2); border-radius: 4px;
             font-size: 11px; padding: 1px 6px; cursor: pointer; vertical-align: middle; }
  .ro { color: #cfd3db; }
  #btnRow { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  #btnRow button { background: #3b3f4a; color: #fff; border: 1px solid rgba(255,255,255,.2);
                   border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 12.5px; }
  #btnRow button:hover { background: #4a4f5d; }
  #btnRow #saveBtn { background: #2563eb; border-color: rgba(255,255,255,.3); }
  #btnRow #saveBtn:hover { background: #1d4ed8; }
  #copyBox { width: 100%; box-sizing: border-box; margin-top: 8px; min-height: 70px;
             background: #14151a; color: #eee; border: 1px solid rgba(255,255,255,.15);
             border-radius: 6px; font-family: Consolas, monospace; font-size: 12px; }
</style>
<div id="wrap">
  <div id="stage"></div>
  <div id="panel">
    <h3>Excel values</h3>
    <div class="hint">Drag elements to move them, drag the corner handle to resize
      (texts resize their font size), recolor texts with the swatches, and give any
      text a background box with the bg swatch (“none” removes it). Font and artistic
      style come from the sidebar / Excel and are shown for reference. A dotted line
      appears when an element is centered on the canvas. Changed values are
      highlighted — click “Save to Excel” to apply them to your sheet in one go.</div>
    <table class="vals">
      <thead><tr><th>Column</th><th>Was</th><th>Now</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div id="btnRow">
      <button id="saveBtn">💾 Save to Excel</button>
      <button id="copyBtn">Copy changed values</button>
      <button id="resetBtn">Reset</button>
    </div>
    <textarea id="copyBox" readonly placeholder="No changes yet — drag something!"></textarea>
  </div>
</div>
<script>
(function () {
  // Payload baked in (standalone test page) or null (Streamlit component
  // mode, where it arrives via the streamlit:render message).
  const EMBEDDED = __PAYLOAD__;
  const stage = document.getElementById('stage');
  const tbody = document.getElementById('rows');
  const copyBox = document.getElementById('copyBox');
  const saveBtn = document.getElementById('saveBtn');
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  let saveSeq = 0;

  function post(type, extra) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, extra), '*');
  }

  function init(DATA, nonce) {
    const scale = __STAGE_W__ / DATA.canvas_w;
    const px = v => (v * scale) + 'px';
    const items = [];   // editable things, each exposing cols() -> [[column, was, now], ...]
    // Sidebar layer order (z-index); the render stacks layers the same way.
    // Default mirrors the engine if an older payload omits it.
    const Z = DATA.z || { video: 1, cta_video: 2, cta_image: 3, text: 4 };

    stage.innerHTML = '';
    tbody.innerHTML = '';
    copyBox.value = '';
    stage.style.width = __STAGE_W__ + 'px';
    stage.style.height = Math.round(DATA.canvas_h * scale) + 'px';

    const bg = document.createElement('img');
    bg.className = 'bg';
    bg.src = DATA.bg;
    bg.draggable = false;
    stage.appendChild(bg);

    // --- center guides: while moving or resizing, an element whose center
    //     comes within __SNAP__ canvas px of the canvas centerline snaps onto
    //     it and shows a dotted line (vertical, horizontal, or both). ---
    const SNAP = __SNAP__;
    const CX = DATA.canvas_w / 2, CY = DATA.canvas_h / 2;
    const guideV = document.createElement('div');
    guideV.className = 'guide'; guideV.id = 'guideV';
    const guideH = document.createElement('div');
    guideH.className = 'guide'; guideH.id = 'guideH';
    stage.appendChild(guideV); stage.appendChild(guideH);

    function showGuides(vx, hy) {
      guideV.style.display = vx == null ? 'none' : 'block';
      if (vx != null) guideV.style.left = px(vx);
      guideH.style.display = hy == null ? 'none' : 'block';
      if (hy != null) guideH.style.top = px(hy);
    }

    function snapMove(item) {
      const c = item.center(), b = item.bounds();
      let vx = null, hy = null;
      const dx = CX - c.cx;
      if (Math.abs(dx) <= SNAP && clamp(item.x + dx, b.minX, b.maxX) === item.x + dx) {
        item.x += dx; vx = CX;
      }
      const dy = CY - c.cy;
      if (Math.abs(dy) <= SNAP && clamp(item.y + dy, b.minY, b.maxY) === item.y + dy) {
        item.y += dy; hy = CY;
      }
      showGuides(vx, hy);
    }

    // Boxes resize from their fixed top-left, so growing w by d moves the
    // center by d/2: snap w/h so the center lands on the canvas centerline.
    // The snapped far edge is 2*CX - x <= canvas_w (x >= 0), so only the
    // minimum dimension needs re-checking.
    function snapResizeBox(item, minDim) {
      let vx = null, hy = null;
      const dw = 2 * (CX - (item.x + item.w / 2));
      if (Math.abs(dw) <= 2 * SNAP && item.w + dw >= minDim) { item.w += dw; vx = CX; }
      const dh = 2 * (CY - (item.y + item.h / 2));
      if (Math.abs(dh) <= 2 * SNAP && item.h + dh >= minDim) { item.h += dh; hy = CY; }
      showGuides(vx, hy);
    }

    function makeDraggable(el, item) {
      el.addEventListener('pointerdown', e => {
        e.preventDefault();
        el.setPointerCapture(e.pointerId);
        el.classList.add('drag');
        const sx = e.clientX, sy = e.clientY, ox = item.x, oy = item.y;
        const move = ev => {
          const b = item.bounds();
          item.x = clamp(ox + (ev.clientX - sx) / scale, b.minX, b.maxX);
          item.y = clamp(oy + (ev.clientY - sy) / scale, b.minY, b.maxY);
          snapMove(item);
          item.place();
          refresh();
        };
        const up = () => {
          el.removeEventListener('pointermove', move);
          el.removeEventListener('pointerup', up);
          el.classList.remove('drag');
          showGuides(null, null);
          item.x = Math.round(item.x);
          item.y = Math.round(item.y);
          item.place();
          refresh();
        };
        el.addEventListener('pointermove', move);
        el.addEventListener('pointerup', up);
      });
    }

    function addHandle(el, item) {
      const h = document.createElement('div');
      h.className = 'handle';
      el.appendChild(h);
      h.addEventListener('pointerdown', e => {
        e.preventDefault();
        e.stopPropagation();
        h.setPointerCapture(e.pointerId);
        el.classList.add('drag');
        const ctx = item.startResize(e);
        const move = ev => {
          item.resize(ctx, ev);
          if (item.snapResize) item.snapResize();
          item.place();
          refresh();
        };
        const up = () => {
          h.removeEventListener('pointermove', move);
          h.removeEventListener('pointerup', up);
          el.classList.remove('drag');
          showGuides(null, null);
          item.endResize();
          item.place();
          refresh();
        };
        h.addEventListener('pointermove', move);
        h.addEventListener('pointerup', up);
      });
    }

    // --- video box (the frame re-fits, centered, whenever the box resizes) ---
    const v = DATA.video;
    const videoEl = document.createElement('div');
    videoEl.className = 'el';
    videoEl.id = 'videoBox';
    const frame = document.createElement('img');
    frame.className = 'frame';
    frame.src = v.frame;
    frame.draggable = false;
    videoEl.appendChild(frame);
    videoEl.style.zIndex = Z.video;
    stage.appendChild(videoEl);
    const videoItem = {
      x: v.x, y: v.y, w: v.w, h: v.h,
      center() { return { cx: this.x + this.w / 2, cy: this.y + this.h / 2 }; },
      snapResize() { snapResizeBox(this, __MIN_VIDEO_DIM__); },
      bounds() {
        return { minX: 0, maxX: DATA.canvas_w - this.w, minY: 0, maxY: DATA.canvas_h - this.h };
      },
      place() {
        videoEl.style.left = px(this.x);
        videoEl.style.top = px(this.y);
        videoEl.style.width = px(this.w);
        videoEl.style.height = px(this.h);
        const r = Math.min(this.w / v.frame_w, this.h / v.frame_h);
        const fw = v.frame_w * r, fh = v.frame_h * r;
        frame.style.width = px(fw);
        frame.style.height = px(fh);
        frame.style.left = px((this.w - fw) / 2);
        frame.style.top = px((this.h - fh) / 2);
      },
      startResize(e) { return { w0: this.w, h0: this.h, sx: e.clientX, sy: e.clientY }; },
      resize(ctx, ev) {
        this.w = clamp(ctx.w0 + (ev.clientX - ctx.sx) / scale, __MIN_VIDEO_DIM__, DATA.canvas_w - this.x);
        this.h = clamp(ctx.h0 + (ev.clientY - ctx.sy) / scale, __MIN_VIDEO_DIM__, DATA.canvas_h - this.y);
      },
      endResize() { this.w = Math.round(this.w); this.h = Math.round(this.h); },
      reset() { this.x = v.x; this.y = v.y; this.w = v.w; this.h = v.h; this.place(); },
      cols() {
        return [
          ['Video_X', v.x, Math.round(this.x)],
          ['Video_Y', v.y, Math.round(this.y)],
          ['Video_Width', v.w, Math.round(this.w)],
          ['Video_Height', v.h, Math.round(this.h)],
        ];
      },
    };
    videoItem.place();
    makeDraggable(videoEl, videoItem);
    addHandle(videoEl, videoItem);
    items.push(videoItem);

    // --- CTA (stretches to its box, like the render) ---
    const c = DATA.cta;
    const ctaEl = document.createElement('div');
    ctaEl.className = 'el';
    const ctaImg = document.createElement('img');
    ctaImg.src = c.img;
    ctaImg.draggable = false;
    ctaEl.appendChild(ctaImg);
    ctaEl.style.zIndex = Z.cta_image;
    stage.appendChild(ctaEl);
    const ctaItem = {
      x: c.x, y: c.y, w: c.w, h: c.h,
      center() { return { cx: this.x + this.w / 2, cy: this.y + this.h / 2 }; },
      snapResize() { snapResizeBox(this, __MIN_CTA_DIM__); },
      bounds() {
        return { minX: 0, maxX: DATA.canvas_w - this.w, minY: 0, maxY: DATA.canvas_h - this.h };
      },
      place() {
        ctaEl.style.left = px(this.x);
        ctaEl.style.top = px(this.y);
        ctaEl.style.width = px(this.w);
        ctaEl.style.height = px(this.h);
      },
      startResize(e) { return { w0: this.w, h0: this.h, sx: e.clientX, sy: e.clientY }; },
      resize(ctx, ev) {
        this.w = clamp(ctx.w0 + (ev.clientX - ctx.sx) / scale, __MIN_CTA_DIM__, DATA.canvas_w - this.x);
        this.h = clamp(ctx.h0 + (ev.clientY - ctx.sy) / scale, __MIN_CTA_DIM__, DATA.canvas_h - this.y);
      },
      endResize() { this.w = Math.round(this.w); this.h = Math.round(this.h); },
      reset() { this.x = c.x; this.y = c.y; this.w = c.w; this.h = c.h; this.place(); },
      cols() {
        return [
          ['CTA_X', c.x, Math.round(this.x)],
          ['CTA_Y', c.y, Math.round(this.y)],
          ['CTA_Width', c.w, Math.round(this.w)],
          ['CTA_Height', c.h, Math.round(this.h)],
        ];
      },
    };
    ctaItem.place();
    makeDraggable(ctaEl, ctaItem);
    addHandle(ctaEl, ctaItem);
    items.push(ctaItem);

    // --- CTA video box (optional; same behavior as the promo video box).
    //     Each layer's CSS z-index (set from Z above) drives the stacking so the
    //     editor matches the render; inserting before ctaEl only sets the DOM
    //     order, which breaks ties when two layers share the same z-index
    //     (priority promo < CTA video < CTA image < texts, as in the render). ---
    if (DATA.cta_video) {
      const cv = DATA.cta_video;
      const cvEl = document.createElement('div');
      cvEl.className = 'el';
      cvEl.id = 'ctaVideoBox';
      const cvFrame = document.createElement('img');
      cvFrame.className = 'frame';
      cvFrame.src = cv.frame;
      cvFrame.draggable = false;
      cvEl.appendChild(cvFrame);
      cvEl.style.zIndex = Z.cta_video;
      stage.insertBefore(cvEl, ctaEl);
      const cvItem = {
        x: cv.x, y: cv.y, w: cv.w, h: cv.h,
        center() { return { cx: this.x + this.w / 2, cy: this.y + this.h / 2 }; },
        snapResize() { snapResizeBox(this, __MIN_VIDEO_DIM__); },
        bounds() {
          return { minX: 0, maxX: DATA.canvas_w - this.w, minY: 0, maxY: DATA.canvas_h - this.h };
        },
        place() {
          cvEl.style.left = px(this.x);
          cvEl.style.top = px(this.y);
          cvEl.style.width = px(this.w);
          cvEl.style.height = px(this.h);
          const r = Math.min(this.w / cv.frame_w, this.h / cv.frame_h);
          const fw = cv.frame_w * r, fh = cv.frame_h * r;
          cvFrame.style.width = px(fw);
          cvFrame.style.height = px(fh);
          cvFrame.style.left = px((this.w - fw) / 2);
          cvFrame.style.top = px((this.h - fh) / 2);
        },
        startResize(e) { return { w0: this.w, h0: this.h, sx: e.clientX, sy: e.clientY }; },
        resize(ctx, ev) {
          this.w = clamp(ctx.w0 + (ev.clientX - ctx.sx) / scale, __MIN_VIDEO_DIM__, DATA.canvas_w - this.x);
          this.h = clamp(ctx.h0 + (ev.clientY - ctx.sy) / scale, __MIN_VIDEO_DIM__, DATA.canvas_h - this.y);
        },
        endResize() { this.w = Math.round(this.w); this.h = Math.round(this.h); },
        reset() { this.x = cv.x; this.y = cv.y; this.w = cv.w; this.h = cv.h; this.place(); },
        cols() {
          return [
            ['CTA_Video_X', cv.x, Math.round(this.x)],
            ['CTA_Video_Y', cv.y, Math.round(this.y)],
            ['CTA_Video_Width', cv.w, Math.round(this.w)],
            ['CTA_Video_Height', cv.h, Math.round(this.h)],
          ];
        },
      };
      cvItem.place();
      makeDraggable(cvEl, cvItem);
      addHandle(cvEl, cvItem);
      items.push(cvItem);
    }

    // --- texts (x/y is the block CENTER; resizing scales the font size
    //     around that center, so X/Y stay put and centeredness can't change —
    //     hence no snapResize). Layers, bottom to top: a live CSS background
    //     box, the baked decoration (outline/shadow/neon), the recolorable
    //     glyph mask. ---
    DATA.texts.forEach(t => {
      const el = document.createElement('div');
      el.className = 'el textEl';
      const bgBox = document.createElement('div');
      bgBox.className = 'bgbox';
      bgBox.style.display = t.bg ? 'block' : 'none';
      if (t.bg) bgBox.style.backgroundColor = t.bg;
      el.appendChild(bgBox);
      const deco = document.createElement('img');
      deco.className = 'deco';
      deco.src = t.deco;
      deco.draggable = false;
      el.appendChild(deco);
      const ink = document.createElement('div');
      ink.className = 'ink';
      ink.style.backgroundColor = t.color;
      ink.style.webkitMaskImage = "url('" + t.mask + "')";
      ink.style.maskImage = "url('" + t.mask + "')";
      el.appendChild(ink);
      el.style.zIndex = Z.text;
      stage.appendChild(el);
      const item = {
        x: t.cx, y: t.cy, s: 1, color: t.color, bg: t.bg || null,
        curW() { return t.w * this.s; },
        curH() { return t.h * this.s; },
        fontSize() { return Math.round(t.size * this.s); },
        center() { return { cx: this.x, cy: this.y }; },
        bounds() {
          const w = this.curW(), h = this.curH();
          return { minX: w / 2, maxX: DATA.canvas_w - w / 2, minY: h / 2, maxY: DATA.canvas_h - h / 2 };
        },
        place() {
          const w = this.curW(), h = this.curH();
          el.style.width = px(w);
          el.style.height = px(h);
          el.style.left = px(this.x - w / 2);
          el.style.top = px(this.y - h / 2);
          // background box: centered, sized from the baked text bbox + padding
          const bw = t.bg_w * this.s, bh = t.bg_h * this.s;
          bgBox.style.width = px(bw);
          bgBox.style.height = px(bh);
          bgBox.style.left = px((w - bw) / 2);
          bgBox.style.top = px((h - bh) / 2);
          bgBox.style.borderRadius = px(t.bg_radius * this.s);
        },
        applyBg() {
          if (this.bg) { bgBox.style.display = 'block'; bgBox.style.backgroundColor = this.bg; }
          else { bgBox.style.display = 'none'; }
          if (this.bgPicker && this.bg) this.bgPicker.value = this.bg;
        },
        startResize(e) {
          const r = el.getBoundingClientRect();
          const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
          return { cx, cy, s0: this.s, d0: Math.max(10, Math.hypot(e.clientX - cx, e.clientY - cy)) };
        },
        resize(ctx, ev) {
          let s = ctx.s0 * Math.hypot(ev.clientX - ctx.cx, ev.clientY - ctx.cy) / ctx.d0;
          s = clamp(s, __MIN_TEXT_SIZE__ / t.size, __MAX_TEXT_SIZE__ / t.size);
          // keep the scaled block no larger than the canvas
          s = Math.min(s, DATA.canvas_w / t.w, DATA.canvas_h / t.h);
          this.s = s;
        },
        endResize() { this.s = this.fontSize() / t.size; },  // snap to a whole font size
        reset() {
          this.x = t.cx; this.y = t.cy; this.s = 1; this.color = t.color;
          this.bg = t.bg || null;
          ink.style.backgroundColor = t.color;
          if (this.picker) this.picker.value = t.color;
          this.applyBg();
          this.place();
        },
        setColor(hex) { this.color = hex.toUpperCase(); ink.style.backgroundColor = this.color; },
        setBg(hex) { this.bg = hex ? hex.toUpperCase() : null; this.applyBg(); },
        cols() {
          return [
            [t.role + '_X', t.cx, Math.round(this.x)],
            [t.role + '_Y', t.cy, Math.round(this.y)],
            [t.role + '_Size', t.size, this.fontSize()],
            [t.role + '_Color', t.color, this.color],
            [t.role + '_BgColor', t.bg || '', this.bg || ''],
            [t.role + '_Font', t.font || '', t.font || ''],
            [t.role + '_Style', t.style || 'classic', t.style || 'classic'],
          ];
        },
      };
      item.place();
      makeDraggable(el, item);
      addHandle(el, item);
      items.push(item);
    });

    // --- values panel ---
    const rowDefs = [];
    items.forEach(item => {
      item.cols().forEach((col, i) => {
        const tr = document.createElement('tr');
        const tdName = document.createElement('td');
        const tdWas = document.createElement('td');
        const tdNow = document.createElement('td');
        tdName.textContent = col[0];
        tdWas.textContent = col[1];
        const nowSpan = document.createElement('span');
        tdNow.appendChild(nowSpan);
        if (col[0].endsWith('_BgColor')) {
          // Live background box: a swatch turns it on / recolors it, "none" clears it.
          const picker = document.createElement('input');
          picker.type = 'color';
          picker.value = col[1] || '#FF2D55';
          picker.addEventListener('input', () => { item.setBg(picker.value); refresh(); });
          item.bgPicker = picker;
          const off = document.createElement('button');
          off.type = 'button';
          off.className = 'miniBtn';
          off.textContent = 'none';
          off.addEventListener('click', () => { item.setBg(null); refresh(); });
          tdNow.append(picker, off);
        } else if (col[0].endsWith('_Color')) {
          const picker = document.createElement('input');
          picker.type = 'color';
          picker.value = col[1];
          picker.addEventListener('input', () => { item.setColor(picker.value); refresh(); });
          item.picker = picker;
          tdNow.appendChild(picker);
        } else if (col[0].endsWith('_Font') || col[0].endsWith('_Style')) {
          // Informational only — chosen in the sidebar / Excel, not edited here.
          nowSpan.classList.add('ro');
        }
        tr.append(tdName, tdWas, tdNow);
        tbody.appendChild(tr);
        rowDefs.push({ item, idx: i, tr, nowSpan });
      });
    });

    function refresh() {
      const changed = [];
      rowDefs.forEach(def => {
        const col = def.item.cols()[def.idx];
        def.nowSpan.textContent = col[2];
        const isChanged = String(col[1]) !== String(col[2]);
        def.tr.classList.toggle('changed', isChanged);
        if (isChanged) changed.push(col[0] + ' = ' + col[2]);
      });
      copyBox.value = changed.join('\n');
    }
    refresh();

    // onclick assignment (not addEventListener) so re-init replaces the
    // previous payload's handlers instead of stacking them.
    document.getElementById('resetBtn').onclick = () => {
      items.forEach(item => item.reset());
      refresh();
    };
    document.getElementById('copyBtn').onclick = () => {
      const text = copyBox.value;
      if (!text) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => {
          copyBox.select();
          document.execCommand('copy');
        });
      } else {
        copyBox.select();
        document.execCommand('copy');
      }
    };
    saveBtn.onclick = () => {
      const values = {};
      items.forEach(item => item.cols().forEach(col => {
        if (String(col[1]) !== String(col[2])) values[col[0]] = col[2];
      }));
      post('streamlit:setComponentValue', {
        dataType: 'json',
        value: { nonce: nonce, token: Date.now() + '-' + (++saveSeq), values: values },
      });
      const label = saveBtn.textContent;
      saveBtn.textContent = 'Saved ✓';
      setTimeout(() => { saveBtn.textContent = label; }, 1500);
    };
  }

  if (EMBEDDED) {
    saveBtn.style.display = 'none';  // standalone page: no app to receive values
    init(EMBEDDED, null);
  } else {
    let lastNonce = null;
    window.addEventListener('message', e => {
      const m = e.data;
      if (!m || m.type !== 'streamlit:render' || !m.args || !m.args.payload) return;
      // Streamlit re-renders on every rerun; only rebuild when the previewed
      // row actually changed, so in-progress edits survive app reruns.
      if (m.args.nonce === lastNonce) return;
      lastNonce = m.args.nonce;
      init(m.args.payload, m.args.nonce);
    });
    post('streamlit:componentReady', { apiVersion: 1 });
    post('streamlit:setFrameHeight', { height: __EDITOR_HEIGHT__ });
  }
})();
</script>
"""

# Module level so the page exists before Streamlit serves the component dir
# (and so the standalone helper can import without a running app).
_write_component_page()
_component = components.declare_component("preview_editor", path=str(_COMPONENT_DIR))
