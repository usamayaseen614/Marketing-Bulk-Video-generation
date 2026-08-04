"""
ui_common.py — Streamlit helpers shared by app.py and the pages/.

Anything importing streamlit lives here rather than in results.py or
workspace.py, which the headless worker imports.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import streamlit as st
from streamlit import config as st_config

# When static serving is enabled (production/Docker), oversized ZIPs are
# published here and streamed from disk by Tornado instead of being buffered in
# Python memory by st.download_button.
STATIC_ROOT = Path(__file__).parent / "static"
STATIC_DOWNLOADS = STATIC_ROOT / "downloads"

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
        st.caption(f"ZIP size: {size_mb:.1f} MB")
        st.download_button(
            label,
            data=zip_path.open("rb"),
            file_name="marketing_videos.zip",
            mime="application/zip",
            type="primary",
            key=f"dl_{slug}",
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
