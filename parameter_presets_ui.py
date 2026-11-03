"""Controls for named parameter presets, independent of saved job files."""

import re

import streamlit as st

from i18n import t, option_labels

import parameter_presets as parameters
import presets_ui

_FLASH = "_parameter_flash"
_PICK = "_parameter_pick"
_NAME = "_parameter_name"


def panel() -> None:
    with st.expander(t("Parameter presets")):
        flash = st.session_state.pop(_FLASH, None)
        if flash:
            getattr(st, flash[0])(flash[1])
        st.caption(t("Save settings without files or material links. Applying a preset keeps "
                     "your current assets and row edits."))
        try:
            records = parameters.list_presets()
        except (OSError, ValueError) as exc:
            st.error(t("Cannot read parameter presets: {0}", exc))
            return
        st.text_input(t("Preset name"), key=_NAME)
        st.button(t("Save new preset"), on_click=_save, key="_ui_parameter_presets_ui_27_" + str("Save new preset"))
        if records:
            by_id = {record["id"]: record for record in records}
            if st.session_state.get(_PICK) not in by_id:
                st.session_state[_PICK] = records[0]["id"]
            selected = st.selectbox(t("Saved parameter presets"), list(by_id),
                                    format_func=option_labels(list(by_id), lambda key: by_id[key]["name"]), key=_PICK)
            record = by_id[selected]
            st.button(t("Apply preset"), on_click=_apply_record, args=(record,), key="_ui_parameter_presets_ui_35_" + str("Apply preset"))
            st.button(t("Overwrite selected"), on_click=_save, args=(record,), key="_ui_parameter_presets_ui_36_" + str("Overwrite selected"))
            filename = re.sub(r"[^\w.-]+", "_", record["name"]).strip(".") or "parameters"
            st.download_button(t("Download preset Excel"), parameters.to_excel(record),
                               file_name=filename + ".xlsx", mime=parameters.MIME, key="_ui_parameter_presets_ui_38_" + str("Download preset Excel"))
            with st.popover(t("Delete parameter preset")):
                st.write(t("Delete {0}?", record['name']))
                st.button(t("Confirm delete"), on_click=_delete, args=(selected,), key="_ui_parameter_presets_ui_42_" + str("Confirm delete"))
        upload = st.file_uploader(t("Import parameter preset (.xlsx)"), type=["xlsx"],
                                  key="_parameter_import")
        if upload is not None:
            try:
                name, values = _read_excel(upload.getvalue())
            except ValueError as exc:
                st.error(t(str(exc)))
            else:
                st.success(t("Valid preset: {0} ({1} parameters)", name, len(values)))
                st.button(t("Apply imported parameters"), on_click=_apply_values, args=(name, values), key="_ui_parameter_presets_ui_52_" + str("Apply imported parameters"))


@st.cache_data(show_spinner=False, max_entries=8)
def _read_excel(data: bytes):
    return parameters.from_excel(data)


def _save(selected: dict | None = None) -> None:
    try:
        values = parameters.validate(presets_ui.parameter_values())
        # Overwrite retains the selected name. Save new uses the name field.
        name = selected["name"] if selected else st.session_state.get(_NAME, "")
        record = parameters.save(name, values, replace_id=selected["id"] if selected else None)
    except (ValueError, OSError) as exc:
        st.session_state[_FLASH] = ("error", t(str(exc)))
    else:
        st.session_state[_PICK] = record["id"]
        st.session_state[_FLASH] = ("success", t("Saved {0}.", record['name']))


def _apply_record(record: dict) -> None:
    try:
        values = parameters.record_values(record)
    except ValueError as exc:
        st.session_state[_FLASH] = ("error", t(str(exc)))
    else:
        _apply_values(record["name"], values)


def _apply_values(name: str, values: dict) -> None:
    presets_ui.apply_parameters(values)
    st.session_state[_NAME] = name
    st.session_state[_FLASH] = ("success", t("Applied {0}. Files and row edits were kept.", name))


def _delete(preset_id: str) -> None:
    try:
        parameters.delete(preset_id)
    except (ValueError, OSError) as exc:
        st.session_state[_FLASH] = ("error", t(str(exc)))
    else:
        st.session_state.pop(_PICK, None)
        st.session_state[_FLASH] = ("success", t("Parameter preset deleted."))
