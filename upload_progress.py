"""
upload_progress.py — one progress bar for everything the browser is uploading.

Streamlit's file uploader draws a hairline bar under each file and nothing for
the set, so 600 promo videos — or a single 4 GB one — upload with no idea of
how far along they are or how long is left. Closing or reloading the tab in
that window silently throws the whole upload away; Streamlit cannot resume one.

The front end uploads every file as its own XMLHttpRequest PUT to
/_stcore/upload_file/…. install() wraps XMLHttpRequest in the page so each of
those requests also reports to one floating panel: files done, bytes, speed and
time left. The requests themselves are untouched — the wrapper only listens —
and anything that is not an upload passes straight through.

While an upload is running, leaving the page asks for confirmation first.

Covered by a browser check rather than a unit test: the behaviour is entirely
client-side. If Streamlit ever moves uploads off XMLHttpRequest or renames the
endpoint, the panel simply stops appearing; uploads keep working.
"""

from __future__ import annotations

import inspect
import json

import streamlit as st

# Runs in the PAGE's window, not an iframe's: the patch has to outlive any one
# Streamlit element, and a function from a removed iframe's realm is not safe
# to keep calling. Guarded so reruns and page switches install it once.
_SCRIPT = r"""
(function () {
  "use strict";
  var w = window;
  if (w.__bvgUploadProgress) { w.__bvgUploadProgressRender(); return; }
  w.__bvgUploadProgress = true;

  var MATCH = "/_stcore/upload_file";
  var files = [];
  var samples = [];
  var panel = null, bar = null, title = null, detail = null, current = null;
  var drawPending = false, hideTimer = null;

  // Decimal units, as Streamlit's own file chips print them, so the panel and
  // the "21.0MB" under each file agree.
  function fmtBytes(n) {
    var units = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    return (i < 2 ? Math.round(n) : n.toFixed(1)) + " " + units[i];
  }
  function fmtTime(s) {
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + (w.__bvgLanguage === "zh-CN" ? " 秒" : "s");
    if (s < 3600) return Math.round(s / 60) + (w.__bvgLanguage === "zh-CN" ? " 分钟" : " min");
    var h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h + (w.__bvgLanguage === "zh-CN" ? " 小时 " : "h ") +
      (m < 10 ? "0" : "") + m + (w.__bvgLanguage === "zh-CN" ? " 分钟" : "m");
  }

  function settled() {
    return files.every(function (f) { return f.done; });
  }

  function track(xhr, body) {
    var name = "file", size = 0;
    if (body && typeof body.forEach === "function") {
      body.forEach(function (v) {
        if (v && typeof v === "object" && typeof v.size === "number") {
          size += v.size;
          name = v.name || name;
        }
      });
    }
    // A new upload after the last set finished starts a fresh tally.
    if (files.length && settled()) { files = []; samples = []; }
    var f = {name: name, size: size, loaded: 0, done: false,
             failed: false, aborted: false};
    files.push(f);
    xhr.upload.addEventListener("progress", function (e) {
      if (!f.size && e.lengthComputable) f.size = e.total;
      // e.total includes the multipart envelope, so scale to the file's own
      // size rather than reporting a few hundred bytes more than was chosen.
      if (e.lengthComputable && e.total > 0) {
        f.loaded = Math.min(f.size, f.size * e.loaded / e.total);
      } else {
        f.loaded = Math.min(f.size, e.loaded);
      }
      draw();
    });
    xhr.addEventListener("abort", function () { f.aborted = true; });
    xhr.addEventListener("loadend", function () {
      f.done = true;
      f.failed = !f.aborted && !(xhr.status >= 200 && xhr.status < 300);
      if (!f.failed && !f.aborted) f.loaded = f.size;
      draw();
    });
    draw();
  }

  var XHR = w.XMLHttpRequest;
  var origOpen = XHR.prototype.open, origSend = XHR.prototype.send;
  XHR.prototype.open = function (method, url) {
    try {
      this.__bvgUpload = String(method).toUpperCase() === "PUT" &&
        String(url).indexOf(MATCH) !== -1;
    } catch (e) { this.__bvgUpload = false; }
    return origOpen.apply(this, arguments);
  };
  XHR.prototype.send = function (body) {
    if (this.__bvgUpload) {
      try { track(this, body); } catch (e) { /* never block an upload */ }
    }
    return origSend.apply(this, arguments);
  };

  w.addEventListener("beforeunload", function (e) {
    var busy = files.some(function (f) { return !f.done; });
    if (busy) { e.preventDefault(); e.returnValue = ""; return ""; }
  });

  function isDark() {
    var app = w.document.querySelector(".stApp") || w.document.body;
    var m = (w.getComputedStyle(app).backgroundColor || "").match(/\d+/g);
    if (!m || m.length < 3) return false;
    return (0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]) < 128;
  }

  function ensurePanel() {
    if (panel && w.document.body.contains(panel)) return;
    var d = w.document;
    panel = d.createElement("div");
    panel.id = "bvg-upload-progress";
    panel.setAttribute("role", "status");
    panel.setAttribute("aria-live", "polite");
    panel.style.cssText = [
      "position:fixed", "right:16px", "bottom:16px", "z-index:1000002",
      "width:min(380px, calc(100vw - 32px))", "box-sizing:border-box",
      "padding:12px 14px", "border-radius:10px",
      "font:13px/1.4 'Source Sans Pro','Source Sans 3',system-ui,sans-serif",
      "box-shadow:0 6px 24px rgba(0,0,0,.18)", "transition:opacity .3s"
    ].join(";");
    title = d.createElement("div");
    title.style.cssText = "font-weight:600;font-size:14px;margin-bottom:6px";
    var track = d.createElement("div");
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.style.cssText = "height:8px;border-radius:4px;overflow:hidden";
    bar = d.createElement("div");
    bar.style.cssText = "height:100%;width:0;border-radius:4px;transition:width .2s";
    track.appendChild(bar);
    detail = d.createElement("div");
    detail.style.cssText = "margin-top:6px;font-variant-numeric:tabular-nums";
    current = d.createElement("div");
    current.style.cssText = "margin-top:2px;opacity:.7;white-space:nowrap;" +
      "overflow:hidden;text-overflow:ellipsis";
    panel.appendChild(title);
    panel.appendChild(track);
    panel.appendChild(detail);
    panel.appendChild(current);
    d.body.appendChild(panel);
    panel._track = track;
  }

  function paint(dark, state) {
    panel.style.background = dark ? "#262730" : "#ffffff";
    panel.style.color = dark ? "#fafafa" : "#31333f";
    panel.style.border = "1px solid " + (dark ? "#3d3f4b" : "#e6e6eb");
    panel._track.style.background = dark ? "#3d3f4b" : "#e6e6eb";
    bar.style.background = state === "failed" ? "#e5484d"
      : state === "done" ? "#2e9e5b" : "#ff4b4b";
  }

  function hide(after) {
    if (hideTimer) w.clearTimeout(hideTimer);
    hideTimer = w.setTimeout(function () {
      if (panel && files.every(function (f) { return f.done; })) {
        panel.style.opacity = "0";
        w.setTimeout(function () {
          if (panel && panel.style.opacity === "0") { panel.remove(); panel = null; }
        }, 400);
      }
    }, after);
  }

  function draw() {
    if (drawPending) return;
    drawPending = true;
    w.setTimeout(function () { drawPending = false; render(); }, 150);
  }

  function render() {
    var zh = w.__bvgLanguage === "zh-CN";
    var live = files.filter(function (f) { return !f.aborted; });
    if (!live.length) { if (panel) hide(0); return; }
    var total = 0, loaded = 0, ok = 0, failed = 0;
    live.forEach(function (f) {
      total += f.size; loaded += f.loaded;
      if (f.done && !f.failed) ok++;
      if (f.failed) failed++;
    });
    var now = Date.now();
    samples.push([now, loaded]);
    while (samples.length > 2 && now - samples[0][0] > 8000) samples.shift();

    ensurePanel();
    if (hideTimer) { w.clearTimeout(hideTimer); hideTimer = null; }
    panel.style.opacity = "1";
    var pct = total ? Math.min(100, 100 * loaded / total) : 0;
    bar.style.width = pct.toFixed(1) + "%";
    panel._track.setAttribute("aria-valuenow", String(Math.round(pct)));
    var finished = live.every(function (f) { return f.done; });

    if (finished) {
      paint(isDark(), failed ? "failed" : "done");
      title.textContent = zh
        ? (failed ? "⚠ " + live.length + " 个文件中有 " + failed + " 个上传失败"
                  : "✓ 已上传 " + ok + " 个文件")
        : (failed ? "⚠ " + failed + " of " + live.length + " file" +
          (live.length === 1 ? "" : "s") + " failed to upload"
                  : "✓ " + ok + " file" + (ok === 1 ? "" : "s") + " uploaded");
      detail.textContent = fmtBytes(loaded) + (failed
        ? (zh ? " 已到达。请移除失败文件后重新添加。"
              : " arrived. Remove the failed files and add them again.") : "");
      current.textContent = "";
      hide(failed ? 15000 : 4000);
      return;
    }

    paint(isDark(), "busy");
    // Streamlit sends several files at once, so "file 2 of 3" would be a
    // fiction — say how many are done instead.
    title.textContent = zh
      ? "正在上传 " + live.length + " 个文件" + (ok ? " · 已完成 " + ok + " 个" : "")
      : "Uploading " + live.length + " file" +
        (live.length === 1 ? "" : "s") + (ok ? " · " + ok + " done" : "");
    var text = fmtBytes(loaded) + (zh ? " / " : " of ") + fmtBytes(total) + " · " +
      Math.floor(pct) + "%";
    if (samples.length > 1) {
      var dt = (now - samples[0][0]) / 1000, db = loaded - samples[0][1];
      if (dt >= 1 && db > 0) {
        var rate = db / dt;
        text += " · " + fmtBytes(rate) + (zh ? "/秒 · 约剩 " : "/s · about ") +
          fmtTime((total - loaded) / rate) + (zh ? "" : " left");
      }
    }
    detail.textContent = text;
    var active = live.filter(function (f) { return !f.done; });
    current.textContent = active.length ? active[0].name +
      (active.length > 1 ? (zh ? "，另有 " : " and ") + (active.length - 1) +
        (zh ? " 个文件" : " more") : "") +
      (zh ? " — 请保持标签页打开，直到上传完成" : " — keep this tab open until it finishes") : "";
  }
  w.__bvgUploadProgressRender = render;
})();
"""


def install() -> None:
    """Put the upload panel on this page. Safe to call on every rerun.

    st.html runs the script in the page itself where it can (Streamlit builds
    with `unsafe_allow_javascript`). Older builds only run scripts inside a
    components iframe, so there the iframe hands the same source to its parent
    as a <script> element — same-origin, which Streamlit's component sandbox
    allows."""
    run_in_page(_SCRIPT)


def run_in_page(script: str) -> None:
    if "unsafe_allow_javascript" in inspect.signature(st.html).parameters:
        st.html(f"<script>{script}</script>", unsafe_allow_javascript=True)
        return
    from streamlit.components.v1 import html as components_html

    loader = (
        "<script>(function(){var p=window.parent;"
        "if(!p)return;"
        "var s=p.document.createElement('script');"
        f"s.textContent={json.dumps(script)};"
        "p.document.head.appendChild(s);})();</script>"
    )
    components_html(loader, height=0)
