"""Tests for the Excel reporting adapter (Step 18).

Everything runs offline against a real generated ``.xlsx``: the archive is
opened, every part is parsed and the package is cross-checked (content types,
workbook relationships, worksheet targets). That is exactly the structural
contract a spreadsheet application relies on, so these tests fail if the
handwritten OOXML drifts.

Also pinned here: the ``ReportingPort.render`` semantics (the returned ``str``
is the artifact path), deterministic output, Excel-safe sheet names, correct
XML escaping, Latvian round-trip, honest cell types and the read-only boundary.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from architecture_assistant.architecture import (
    ARCHITECTURE_CURRENT,
    ROOT_PACKAGE,
    ArchitectureValidator,
    layer_of,
    scan_directory,
)
from architecture_assistant.infrastructure import ExcelReportingAdapter
from architecture_assistant.infrastructure.excel_reporting import SHEET_NAMES
from architecture_assistant.ports.capabilities import ReportingPort

NOW = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)

_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_PACKAGE_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CONTENT_TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"

#: The parts a minimal, spreadsheet-readable workbook must carry.
_STATIC_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
)

_COST_STEP_ONE = {
    "total_usd": 0.18,
    "input_tokens": 120,
    "output_tokens": 30,
    "record_count": 1,
    "priced_record_count": 1,
    "unpriced_record_count": 0,
}

_COST_STEP_TWO = {
    "total_usd": 0.0,
    "input_tokens": 0,
    "output_tokens": 0,
    "record_count": 0,
    "priced_record_count": 0,
    "unpriced_record_count": 0,
}


def make_payload(**overrides: Any) -> dict[str, Any]:
    """A payload shaped exactly like ``ReportSnapshot.to_dict()``."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": NOW.isoformat(),
        "project": {
            "name": "Architecture Lifecycle Assistant",
            "plan_version": "0.3",
            "mode": "MANUAL",
            "paused": False,
        },
        "steps": [
            {
                "step_no": 1,
                "phase": "FOUNDATION",
                "title": "Foundation and domain core",
                "state": "VERIFIED",
                "attempt": 1,
                "max_attempts": 3,
                "risk": "LOW",
                "requires_human": False,
                "created_at": NOW.isoformat(),
                "started_at": NOW.isoformat(),
                "finished_at": NOW.isoformat(),
                "verified_at": NOW.isoformat(),
            },
            {
                "step_no": 2,
                "phase": "FOUNDATION",
                "title": "Otrais solis - ā, ē, ī, ū, ķ",
                "state": "READY",
                "attempt": 1,
                "max_attempts": 3,
                "risk": "MEDIUM",
                "requires_human": True,
                "created_at": NOW.isoformat(),
                "started_at": None,
                "finished_at": None,
                "verified_at": None,
            },
        ],
        "tasks": [],
        "architecture": {"version": "1.1", "baseline": "Layered assistant"},
        "architecture_versions": [],
        "adrs": [],
        "risks": [],
        "findings": [],
        "decisions": [],
        "cost": {
            "total_usd": 0.18,
            "input_tokens": 120,
            "output_tokens": 30,
            "record_count": 1,
            "priced_record_count": 1,
            "unpriced_record_count": 0,
        },
        "cost_by_step": [
            {"step_no": 1, "cost": dict(_COST_STEP_ONE)},
            {"step_no": 2, "cost": dict(_COST_STEP_TWO)},
        ],
        "health": {
            "project_paused": False,
            "step_count": 2,
            "current_step_no": 2,
            "current_state": "READY",
            "next_step_no": None,
            "complete": False,
        },
    }
    payload.update(overrides)
    return payload


def read_parts(path: Path) -> dict[str, bytes]:
    """Every part of the workbook, keyed by its archive name."""
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def sheet_cells(parts: dict[str, bytes], index: int) -> list[list[tuple]]:
    """The rows of one worksheet as ``(kind, value)`` cell tuples."""
    root = ElementTree.fromstring(parts[f"xl/worksheets/sheet{index}.xml"])
    rows = []
    for row in root.iter(f"{_MAIN_NS}row"):
        rows.append([_cell_value(cell) for cell in row.findall(f"{_MAIN_NS}c")])
    return rows


def _cell_value(cell: ElementTree.Element) -> tuple:
    if cell.get("t") == "inlineStr":
        text = cell.find(f"{_MAIN_NS}is/{_MAIN_NS}t")
        return ("text", "" if text is None else text.text or "")
    value = cell.find(f"{_MAIN_NS}v")
    if value is None:
        return ("empty", None)
    if cell.get("t") == "b":
        return ("bool", value.text == "1")
    return ("number", value.text)


def column_values(rows: list[list[tuple]], column: int) -> list[Any]:
    """The raw values of one column, skipping the header row."""
    return [row[column][1] for row in rows[1:] if len(row) > column]


def make_adapter(tmp_path, **overrides: Any) -> ExcelReportingAdapter:
    kwargs: dict[str, Any] = {"clock": lambda: NOW}
    kwargs.update(overrides)
    return ExcelReportingAdapter(tmp_path / "reports", **kwargs)


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


_DOCUMENT_RELATIONSHIP_NS = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
)


class TestArtifactSemantics:
    """``render`` writes the workbook and returns its path - the locator."""

    def test_the_adapter_is_a_reporting_port(self, tmp_path) -> None:
        assert isinstance(make_adapter(tmp_path), ReportingPort)

    def test_render_returns_the_path_of_the_written_artifact(self, tmp_path) -> None:
        adapter = make_adapter(tmp_path)

        locator = adapter.render(make_payload())

        assert isinstance(locator, str)
        artifact = Path(locator)
        assert artifact.is_absolute()
        assert artifact.exists()
        assert artifact.suffix == ".xlsx"
        assert artifact.parent == Path(adapter.output_dir).resolve()

    def test_the_locator_is_a_path_and_not_the_document(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render(make_payload())

        assert locator.endswith(".xlsx")
        assert "<?xml" not in locator

    def test_the_output_directory_is_created_on_demand(self, tmp_path) -> None:
        adapter = make_adapter(tmp_path)

        assert not (tmp_path / "reports").exists()

        adapter.render(make_payload())

        assert (tmp_path / "reports").is_dir()
        assert adapter.output_dir == tmp_path / "reports"

    def test_the_artifact_name_is_deterministic(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render(make_payload())

        assert Path(locator).name == (
            "report-architecture-lifecycle-assistant-20260921T170000Z.xlsx"
        )

    def test_the_project_name_is_slugified(self, tmp_path) -> None:
        payload = make_payload(
            project={
                "name": "My/Weird Project (v2)!",
                "plan_version": "0.3",
                "mode": "MANUAL",
                "paused": False,
            }
        )

        locator = make_adapter(tmp_path).render(payload)

        assert Path(locator).name == (
            "report-my-weird-project-v2-20260921T170000Z.xlsx"
        )

    def test_a_missing_timestamp_falls_back_to_the_clock(self, tmp_path) -> None:
        payload = make_payload()
        payload.pop("generated_at")

        locator = make_adapter(tmp_path).render(payload)

        assert Path(locator).name.endswith("20260921T170000Z.xlsx")

    def test_a_non_mapping_payload_is_refused(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="must be a Mapping"):
            make_adapter(tmp_path).render("not a payload")

    def test_a_non_callable_clock_is_refused(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="callable"):
            ExcelReportingAdapter(tmp_path, clock="not callable")

    def test_an_empty_payload_still_produces_a_workbook(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render({})

        rows = sheet_cells(read_parts(Path(locator)), 1)
        assert rows[0][0] == ("text", "Report")
        assert Path(locator).name == "report-project-20260921T170000Z.xlsx"


class TestPackageStructure:
    """The generated file is a valid, internally consistent OOXML package."""

    def test_the_archive_carries_every_required_part(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        for name in _STATIC_PARTS:
            assert name in parts, name
        for index in range(1, len(SHEET_NAMES) + 1):
            assert f"xl/worksheets/sheet{index}.xml" in parts

    def test_every_part_is_well_formed_xml(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        for name, payload in parts.items():
            root = ElementTree.fromstring(payload)
            assert root.tag.startswith("{"), name

    def test_the_content_types_declare_every_part(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))
        root = ElementTree.fromstring(parts["[Content_Types].xml"])

        declared = {
            item.get("PartName")
            for item in root.findall(f"{_CONTENT_TYPES_NS}Override")
        }

        assert declared == {
            "/xl/workbook.xml",
            "/xl/worksheets/sheet1.xml",
            "/xl/worksheets/sheet2.xml",
            "/xl/worksheets/sheet3.xml",
        }

    def test_the_root_relationship_points_at_the_workbook(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))
        root = ElementTree.fromstring(parts["_rels/.rels"])

        targets = {
            item.get("Target")
            for item in root.findall(f"{_PACKAGE_NS}Relationship")
        }

        assert targets == {"xl/workbook.xml"}

    def test_every_sheet_relationship_resolves(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))
        workbook = ElementTree.fromstring(parts["xl/workbook.xml"])
        rels = ElementTree.fromstring(parts["xl/_rels/workbook.xml.rels"])
        targets = {
            item.get("Id"): item.get("Target")
            for item in rels.findall(f"{_PACKAGE_NS}Relationship")
        }

        sheet_ids = [
            sheet.get(f"{_DOCUMENT_RELATIONSHIP_NS}id")
            for sheet in workbook.iter(f"{_MAIN_NS}sheet")
        ]

        assert sheet_ids == ["rId1", "rId2", "rId3"]
        for sheet_id in sheet_ids:
            assert f"xl/{targets[sheet_id]}" in parts

    def test_the_workbook_names_its_sheets_in_order(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))
        workbook = ElementTree.fromstring(parts["xl/workbook.xml"])

        names = [
            sheet.get("name") for sheet in workbook.iter(f"{_MAIN_NS}sheet")
        ]

        assert tuple(names) == SHEET_NAMES
        assert len(names) == 3

    def test_the_sheet_names_are_spreadsheet_safe(self) -> None:
        assert SHEET_NAMES == ("Overview", "Steps", "Costs")
        for name in SHEET_NAMES:
            assert len(name) <= 31
            assert name.strip() == name
            assert not {":", "\\", "/", "?", "*", "[", "]", "'"} & set(name)


class TestSheetContents:
    """Exactly the three approved sheets, each with its own content."""

    def test_the_overview_sheet_carries_the_project_facts(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 1)
        facts = {row[0][1]: row[1][1] for row in rows if len(row) > 1}

        assert facts["Project"] == "Architecture Lifecycle Assistant"
        assert facts["Plan version"] == "0.3"
        assert facts["Mode"] == "MANUAL"
        assert facts["Paused"] is False
        assert facts["Baseline version"] == "1.1"

    def test_the_overview_sheet_carries_the_loop_health(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 1)
        facts = {row[0][1]: row[1][1] for row in rows if len(row) > 1}

        assert facts["Current step"] == "2"
        assert facts["Current state"] == "READY"
        assert facts["Step count"] == "2"
        assert facts["Complete"] is False

    def test_the_overview_sheet_carries_the_cost_totals(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 1)
        facts = {row[0][1]: row[1][1] for row in rows if len(row) > 1}

        assert facts["Cost total (USD)"] == "0.18"
        assert facts["Cost records"] == "1"
        assert facts["Priced records"] == "1"
        assert facts["Unpriced records"] == "0"

    def test_the_overview_labels_follow_the_defined_order(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 1)
        labels = [row[0][1] for row in rows if row]

        assert labels[:4] == [
            "Report",
            "Schema version",
            "Generated at",
            "Project",
        ]
        assert labels[-1] == "Output tokens"

    def test_the_steps_sheet_has_one_row_per_step(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 2)

        assert [cell[1] for cell in rows[0]] == [
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
        ]
        assert len(rows) == 3
        assert column_values(rows, 0) == ["1", "2"]
        assert column_values(rows, 3) == ["VERIFIED", "READY"]
        assert column_values(rows, 6) == ["LOW", "MEDIUM"]
        assert column_values(rows, 7) == [False, True]

    def test_the_steps_sheet_carries_each_step_cost(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 2)

        assert column_values(rows, 12) == ["0.18", "0.0"]
        assert column_values(rows, 13) == ["1", "0"]

    def test_the_costs_sheet_lists_the_total_then_each_step(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 3)

        assert [cell[1] for cell in rows[0]] == [
            "Scope",
            "Total USD",
            "Records",
            "Priced records",
            "Unpriced records",
            "Input tokens",
            "Output tokens",
        ]
        assert len(rows) == 4
        assert column_values(rows, 0) == ["All", "Step 1", "Step 2"]
        assert column_values(rows, 1) == ["0.18", "0.18", "0.0"]
        assert column_values(rows, 5) == ["120", "120", "0"]


class TestCellTypes:
    """Numbers, text and booleans stay distinguishable in the file."""

    def test_each_cell_type_is_written_as_itself(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 2)

        assert rows[0][0] == ("text", "Step")
        assert rows[1][0] == ("number", "1")
        assert rows[1][2] == ("text", "Foundation and domain core")
        assert rows[1][7] == ("bool", False)
        assert rows[2][7] == ("bool", True)

    def test_a_number_is_never_written_as_text(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        xml = parts["xl/worksheets/sheet1.xml"].decode("utf-8")

        assert "<v>0.18</v>" in xml
        assert "<t>0.18</t>" not in xml

    def test_an_empty_value_keeps_its_cell_address(self, tmp_path) -> None:
        """A sparse row stays rectangular, so columns never shift."""
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 2)

        assert len(rows[1]) == len(rows[0]) == 14
        assert rows[2][10] == ("empty", None)

    def test_a_boolean_is_not_confused_with_a_number(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        xml = parts["xl/worksheets/sheet2.xml"].decode("utf-8")

        assert 't="b"' in xml
        assert "<v>1</v>" in xml
        assert "<v>0</v>" in xml


class TestTextEncoding:
    """Escaping and Unicode survive the round trip."""

    def test_latvian_text_round_trips(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        rows = sheet_cells(parts, 2)

        assert rows[2][2] == ("text", "Otrais solis - ā, ē, ī, ū, ķ")

    def test_xml_special_characters_are_escaped(self, tmp_path) -> None:
        payload = make_payload()
        payload["steps"][0]["title"] = "A & B <tag> quoted"

        parts = read_parts(Path(make_adapter(tmp_path).render(payload)))
        rows = sheet_cells(parts, 2)
        raw = parts["xl/worksheets/sheet2.xml"].decode("utf-8")

        assert rows[1][2] == ("text", "A & B <tag> quoted")
        assert "&amp;" in raw
        assert "&lt;tag&gt;" in raw

    def test_the_declared_encoding_is_utf8(self, tmp_path) -> None:
        parts = read_parts(Path(make_adapter(tmp_path).render(make_payload())))

        for name in ("xl/workbook.xml", "xl/worksheets/sheet1.xml"):
            assert parts[name].decode("utf-8").startswith("<?xml")


class TestDeterminism:
    """The same payload always yields the same file."""

    def test_the_same_payload_produces_byte_identical_files(self, tmp_path) -> None:
        first = Path(make_adapter(tmp_path).render(make_payload())).read_bytes()
        second = Path(make_adapter(tmp_path).render(make_payload())).read_bytes()

        assert first == second

    def test_the_archive_entry_order_is_fixed(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render(make_payload())

        with zipfile.ZipFile(locator) as archive:
            names = archive.namelist()

        assert names == list(_STATIC_PARTS) + [
            "xl/worksheets/sheet1.xml",
            "xl/worksheets/sheet2.xml",
            "xl/worksheets/sheet3.xml",
        ]

    def test_the_zip_timestamps_are_fixed(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render(make_payload())

        with zipfile.ZipFile(locator) as archive:
            stamps = {item.date_time for item in archive.infolist()}

        assert stamps == {(1980, 1, 1, 0, 0, 0)}

    def test_the_row_and_column_order_is_fixed(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render(make_payload())

        with zipfile.ZipFile(locator) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

        assert xml.index('r="A1"') < xml.index('r="B1"')
        assert xml.index('r="A1"') < xml.index('r="A2"')


class TestReadOnlyBoundary:
    """The adapter renders a payload; it never reaches the source of truth."""

    def test_the_adapter_imports_no_repository_or_database(self) -> None:
        source = scan_directory(_src_root())
        targets = source.imports_of(
            f"{ROOT_PACKAGE}.infrastructure.excel_reporting"
        )

        assert not any("ports.repositories" in target for target in targets)
        assert not any(target == "sqlite3" for target in targets)
        assert not any("infrastructure.cost" in target for target in targets)

    def test_the_adapter_imports_no_provider_adapter(self) -> None:
        source = scan_directory(_src_root())
        targets = source.imports_of(
            f"{ROOT_PACKAGE}.infrastructure.excel_reporting"
        )

        for provider in ("openai", "claude", "grok", "openai_judge"):
            assert not any(
                target == f"{ROOT_PACKAGE}.infrastructure.{provider}"
                for target in targets
            ), provider

    def test_the_adapter_never_imports_outwards(self) -> None:
        source = scan_directory(_src_root())

        for target in source.imports_of(
            f"{ROOT_PACKAGE}.infrastructure.excel_reporting"
        ):
            assert layer_of(target) not in (
                "architecture",
                "composition",
                "application",
            )

    def test_rendering_never_mutates_the_payload(self, tmp_path) -> None:
        payload = make_payload()
        before = json.loads(json.dumps(payload))

        make_adapter(tmp_path).render(payload)

        assert payload == before

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        source = scan_directory(_src_root())
        result = ArchitectureValidator().validate(source)

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
