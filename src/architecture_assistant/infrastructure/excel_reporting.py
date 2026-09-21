"""Excel reporting adapter - writes a read-only ``.xlsx`` artifact.

``ReportingPort.render`` semantics
----------------------------------
The returned ``str`` is the **artifact locator** - the path of the written file
- and never the document content. This adapter writes a ``.xlsx`` workbook and
returns its path; the markdown adapter (Step 21) will do the same with a
``.md`` file. Because every reporting adapter follows that one rule, a caller
treats them uniformly: hand over the payload, get back a path, read the file.

Artifact naming
---------------
The name is shared with the markdown adapter (Step 21): both build
``report-<project slug>-<UTC stamp>`` through :mod:`._reporting`, so one snapshot
always produces one predictable path inside ``report_dir``, per format.

Read-only
---------
The adapter renders the payload it is given. It never opens the database, never
imports a repository and never touches domain state: the projection is produced
by the application layer (``ReportBuilder``) and this module only turns it into
a spreadsheet. Writing the artifact cannot change a Project, Step, Task, ADR,
Risk, ArchitectureVersion, ACR or cost record.

No third-party dependency
-------------------------
The workbook is assembled from the standard OOXML package parts with
``zipfile`` and ``xml.etree.ElementTree`` only. Text is written as *inline*
strings and no style part is emitted, which keeps the writer small and makes the
cell types honest: numbers stay numeric cells, booleans stay booleans and text
stays text. The package is byte-deterministic (fixed entry order and fixed zip
timestamps), so the same payload always yields the same file.
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from xml.etree.ElementTree import Element, SubElement, tostring

from ..domain.models import utc_now
from ._reporting import artifact_name, as_mapping

__all__ = [
    "SHEET_NAMES",
    "ExcelReportingAdapter",
]

#: The three sheets this export writes, in order. Excel-safe names: alphanumeric,
#: no forbidden characters and well under the 31-character limit.
SHEET_NAMES: tuple[str, ...] = ("Overview", "Steps", "Costs")

_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

_SPREADSHEET_NS = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
)
_DOCUMENT_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_PACKAGE_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_CONTENT_TYPES_NS = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.sheet.main+xml"
)
_WORKSHEET_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "spreadsheetml.worksheet+xml"
)

#: Fixed zip metadata: identical payloads must produce identical archives.
_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


# ---------------------------------------------------------------------------
# XML primitives
# ---------------------------------------------------------------------------

def _xml_bytes(root: Element) -> bytes:
    """Serialize one OOXML part, declaration included, as UTF-8 bytes."""
    return (_XML_DECLARATION + tostring(root, encoding="unicode")).encode(
        "utf-8"
    )


def _column_name(index: int) -> str:
    """Spreadsheet column label of a zero-based index (A..Z, AA, AB, ...)."""
    name = ""
    position = index
    while position >= 0:
        name = chr(ord("A") + position % 26) + name
        position = position // 26 - 1
    return name


def _cell_address(column_index: int, row_index: int) -> str:
    """A1-style address of a zero-based column in a one-based row."""
    return f"{_column_name(column_index)}{row_index}"


def _fill_cell(cell: Element, value: Any) -> None:
    """Write one typed cell: inline string, number or boolean.

    ``ElementTree`` escapes text, so ``&``, ``<`` and ``>`` are always written
    correctly and non-ASCII characters (Latvian included) round-trip as UTF-8.
    """
    if value is None or (isinstance(value, str) and not value):
        return
    if isinstance(value, bool):
        cell.set("t", "b")
        SubElement(cell, "v").text = "1" if value else "0"
    elif isinstance(value, int):
        SubElement(cell, "v").text = str(value)
    elif isinstance(value, float):
        SubElement(cell, "v").text = repr(value)
    else:
        cell.set("t", "inlineStr")
        inline = SubElement(cell, "is")
        SubElement(inline, "t").text = str(value)


def _worksheet_xml(rows: Sequence[Sequence[Any]]) -> bytes:
    """One worksheet part: rows and cells in deterministic order."""
    worksheet = Element("worksheet", {"xmlns": _SPREADSHEET_NS})
    sheet_data = SubElement(worksheet, "sheetData")
    for row_index, values in enumerate(rows, start=1):
        row = SubElement(sheet_data, "row", {"r": str(row_index)})
        for column_index, value in enumerate(values):
            cell = SubElement(
                row, "c", {"r": _cell_address(column_index, row_index)}
            )
            _fill_cell(cell, value)
    return _xml_bytes(worksheet)


# ---------------------------------------------------------------------------
# OOXML package parts
# ---------------------------------------------------------------------------

def _content_types_xml(sheet_count: int) -> bytes:
    """``[Content_Types].xml`` - declares every part of the package."""
    root = Element("Types", {"xmlns": _CONTENT_TYPES_NS})
    SubElement(
        root,
        "Default",
        {
            "Extension": "rels",
            "ContentType": (
                "application/vnd.openxmlformats-package.relationships+xml"
            ),
        },
    )
    SubElement(
        root, "Default", {"Extension": "xml", "ContentType": "application/xml"}
    )
    SubElement(
        root,
        "Override",
        {
            "PartName": "/xl/workbook.xml",
            "ContentType": _WORKBOOK_CONTENT_TYPE,
        },
    )
    for index in range(1, sheet_count + 1):
        SubElement(
            root,
            "Override",
            {
                "PartName": f"/xl/worksheets/sheet{index}.xml",
                "ContentType": _WORKSHEET_CONTENT_TYPE,
            },
        )
    return _xml_bytes(root)


def _root_relationships_xml() -> bytes:
    """``_rels/.rels`` - points the package at the workbook."""
    root = Element("Relationships", {"xmlns": _PACKAGE_RELATIONSHIP_NS})
    SubElement(
        root,
        "Relationship",
        {
            "Id": "rId1",
            "Type": f"{_DOCUMENT_RELATIONSHIP_NS}/officeDocument",
            "Target": "xl/workbook.xml",
        },
    )
    return _xml_bytes(root)


def _workbook_xml(sheet_names: Sequence[str]) -> bytes:
    """``xl/workbook.xml`` - the sheet list, in order."""
    root = Element(
        "workbook",
        {
            "xmlns": _SPREADSHEET_NS,
            "xmlns:r": _DOCUMENT_RELATIONSHIP_NS,
        },
    )
    sheets = SubElement(root, "sheets")
    for index, name in enumerate(sheet_names, start=1):
        SubElement(
            sheets,
            "sheet",
            {
                "name": name,
                "sheetId": str(index),
                "r:id": f"rId{index}",
            },
        )
    return _xml_bytes(root)


def _workbook_relationships_xml(sheet_count: int) -> bytes:
    """``xl/_rels/workbook.xml.rels`` - maps ``r:id`` to each worksheet."""
    root = Element("Relationships", {"xmlns": _PACKAGE_RELATIONSHIP_NS})
    for index in range(1, sheet_count + 1):
        SubElement(
            root,
            "Relationship",
            {
                "Id": f"rId{index}",
                "Type": f"{_DOCUMENT_RELATIONSHIP_NS}/worksheet",
                "Target": f"worksheets/sheet{index}.xml",
            },
        )
    return _xml_bytes(root)


def _package(
    sheet_names: Sequence[str],
    sheets: Sequence[Sequence[Sequence[Any]]],
) -> list[tuple[str, bytes]]:
    """Every part of the workbook, in deterministic archive order."""
    parts: list[tuple[str, bytes]] = [
        ("[Content_Types].xml", _content_types_xml(len(sheets))),
        ("_rels/.rels", _root_relationships_xml()),
        ("xl/workbook.xml", _workbook_xml(sheet_names)),
        (
            "xl/_rels/workbook.xml.rels",
            _workbook_relationships_xml(len(sheets)),
        ),
    ]
    for index, rows in enumerate(sheets, start=1):
        parts.append(
            (f"xl/worksheets/sheet{index}.xml", _worksheet_xml(rows))
        )
    return parts


def _write_archive(target: Path, parts: Sequence[tuple[str, bytes]]) -> None:
    """Write the package with a fixed entry order and fixed zip timestamps."""
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts:
            info = zipfile.ZipInfo(
                filename=name, date_time=_FIXED_ZIP_TIMESTAMP
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, payload)


# ---------------------------------------------------------------------------
# payload -> rows
# ---------------------------------------------------------------------------

_STEP_HEADERS: tuple[str, ...] = (
    "Step",
    "Phase",
    "Title",
    "State",
    "Attempt",
    "Max attempts",
    "Risk",
    "Requires human",
    "Created at",
    "Started at",
    "Finished at",
    "Verified at",
    "Cost (USD)",
    "Cost records",
)

_COST_HEADERS: tuple[str, ...] = (
    "Scope",
    "Total USD",
    "Records",
    "Priced records",
    "Unpriced records",
    "Input tokens",
    "Output tokens",
)


def _overview_rows(payload: Mapping[str, Any]) -> list[list[Any]]:
    """Project identity, loop health and the cost totals."""
    project = as_mapping(payload.get("project"))
    health = as_mapping(payload.get("health"))
    cost = as_mapping(payload.get("cost"))
    baseline = as_mapping(payload.get("architecture")).get("version", "")
    return [
        ["Report", "Architecture Lifecycle Assistant"],
        ["Schema version", payload.get("schema_version", "")],
        ["Generated at", payload.get("generated_at", "")],
        [],
        ["Project", project.get("name", "")],
        ["Plan version", project.get("plan_version", "")],
        ["Mode", project.get("mode", "")],
        ["Paused", bool(project.get("paused", False))],
        [],
        ["Current step", health.get("current_step_no")],
        ["Current state", health.get("current_state")],
        ["Step count", health.get("step_count")],
        ["Next step", health.get("next_step_no")],
        ["Complete", bool(health.get("complete", False))],
        ["Baseline version", baseline],
        [],
        ["Cost total (USD)", cost.get("total_usd")],
        ["Cost records", cost.get("record_count")],
        ["Priced records", cost.get("priced_record_count")],
        ["Unpriced records", cost.get("unpriced_record_count")],
        ["Input tokens", cost.get("input_tokens")],
        ["Output tokens", cost.get("output_tokens")],
    ]


def _cost_by_step(payload: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """The per-step cost summaries, keyed by step number."""
    summaries: dict[int, dict[str, Any]] = {}
    for entry in payload.get("cost_by_step") or ():
        item = as_mapping(entry)
        if "step_no" in item:
            summaries[int(item["step_no"])] = as_mapping(item.get("cost"))
    return summaries


def _step_rows(payload: Mapping[str, Any]) -> list[list[Any]]:
    """One row per step, with that step's cost totals alongside."""
    summaries = _cost_by_step(payload)
    rows: list[list[Any]] = [list(_STEP_HEADERS)]
    for entry in payload.get("steps") or ():
        step = as_mapping(entry)
        cost = summaries.get(int(step.get("step_no", 0)), {})
        rows.append(
            [
                step.get("step_no"),
                step.get("phase"),
                step.get("title"),
                step.get("state"),
                step.get("attempt"),
                step.get("max_attempts"),
                step.get("risk"),
                bool(step.get("requires_human", False)),
                step.get("created_at") or "",
                step.get("started_at") or "",
                step.get("finished_at") or "",
                step.get("verified_at") or "",
                cost.get("total_usd"),
                cost.get("record_count"),
            ]
        )
    return rows


def _cost_row(scope: str, summary: Mapping[str, Any]) -> list[Any]:
    """One cost aggregation row."""
    return [
        scope,
        summary.get("total_usd"),
        summary.get("record_count"),
        summary.get("priced_record_count"),
        summary.get("unpriced_record_count"),
        summary.get("input_tokens"),
        summary.get("output_tokens"),
    ]


def _cost_rows(payload: Mapping[str, Any]) -> list[list[Any]]:
    """The unfiltered total first, then one row per step."""
    rows: list[list[Any]] = [list(_COST_HEADERS)]
    rows.append(_cost_row("All", as_mapping(payload.get("cost"))))
    for step_no, summary in sorted(_cost_by_step(payload).items()):
        rows.append(_cost_row(f"Step {step_no}", summary))
    return rows


# ---------------------------------------------------------------------------
# artifact naming + the adapter
# ---------------------------------------------------------------------------

#: The artifact naming rule (project slug plus report timestamp) is shared with
#: the markdown adapter and lives in :mod:`._reporting`.


class ExcelReportingAdapter:
    """Writes one ``.xlsx`` workbook and returns where it landed.

    Implements ``ReportingPort``: ``render(payload)`` takes a
    ``ReportSnapshot.to_dict()`` mapping and returns the path of the artifact -
    the locator, never the content. The payload is the whole input, so this
    adapter never reads the source of truth and the export cannot mutate a
    Project, Step, Task, ADR, Risk, ArchitectureVersion, ACR or cost record.
    """

    def __init__(
        self,
        output_dir: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._output_dir = Path(output_dir)
        self._clock = clock

    @property
    def output_dir(self) -> Path:
        """The directory artifacts are written to (created on demand)."""
        return self._output_dir

    @property
    def sheet_names(self) -> tuple[str, ...]:
        """The sheets this export writes, in order."""
        return SHEET_NAMES

    def render(self, payload: Mapping[str, Any]) -> str:
        """Write the workbook for ``payload`` and return its absolute path."""
        if not isinstance(payload, Mapping):
            raise ValueError(
                f"payload must be a Mapping; got {type(payload).__name__}"
            )
        sheets = (
            _overview_rows(payload),
            _step_rows(payload),
            _cost_rows(payload),
        )
        target = self._output_dir / artifact_name(
            payload, self._clock(), suffix=".xlsx"
        )
        self._output_dir.mkdir(parents=True, exist_ok=True)
        _write_archive(target, _package(SHEET_NAMES, sheets))
        return str(target.resolve())
