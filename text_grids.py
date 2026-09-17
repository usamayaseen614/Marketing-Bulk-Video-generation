"""
text_grids.py — per-promo Headline / Subheading / Footer text.

## The problem

The main sheet has one row per output video and one `Headline` cell per row, so
every promo video rendered from that row says exactly the same thing. A render
of 5 rows x 3 promos is 15 videos carrying 5 distinct headlines. What was
wanted instead is 15 distinct headlines — the text tailored to the promo, while
everything else about the row (size, font, colour, position, background box)
stays the shared design it already is.

## The shape

One optional workbook per role. Each is a GRID: the column HEADER names an
uploaded promo video, and the cells beneath it are that promo's text, one per
sheet row.

        | video 1        | video 2        | video 3        |
        | pov            | omsdhajkl      | askfjnan       |
        | awsdlan        | salfjnal       | sakfjn         |

Upload the Headline grid and the main sheet's `Headline` column stops being
consulted; `Subheading` and `Footer` carry on coming from the main sheet unless
their own grids are uploaded too. Nothing but the text is affected — the grid
has no styling columns and never will, because the point is that one design is
reused across every promo.

## Why the match is resolved in the UI, not in the worker

`stage_uploads` renames the promos to `input.mp4`, `input_2.mp4`, … so the
uploaded filenames simply do not exist by the time the worker runs. A header
therefore has to be resolved to a promo **index** while the browser still knows
the names, and what gets staged for the worker is the already-resolved mapping
(`assets/text_overrides.json`). That also makes resume free: the worker re-reads
a plain index-keyed table rather than re-running a fuzzy name match whose result
could drift between attempts.

Matching is deliberately forgiving — case, surrounding whitespace, separators
and a video extension are all ignored, so `video 1`, `Video_1` and
`video-1.mp4` all name `video 1.mp4`. Anything left unmatched is an error the
Check button reports rather than something quietly rendered with the wrong text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

import pandas as pd

from video_generator import TEXT_ROLES, _clean_str
from workspace import VIDEO_SUFFIXES

# Roles a grid can be uploaded for — the same three the sheet and the renderer
# already know about, imported rather than re-listed so they cannot drift.
ROLES: tuple[str, ...] = tuple(TEXT_ROLES)

# What the resolved mapping is called inside a job's assets/ folder. Resolved
# and index-keyed, so the worker needs none of this module's matching logic —
# see the module docstring.
OVERRIDES_FILENAME = "text_overrides.json"

# pandas names a blank header cell `Unnamed: 3`. Trailing blank columns are
# routine in a hand-edited sheet, so they are dropped rather than reported as
# columns that match no promo.
_UNNAMED = re.compile(r"^Unnamed:\s*\d+$")


def match_key(value) -> str:
    """The key two names are compared on.

    Lowercased, video extension removed, and every run of non-alphanumerics
    squeezed out entirely — so `video 1`, `Video_1`, ` VIDEO-1 ` and
    `video 1.mp4` all reduce to `video1`. Squeezing rather than collapsing to a
    single space is what lets a header typed as `video1` still find
    `video 1.mp4`, which is the mistake people actually make."""
    text = str(value if value is not None else "").strip()
    path = Path(text)
    if path.suffix.lower() in VIDEO_SUFFIXES:
        text = path.stem
    return re.sub(r"[^a-z0-9]+", "", text.lower())


@dataclass(frozen=True)
class Grid:
    """One uploaded workbook, read and tidied but not yet matched to anything."""
    role: str
    columns: list[str]              # header text, in sheet order, blanks dropped
    values: dict[str, list[str]]    # header -> one cleaned string per row
    n_rows: int

    def column_for(self, key: str) -> Optional[str]:
        """The header whose match_key is `key`, or None. First wins — a
        duplicate is reported as an error, so which one is chosen never
        decides anything."""
        for name in self.columns:
            if match_key(name) == key:
                return name
        return None


@dataclass
class GridReport:
    """What the Check button shows for one uploaded grid.

    Errors block generation; warnings do not. The split matters: a header that
    matches no promo means some video would silently render the wrong text,
    while a promo with no column just falls back to the main sheet, which is a
    choice someone might have made on purpose."""
    role: str
    grid_rows: int
    sheet_rows: int
    # (promo index, promo name, matched header or None) for every promo, in
    # upload order — this is the table the user reads.
    pairs: list[tuple[int, str, Optional[str]]] = field(default_factory=list)
    unmatched_columns: list[str] = field(default_factory=list)
    duplicate_columns: list[str] = field(default_factory=list)
    blank_cells: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def matched(self) -> int:
        return sum(1 for _i, _n, col in self.pairs if col)


# --------------------------------------------------------------------------- reading

def read_grid(source: Union[bytes, str, Path], role: str) -> Grid:
    """Read one grid workbook.

    Trailing blank rows and unnamed columns are trimmed first. Excel hands out
    a few of both to anyone who has ever clicked below the data, and counting
    them would turn "6 rows, same as the sheet" into a row-count error nobody
    can see the cause of."""
    if isinstance(source, bytes):
        import io

        frame = pd.read_excel(io.BytesIO(source), engine="openpyxl", dtype=object)
    else:
        frame = pd.read_excel(Path(source), engine="openpyxl", dtype=object)

    columns = [str(c).strip() for c in frame.columns]
    keep = [i for i, name in enumerate(columns)
            if name and not _UNNAMED.match(name)]
    columns = [columns[i] for i in keep]

    cells = [[_clean_str(frame.iloc[r, i]) for i in keep]
             for r in range(len(frame))]
    # Trailing rows where every kept column is blank are Excel debris, not data.
    while cells and not any(cells[-1]):
        cells.pop()

    values = {name: [row[c] for row in cells] for c, name in enumerate(columns)}
    return Grid(role=role, columns=columns, values=values, n_rows=len(cells))


# --------------------------------------------------------------------------- checking

def check_grid(grid: Grid, promo_names: Iterable[str], sheet_rows: int) -> GridReport:
    """Match a grid's columns to the uploaded promos and report what happened.

    Everything the Check button says comes from here, and so does the decision
    to block generation — one function so the report and the gate can never
    disagree about whether a sheet is usable."""
    promos = [str(n) for n in promo_names]
    report = GridReport(role=grid.role, grid_rows=grid.n_rows, sheet_rows=int(sheet_rows))

    if not grid.columns:
        report.errors.append(
            f"The {grid.role} sheet has no column headers — the first row must "
            "name the promo videos.")
        return report

    # Two headers naming the same promo is ambiguous rather than merely untidy:
    # one of them would be used and the other silently ignored.
    seen: dict[str, list[str]] = {}
    for name in grid.columns:
        seen.setdefault(match_key(name), []).append(name)
    for key, names in seen.items():
        if len(names) > 1:
            report.duplicate_columns.extend(names)
            report.errors.append(
                f"Columns {', '.join(repr(n) for n in names)} all name the same "
                "promo video — keep one.")

    # Two promos whose filenames reduce to the same key cannot be addressed
    # apart by any header, so no column arrangement could make this sheet mean
    # one thing. Reported here because this is where the user is looking.
    promo_seen: dict[str, list[str]] = {}
    for name in promos:
        promo_seen.setdefault(match_key(name), []).append(name)
    for key, names in promo_seen.items():
        if len(names) > 1:
            report.errors.append(
                f"{len(names)} promo videos are all named {names[0]!r} — one "
                "column cannot give them different text. Rename them apart and "
                "upload again.")

    promo_keys = set(promo_seen)
    for index, promo in enumerate(promos):
        report.pairs.append((index, promo, grid.column_for(match_key(promo))))

    for name in grid.columns:
        if match_key(name) not in promo_keys:
            report.unmatched_columns.append(name)
    if report.unmatched_columns:
        report.errors.append(
            f"No uploaded promo video matches "
            f"{', '.join(repr(n) for n in report.unmatched_columns)}. "
            "Rename the column to the promo's filename, or upload that promo.")

    absent = [n for i, n, col in report.pairs if not col]
    if absent:
        # Not an error: that promo simply keeps using the main sheet's text.
        report.warnings.append(
            f"{len(absent)} promo video(s) have no column here and will use the "
            f"main Excel's {grid.role} — {', '.join(repr(n) for n in absent)}.")

    # sheet_rows <= 0 means the main Excel is not loaded yet (or failed to
    # load). Reporting "3 rows vs 0" would blame this sheet for that.
    if int(sheet_rows) > 0 and grid.n_rows != int(sheet_rows):
        report.errors.append(
            f"The {grid.role} sheet has {grid.n_rows} row(s) but the main Excel "
            f"has {int(sheet_rows)}. Row 1 here is row 1 there, so the counts "
            "must match.")

    report.blank_cells = sum(
        1 for _i, _n, col in report.pairs if col
        for cell in grid.values.get(col, []) if not cell)
    if report.blank_cells:
        report.warnings.append(
            f"{report.blank_cells} blank cell(s) — those videos fall back to the "
            f"main Excel's {grid.role}.")

    return report


def check_uploads(uploads: dict, promo_names: Iterable[str], sheet_rows: int,
                  reader=None) -> tuple[dict, dict, list[str]]:
    """Read, check and resolve every uploaded grid in one pass.

    `uploads` maps a role to an uploaded-file object (anything with
    `.getvalue()` and `.name`) or None. Returns `(reports, resolved, errors)`,
    where `resolved` holds only the grids that passed — a sheet with an error
    contributes nothing, so a half-broken upload can never half-apply.

    This lives here rather than in app.py so the rules the Check button reports
    and the rules that gate the Generate button are one piece of code, testable
    without a browser. `reader` exists for the Streamlit cache to slot in."""
    read = reader or read_grid
    promos = [str(n) for n in promo_names]
    reports: dict[str, GridReport] = {}
    resolved: dict[str, dict[str, list[str]]] = {}
    errors: list[str] = []

    for role in ROLES:
        upload = uploads.get(role)
        if upload is None:
            continue
        try:
            grid = read(upload.getvalue(), role)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Could not read the {role} sheet "
                          f"`{getattr(upload, 'name', '?')}`: {exc}")
            continue
        if not promos:
            # Every column would be reported as matching no promo, which blames
            # this sheet for a promo video that has simply not been uploaded
            # yet. Nothing can be generated without one anyway.
            continue
        report = check_grid(grid, promos, sheet_rows)
        reports[role] = report
        errors.extend(report.errors)
        if report.ok:
            resolved[role] = resolve(grid, report)

    return reports, resolved, errors


# --------------------------------------------------------------------------- resolving

def resolve(grid: Grid, report: GridReport) -> dict[str, list[str]]:
    """The grid as {promo index (as a string): text per row}.

    String keys because this is written straight to JSON, where an int key
    would come back as a string anyway — better to be the same shape on both
    sides of the file than to look tidier on one. Promos with no column are
    left out entirely, which is what makes them fall back."""
    out: dict[str, list[str]] = {}
    for index, _name, column in report.pairs:
        if column:
            out[str(index)] = list(grid.values.get(column, []))
    return out


def write_overrides(assets_dir: Union[str, Path],
                    resolved: dict[str, dict[str, list[str]]]) -> Optional[Path]:
    """Stage the resolved mapping for the worker. No grids, no file."""
    payload = {role: table for role, table in resolved.items() if table}
    if not payload:
        return None
    path = Path(assets_dir) / OVERRIDES_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def read_overrides(assets_dir: Union[str, Path]) -> dict[str, dict[str, list[str]]]:
    """The staged mapping, or {} when this job has no grids.

    Never raises: a job whose overrides file is unreadable should render with
    the main sheet's text rather than fail hours of work outright."""
    path = Path(assets_dir) / OVERRIDES_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(role): {str(k): [str(v) for v in texts]
                    for k, texts in table.items() if isinstance(texts, list)}
        for role, table in data.items()
        if role in ROLES and isinstance(table, dict)
    }


# --------------------------------------------------------------------------- applying

def apply_overrides(df: pd.DataFrame,
                    overrides: dict[str, dict[str, list[str]]],
                    promo_index: int) -> pd.DataFrame:
    """The sheet as promo `promo_index` should see it.

    This is the whole integration: `RowSpec.from_row` reads the text out of
    `row['Headline']`, so replacing that cell is enough for the wrap, the fit
    box, the styling and the editor payload to follow along with no idea a grid
    was involved.

    Blank override cells keep the main sheet's value — an empty cell means "no
    override for this one", not "this video has no headline". Returns `df`
    itself when nothing applies, so the common no-grids path copies nothing."""
    if not overrides:
        return df

    out: Optional[pd.DataFrame] = None
    for role in ROLES:
        texts = (overrides.get(role) or {}).get(str(int(promo_index)))
        if not texts:
            continue
        if out is None:
            out = df.copy()
        if role not in list(out.columns):
            out[role] = ""
        # Positional from here on. An all-blank optional column reads back as
        # float64 and pandas refuses to take a string into it, and a sheet that
        # happens to carry two same-named columns would make out[role] a frame
        # rather than a series.
        position = list(out.columns).index(role)
        column = [_clean_str(v) for v in out.iloc[:, position]]
        for r, text in enumerate(texts[:len(column)]):
            if text:
                column[r] = text
        out.isetitem(position, pd.Series(column, index=out.index, dtype=object))
    return df if out is None else out


def override_text(overrides: dict[str, dict[str, list[str]]],
                  role: str, promo_index: int, row_number: int) -> str:
    """One cell — for the naming code, which needs a single row's Headline
    without materialising a whole frame per video. `row_number` is 1-based,
    matching the sheet."""
    texts = (overrides.get(role) or {}).get(str(int(promo_index))) or []
    position = int(row_number) - 1
    return texts[position] if 0 <= position < len(texts) else ""


# --------------------------------------------------------------------------- templates

def template_bytes(role: str, promo_names: Iterable[str], n_rows: int) -> bytes:
    """A grid workbook already headed with the uploaded promo names.

    Generated rather than shipped, for the same reason the hashtag template is:
    a committed sample would be missing from the container image on the VM.
    Handing back a file whose headers are correct by construction is also the
    cheapest way to make a strict name match painless."""
    names: list[str] = []
    for name in promo_names:
        # A repeated name would collapse into one column and quietly produce a
        # template narrower than the upload it is meant to describe.
        if str(name) not in names:
            names.append(str(name))
    rows = max(1, int(n_rows))
    frame = pd.DataFrame({name: [""] * rows for name in names or ["promo.mp4"]})
    import io

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=str(role)[:31], index=False)
    return buf.getvalue()
