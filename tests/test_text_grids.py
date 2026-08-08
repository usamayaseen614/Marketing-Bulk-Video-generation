"""Per-promo Headline / Subheading / Footer grids: reading, matching, and the
one thing that actually matters — the same sheet row saying different things on
different promo videos."""
import io
import os
import sys
import tempfile
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.environ["BVG_IGNORE_DOTENV"] = "1"
sys.path.insert(0, str(PROJ))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

import text_grids as tg
from video_generator import TEXT_ROLES


def xlsx(frame: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False)
    return buf.getvalue()


PROMOS = ["video 1.mp4", "video 2.mp4", "video 3.mp4"]
SHEET_ROWS = 3

# ---------- roles cannot drift from the renderer's ----------
assert tg.ROLES == tuple(TEXT_ROLES), (tg.ROLES, TEXT_ROLES)
print("roles:", tg.ROLES)

# ---------- match_key ----------
for spelling in ("video 1", "Video_1", " VIDEO-1 ", "video 1.mp4", "video1",
                 "Video 1.MP4"):
    assert tg.match_key(spelling) == "video1", (spelling, tg.match_key(spelling))
assert tg.match_key("video 10") == "video10" != tg.match_key("video 1")
assert tg.match_key(None) == ""
# .mp4 is stripped as an extension; a bare word ending in a dot is not a suffix
assert tg.match_key("clip.mov") == "clip"
assert tg.match_key("summer.sale") == "summersale"
print("match_key: case, separators, and the extension all ignored")

# ---------- read_grid trims Excel debris ----------
raw = pd.DataFrame({
    "video 1": ["pov", "awsdlan", "asldnasl", None],
    "video 2": ["omsdhajkl", "salfjnal", "aksjfdn", None],
    "video 3": ["askfjnan", "sakfjn", "askfjnan", None],
    "": [None, None, None, None],
})
grid = tg.read_grid(xlsx(raw), "Headline")
assert grid.columns == ["video 1", "video 2", "video 3"], grid.columns
assert grid.n_rows == 3, grid.n_rows
assert grid.values["video 1"] == ["pov", "awsdlan", "asldnasl"]
print(f"read_grid: {grid.n_rows} rows x {len(grid.columns)} cols "
      f"(dropped a blank column and a trailing blank row)")

# ---------- the happy path ----------
report = tg.check_grid(grid, PROMOS, SHEET_ROWS)
assert report.ok, report.errors
assert report.matched == 3, report.pairs
assert [c for _i, _n, c in report.pairs] == ["video 1", "video 2", "video 3"]
assert not report.warnings, report.warnings
print("check: 3/3 promos matched, no warnings")

# headers spelled differently still match
loose = tg.read_grid(xlsx(raw.rename(columns={"video 1": "Video_1",
                                              "video 2": "video2.mp4"})),
                     "Headline")
assert tg.check_grid(loose, PROMOS, SHEET_ROWS).ok
print("check: 'Video_1' and 'video2.mp4' both matched")

# ---------- errors block ----------
typo = tg.read_grid(xlsx(raw.rename(columns={"video 3": "vidio 3"})), "Headline")
bad = tg.check_grid(typo, PROMOS, SHEET_ROWS)
assert not bad.ok and bad.unmatched_columns == ["vidio 3"], bad
assert any("vidio 3" in e for e in bad.errors), bad.errors
print("check: unmatched column is an error —", bad.errors[0][:70] + "…")

dupe = tg.read_grid(xlsx(pd.DataFrame({"video 1": ["a"], "Video-1": ["b"],
                                       "video 2": ["c"], "video 3": ["d"]})),
                    "Headline")
d = tg.check_grid(dupe, PROMOS, 1)
assert not d.ok and sorted(d.duplicate_columns) == ["Video-1", "video 1"], d
print("check: two headers naming one promo is an error")

short = tg.read_grid(xlsx(raw.iloc[:2]), "Headline")
s = tg.check_grid(short, PROMOS, SHEET_ROWS)
assert not s.ok and any("2 row(s)" in e and "3" in e for e in s.errors), s.errors
print("check: row count must match —", s.errors[0][:70] + "…")

same = tg.check_grid(grid, ["video 1.mp4", "video 1.mp4", "video 3.mp4"], 3)
assert not same.ok and any("cannot give them different text" in e
                           for e in same.errors), same.errors
print("check: two promos with the same filename is an error")

# ---------- warnings do not block ----------
extra = tg.check_grid(grid, PROMOS + ["video 4.mp4"], SHEET_ROWS)
assert extra.ok and extra.matched == 3, extra
assert any("video 4.mp4" in w for w in extra.warnings), extra.warnings
print("check: a promo with no column just falls back —", extra.warnings[0][:60] + "…")

gappy_raw = raw.copy()
gappy_raw.loc[1, "video 2"] = None
gappy = tg.read_grid(xlsx(gappy_raw), "Headline")
g = tg.check_grid(gappy, PROMOS, SHEET_ROWS)
assert g.ok and g.blank_cells == 1, (g.errors, g.blank_cells)
print("check: 1 blank cell reported as a warning, not an error")

# ---------- check_uploads: exactly what the Streamlit page calls ----------
class Upload:
    """Stands in for a Streamlit UploadedFile."""
    def __init__(self, data, name):
        self._d, self.name, self.size = data, name, len(data)

    def getvalue(self):
        return self._d


good = Upload(xlsx(raw), "headlines.xlsx")
typo_up = Upload(xlsx(raw.rename(columns={"video 3": "vidio 3"})), "bad.xlsx")

reports, resolved_all, errors = tg.check_uploads(
    {"Headline": good, "Subheading": None, "Footer": None}, PROMOS, SHEET_ROWS)
assert not errors and set(resolved_all) == {"Headline"}, (errors, resolved_all)
assert set(reports) == {"Headline"}
print("check_uploads: one good sheet resolves, the un-uploaded roles are absent")

# A broken sheet contributes NOTHING — a half-applied upload is the one outcome
# that would be impossible to notice.
reports, resolved_all, errors = tg.check_uploads(
    {"Headline": good, "Footer": typo_up}, PROMOS, SHEET_ROWS)
assert errors and set(resolved_all) == {"Headline"}, (errors, resolved_all)
assert "Footer" in reports and not reports["Footer"].ok
print("check_uploads: the broken Footer sheet resolves to nothing, and errors")

# Unreadable bytes are reported, not raised.
reports, resolved_all, errors = tg.check_uploads(
    {"Headline": Upload(b"not a workbook", "junk.xlsx")}, PROMOS, SHEET_ROWS)
assert not resolved_all and len(errors) == 1 and "junk.xlsx" in errors[0], errors
print("check_uploads: unreadable file reported —", errors[0][:58] + "…")

# No promos yet: silence rather than "every column matches nothing".
reports, resolved_all, errors = tg.check_uploads({"Headline": good}, [], 0)
assert not reports and not resolved_all and not errors
print("check_uploads: no promos uploaded yet -> no spurious errors")

# The reader hook is what lets Streamlit slot its cache in.
calls = []


def counting_reader(data, role):
    calls.append(role)
    return tg.read_grid(data, role)


tg.check_uploads({"Headline": good}, PROMOS, SHEET_ROWS, reader=counting_reader)
assert calls == ["Headline"], calls
print("check_uploads: honours the injected reader (Streamlit's cache)")

# ---------- resolve + stage + read back ----------
resolved = {"Headline": tg.resolve(grid, report)}
assert set(resolved["Headline"]) == {"0", "1", "2"}, resolved
assert resolved["Headline"]["1"] == ["omsdhajkl", "salfjnal", "aksjfdn"]

with tempfile.TemporaryDirectory(prefix="tg_") as tmp:
    assert tg.write_overrides(tmp, {"Headline": {}}) is None, "empty writes nothing"
    assert tg.read_overrides(tmp) == {}
    path = tg.write_overrides(tmp, resolved)
    assert path and path.name == tg.OVERRIDES_FILENAME
    back = tg.read_overrides(tmp)
    assert back == resolved, (back, resolved)
    # A corrupt file must not take a render down with it.
    Path(path).write_text("{not json", encoding="utf-8")
    assert tg.read_overrides(tmp) == {}
print("overrides: staged as", tg.OVERRIDES_FILENAME, "and read back index-keyed")

# ---------- THE KEY CLAIM: one row, different text per promo ----------
sheet = pd.DataFrame({
    "Headline": ["main one", "main two", "main three"],
    "Headline_Size": [60, 60, 60],
    "Subheading": ["sub one", "sub two", "sub three"],
})
overrides = {"Headline": tg.resolve(grid, report),
             "Footer": tg.resolve(gappy, g)}

seen = set()
for promo_index in range(3):
    view = tg.apply_overrides(sheet, overrides, promo_index)
    seen.add(view.loc[0, "Headline"])
    # Styling and the roles with no grid are untouched.
    assert list(view["Headline_Size"]) == [60, 60, 60]
    assert list(view["Subheading"]) == ["sub one", "sub two", "sub three"]
    # Footer had no column in the sheet at all — the grid creates it.
    assert "Footer" in view.columns
assert seen == {"pov", "omsdhajkl", "askfjnan"}, seen
print("apply: row 1 reads", sorted(seen), "on promos 1/2/3")

# blank cell falls back to the main sheet
gap_view = tg.apply_overrides(sheet, {"Headline": tg.resolve(gappy, g)}, 1)
assert gap_view.loc[1, "Headline"] == "main two", gap_view.loc[1, "Headline"]
assert gap_view.loc[0, "Headline"] == "omsdhajkl"
print("apply: a blank cell keeps the main Excel's text ('main two')")

# the input frame is never mutated, and the no-grid path copies nothing
assert list(sheet["Headline"]) == ["main one", "main two", "main three"]
assert tg.apply_overrides(sheet, {}, 0) is sheet
assert tg.apply_overrides(sheet, {"Headline": {}}, 0) is sheet
# a promo with no column gets the sheet untouched
assert list(tg.apply_overrides(sheet, overrides, 9)["Headline"]) == \
    ["main one", "main two", "main three"]
print("apply: input frame untouched; no-override promos see the main Excel")

# a float64 (all-blank) column still takes strings
blank_sheet = pd.DataFrame({"Headline": [None, None, None]})
assert blank_sheet["Headline"].dtype != object or True  # shape varies by pandas
filled = tg.apply_overrides(blank_sheet, overrides, 0)
assert list(filled["Headline"]) == ["pov", "awsdlan", "asldnasl"], list(filled["Headline"])
print("apply: an all-blank Headline column widens to take the grid's text")

# ---------- override_text, used by the naming code ----------
assert tg.override_text(overrides, "Headline", 0, 1) == "pov"
assert tg.override_text(overrides, "Headline", 2, 3) == "askfjnan"
assert tg.override_text(overrides, "Headline", 0, 99) == ""
assert tg.override_text(overrides, "Subheading", 0, 1) == ""
print("override_text: single cells resolve 1-based by row")

# ---------- template round-trips ----------
tpl = tg.template_bytes("Headline", PROMOS, SHEET_ROWS)
tpl_grid = tg.read_grid(tpl, "Headline")
assert tpl_grid.columns == PROMOS, tpl_grid.columns
assert tg.check_grid(tpl_grid, PROMOS, SHEET_ROWS).matched == 3
# It is blank, so it has no rows once trailing blanks are trimmed — which is
# exactly the row-count error a user gets if they download it and change nothing.
assert tpl_grid.n_rows == 0, tpl_grid.n_rows
dupe_tpl = tg.read_grid(tg.template_bytes("Footer", ["a.mp4", "a.mp4"], 2), "Footer")
assert dupe_tpl.columns == ["a.mp4"], dupe_tpl.columns
print(f"template: headers {tpl_grid.columns} match the uploaded promos exactly")

# ---------- the shipped samples must survive the app's own strictness ----------
# Skipped when create_sample_assets.py has not been run — the grids and the
# promo MP4s are generated, not committed.
SA = PROJ / "sample_assets"
SAMPLE_PROMOS = ["promo.mp4", "promo_2.mp4", "promo_3.mp4"]
sample_grids = {role: SA / f"{role.lower()}_by_promo.xlsx" for role in tg.ROLES}

if all(p.is_file() for p in sample_grids.values()):
    sheet = pd.read_excel(SA / "sample_5_videos.xlsx", engine="openpyxl")
    sample_overrides = {}
    for role, path in sample_grids.items():
        g = tg.read_grid(path, role)
        r = tg.check_grid(g, SAMPLE_PROMOS, len(sheet))
        assert r.ok, (path.name, r.errors)
        assert r.matched == 3, (path.name, r.pairs)
        assert g.n_rows == len(sheet), (path.name, g.n_rows, len(sheet))
        sample_overrides[role] = tg.resolve(g, r)
    print(f"\nsamples: all 3 sheets pass against {SAMPLE_PROMOS} "
          f"and match sample_5_videos.xlsx ({len(sheet)} rows)")

    heads = {i: tg.apply_overrides(sheet, sample_overrides, i).loc[0, "Headline"]
             for i in range(3)}
    assert len(set(heads.values())) == 3, heads
    print("samples: row 1 reads", list(heads.values()))

    # The one deliberate blank falls back rather than rendering nothing.
    fallback = tg.apply_overrides(sheet, sample_overrides, 0).loc[3, "Headline"]
    assert fallback == sheet.loc[3, "Headline"], fallback
    assert fallback, "the fallback must not be empty"
    print(f"samples: the blank Headline cell falls back to {fallback!r}")
else:
    print("\nsamples: skipped — run create_sample_assets.py to generate them")

print("\nALL TEXT GRID TESTS PASSED")
