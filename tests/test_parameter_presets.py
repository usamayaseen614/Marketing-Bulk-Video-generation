"""Parameter presets: real workbook edits, persistence and shared defaults."""
import ast
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="parameterpresets_")
os.environ["BVG_PRESETS_MAX"] = "1"

from openpyxl import load_workbook

import config
import generation_defaults as defaults
import parameter_presets as pp
import presets
from video_generator import RenderConfig

values = pp.validate({})
cfg = RenderConfig()
assert values["bg_video_opacity"] == 10 and cfg.bg_video_opacity == .10
assert values["bg_video_min_seconds"] == cfg.bg_video_min_seconds == 20.0
assert values["music_volume"] == 10 and cfg.music_volume == .10
assert values["music_min_seconds"] == cfg.music_min_seconds == 20.0
assert values["split_audio"] is cfg.split_audio is True
assert values["split_audio_chunks"] == cfg.split_audio_chunks == 4
assert values["split_audio_spread"] == 10 and cfg.split_audio_spread == .10
assert values["subliminal_targets"] == cfg.subliminal_targets == ["Subheading"]
assert values["subliminal_k"] == cfg.subliminal_k == 7
assert values["subliminal_hide_pct"] == cfg.subliminal_hide_pct == 55
assert values["subliminal_granularity"] == cfg.subliminal_granularity == "word"
assert values["subliminal_all_intra"] is cfg.subliminal_all_intra is False
assert values["encoder_preset"] == cfg.preset == "veryfast"
assert values["workers"] == max(4, os.cpu_count() or 4)
assert values["hashtag_source"] == "none"
assert values["upload_platforms"] == "yt"
assert config.MAIL_TO == ["founder@quislab.com"]
assert config.DRIVE_ROOT_FOLDER_ID == defaults.DRIVE_FOLDER
for source in ("gif_source", "bg_image_source", "bg_video_source", "music_source"):
    assert values[source] == "drive_folder"
text = (ROOT / "docs/common_parameter_settings.txt").read_text()
assert defaults.DRIVE_FOLDER in text
assert all(url in text for url in defaults.MATERIAL_FOLDERS.values())
print("global defaults match the operator's settings")

subprocess.run([sys.executable, "-c", """
import config
import parameter_presets as p
assert config.MAIL_TO == ['operator@example.com']
assert config.DRIVE_ROOT_FOLDER_ID == 'custom-output'
assert p.default_values()['upload_platforms'] == 'yt,tk'
"""], cwd=ROOT, check=True, env={**os.environ,
    "BVG_MAIL_TO": "operator@example.com", "BVG_DRIVE_ROOT_FOLDER_ID": "custom-output",
    "BVG_UPLOAD_PLATFORMS": "tk,yt"})

# Catch missing controls when the generation page gains new parameters.
excluded = {"batch_label", "drive_folder", "notify_email", "scrape_account",
            "cta_clips_drive_pooled", *defaults.MATERIAL_FOLDERS}
for node in ast.walk(ast.parse((ROOT / "app.py").read_text())):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "keep":
        if isinstance(node.args[0], ast.Constant):
            assert node.args[0].value in values.keys() | excluded, node.args[0].value
assert not excluded.intersection(values)

values.update(bg_video_opacity=15, bg_video_min_seconds=12.5,
              caption_mode="none",
              script_prompt="=A literal prompt\n第二行", subliminal_targets=["Headline", "Footer"],
              voice_set_kokoro=["af_heart", "am_adam"], cta_speed_10=1.75)
first = pp.save("  Campaign A  ", values)
assert first["name"] == "Campaign A"
assert pp.list_presets()[0]["values"] == values
assert pp.record_values(first) == values
raw = pp.to_excel(first)
assert pp.from_excel(raw) == ("Campaign A", values)
wb = load_workbook(io.BytesIO(raw))
ws = wb["Parameters"]
rows = {ws.cell(i, 1).value: i for i in range(5, ws.max_row + 1)}
assert ws.cell(rows["script_prompt"], 4).data_type == "s"
ws.cell(rows["bg_video_opacity"], 4, 20)
ws.cell(rows["split_audio"], 4, "FALSE")
ws.cell(rows["voice_set_kokoro"], 4, "af_bella, am_adam")
ws.cell(rows["subliminal_targets"], 4).value = None
ws.cell(rows["script_prompt"], 4).value = None
ws.delete_rows(rows["bg_video_min_seconds"])
buf = io.BytesIO()
wb.save(buf)
_, edited = pp.from_excel(buf.getvalue())
assert edited["bg_video_opacity"] == 20
assert edited["bg_video_min_seconds"] == 20.0
assert edited["split_audio"] is False
assert edited["voice_set_kokoro"] == ["af_bella", "am_adam"]
assert edited["subliminal_targets"] == [] and edited["script_prompt"] == ""
print("Excel round trip and actual cell edits passed")

for bad in ({"bg_video_opacity": 51}, {"workers": defaults.max_workers() + 1},
            {"fps": 31}, {"split_audio": "yes"}, {"music_volume": None},
            {"crf": 18.5}, {"music_min_seconds": float("nan")},
            {"bg_color": "red"}, {"voice_set_pocket": ["af_heart"]},
            {"subliminal_targets": ["Headline", "Subheading", "Footer"]},
            {"subliminal_targets": ["Headline", "Headline"]}, {"drive_folder": "x"}):
    try:
        pp.validate(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(bad)

for mode in ("duplicate", "unknown", "formula", "version", "blank", "sheet", "missing_key"):
    wb = load_workbook(io.BytesIO(raw))
    ws = wb["Parameters"]
    if mode == "duplicate":
        ws.append(["bg_video_opacity", "", "", 10])
    elif mode == "unknown":
        ws.append(["unknown", "", "", 10])
    elif mode == "formula":
        ws.cell(5, 4, "=1+1")
    elif mode == "version":
        ws["B1"] = 99
    elif mode == "sheet":
        ws.title = "Data"
    elif mode == "missing_key":
        for coordinate in ("A5", "D5"):
            ws[coordinate].value = None
            ws[coordinate].style = "Normal"
    else:
        ws.cell(rows["bg_video_opacity"], 4).value = None
    buf = io.BytesIO()
    wb.save(buf)
    try:
        pp.from_excel(buf.getvalue())
    except ValueError:
        pass
    else:
        raise AssertionError(mode)
assert pp.list_presets()[0]["values"] == values
print("invalid imports rejected without changing stored values")

try:
    pp.save("Campaign A", values)
except ValueError:
    pass
else:
    raise AssertionError("duplicate name accepted")
updated = pp.save("Campaign A", edited, replace_id=first["id"])
assert updated["id"] == first["id"]
assert pp.list_presets()[0]["values"] == edited
second = pp.save("Campaign B", values)
presets.save("job 1", {}, {})
presets.save("job 2", {}, {})
presets.prune(1)
presets.gc_blobs(grace_seconds=0)
assert len(presets.list_presets()) == 1
assert len(pp.list_presets()) == 2
subprocess.run([sys.executable, "-c", "import parameter_presets as p; assert len(p.list_presets()) == 2"], cwd=ROOT, check=True)
pp.delete(second["id"])
assert len(pp.list_presets()) == 1
pp.delete(first["id"])
assert pp.list_presets() == []
print("persistence, overwrite, deletion and historical-job isolation passed")
print("ALL PARAMETER PRESET TESTS PASSED")
