"""build_workspace() must behave exactly as before, and the split halves must
round-trip a job's assets folder identically."""
import io, os, sys, tempfile, zipfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
# Never let a developer's real .env under test — it would put live
# SMTP credentials and a real Shared Drive behind these assertions.
os.environ["BVG_IGNORE_DOTENV"] = "1"
os.environ["BVG_JOBS_ROOT"] = tempfile.mkdtemp(prefix="wstest_")
sys.path.insert(0, str(PROJ))

# workspace.py imports no streamlit, precisely so the headless worker can use
# it — which means it imports cleanly here too.
from workspace import Workspace, build_workspace, stage_uploads, workspace_from_dir


class Fake:
    """Stands in for a Streamlit UploadedFile."""
    def __init__(self, name, data):
        self.name = name
        self._data = data
        self.size = len(data)

    def getvalue(self):
        return self._data


def make_zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, b"fakeimage")
    return buf.getvalue()


# app.py must still re-export these, since it is what the UI calls.
import ast
app_src = (PROJ / "app.py").read_text(encoding="utf-8")
imported = {a.name for n in ast.walk(ast.parse(app_src))
            if isinstance(n, ast.ImportFrom) and n.module == "workspace"
            for a in n.names}
assert {"build_workspace", "stage_uploads"} <= imported, imported
print("app.py imports the shared helpers rather than redefining them")

video = Fake("promo.mp4", b"VIDEOBYTES")
zipf = Fake("bg.zip", make_zip(["a.jpg", "sub/b.png"]))
cta = Fake("cta.png", b"PNGBYTES")
font = Fake("MyFont.OTF", b"FONTBYTES")
slots = [
    [Fake("clip_b.mp4", b"B"), Fake("clip_a.mp4", b"A")],   # slot 1, 2 clips
    [],                                                      # slot 2, EMPTY
    [Fake("clip_c.mp4", b"C")],                              # slot 3
]

# --- classic path
t1 = Path(tempfile.mkdtemp(prefix="classic_"))
ws1 = build_workspace(t1, video, zipf, cta, font, slots)

assert ws1.video_path.read_bytes() == b"VIDEOBYTES"
assert ws1.cta_path.read_bytes() == b"PNGBYTES"
assert ws1.font_path.name == "custom_font.otf", ws1.font_path
assert ws1.font_path.read_bytes() == b"FONTBYTES"
assert sorted(p.name for p in ws1.bg_dir.rglob("*") if p.is_file()) == ["a.jpg", "b.png"]
assert ws1.work_dir.is_dir()
print("build_workspace ok:", [[p.name for p in s] for s in ws1.cta_video_slots])

# The empty middle slot must still occupy position 2, or per-slot speeds
# (cta_video_speeds[i]) would silently shift onto the wrong clips.
assert len(ws1.cta_video_slots) == 3, ws1.cta_video_slots
assert ws1.cta_video_slots[1] == [], "empty slot must stay in position"
assert [p.name for p in ws1.cta_video_slots[0]] == ["clip_a.mp4", "clip_b.mp4"]
assert [p.name for p in ws1.cta_video_slots[2]] == ["clip_c.mp4"]

# --- job path: stage once, reconstruct later (as the worker does)
t2 = Path(tempfile.mkdtemp(prefix="job_"))
assets, work = t2 / "assets", t2 / "work"
stage_uploads(assets, video, zipf, cta, font, slots)
ws2 = workspace_from_dir(assets, work)

def shape(ws, root):
    return {
        "video": ws.video_path.relative_to(root).as_posix(),
        "cta": ws.cta_path.relative_to(root).as_posix() if ws.cta_path else None,
        "font": ws.font_path.relative_to(root).as_posix() if ws.font_path else None,
        "bgs": sorted(p.relative_to(root).as_posix() for p in ws.bg_dir.rglob("*") if p.is_file()),
        "slots": [[p.name for p in s] for s in ws.cta_video_slots],
    }

s1, s2 = shape(ws1, t1), shape(ws2, assets)
assert s1 == s2, f"\nclassic: {s1}\njob:     {s2}"
print("round-trip identical:", s2["slots"])

# --- reconstructing a SECOND time must be byte-identical (resume determinism)
ws3 = workspace_from_dir(assets, work)
assert shape(ws3, assets) == s2
print("resume reconstruction stable")

# --- optional uploads absent
t3 = Path(tempfile.mkdtemp(prefix="minimal_"))
stage_uploads(t3 / "assets", video, None, None, None, None)
ws4 = workspace_from_dir(t3 / "assets", t3 / "work")
assert ws4.cta_path is None and ws4.font_path is None
assert ws4.cta_video_slots == [] and ws4.bg_dir.is_dir()
assert not any(ws4.bg_dir.iterdir()), "no ZIP -> empty backgrounds dir"
print("minimal upload set ok (no zip / cta / font / clips)")

print("\nALL WORKSPACE TESTS PASSED")
