"""Drive the actual generation page and preset callbacks with Streamlit."""
import io
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="parameterui_")

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

import generation_defaults as defaults
import parameter_presets as pp
import presets

_app_dir = tempfile.TemporaryDirectory(prefix="parameterui_app_")
_app_path = Path(_app_dir.name) / "app.py"
(_app_path.parent / "pages").symlink_to(ROOT / "pages", target_is_directory=True)
_app_path.write_text(
    (ROOT / "app.py").read_text()
    + '\nst.session_state["_test_render_config"] = asdict(config)\n'
    + 'st.session_state["_test_cta_slots"] = [[f.name for f in slot] for slot in cta_video_slot_files]\n',
    encoding="utf-8")
at = AppTest.from_file(str(_app_path), default_timeout=30).run()


def control(kind, label):
    return next(item for item in getattr(at, kind) if item.label == label)


def run():
    at.run()
    assert not at.exception, [(e.message, e.stack_trace) for e in at.exception]


assert not at.exception
assert control("slider", "Background video opacity (%)").value == 10
assert control("slider", "Parallel renders").value == defaults.max_workers()
assert control("selectbox", "Publish which names?").value == "yt"
assert control("radio", "Hashtags").value == "none"
assert control("text_input", "GIF folder — Drive link").value == defaults.MATERIAL_FOLDERS["gifs_drive"]

control("slider", "Background video opacity (%)").set_value(15)
control("number_input", "Minimum seconds per background video").set_value(12.5)
control("text_input", "Preset name").set_value("Campaign")
control("button", "Save new preset").click()
run()
record = pp.list_presets()[0]
assert record["values"]["bg_video_opacity"] == 15
assert record["values"]["bg_video_min_seconds"] == 12.5
assert len(record["values"]) == len(pp.parameters())
assert presets.list_presets() == [], "saving a preset must not queue/save a job"

control("slider", "Chunks").set_value(6)
run()
control("checkbox", "Split the audio track (random speed)").uncheck()
run()
control("checkbox", "Split the audio track (random speed)").check()
run()
assert control("slider", "Chunks").value == 6

control("radio", "Layout mode").set_value("split")
run()
control("number_input", "Panel height (px)").set_value(1200)
control("checkbox", "Swap left / right").check()
run()
control("radio", "Layout mode").set_value("free")
run()
control("button", "Overwrite selected").click()
run()
record = pp.list_presets()[0]
assert record["values"]["split_panel_h"] == 1200
assert record["values"]["swap_sides"] is True
assert record["values"]["split_audio_chunks"] == 6

# Source links survive being hidden when a parameter changes the source mode.
custom_link = "https://drive.google.com/drive/folders/custom"
control("text_input", "GIF folder — Drive link").set_value(custom_link)
run()
control("radio", "Where do the GIFs come from?").set_value("upload")
run()
control("radio", "Where do the GIFs come from?").set_value("drive_folder")
run()
assert control("text_input", "GIF folder — Drive link").value == custom_link

control("slider", "Background video opacity (%)").set_value(7)
run()
at.session_state["row_edits"] = {1: {"Headline_X": 300}}
at.session_state["excel_file_key"] = "kept-sheet"
at.session_state["preview_payload"] = {"stale": True}
upload_ids = [u.proto.id for u in at.get("file_uploader")]
control("button", "Apply preset").click()
run()
assert control("slider", "Background video opacity (%)").value == 15
assert control("number_input", "Minimum seconds per background video").value == 12.5
assert control("text_input", "GIF folder — Drive link").value == custom_link
assert at.session_state["row_edits"] == {1: {"Headline_X": 300}}
assert at.session_state["excel_file_key"] == "kept-sheet"
assert "preview_payload" not in at.session_state
assert [u.proto.id for u in at.get("file_uploader")] == upload_ids
assert at.session_state["_test_render_config"]["bg_video_opacity"] == .15
assert at.session_state["_test_render_config"]["bg_video_min_seconds"] == 12.5

# Load a real saved-job record with files, then apply parameters over it.
wb = Workbook()
wb.active.append(["Headline"])
wb.active.append(["Example"])
buf = io.BytesIO()
wb.save(buf)
history = presets.save("Earlier job", {
    "bg_video_opacity": 25, "bg_video_min_seconds": 30.0,
    "batch_label": "Earlier batch", "n_batches": 3, "n_folders": 2,
    "gif_source": "upload", "bg_image_source": "upload",
    "bg_video_source": "upload", "music_source": "upload",
}, {"excel": [presets.MemoryFile("data.xlsx", buf.getvalue())],
    "promos": [presets.MemoryFile("promo.mp4", b"fixture-not-rendered")]})
run()
control("button", "Load").click()
run()
assert control("slider", "Background video opacity (%)").value == 25
assert control("number_input", "Batches to render").value == 3
assert control("number_input", "Output folders").value == 2
assert control("text_input", "Batch name (optional)").value == "Earlier batch"
files_before = at.session_state["_preset_files"]
at.session_state["row_edits"] = {1: {"Headline_X": 300}}
control("button", "Apply preset").click()
run()
assert control("slider", "Background video opacity (%)").value == 15
assert control("text_input", "Batch name (optional)").value == "Earlier batch"
assert at.session_state["_preset_files"] == files_before
assert at.session_state["row_edits"] == {1: {"Headline_X": 300}}
control("button", "Load").click()
run()
assert control("slider", "Background video opacity (%)").value == 25
assert at.session_state.get("row_edits", {}) == {}

# An old/partial preset fills missing values; apply never queues a new job.
partial = pp.save("Partial", {"bg_video_opacity": 17})
run()
control("selectbox", "Saved parameter presets").select(partial["id"])
run()
control("button", "Apply preset").click()
run()
assert control("slider", "Background video opacity (%)").value == 17
assert control("slider", "Chunks").value == 4

# Exercise the real import uploader, validator and Apply callback together.
imported = pp.validate({"bg_video_opacity": 22, "cta_slot_count": 2,
                        "cta_speed_5": 1.75, "n_batches": 5, "n_folders": 4})
excel = pp.to_excel({"name": "Imported", "values": imported})
control("file_uploader", "Clip 5 — sample videos (MP4)").set_value(
    [("five.mp4", b"five-bytes", "video/mp4")])
run()
control("file_uploader", "Import parameter preset (.xlsx)").set_value(
    ("settings.xlsx", excel, pp.MIME))
run()
control("button", "Apply imported parameters").click()
run()
assert control("slider", "Background video opacity (%)").value == 22
assert control("number_input", "Batches to render").value == 5
assert control("number_input", "Output folders").value == 4
assert len(pp.list_presets()) == 2, "import must not automatically save"
assert at.session_state["_kept_uploads"]["cta_clip_5"][0].getvalue() == b"five-bytes"
run()
assert control("number_input", "Output folders").value == 4
control("number_input", "Number of clip slots").set_value(5)
run()
assert at.session_state["_test_cta_slots"][4] == ["five.mp4"]
assert control("number_input", "Clip 5 speed (×)").value == 1.75
at.button(key="_drop_saved_cta_clip_5").click()
run()
assert at.session_state["_test_cta_slots"][4] == []

control("file_uploader", "Import parameter preset (.xlsx)").set_value(
    ("broken.xlsx", b"not an Excel file", pp.MIME))
run()
assert any("Cannot read" in error.value for error in at.error)
assert not any(b.label == "Apply imported parameters" for b in at.button)
assert control("slider", "Background video opacity (%)").value == 22

from jobs import store
assert store.list_jobs() == []
assert not list(Path(os.environ["BVG_JOBS_ROOT"]).glob("*/assets"))
print("ALL PARAMETER UI TESTS PASSED")
