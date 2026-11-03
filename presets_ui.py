"""
presets_ui.py — the main page's side of presets.py.

Three jobs:

  * Recording form fields through keep(), including retained values for
    controls hidden by a mode or toggle.
  * The "Reuse a previous job" panel: a dropdown of saved jobs with Load and
    Delete, and a way back to an empty form.
  * Saved files standing in for uploads. Streamlit cannot put a file INTO an
    uploader, so a loaded job's files sit beside each uploader instead, and
    with_saved() hands them to the rest of app.py wherever nothing new was
    uploaded. Uploading into that slot replaces them.

Loading works through Streamlit's session state: every saved value is written
under its widget's key from a button callback, which runs before the next
script run, so each widget comes up already showing it. Widgets therefore need
keys — and a keyed widget no longer resets when its default changes, which is
what follow_default() is for.

Every key carries a counter that Load and Clear bump, so each of them rebuilds
the whole form as brand-new widgets. Parameter-only loading has a separate
counter for settings, leaving uploaders and task information in place.
Deleting a widget's state is NOT enough:
Streamlit leaves the browser showing the old value and sends it straight back
on the next click, so a "cleared" field would look and act uncleared. Saved
values are stored under the plain name (`music_volume`), never the counted
key, which is why app.py reads a field through value() rather than
st.session_state directly.
"""

from __future__ import annotations

import time

import streamlit as st

from i18n import t, option_labels

import config
import presets
import parameter_presets
import generation_defaults as defaults
from ui_common import human_bytes

_FIELDS = "_preset_fields"      # names registered by keep() during this run
_LOADED = "_preset_loaded"      # {"id", "name"} of the saved job on the form
_FILES = "_preset_files"        # slot -> list[presets.StoredFile]
_NONCE = "_form_nonce"          # bumped to rebuild every widget at once
_FLASH = "_preset_flash"        # (kind, message) shown on the next run
_PICK = "_preset_pick"          # the dropdown's own key
_FOLLOW = "_follows_default__"  # prefix for follow_default()'s markers
_PARAMETER_VALUES = "_parameter_values"
_PARAMETER_NONCE = "_parameter_nonce"
_PARAMETERS = parameter_presets.parameters()
_KEPT_VALUES = "_kept_values"
_UPLOAD_SLOTS = "_upload_slots"
_KEPT_UPLOADS = "_kept_uploads"

# Session state tied to the sheet and preview on screen. Replacing the form
# replaces the sheet, so all of it goes — the saved sheet already carries any
# preview-editor edits written into its cells.
_SHEET_STATE = ("row_edits", "preview_payload", "preview_nonce", "preview_row",
                "preview_baseline_edits", "preview_promo", "row_render",
                "excel_file_key")

# How each upload slot is described in the panel. Slots numbered or named at
# run time (clip slots, per-promo text sheets) are matched by prefix below.
_SLOT_LABELS = {
    "excel": ("Excel sheet", "Excel sheets"),
    "promos": ("promo video", "promo videos"),
    "bg_zip": ("background ZIP", "background ZIPs"),
    "cta_image": ("CTA image", "CTA images"),
    "font": ("custom font", "custom fonts"),
    "gif_pool": ("GIF", "GIFs"),
    "bg_video_pool": ("background video", "background videos"),
    "music_pool": ("music track", "music tracks"),
    "promo_alt_pool": ("Promo Alternate clip", "Promo Alternate clips"),
    "scripts": ("script pool", "script pools"),
    "hashtags": ("hashtag sheet", "hashtag sheets"),
}


# --------------------------------------------------------------------------- fields

def start_run() -> None:
    """Call once at the top of every run, before any widget."""
    # Capture fields before a changed toggle hides them and Streamlit cleans
    # up their widget state at the end of this run.
    st.session_state[_PARAMETER_VALUES] = parameter_values()
    kept = st.session_state.setdefault(_KEPT_VALUES, {})
    for name in st.session_state.get(_FIELDS, []):
        if name not in _PARAMETERS and _key(name) in st.session_state:
            kept[name] = st.session_state[_key(name)]
    st.session_state[_FIELDS] = []


def _key(name: str) -> str:
    """The widget key for a field or uploader in the current form generation.
    Plain until the first Load or Clear, so an ordinary session keeps the
    names it always had."""
    nonce = st.session_state.get(_NONCE, 0)
    key = f"{name}__{nonce}" if nonce else name
    parameter_nonce = st.session_state.get(_PARAMETER_NONCE, 0)
    if name in _PARAMETERS and parameter_nonce:
        key += f"__params_{parameter_nonce}"
    return key


def keep(name: str) -> str:
    """Mark a widget as part of a job's saved settings and return its key, so
    it reads inline: `st.checkbox(..., key=keep("split_audio"))`."""
    fields = st.session_state.setdefault(_FIELDS, [])
    if name not in fields:
        fields.append(name)
    key = _key(name)
    if key not in st.session_state:
        if name in _PARAMETERS:
            st.session_state[key] = st.session_state.get(
                _PARAMETER_VALUES, {}).get(name, _PARAMETERS[name].default)
        elif name in st.session_state.get(_KEPT_VALUES, {}):
            st.session_state[key] = st.session_state[_KEPT_VALUES][name]
        elif name in defaults.MATERIAL_FOLDERS:
            st.session_state[key] = defaults.MATERIAL_FOLDERS[name]
    return key


def value(name: str, default=None):
    """A kept field's current value, for code that needs it before (or apart
    from) the widget call — the sidebar reading the Promo Alternate tick."""
    return st.session_state.get(_key(name), default)


def follow_default(name: str, default) -> None:
    """Make a kept widget reset to its default whenever that default changes.

    Before widgets were keyed, "Batches to render" re-defaulted to the promo
    count on every new upload, because Streamlit treated a changed default as
    a new widget. A keyed widget keeps its value instead, so that behaviour is
    put back by hand. A run after Load has no marker yet, so the loaded value
    is left alone rather than overwritten by the new default."""
    marker = _FOLLOW + name
    previous = st.session_state.get(marker)
    st.session_state[marker] = default
    if (_key(name) not in st.session_state
            and name not in st.session_state.get(_PARAMETER_VALUES, {})) or (
            previous is not None and previous != default):
        st.session_state[_key(name)] = default


def current_values() -> dict:
    """Current and retained hidden fields, under stable names."""
    return {**st.session_state.get(_KEPT_VALUES, {}), **parameter_values(), **{
        name: st.session_state[_key(name)]
        for name in st.session_state.get(_FIELDS, [])
        if _key(name) in st.session_state}}


def parameter_values() -> dict:
    values = dict(st.session_state.get(_PARAMETER_VALUES, {}))
    for name in _PARAMETERS:
        if _key(name) in st.session_state:
            values[name] = st.session_state[_key(name)]
    return values


def apply_parameters(values: dict) -> None:
    """Apply validated settings without replacing uploaders or sheet edits."""
    # Changing slot count or mode may hide an uploader. Preserve its files
    # before Streamlit discards the hidden widget at the end of the run.
    kept = st.session_state.setdefault(_KEPT_UPLOADS, {})
    for slot in st.session_state.get(_UPLOAD_SLOTS, []):
        uploaded = st.session_state.get(_key(slot))
        if uploaded:
            kept[slot] = uploaded if isinstance(uploaded, list) else [uploaded]
    for name in _PARAMETERS:
        st.session_state.pop(_key(name), None)
    st.session_state[_PARAMETER_NONCE] = int(st.session_state.get(_PARAMETER_NONCE, 0)) + 1
    st.session_state[_PARAMETER_VALUES] = dict(values)
    for name, saved in values.items():
        st.session_state[_key(name)] = saved
    for key in list(st.session_state):
        if str(key).startswith(_FOLLOW):
            st.session_state.pop(key, None)
    for key in _SHEET_STATE:
        if key not in ("row_edits", "excel_file_key"):
            st.session_state.pop(key, None)
    st.session_state.pop("sample_script", None)


# --------------------------------------------------------------------------- files

def uploader_key(name: str) -> str:
    """Key for a file uploader. Changing it is also the only way to empty an
    uploader, which Load and Clear do to all of them at once."""
    slots = st.session_state.setdefault(_UPLOAD_SLOTS, [])
    if name not in slots:
        slots.append(name)
    return _key(name)


def upload_changed(slot: str) -> None:
    # Removing/replacing an upload must not resurrect the pre-preset copy.
    st.session_state.get(_KEPT_UPLOADS, {}).pop(slot, None)


def has_saved(slot: str) -> bool:
    return bool((st.session_state.get(_KEPT_UPLOADS) or {}).get(slot)
                or (st.session_state.get(_FILES) or {}).get(slot))


def with_saved(slot: str, uploaded, *, multiple: bool, where=None):
    """The files for one upload slot: whatever was uploaded, or else the
    loaded saved job's. Draws a note under the uploader while saved files are
    standing in, with a button to stop using them."""
    if (multiple and uploaded) or (not multiple and uploaded is not None):
        return uploaded
    kept = (st.session_state.get(_KEPT_UPLOADS) or {}).get(slot) or []
    stored = kept or (st.session_state.get(_FILES) or {}).get(slot) or []
    if not stored:
        return uploaded
    where = where or st
    loaded = st.session_state.get(_LOADED) or {}
    size = human_bytes(sum(f.size for f in stored))
    what = (f"`{stored[0].name}`" if len(stored) == 1
            else t("{0} files", len(stored)))
    source = t("current uploads") if kept else loaded.get('name', t('a saved job'))
    where.caption(t("📎 Using {0} ({1}) from **{2}** — upload here to replace.", what, size, source))
    where.button(t("✕ Don't use"), key=f"_drop_saved_{slot}", type="tertiary",
                 on_click=_drop_slot, args=(slot,),
                 help=t("Stop using the saved file(s) for this one upload."))
    return list(stored) if multiple else stored[0]


def _drop_slot(slot: str) -> None:
    st.session_state.get(_KEPT_UPLOADS, {}).pop(slot, None)
    files = dict(st.session_state.get(_FILES) or {})
    files.pop(slot, None)
    st.session_state[_FILES] = files


# --------------------------------------------------------------------------- save

def save_job(name: str, files: dict, *, summary: dict, job_id: str,
             progress=None) -> dict:
    """Save the job just queued: every registered field plus its files."""
    return presets.save(name, current_values(), files, summary=summary,
                        job_id=job_id, progress=progress)


# --------------------------------------------------------------------------- panel

def _slot_order(slot: str) -> int:
    order = list(_SLOT_LABELS)
    return order.index(slot) if slot in order else len(order)


def _describe_files(record: dict) -> str:
    parts = []
    clip_slots = grids = 0
    clip_files = 0
    stored = record.get("files") or {}
    for slot in sorted(stored, key=_slot_order):
        entries = stored[slot]
        n = len(entries or [])
        if slot.startswith("cta_clip_"):
            clip_slots += 1
            clip_files += n
        elif slot.startswith("text_grid_"):
            grids += 1
        elif slot == "excel" and entries:
            parts.append(f"`{entries[0].get('name')}`")
        elif slot in _SLOT_LABELS:
            one, many = map(t, _SLOT_LABELS[slot])
            parts.append(f"{n} {one if n == 1 else many}")
    if clip_files:
        parts.append(t("{0} CTA clip(s) in {1} slot(s)", clip_files, clip_slots))
    if grids:
        parts.append(t("{0} per-promo text sheet(s)", grids))
    return " · ".join(parts) or t("no files")


def _label(record: dict) -> str:
    summary = record.get("summary") or {}
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(record.get("created_at") or 0)))
    bits = [f"{record.get('name') or t('Untitled')} — {when}"]
    if summary.get("videos"):
        bits.append(t("{0} videos", f"{int(summary['videos']):,}"))
    bits.append(t("{0} files, {1}", presets.file_count(record), human_bytes(presets.total_size(record))))
    return " · ".join(bits)


def _reset_form() -> None:
    """Everything back to defaults: fields, uploaders, saved files, sheet.

    Bumping the counter is what actually resets the widgets (see the module
    docstring); dropping the old generation's state just stops it lingering."""
    for name in st.session_state.get(_FIELDS, []):
        st.session_state.pop(_key(name), None)
    for key in list(st.session_state.keys()):
        if str(key).startswith(_FOLLOW):
            st.session_state.pop(key, None)
    for key in _SHEET_STATE:
        st.session_state.pop(key, None)
    st.session_state[_NONCE] = int(st.session_state.get(_NONCE, 0)) + 1
    st.session_state.pop(_PARAMETER_VALUES, None)
    st.session_state.pop(_PARAMETER_NONCE, None)
    st.session_state.pop(_KEPT_VALUES, None)
    st.session_state.pop(_KEPT_UPLOADS, None)
    st.session_state.pop(_FILES, None)
    st.session_state.pop(_LOADED, None)


def _load(preset_id: str) -> None:
    record = presets.get(preset_id)
    if record is None:
        st.session_state[_FLASH] = ("error", t("That saved job no longer exists."))
        return
    _reset_form()
    for name, saved in (record.get("values") or {}).items():
        st.session_state[_key(name)] = saved
    st.session_state[_PARAMETER_VALUES] = {
        name: saved for name, saved in (record.get("values") or {}).items()
        if name in _PARAMETERS}
    st.session_state[_KEPT_VALUES] = {
        name: saved for name, saved in (record.get("values") or {}).items()
        if name not in _PARAMETERS}
    files, missing = presets.load(record)
    st.session_state[_FILES] = files
    st.session_state[_LOADED] = {"id": record["id"],
                                 "name": record.get("name") or t("Untitled")}
    if missing:
        st.session_state[_FLASH] = (
            "warning",
            t("Loaded **{0}**, but {1} of its files are no longer on the server and need uploading again: ", record.get('name'), len(missing))
            + t(", ".join(f"`{n}`" for n in missing[:10]))
            + (" …" if len(missing) > 10 else ""))


def _delete(preset_id: str) -> None:
    record = presets.get(preset_id) or {}
    name = record.get("name") or t("that saved job")
    presets.delete(preset_id)
    st.session_state.pop(_PICK, None)
    loaded = st.session_state.get(_LOADED) or {}
    if loaded.get("id") == preset_id:
        # Its files may be collected from disk at any point now, so the form
        # must stop pointing at them. The field values are harmless and stay.
        st.session_state.pop(_FILES, None)
        st.session_state.pop(_LOADED, None)
        st.session_state[_FLASH] = (
            "info", t("Deleted **{0}**. Its files were on this form, so they have been taken off"
                      " it — upload them again to use them.", name))
    else:
        st.session_state[_FLASH] = ("success", t("Deleted **{0}**.", name))


def _clear() -> None:
    _reset_form()
    st.session_state[_FLASH] = ("success", t("Form cleared."))


def panel() -> None:
    """The "Reuse a previous job" panel. Put it above the form."""
    flash = st.session_state.pop(_FLASH, None)
    records = presets.list_presets()
    loaded = st.session_state.get(_LOADED)
    with st.expander(t("♻️ Reuse a previous job — its settings and files"),
                     expanded=bool(loaded or flash)):
        if flash:
            getattr(st, flash[0], st.info)(flash[1])
        if not records:
            st.caption(t("Nothing saved yet. Every batch you queue is saved here — "
                       "every setting and every file — so a later batch can "
                       "start from it instead of from scratch."))
        else:
            by_id = {r["id"]: r for r in records}
            ids = list(by_id)
            if st.session_state.get(_PICK) not in by_id:
                st.session_state[_PICK] = (loaded or {}).get("id") \
                    if (loaded or {}).get("id") in by_id else ids[0]
            col_pick, col_load, col_del = st.columns(
                [5, 1.2, 1.2], vertical_alignment="bottom")
            pick = col_pick.selectbox(
                t("Previous jobs (newest first)"), ids, key=_PICK,
                format_func=option_labels(ids, lambda i: t(_label(by_id[i]))),
                help=t("Load puts back every setting this job was queued with "
                     "and uses its files wherever you don't upload new ones."))
            col_load.button(t("Load"), type="primary", width="stretch",
                            on_click=_load, args=(pick,),
                            help=t("Replaces everything on this form."), key="_ui_presets_ui_390_" + str("Load"))
            with col_del.popover(t("🗑️ Delete")):
                record = by_id[pick]
                st.markdown(
                    t("Delete **{0}** and its {1} saved file(s)? This can't be undone. Files "
                      "another saved job also uses are kept.", record.get('name'), presets.file_count(record)))
                st.button(t("Delete it"), type="primary", key="_preset_delete",
                          on_click=_delete, args=(pick,))
            st.caption(t("Files: ") + t(_describe_files(by_id[pick])))
            st.caption(
                t("{0} saved job(s) using {1} on the server", len(records), human_bytes(presets.disk_usage()))
                + (t(" · the newest {0} are kept", config.PRESETS_MAX)
                   if config.PRESETS_MAX else ""))
        if loaded:
            st.info(t("This form was loaded from **{0}**. Its files are used wherever you don't "
                      "upload a new one (📎 marks them). Change anything you like, then generate "
                      "as usual.", loaded['name']))
            st.button(t("↺ Clear the form"), on_click=_clear,
                      help=t("Every field back to its default, every upload and "
                           "saved file removed."), key="_ui_presets_ui_411_" + str("↺ Clear the form"))
