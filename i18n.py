"""Presentation-only translations; stored parameter values stay language neutral."""

from functools import lru_cache
import json
from pathlib import Path

import streamlit as st


@lru_cache(maxsize=1)
def chinese_catalog() -> dict[str, str]:
    return json.loads((Path(__file__).parent / "locales" / "zh-CN.json").read_text(encoding="utf-8"))


def language() -> str:
    if "ui_language" not in st.session_state:
        requested = st.query_params.get("lang", "en")
        st.session_state["ui_language"] = requested if requested in ("en", "zh-CN") else "en"
    return st.session_state["ui_language"]


def t(message, *values):
    if not isinstance(message, str):
        return message
    translated = chinese_catalog().get(message, message) if language() == "zh-CN" else message
    return translated.format(*values) if values else translated


def localize_format(format_func):
    return lambda value: t(format_func(value))


def option_labels(options, formatter):
    # Freeze display text for this render; callbacks may run outside its context.
    labels = {value: formatter(value) for value in options}
    return labels.__getitem__


def language_header() -> None:
    current = language()
    if st.query_params.get("lang") != current:
        st.query_params["lang"] = current
    from upload_progress import run_in_page

    messages = {text: t(text) for text in _NATIVE_TEXT}
    run_in_page("window.__bvgLanguage = " + json.dumps(current) + ";"
                + "window.__bvgNativeMessages = " + json.dumps(messages) + ";"
                + _NATIVE_SCRIPT)
    st.markdown(
        "<style>.st-key-language_switch {position:fixed;top:0.6rem;right:1.2rem;"
        "width:auto;z-index:1000001}</style>",
        unsafe_allow_html=True,
    )
    with st.container(key="language_switch"):
        st.button("简体中文" if current == "en" else "English", key="switch_language",
                  on_click=_switch_language,
                  help="切换到简体中文" if current == "en" else "Switch to English")
    for page, label, icon in (
        ("app.py", "Generate", "🎬"),
        ("pages/1_Jobs.py", "Jobs", "📋"),
        ("pages/2_Scraper.py", "Scraper", "🎵"),
        ("pages/3_Setup.py", "Setup", "🔧"),
    ):
        st.sidebar.page_link(page, label=t(label), icon=icon)


def _switch_language() -> None:
    selected = "zh-CN" if language() == "en" else "en"
    st.session_state["ui_language"] = selected
    st.query_params["lang"] = selected


_NATIVE_TEXT = (
    "Drag and drop files here", "Drag and drop file here", "Browse files",
    "Limit {0} per file", "Press Enter to apply", "Press Ctrl+Enter to apply",
    "Choose an option", "Choose options", "No results", "No options to select", "Upload", "{0} per file",
)

_NATIVE_SCRIPT = r"""
(function () {
  const w = window;
  document.documentElement.lang = w.__bvgLanguage;
  if (w.__bvgTranslateNative) { w.__bvgTranslateNative(); return; }
  function translate() {
    const roots = document.querySelectorAll(
      '[data-testid="stFileUploaderDropzone"], [data-testid="InputInstructions"], '
      + '[data-testid="stSelectbox"], [data-testid="stMultiSelect"]');
    roots.forEach(root => {
      root.querySelectorAll('input[placeholder]').forEach(input => {
        const original = input.__bvgPlaceholder || input.placeholder;
        const value = w.__bvgNativeMessages[original];
        if (value !== undefined && input.placeholder !== value) {
          input.__bvgPlaceholder = original;
          input.placeholder = value;
        }
      });
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const original = node.__bvgOriginal || node.textContent;
        const match = original.match(/^(Limit )?(.+?) per file(.*)$/);
        let value = w.__bvgNativeMessages[original];
        if (match) value = w.__bvgNativeMessages[match[1] ? 'Limit {0} per file' : '{0} per file']
          .replace('{0}', match[2]) + match[3];
        if (value !== undefined && node.textContent !== value) {
          node.__bvgOriginal = original;
          node.textContent = value;
        }
      }
    });
  }
  w.__bvgTranslateNative = translate;
  new MutationObserver(translate).observe(document.body,
    {childList: true, subtree: true, characterData: true});
  translate();
})();
"""


_STAGE_PREFIXES = (
    "downloading promo videos from Drive", "downloading promo videos",
    "downloading background images from Drive", "downloading background videos from Drive",
    "downloading clips from Drive", "downloading GIFs from Drive", "downloading music from Drive",
    "downloading background images", "downloading background videos", "downloading clips",
    "downloading GIFs", "downloading music", "generating voiceovers", "generating captions",
    "building metadata", "building manifests", "writing scripts", "preparing", "enumerating",
    "downloading", "generating", "rendering", "uploading", "packaging", "packing", "scraping", "captions",
)


def stage_label(stage: str) -> str:
    for prefix in _STAGE_PREFIXES:
        if stage == prefix or stage.startswith(prefix + " "):
            return t(prefix) + stage[len(prefix):]
    return stage
