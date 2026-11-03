"""Exercise language switching on the real Streamlit pages and form state."""
import io
import os
from pathlib import Path
import string
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="i18n_ui_")

from openpyxl import Workbook
from streamlit.testing.v1 import AppTest

from i18n import chinese_catalog
import parameter_presets
from jobs import store


def control(kind, label):
    return next(item for item in getattr(at, kind) if item.label == label)


def run():
    at.run()
    assert not at.exception, [(e.message, e.stack_trace) for e in at.exception]


at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
assert not at.exception
assert at.title[0].value == "🎬 Bulk Marketing Video Generator"
control("slider", "Background video opacity (%)").set_value(17)
control("radio", "Layout mode").set_value("split")
control("selectbox", "Default artistic style").select("neon")
control("text_input", "Preset name").set_value("Layout")
wb = Workbook()
wb.active.append(["Headline"])
wb.active.append(["Keep original marketing text"])
buffer = io.BytesIO()
wb.save(buffer)
control("file_uploader", "Excel file (.xlsx)").set_value(
    ("campaign.xlsx", buffer.getvalue(), parameter_presets.MIME))
control("file_uploader", "Promo video(s) (MP4) — up to 600").set_value(
    [("promo.mp4", b"not rendered in this test", "video/mp4")])
run()
control("text_input", "Batch name (optional)").set_value("Campaign 原文")
run()
upload_ids = [item.proto.id for item in at.get("file_uploader")]
at.session_state["row_edits"] = {1: {"Headline_X": 321}}

at.button(key="switch_language").click()
run()
assert at.session_state["ui_language"] == "zh-CN"
assert at.query_params["lang"] == "zh-CN"
assert at.title[0].value == "🎬 批量营销视频生成器"
assert control("slider", "背景视频不透明度（%）").value == 17
assert control("radio", "布局模式").value == "split"
assert control("radio", "布局模式").options == ["自由布局（分别放置各元素）", "左右分屏（并排显示）"]
assert control("selectbox", "默认艺术样式").value == "neon"
assert control("text_input", "批次名称（可选）").value == "Campaign 原文"
assert control("file_uploader", "Excel 文件（.xlsx）").value.name == "campaign.xlsx"
assert [item.proto.id for item in at.get("file_uploader")] == upload_ids
assert at.session_state["row_edits"] == {1: {"Headline_X": 321}}
assert any("Keep original marketing text" in str(item.value) for item in at.dataframe)

control("button", "保存为新预设").click()
run()
assert any(item.value == "已保存 Layout。" for item in at.success)
assert control("selectbox", "已保存的参数预设").options == ["Layout"]
assert parameter_presets.list_presets()[0]["values"]["default_style"] == "neon"

at.button(key="switch_language").click()
run()
assert at.title[0].value == "🎬 Bulk Marketing Video Generator"
assert control("slider", "Background video opacity (%)").value == 17
assert control("selectbox", "Default artistic style").value == "neon"
assert control("file_uploader", "Excel file (.xlsx)").value.name == "campaign.xlsx"
assert at.session_state["row_edits"] == {1: {"Headline_X": 321}}
at.button(key="switch_language").click()
run()

# Exercise the live Jobs page as well as its empty state.
job_id = store.create_job(store.KIND_RENDER, label="Campaign 原文")
assert store.claim_next_job()["id"] == job_id
store.set_stage(job_id, "downloading background images 1/3")

for page, title in (
    ("pages/1_Jobs.py", "📋 任务"),
    ("pages/2_Scraper.py", "🎵 素材抓取"),
    ("pages/3_Setup.py", "🔧 设置与诊断"),
):
    at.switch_page(page)
    run()
    assert at.title[0].value == title
    assert at.button(key="switch_language").label == "English"
    assert at.query_params["lang"] == "zh-CN"
    if page == "pages/1_Jobs.py":
        assert any("Campaign 原文" in item.label for item in at.expander)
        assert at.get("progress")[0].proto.text == "正在下载背景图片 1/3"
        at.button(key="switch_language").click()
        run()
        assert at.get("progress")[0].proto.text == "downloading background images 1/3"
        at.button(key="switch_language").click()
        run()
    if page == "pages/2_Scraper.py":
        control("radio", "抓取内容").set_value("music")
        run()
        control("checkbox", "裁剪每首曲目").check()
        run()
        control("number_input", "开始时间（秒）").set_value(3.5)
        run()
        at.button(key="switch_language").click()
        run()
        assert control("number_input", "Start at (s)").value == 3.5
        at.button(key="switch_language").click()
        run()

# A bookmarked language survives a new browser session; invalid input defaults to English.
for requested, expected in (("zh-CN", "🎬 批量营销视频生成器"),
                            ("invalid", "🎬 Bulk Marketing Video Generator")):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.query_params["lang"] = requested
    run()
    assert at.title[0].value == expected

# Validate format placeholders in the real catalog, including reordered values.
formatter = string.Formatter()
for english, chinese in chinese_catalog().items():
    original = [field for _, field, _, _ in formatter.parse(english) if field is not None]
    translated = [field for _, field, _, _ in formatter.parse(chinese) if field is not None]
    assert sorted(original) == sorted(translated), (english, chinese)
print("ALL LANGUAGE UI TESTS PASSED")
