"""Controls for named parameter presets, independent of saved job files."""

import re

import streamlit as st

import parameter_presets as parameters
import presets_ui

_FLASH = "_parameter_flash"
_PICK = "_parameter_pick"
_NAME = "_parameter_name"


def panel() -> None:
    with st.expander("Parameter presets"):
        flash = st.session_state.pop(_FLASH, None)
        if flash:
            getattr(st, flash[0])(flash[1])
        st.caption("Save settings without files or material links. Applying a preset keeps your current assets and row edits.")
        try:
            records = parameters.list_presets()
        except (OSError, ValueError) as exc:
            st.error(f"Cannot read parameter presets: {exc}")
            return
        st.text_input("Preset name", key=_NAME)
        st.button("Save new preset", on_click=_save)
        if records:
            by_id = {record["id"]: record for record in records}
            if st.session_state.get(_PICK) not in by_id:
                st.session_state[_PICK] = records[0]["id"]
            selected = st.selectbox("Saved parameter presets", list(by_id),
                                    format_func=lambda key: by_id[key]["name"], key=_PICK)
            record = by_id[selected]
            st.button("Apply preset", on_click=_apply_record, args=(record,))
            st.button("Overwrite selected", on_click=_save, args=(record,))
            filename = re.sub(r"[^\w.-]+", "_", record["name"]).strip(".") or "parameters"
            st.download_button("Download preset Excel", parameters.to_excel(record),
                               file_name=filename + ".xlsx", mime=parameters.MIME)
            with st.popover("Delete parameter preset"):
                st.write(f"Delete {record['name']}?")
                st.button("Confirm delete", on_click=_delete, args=(selected,))
        upload = st.file_uploader("Import parameter preset (.xlsx)", type=["xlsx"],
                                  key="_parameter_import")
        if upload is not None:
            try:
                name, values = _read_excel(upload.getvalue())
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"Valid preset: {name} ({len(values)} parameters)")
                st.button("Apply imported parameters", on_click=_apply_values, args=(name, values))


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
        st.session_state[_FLASH] = ("error", str(exc))
    else:
        st.session_state[_PICK] = record["id"]
        st.session_state[_FLASH] = ("success", f"Saved {record['name']}.")


def _apply_record(record: dict) -> None:
    try:
        values = parameters.record_values(record)
    except ValueError as exc:
        st.session_state[_FLASH] = ("error", str(exc))
    else:
        _apply_values(record["name"], values)


def _apply_values(name: str, values: dict) -> None:
    presets_ui.apply_parameters(values)
    st.session_state[_NAME] = name
    st.session_state[_FLASH] = ("success", f"Applied {name}. Files and row edits were kept.")


def _delete(preset_id: str) -> None:
    try:
        parameters.delete(preset_id)
    except (ValueError, OSError) as exc:
        st.session_state[_FLASH] = ("error", str(exc))
    else:
        st.session_state.pop(_PICK, None)
        st.session_state[_FLASH] = ("success", "Parameter preset deleted.")
