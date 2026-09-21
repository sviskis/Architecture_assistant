"""Tests for the markdown reporting adapter (Step 21).

Everything runs offline against a real generated ``.md`` file: the document is
read back as UTF-8, its section structure is compared with ``SECTION_NAMES`` and
every table is decoded row by row with a real markdown table parser (unescaped
pipes only), so the escaping is checked by the same rule a renderer uses.

Also pinned here: the ``ReportingPort.render`` semantics (the returned ``str`` is
the artifact path), deterministic output, a hostile project name that can never
leave the output directory, agreement with the Excel adapter on the same
snapshot, and the read-only boundary.
"""

from __future__ import annotations

import inspect
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
from architecture_assistant.infrastructure import (
    ExcelReportingAdapter,
    MarkdownReportingAdapter,
)
from architecture_assistant.infrastructure.markdown_reporting import SECTION_NAMES
from architecture_assistant.ports.capabilities import ReportingPort

NOW = datetime(2026, 9, 21, 21, 0, tzinfo=timezone.utc)
LATER = NOW.replace(hour=23)

#: A title carrying every character that can break a table.
HOSTILE_TITLE = "ADR with | pipe, \\ backslash and \nsplit line"

#: Unique markers that must never reach the document.
TASK_MARKER = "TASK-MARKER-MUST-NOT-APPEAR"
FINDING_MARKER = "FINDING-MARKER-MUST-NOT-APPEAR"
DECISION_MARKER = "DECISION-MARKER-MUST-NOT-APPEAR"

_COST_ONE = {
    "total_usd": 0.18,
    "input_tokens": 120,
    "output_tokens": 30,
    "record_count": 1,
    "priced_record_count": 1,
    "unpriced_record_count": 0,
}

_COST_TWO = {
    "total_usd": 0.5,
    "input_tokens": 10,
    "output_tokens": 5,
    "record_count": 2,
    "priced_record_count": 1,
    "unpriced_record_count": 1,
}


def make_payload(**overrides: Any) -> dict[str, Any]:
    """A payload shaped exactly like ``ReportSnapshot.to_dict()``."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": NOW.isoformat(),
        "project": {
            "name": "Architecture Lifecycle Assistant",
            "plan_version": "0.3",
            "plan_hash": "abc123",
            "mode": "MANUAL",
            "paused": False,
        },
        "architecture": {
            "version": "1.1",
            "baseline": "Layered assistant core",
            "rules": ["RULE-1", "RULE-2"],
            "superseded_by": None,
            "is_current": True,
            "created_at": NOW.isoformat(),
        },
        "architecture_versions": [
            {
                "version": "1.0",
                "baseline": "Foundations",
                "is_current": False,
                "superseded_by": "1.1",
                "created_at": NOW.isoformat(),
            },
            {
                "version": "1.1",
                "baseline": "Layered assistant core",
                "is_current": True,
                "superseded_by": None,
                "created_at": NOW.isoformat(),
            },
        ],
        "adrs": [
            {
                "id": "ADR-001",
                "title": "Import rules",
                "status": "ACCEPTED",
                "version": 1,
                "superseded_by": None,
                "created_at": NOW.isoformat(),
            },
            {
                "id": "ADR-002",
                "title": HOSTILE_TITLE,
                "status": "SUPERSEDED",
                "version": 2,
                "superseded_by": "ADR-003",
                "created_at": NOW.isoformat(),
            },
        ],
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
                "phase": "EXPORT",
                "title": "Otrais solis - ā, ē, ī, ū, ķ",
                "state": "READY",
                "attempt": 2,
                "max_attempts": 3,
                "risk": "MEDIUM",
                "requires_human": True,
                "created_at": NOW.isoformat(),
                "started_at": None,
                "finished_at": None,
                "verified_at": None,
            },
        ],
        "risks": [
            {
                "id": "RISK-001",
                "description": "Open risk",
                "severity": "HIGH",
                "probability": 0.3,
                "impact": "HIGH",
                "owner": "architect",
                "mitigation": "watch it",
                "status": "OPEN",
                "created_at": NOW.isoformat(),
            },
            {
                "id": "RISK-002",
                "description": "Mitigated risk",
                "severity": "LOW",
                "probability": 0.1,
                "impact": "LOW",
                "owner": "architect",
                "mitigation": "done",
                "status": "MITIGATED",
                "created_at": NOW.isoformat(),
            },
            {
                "id": "RISK-003",
                "description": "Closed risk",
                "severity": "LOW",
                "probability": 0.1,
                "impact": "LOW",
                "owner": "architect",
                "mitigation": "done",
                "status": "CLOSED",
                "created_at": NOW.isoformat(),
            },
        ],
        "change_requests": [
            {
                "request_id": "ACR-002",
                "title": "Add composition layer",
                "status": "APPLIED",
                "source_version": "1.0",
                "target_version": "1.1",
                "adr_id": "ADR-001",
                "rule_ids": ["RULE-1", "RULE-2"],
                "roadmap_impact": ["Step 9", "Step 11"],
                "rationale": "the graph needs one owner",
                "approved_by": "architect",
                "created_at": NOW.isoformat(),
            },
            {
                "request_id": "ACR-001",
                "title": "Proposed change",
                "status": "PROPOSED",
                "source_version": "1.1",
                "target_version": "1.2",
                "adr_id": "ADR-002",
                "rule_ids": ["RULE-3"],
                "roadmap_impact": [],
                "rationale": "",
                "approved_by": None,
                "created_at": NOW.isoformat(),
            },
        ],
        "cost": {
            "total_usd": 0.68,
            "input_tokens": 130,
            "output_tokens": 35,
            "record_count": 3,
            "priced_record_count": 2,
            "unpriced_record_count": 1,
        },
        "cost_by_step": [
            {"step_no": 1, "cost": dict(_COST_ONE)},
            {"step_no": 2, "cost": dict(_COST_TWO)},
        ],
        "health": {
            "project_paused": False,
            "step_count": 2,
            "current_step_no": 2,
            "current_state": "READY",
            "next_step_no": None,
            "complete": False,
        },
        "tasks": [{"title": TASK_MARKER}],
        "findings": [{"id": "F-1", "claim": FINDING_MARKER}],
        "decisions": [{"id": "D-1", "decision": DECISION_MARKER}],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# reading helpers - a real markdown table parser
# ---------------------------------------------------------------------------

def _cells(line: str) -> tuple[str, ...]:
    """Split one table row on *unescaped* pipes only.

    This is the rule a markdown renderer applies, so it is also the honest way to
    check the escaping: an escaped ``\\|`` stays inside its cell, a bare ``|``
    starts a new one.
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells: list[str] = []
    current = ""
    escaped = False
    for character in stripped:
        if escaped:
            current += character
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append(current.strip())
            current = ""
        else:
            current += character
    cells.append(current.strip())
    return tuple(cells)


def read_document(locator: str) -> str:
    """The rendered document, read back as UTF-8."""
    return Path(locator).read_text(encoding="utf-8")


def section(document: str, name: str) -> list[str]:
    """The lines of one ``## name`` section, the heading excluded."""
    lines = document.splitlines()
    start = lines.index(f"## {name}")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def table(lines: list[str]) -> list[tuple[str, ...]]:
    """Every row of a section's table, the separator row dropped."""
    rows = [_cells(line) for line in lines if line.startswith("|")]
    return [row for row in rows if set(row) != {"---"}]


def facts(lines: list[str]) -> dict[str, str]:
    """A two-column section as a mapping, so values can be asserted by name."""
    rows = table(lines)
    return {row[0]: row[1] for row in rows[1:]}


def make_adapter(tmp_path: Any) -> MarkdownReportingAdapter:
    return MarkdownReportingAdapter(tmp_path, clock=lambda: NOW)


def render(
    adapter: MarkdownReportingAdapter, payload: dict[str, Any]
) -> tuple[str, str]:
    """Render a payload and return ``(locator, document)``."""
    locator = adapter.render(payload)
    return locator, read_document(locator)


def _src_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


class TestArtifactSemantics:
    """``render`` writes the document and returns its path - the locator."""

    def test_the_adapter_is_a_reporting_port(self, tmp_path) -> None:
        assert isinstance(make_adapter(tmp_path), ReportingPort)

    def test_render_returns_the_path_of_the_written_artifact(
        self, tmp_path
    ) -> None:
        locator = make_adapter(tmp_path).render(make_payload())

        assert isinstance(locator, str)
        artifact = Path(locator)
        assert artifact.is_absolute()
        assert artifact.exists()
        assert artifact.suffix == ".md"

    def test_the_locator_is_a_path_and_not_the_document(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render(make_payload())

        assert "# Architecture Lifecycle Assistant" not in locator
        assert "# Architecture Lifecycle Assistant" in read_document(locator)

    def test_the_output_directory_is_created_on_demand(self, tmp_path) -> None:
        target = tmp_path / "nested" / "reports"

        locator = MarkdownReportingAdapter(
            target, clock=lambda: NOW
        ).render(make_payload())

        assert Path(locator).parent == target.resolve()
        assert target.is_dir()

    def test_the_artifact_name_is_deterministic(self, tmp_path) -> None:
        first = make_adapter(tmp_path).render(make_payload())
        second = make_adapter(tmp_path).render(make_payload())

        assert first == second

    def test_the_project_name_is_slugified(self, tmp_path) -> None:
        payload = make_payload(
            project={"name": "Architecture Lifecycle Assistant!"}
        )

        locator = make_adapter(tmp_path).render(payload)

        assert Path(locator).name == (
            "report-architecture-lifecycle-assistant-20260921T210000Z.md"
        )

    def test_a_missing_timestamp_falls_back_to_the_clock(self, tmp_path) -> None:
        payload = make_payload()
        payload.pop("generated_at")

        locator = make_adapter(tmp_path).render(payload)

        assert Path(locator).name.endswith("-20260921T210000Z.md")

    def test_an_invalid_timestamp_falls_back_to_the_clock(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render(
            make_payload(generated_at="not-a-timestamp")
        )

        assert Path(locator).name.endswith("-20260921T210000Z.md")

    def test_a_non_mapping_payload_is_refused(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="payload must be a Mapping"):
            make_adapter(tmp_path).render(["not", "a", "mapping"])

    def test_a_non_callable_clock_is_refused(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="clock"):
            MarkdownReportingAdapter(tmp_path, clock="not callable")

    def test_an_empty_payload_still_produces_a_document(self, tmp_path) -> None:
        locator, document = render(make_adapter(tmp_path), {})

        assert document.startswith("# Project\n")
        assert [line for line in document.splitlines() if line.startswith("## ")] == [
            f"## {name}" for name in SECTION_NAMES
        ]
        assert "No entries." in document


class TestStructure:
    """The document structure is the ``SECTION_NAMES`` contract, nothing else."""

    def test_the_document_is_titled_with_the_project_name(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        assert document.splitlines()[0] == "# Architecture Lifecycle Assistant"

    def test_every_section_appears_exactly_once_in_order(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        headings = [
            line for line in document.splitlines() if line.startswith("## ")
        ]

        assert headings == [f"## {name}" for name in SECTION_NAMES]

    def test_the_adapter_declares_the_same_sections(self, tmp_path) -> None:
        assert make_adapter(tmp_path).section_names == SECTION_NAMES

    def test_an_empty_section_keeps_its_heading_and_its_table(self, tmp_path) -> None:
        payload = make_payload(adrs=[], risks=[], change_requests=[])

        _, document = render(make_adapter(tmp_path), payload)

        for name, headers in (
            ("ADR index", ("ID", "Title", "Status", "Version", "Superseded by", "Created at")),
            ("Open risks", ("ID", "Description", "Severity", "Probability", "Impact", "Owner", "Mitigation", "Status", "Created at")),
        ):
            body = section(document, name)
            assert table(body) == [headers], name
            assert "No entries." in body, name

    def test_tasks_findings_and_decisions_are_not_rendered(self, tmp_path) -> None:
        """Step 21 does not require them, so they are deliberately left out."""
        _, document = render(make_adapter(tmp_path), make_payload())

        assert TASK_MARKER not in document
        assert FINDING_MARKER not in document
        assert DECISION_MARKER not in document

    def test_one_render_writes_one_artifact(self, tmp_path) -> None:
        locator, _ = render(make_adapter(tmp_path), make_payload())

        assert [path.name for path in Path(tmp_path).iterdir()] == [
            Path(locator).name
        ]


class TestContent:
    """Every section renders the payload's own values."""

    def test_the_project_summary_carries_the_project_facts(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        values = facts(section(document, "Project summary"))

        assert values["Project"] == "Architecture Lifecycle Assistant"
        assert values["Plan version"] == "0.3"
        assert values["Plan hash"] == "abc123"
        assert values["Mode"] == "MANUAL"
        assert values["Paused"] == "false"
        assert values["Schema version"] == "1.0"
        assert values["Generated at"] == NOW.isoformat()

    def test_a_paused_project_is_reported_as_paused(self, tmp_path) -> None:
        payload = make_payload(
            project={"name": "x", "mode": "AUTO", "paused": True}
        )

        _, document = render(make_adapter(tmp_path), payload)

        assert facts(section(document, "Project summary"))["Paused"] == "true"

    def test_the_current_architecture_carries_the_baseline(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        values = facts(section(document, "Current architecture"))

        assert values["Version"] == "1.1"
        assert values["Baseline"] == "Layered assistant core"
        assert values["Rules"] == "RULE-1, RULE-2"
        assert values["Superseded by"] == "-"

    def test_a_missing_current_baseline_renders_a_note(self, tmp_path) -> None:
        _, document = render(
            make_adapter(tmp_path), make_payload(architecture=None)
        )

        assert "No entries." in section(document, "Current architecture")

    def test_the_version_history_lists_every_version_in_order(
        self, tmp_path
    ) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "Architecture versions"))

        assert rows[0] == (
            "Version",
            "Baseline",
            "Current",
            "Superseded by",
            "Created at",
        )
        assert [row[0] for row in rows[1:]] == ["1.0", "1.1"]
        assert rows[1][2] == "false"
        assert rows[2][2] == "true"

    def test_the_adr_index_lists_every_adr_in_order(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "ADR index"))

        assert rows[0] == (
            "ID",
            "Title",
            "Status",
            "Version",
            "Superseded by",
            "Created at",
        )
        assert [row[0] for row in rows[1:]] == ["ADR-001", "ADR-002"]
        assert rows[1][2] == "ACCEPTED"
        assert rows[2][4] == "ADR-003"

    def test_the_step_status_lists_every_step_in_order(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "Step status"))

        assert rows[0][:8] == (
            "Step",
            "Phase",
            "Title",
            "State",
            "Attempt",
            "Max attempts",
            "Risk",
            "Requires human",
        )
        assert [row[0] for row in rows[1:]] == ["1", "2"]
        assert rows[1][8:12] == (NOW.isoformat(),) * 4
        assert rows[2][9] == "-"  # never started
        assert rows[2][13] == "2"  # cost records

    def test_the_step_status_carries_each_steps_own_cost(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "Step status"))

        assert rows[1][12] == "0.18"
        assert rows[1][13] == "1"
        assert rows[2][12] == "0.5"

    def test_the_open_risks_section_lists_only_open_risks(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        body = section(document, "Open risks")
        rows = table(body)
        text = "\n".join(body)

        assert [row[0] for row in rows[1:]] == ["RISK-001"]
        assert rows[1][7] == "OPEN"
        assert "RISK-002" not in text
        assert "RISK-003" not in text

    def test_the_change_requests_section_carries_the_evolution_trail(
        self, tmp_path
    ) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "Architecture change requests"))

        assert [row[0] for row in rows[1:]] == ["ACR-002", "ACR-001"]
        assert rows[1][3] == "1.0 -> 1.1"
        assert rows[1][5] == "RULE-1, RULE-2"
        assert rows[1][6] == "Step 9, Step 11"
        assert rows[2][6] == "-"  # an empty roadmap impact
        assert rows[2][8] == "-"  # no approver yet

    def test_the_cost_summary_lists_the_total_then_each_step(
        self, tmp_path
    ) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "Cost summary"))

        assert rows[0] == (
            "Scope",
            "Total USD",
            "Records",
            "Priced records",
            "Unpriced records",
            "Input tokens",
            "Output tokens",
        )
        assert [row[0] for row in rows[1:]] == ["All", "Step 1", "Step 2"]
        assert rows[1][1] == "0.68"
        assert rows[1][5] == "130"
        assert rows[2][1] == "0.18"
        assert rows[3][1] == "0.5"

    def test_the_cost_summary_is_ordered_by_step_number(self, tmp_path) -> None:
        payload = make_payload(
            cost_by_step=[
                {"step_no": 2, "cost": dict(_COST_TWO)},
                {"step_no": 1, "cost": dict(_COST_ONE)},
            ]
        )

        _, document = render(make_adapter(tmp_path), payload)

        rows = table(section(document, "Cost summary"))

        assert [row[0] for row in rows[1:]] == ["All", "Step 1", "Step 2"]

    def test_the_loop_health_section_carries_the_loop_facts(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        values = facts(section(document, "Loop health"))

        assert values["Project paused"] == "false"
        assert values["Step count"] == "2"
        assert values["Current step"] == "2"
        assert values["Current state"] == "READY"
        assert values["Next step"] == "-"
        assert values["Complete"] == "false"

    def test_a_paused_loop_is_reported_as_paused(self, tmp_path) -> None:
        payload = make_payload(
            health={
                "project_paused": True,
                "step_count": 0,
                "current_step_no": None,
                "current_state": None,
                "next_step_no": None,
                "complete": False,
            }
        )

        _, document = render(make_adapter(tmp_path), payload)

        values = facts(section(document, "Loop health"))

        assert values["Project paused"] == "true"
        assert values["Current state"] == "-"
        assert values["Current step"] == "-"


class TestCellFormatting:
    """The payload's JSON types keep their meaning inside a cell."""

    def test_none_and_empty_become_the_empty_marker(self, tmp_path) -> None:
        payload = make_payload(
            adrs=[
                {
                    "id": "ADR-001",
                    "title": "",
                    "status": None,
                    "version": 1,
                    "superseded_by": None,
                    "created_at": NOW.isoformat(),
                }
            ]
        )

        _, document = render(make_adapter(tmp_path), payload)

        row = table(section(document, "ADR index"))[1]

        assert row[1] == "-"
        assert row[2] == "-"
        assert row[4] == "-"

    def test_booleans_are_spelled_like_json(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "Step status"))

        assert rows[1][7] == "false"
        assert rows[2][7] == "true"

    def test_numbers_keep_their_payload_spelling(self, tmp_path) -> None:
        payload = make_payload(
            cost={
                "total_usd": 0.30000000000000004,
                "input_tokens": 7,
                "output_tokens": 0,
                "record_count": 7,
                "priced_record_count": 1,
                "unpriced_record_count": 6,
            }
        )

        _, document = render(make_adapter(tmp_path), payload)

        row = table(section(document, "Cost summary"))[1]

        assert row[1] == repr(0.30000000000000004)
        assert row[2] == "7"
        assert row[5] == "7"

    def test_lists_are_comma_joined_in_payload_order(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        baseline = facts(section(document, "Current architecture"))
        requests = table(section(document, "Architecture change requests"))

        assert baseline["Rules"] == "RULE-1, RULE-2"
        assert requests[1][5] == "RULE-1, RULE-2"
        assert requests[1][6] == "Step 9, Step 11"

    def test_enum_values_are_written_verbatim(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        row = table(section(document, "Step status"))[2]

        assert row[1] == "EXPORT"
        assert row[3] == "READY"
        assert row[6] == "MEDIUM"


class TestEscaping:
    """Escaping keeps the table structure intact, whatever the data says."""

    def test_a_pipe_stays_inside_its_cell(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "ADR index"))

        assert len(rows[0]) == 6
        assert {len(row) for row in rows} == {6}  # no row gained a column
        assert rows[2][1] == HOSTILE_TITLE.replace("\n", " ")

    def test_a_backslash_round_trips(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        cell = table(section(document, "ADR index"))[2][1]

        assert " \\ " in cell  # the literal backslash survived verbatim
        assert cell.count("\\") == 1

    def test_a_newline_cannot_create_a_row(self, tmp_path) -> None:
        payload = make_payload(
            adrs=[
                {
                    "id": "ADR-009",
                    "title": "first\nsecond\r\nthird",
                    "status": "ACCEPTED",
                    "version": 1,
                    "superseded_by": None,
                    "created_at": NOW.isoformat(),
                }
            ]
        )

        _, document = render(make_adapter(tmp_path), payload)

        rows = table(section(document, "ADR index"))

        assert len(rows) == 2  # header plus exactly one row
        assert rows[1][1] == "first second third"

    def test_surrounding_whitespace_is_trimmed(self, tmp_path) -> None:
        payload = make_payload(
            adrs=[
                {
                    "id": "  ADR-010  ",
                    "title": "   padded   ",
                    "status": "ACCEPTED",
                    "version": 1,
                    "superseded_by": None,
                    "created_at": NOW.isoformat(),
                }
            ]
        )

        _, document = render(make_adapter(tmp_path), payload)

        row = table(section(document, "ADR index"))[1]

        assert row[0] == "ADR-010"
        assert row[1] == "padded"

    def test_only_structure_breaking_characters_are_escaped(self, tmp_path) -> None:
        payload = make_payload(
            adrs=[
                {
                    "id": "ADR-011",
                    "title": "a & b <c> d",
                    "status": "ACCEPTED",
                    "version": 1,
                    "superseded_by": None,
                    "created_at": NOW.isoformat(),
                }
            ]
        )

        _, document = render(make_adapter(tmp_path), payload)

        assert table(section(document, "ADR index"))[1][1] == "a & b <c> d"

    def test_latvian_text_round_trips(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        rows = table(section(document, "Step status"))

        assert rows[2][2] == "Otrais solis - ā, ē, ī, ū, ķ"

    def test_the_file_is_utf8_with_lf_endings(self, tmp_path) -> None:
        locator, document = render(make_adapter(tmp_path), make_payload())

        raw = Path(locator).read_bytes()

        assert raw.decode("utf-8") == document
        assert b"\r\n" not in raw


#: The spreadsheet namespace, for the cross-format comparison below.
_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def sheet_rows(workbook: Path, index: int) -> list[tuple[str, ...]]:
    """The rows of one worksheet of a real ``.xlsx`` as plain strings.

    A minimal reader (inline strings, numbers, booleans), so the two adapters'
    view of the same snapshot can be compared without reaching into the Excel
    module's internals.
    """
    with zipfile.ZipFile(workbook) as archive:
        xml = archive.read(f"xl/worksheets/sheet{index}.xml").decode("utf-8")
    rows: list[tuple[str, ...]] = []
    for row in ElementTree.fromstring(xml).iter(f"{_MAIN_NS}row"):
        cells: list[str] = []
        for cell in row.iter(f"{_MAIN_NS}c"):
            if cell.get("t") == "inlineStr":
                text = cell.find(f"{_MAIN_NS}is/{_MAIN_NS}t")
                cells.append(text.text if text is not None and text.text else "")
                continue
            value = cell.find(f"{_MAIN_NS}v")
            raw = value.text if value is not None and value.text else ""
            if cell.get("t") == "b":
                raw = "true" if raw == "1" else "false"
            cells.append(raw)
        rows.append(tuple(cells))
    return rows


class TestNaming:
    """The artifact name is deterministic and cannot leave the directory."""

    def test_a_hostile_project_name_cannot_escape_the_directory(
        self, tmp_path
    ) -> None:
        target = tmp_path / "reports"
        payload = make_payload(
            project={"name": "../../etc/passwd", "mode": "AUTO"}
        )

        locator = MarkdownReportingAdapter(
            target, clock=lambda: NOW
        ).render(payload)

        artifact = Path(locator)
        assert artifact.parent == target.resolve()
        assert artifact.name == "report-etc-passwd-20260921T210000Z.md"

    def test_a_windows_style_path_cannot_escape_the_directory(
        self, tmp_path
    ) -> None:
        target = tmp_path / "reports"
        payload = make_payload(
            project={"name": "..\\..\\Windows\\System32", "mode": "AUTO"}
        )

        locator = MarkdownReportingAdapter(
            target, clock=lambda: NOW
        ).render(payload)

        assert Path(locator).parent == target.resolve()
        assert Path(locator).name == "report-windows-system32-20260921T210000Z.md"

    def test_a_name_without_usable_characters_falls_back(self, tmp_path) -> None:
        locator = make_adapter(tmp_path).render(
            make_payload(project={"name": "***", "mode": "AUTO"})
        )

        assert Path(locator).name == "report-project-20260921T210000Z.md"

    def test_the_same_payload_overwrites_the_same_path(self, tmp_path) -> None:
        first, _ = render(make_adapter(tmp_path), make_payload())
        second, _ = render(make_adapter(tmp_path), make_payload())

        assert first == second
        assert [path.name for path in Path(tmp_path).iterdir()] == [
            Path(first).name
        ]


class TestDeterminism:
    """One payload, one document - byte for byte."""

    def test_the_same_payload_produces_byte_identical_files(
        self, tmp_path
    ) -> None:
        first = Path(make_adapter(tmp_path).render(make_payload())).read_bytes()
        second = Path(make_adapter(tmp_path).render(make_payload())).read_bytes()

        assert first == second

    def test_the_document_ends_with_a_single_newline(self, tmp_path) -> None:
        _, document = render(make_adapter(tmp_path), make_payload())

        assert document.endswith("\n")
        assert not document.endswith("\n\n")

    def test_the_section_order_is_stable(self, tmp_path) -> None:
        _, first = render(make_adapter(tmp_path), make_payload())
        _, second = render(make_adapter(tmp_path), make_payload())

        def headings(text: str) -> list[str]:
            return [
                line for line in text.splitlines() if line.startswith("## ")
            ]

        assert headings(first) == headings(second)
        assert headings(first) == [f"## {name}" for name in SECTION_NAMES]

    def test_the_payload_row_order_is_preserved(self, tmp_path) -> None:
        payload = make_payload()

        _, document = render(make_adapter(tmp_path), payload)

        assert [
            row[0] for row in table(section(document, "ADR index"))[1:]
        ] == [adr["id"] for adr in payload["adrs"]]
        assert [
            row[0]
            for row in table(section(document, "Architecture change requests"))[
                1:
            ]
        ] == [
            request["request_id"] for request in payload["change_requests"]
        ]
        assert [
            row[0] for row in table(section(document, "Step status"))[1:]
        ] == [str(step["step_no"]) for step in payload["steps"]]


class TestExcelAgreement:
    """The spreadsheet and the document read one snapshot the same way."""

    def test_both_artifacts_share_the_slug_and_the_stamp(self, tmp_path) -> None:
        payload = make_payload()

        excel = Path(
            ExcelReportingAdapter(tmp_path, clock=lambda: NOW).render(payload)
        )
        markdown = Path(
            MarkdownReportingAdapter(tmp_path, clock=lambda: NOW).render(payload)
        )

        assert excel.parent == markdown.parent
        assert excel.name.removeprefix("report-").removesuffix(".xlsx") == (
            markdown.name.removeprefix("report-").removesuffix(".md")
        )
        assert {path.name for path in tmp_path.iterdir()} == {
            excel.name,
            markdown.name,
        }

    def test_both_artifacts_list_the_same_costs(self, tmp_path) -> None:
        payload = make_payload()
        excel = Path(
            ExcelReportingAdapter(tmp_path, clock=lambda: NOW).render(payload)
        )
        _, document = render(
            MarkdownReportingAdapter(tmp_path, clock=lambda: NOW), payload
        )

        assert sheet_rows(excel, 3) == table(
            section(document, "Cost summary")
        )

    def test_both_artifacts_list_the_same_steps(self, tmp_path) -> None:
        payload = make_payload()
        excel = Path(
            ExcelReportingAdapter(tmp_path, clock=lambda: NOW).render(payload)
        )
        _, document = render(
            MarkdownReportingAdapter(tmp_path, clock=lambda: NOW), payload
        )

        excel_rows = sheet_rows(excel, 2)
        markdown_rows = table(section(document, "Step status"))

        assert excel_rows[0] == markdown_rows[0]  # the very same columns
        assert [row[:8] for row in excel_rows[1:]] == [
            row[:8] for row in markdown_rows[1:]
        ]
        assert [row[12:] for row in excel_rows[1:]] == [
            row[12:] for row in markdown_rows[1:]
        ]

    def test_the_only_difference_is_how_absence_is_shown(self, tmp_path) -> None:
        """Excel leaves the cell blank, markdown shows ``-``: same meaning."""
        payload = make_payload()
        excel = Path(
            ExcelReportingAdapter(tmp_path, clock=lambda: NOW).render(payload)
        )
        _, document = render(
            MarkdownReportingAdapter(tmp_path, clock=lambda: NOW), payload
        )

        excel_rows = sheet_rows(excel, 2)
        markdown_rows = table(section(document, "Step status"))

        assert excel_rows[2][9] == ""  # started_at of step 2
        assert markdown_rows[2][9] == "-"


class TestReadOnlyBoundary:
    """The adapter renders a payload; it never reaches the source of truth."""

    def test_the_adapter_imports_no_repository_or_database(self) -> None:
        targets = scan_directory(_src_root()).imports_of(
            f"{ROOT_PACKAGE}.infrastructure.markdown_reporting"
        )

        assert not any("ports.repositories" in target for target in targets)
        assert not any(target == "sqlite3" for target in targets)
        assert not any("infrastructure.cost" in target for target in targets)

    def test_the_adapter_imports_no_monitor_and_no_human_override(self) -> None:
        targets = scan_directory(_src_root()).imports_of(
            f"{ROOT_PACKAGE}.infrastructure.markdown_reporting"
        )

        assert not any("monitor" in target for target in targets)
        assert not any("human_override" in target for target in targets)
        assert not any("transactions" in target for target in targets)

    def test_the_adapter_never_imports_outwards(self) -> None:
        source = scan_directory(_src_root())

        for target in source.imports_of(
            f"{ROOT_PACKAGE}.infrastructure.markdown_reporting"
        ):
            assert layer_of(target) not in (
                "architecture",
                "composition",
                "application",
            )

    def test_the_constructor_takes_only_an_output_directory_and_a_clock(
        self,
    ) -> None:
        parameters = set(
            inspect.signature(MarkdownReportingAdapter.__init__).parameters
        )

        assert parameters == {"self", "output_dir", "clock"}
        for forbidden in (
            "storage",
            "transactions",
            "projects",
            "steps",
            "audit",
            "monitor",
            "human_override",
            "report_builder",
        ):
            assert forbidden not in parameters

    def test_rendering_never_mutates_the_payload(self, tmp_path) -> None:
        payload = make_payload()
        before = json.loads(json.dumps(payload))

        make_adapter(tmp_path).render(payload)

        assert payload == before

    def test_the_whole_tree_still_satisfies_the_current_baseline(self) -> None:
        result = ArchitectureValidator().validate(scan_directory(_src_root()))

        assert result.is_compliant is True
        assert result.violations == ()
        assert result.baseline_version == ARCHITECTURE_CURRENT.version
