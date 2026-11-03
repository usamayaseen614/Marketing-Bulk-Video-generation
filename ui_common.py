"""
ui_common.py — Streamlit helpers shared by app.py and the pages/.

Anything importing streamlit lives here rather than in results.py or
workspace.py, which the headless worker imports.
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

import streamlit as st
from streamlit import config as st_config

import config

# When static serving is enabled (production/Docker), oversized ZIPs are
# published here and streamed from disk by Tornado instead of being buffered in
# Python memory by st.download_button.
#
# The path itself lives in config so the headless worker can clean these up
# when it purges the job they belong to — it cannot import this module.
STATIC_DOWNLOADS = config.STATIC_DOWNLOADS

# Above this size the ZIP is not loaded into memory for the download button.
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024 * 1024


def get_session_id() -> str:
    """Stable id for this browser session."""
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = uuid.uuid4().hex[:12]
    return st.session_state["session_id"]


def static_serving_enabled() -> bool:
    try:
        return bool(st_config.get_option("server.enableStaticServing"))
    except Exception:  # noqa: BLE001 — option missing on old Streamlit
        return False


def ensure_static_dir() -> None:
    if static_serving_enabled():
        STATIC_DOWNLOADS.mkdir(parents=True, exist_ok=True)


# Files at or below this download in one click, with their bytes cached.
# Above it, the download is gated behind an explicit click instead — see
# offer_file_download.
INLINE_DOWNLOAD_LIMIT = 25 * 1024 * 1024


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def human_bytes(n: float) -> str:
    """1.2 GB, 340 MB, 12 KB — for progress text, where _human_size's MB
    ceiling reads badly on an 18 GB promo set."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class ByteProgress:
    """An st.progress bar driven by byte counts.

    The instance is the callback: pass it as `progress=` to
    workspace.stage_uploads or presets.save, which call it with each chunk's
    size. Redraws are throttled — an 18 GB staging reports in 8 MB chunks, and
    two thousand websocket messages in a minute make the page lag behind the
    work it is describing."""

    _MIN_REDRAW_SECONDS = 0.25

    def __init__(self, total: int, text: str):
        self.total = max(0, int(total))
        self.done = 0
        self.text = text
        self._started = time.monotonic()
        self._last_draw = 0.0
        self._bar = st.progress(0.0, text=self._label())

    def __call__(self, n_bytes: int) -> None:
        self.done += int(n_bytes)
        self._draw()

    def phase(self, text: str) -> None:
        """Change the caption without resetting the count (one bar, several
        steps)."""
        self.text = text
        self._draw(force=True)

    def clear(self) -> None:
        self._bar.empty()

    def _draw(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_draw < self._MIN_REDRAW_SECONDS:
            return
        self._last_draw = now
        fraction = 1.0 if not self.total else min(1.0, self.done / self.total)
        self._bar.progress(fraction, text=self._label())

    def _label(self) -> str:
        if not self.total:
            return self.text
        pct = min(100, int(100 * self.done / self.total))
        label = (f"{self.text} — {human_bytes(self.done)} of "
                 f"{human_bytes(self.total)} ({pct}%)")
        elapsed = time.monotonic() - self._started
        if self.done and elapsed > 2 and self.done < self.total:
            rate = self.done / elapsed
            label += (f" · {human_bytes(rate)}/s · about "
                      f"{_human_duration((self.total - self.done) / rate)} left")
        return label


def _human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


@st.cache_data(show_spinner=False, max_entries=16)
def _cached_bytes(path: str, mtime: float, size: int) -> bytes:
    """Read a file once and keep it.

    st.download_button needs its payload at render time, and the Jobs page
    reruns on a 5-second timer — so without this, every finished job's file was
    re-read on every rerun. Keyed on mtime and size so a rebuilt file is picked
    up rather than served stale."""
    return Path(path).read_bytes()


def offer_file_download(path: Path, slug: str, label: str,
                        mime: str = "application/octet-stream",
                        file_name: str | None = None) -> None:
    """Download control for a job artifact.

    Small files (manifests, caption sheets) download on ONE click, with their
    bytes cached so repeated reruns cost nothing. Only genuinely large files
    are gated behind a confirm step, because reading a multi-GB ZIP into memory
    is worth an extra click and a few kilobytes is not."""
    path = Path(path)
    if not path.is_file():
        return
    stat = path.stat()
    size_label = _human_size(stat.st_size)

    if stat.st_size <= INLINE_DOWNLOAD_LIMIT:
        st.download_button(
            f"{label} ({size_label})",
            data=_cached_bytes(str(path), stat.st_mtime, stat.st_size),
            file_name=file_name or path.name, mime=mime,
            key=f"dl_{slug}",
        )
        return

    state_key = f"want_dl_{slug}"
    if not st.session_state.get(state_key):
        if st.button(f"{label} ({size_label})", key=f"ask_{slug}"):
            st.session_state[state_key] = True
            st.rerun()
        return
    st.download_button(
        label, data=path.read_bytes(),
        file_name=file_name or path.name, mime=mime,
        key=f"dlbig_{slug}",
    )


def offer_zip_download(zip_path: Path, slug: str, label: str = "⬇️ Download all videos (ZIP)") -> None:
    """Download control for a finished batch.

    st.download_button buffers the whole file in memory per click, which is too
    risky for multi-GB batches — above the threshold the ZIP is published under
    ./static and streamed from disk by Tornado instead."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return
    size = zip_path.stat().st_size
    size_mb = size / 1024 / 1024

    if size <= MAX_DOWNLOAD_BYTES:
        # Gated, so a ZIP is never read (or re-offered to the browser) until
        # it is actually wanted — see offer_file_download.
        state_key = f"want_zip_{slug}"
        if not st.session_state.get(state_key):
            if st.button(f"{label} ({size_mb:.1f} MB)", key=f"askzip_{slug}",
                         type="primary"):
                st.session_state[state_key] = True
                st.rerun()
            return
        st.download_button(
            label,
            data=zip_path.read_bytes(),
            file_name=zip_path.name,
            mime="application/zip",
            type="primary",
            key=f"dlzip_{slug}",
        )
        return

    if static_serving_enabled():
        dest = STATIC_DOWNLOADS / slug / zip_path.name
        if zip_path != dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(zip_path), dest)
        st.caption(f"ZIP size: {size_mb:.1f} MB")
        st.markdown(f"### [{label}](/app/static/downloads/{slug}/{dest.name})")
    else:
        st.info(
            "The ZIP is too large to stream through the browser reliably. "
            f"Grab it directly from disk:\n\n`{zip_path}`"
        )
