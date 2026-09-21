"""Markdown reporting adapter - writes a read-only ``.md`` artifact.

``ReportingPort.render`` semantics
----------------------------------
Identical to the Excel adapter: the returned ``str`` is the **artifact locator**
- the path of the written file - never the document content. One payload in, one
path out, one file on disk, both artifacts in the same ``report_dir``.

The same projection, the same facts
-----------------------------------
The document is rendered from exactly the payload the spreadsheet is rendered
from: ``ReportSnapshot.to_dict()``, produced by the application layer
(``ReportBuilder``). Nothing here reads a repository, opens a database or talks
to SQLite, so the markdown view can never disagree with the Excel view about a
value. Where a section overlaps the spreadsheet (Step status, Cost summary) it
uses the very same columns, in the very same order.

The two formats differ only in depth. A markdown table cannot carry raw free
text, so cells are escaped and collapsed onto one line - which is why:

* the canonical **lossless** forms remain the snapshot payload and the Excel
  artifact;
* this document is the **human-readable summary** of the same state.

Read-only
---------
Writing the artifact cannot change a Project, Step, Task, ADR, Risk,
ArchitectureVersion, ACR or cost record. The adapter is handed a mapping, it
holds an output directory and a clock, and it imports neither a repository port,
nor ``sqlite3``, nor the monitor, nor the human-override use-case.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from ..domain.models import utc_now
from ._reporting import artifact_name, as_mapping

__all__ = [
    "SECTION_NAMES",
    "MarkdownReportingAdapter",
]

#: Rendered in place of a missing value (``None`` or an empty string).
EMPTY_CELL = "-"

#: Rendered under a table that has no rows, so an empty section still shows its
#: structure instead of silently disappearing.
NO_ENTRIES = "No entries."

#: The line breaks a single cell must never contain: a raw one would start a new
#: table row and break the document structure.
_LINE_BREAKS: tuple[str, ...] = ("\r\n", "\r", "\n")


# ---------------------------------------------------------------------------
# formatting primitives
# ---------------------------------------------------------------------------

def _single_line(value: Any) -> str:
    """Collapse a value onto one trimmed line (no cell escaping)."""
    text = str(value)
    for line_break in _LINE_BREAKS:
        text = text.replace(line_break, " ")
    return text.strip()


def _text(value: Any) -> str:
    """One cell's text: single line, pipe and backslash escaped.

    Order matters: the backslash is escaped **first**, otherwise an
    already-escaped pipe (``\\|``) would turn into an escaped backslash followed
    by a live pipe - which is exactly the way to break a table. ``<`` and ``>``
    are deliberately left alone; they cannot break a table.
    """
    return _single_line(value).replace("\\", "\\\\").replace("|", "\\|")


def _cell(value: Any) -> str:
    """One table cell: deterministic, escaped and never multi-line.

    The types mirror the payload exactly - ``ReportSnapshot.to_dict()`` has
    already turned enums into their ``UPPER`` values and datetimes into ISO
    strings, so only the six JSON types can appear here. Booleans stay ``true``/
    ``false`` and floats keep the very same ``repr`` spelling the Excel adapter
    writes, so one snapshot reads identically in both artifacts.
    """
    if value is None:
        return EMPTY_CELL
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(_cell(item) for item in value) or EMPTY_CELL
    return _text(value) or EMPTY_CELL


def _row(values: Any) -> str:
    """One markdown table row."""
    return "| " + " | ".join(_cell(value) for value in values) + " |"


def _separator(width: int) -> str:
    """The header/body separator of a ``width``-column table."""
    return "| " + " | ".join("---" for _ in range(width)) + " |"


def _table(headers: tuple[str, ...], rows: Any) -> list[str]:
    """A complete markdown table: header, separator, then the rows."""
    lines = [_row(headers), _separator(len(headers))]
    body = [_row(values) for values in rows]
    lines.extend(body if body else [NO_ENTRIES])
    return lines


def _facts(pairs: Any) -> list[str]:
    """A two-column "field / value" table."""
    return _table(("Field", "Value"), pairs)


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def _project_summary(payload: Mapping[str, Any]) -> list[str]:
    """Who the project is, how it is governed and when this snapshot was taken."""
    project = as_mapping(payload.get("project"))
    return _facts(
        (
            ("Project", project.get("name")),
            ("Plan version", project.get("plan_version")),
            ("Plan hash", project.get("plan_hash")),
            ("Mode", project.get("mode")),
            ("Paused", bool(project.get("paused", False))),
            ("Schema version", payload.get("schema_version")),
            ("Generated at", payload.get("generated_at")),
        )
    )


def _current_architecture(payload: Mapping[str, Any]) -> list[str]:
    """The single authoritative baseline, or a note when none is current."""
    current = payload.get("architecture")
    if not isinstance(current, Mapping):
        return [NO_ENTRIES]
    baseline = as_mapping(current)
    return _facts(
        (
            ("Version", baseline.get("version")),
            ("Baseline", baseline.get("baseline")),
            ("Rules", baseline.get("rules")),
            ("Superseded by", baseline.get("superseded_by")),
            ("Created at", baseline.get("created_at")),
        )
    )


def _architecture_versions(payload: Mapping[str, Any]) -> list[str]:
    """The version history, in the projection's order."""
    rows = [
        (
            version.get("version"),
            version.get("baseline"),
            version.get("is_current"),
            version.get("superseded_by"),
            version.get("created_at"),
        )
        for version in (
            as_mapping(entry)
            for entry in payload.get("architecture_versions") or ()
        )
    ]
    return _table(
        ("Version", "Baseline", "Current", "Superseded by", "Created at"), rows
    )


def _adr_index(payload: Mapping[str, Any]) -> list[str]:
    """The ADR index: identity and lifecycle, not the prose."""
    rows = [
        (
            adr.get("id"),
            adr.get("title"),
            adr.get("status"),
            adr.get("version"),
            adr.get("superseded_by"),
            adr.get("created_at"),
        )
        for adr in (as_mapping(entry) for entry in payload.get("adrs") or ())
    ]
    return _table(
        ("ID", "Title", "Status", "Version", "Superseded by", "Created at"),
        rows,
    )


def _step_costs(payload: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """The per-step cost summaries, keyed by step number.

    A traversal of this module's own: only the four naming and payload
    primitives in :mod:`._reporting` are shared with the Excel adapter, and each
    adapter owns the shaping of its own output.
    """
    summaries: dict[int, dict[str, Any]] = {}
    for entry in payload.get("cost_by_step") or ():
        item = as_mapping(entry)
        if "step_no" in item:
            summaries[int(item["step_no"])] = as_mapping(item.get("cost"))
    return summaries


#: Deliberately the same columns, in the same order, as the Excel ``Steps``
#: sheet: the roadmap reads identically in both artifacts.
_STEP_COLUMNS: tuple[str, ...] = (
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


def _step_status(payload: Mapping[str, Any]) -> list[str]:
    """The ordered build plan - the roadmap - with each step's own cost."""
    costs = _step_costs(payload)
    rows = []
    for entry in payload.get("steps") or ():
        step = as_mapping(entry)
        cost = costs.get(int(step.get("step_no", 0)), {})
        rows.append(
            (
                step.get("step_no"),
                step.get("phase"),
                step.get("title"),
                step.get("state"),
                step.get("attempt"),
                step.get("max_attempts"),
                step.get("risk"),
                bool(step.get("requires_human", False)),
                step.get("created_at"),
                step.get("started_at"),
                step.get("finished_at"),
                step.get("verified_at"),
                cost.get("total_usd"),
                cost.get("record_count"),
            )
        )
    return _table(_STEP_COLUMNS, rows)


#: The only risk status this section lists.
_OPEN_RISK_STATUS = "OPEN"


def _open_risks(payload: Mapping[str, Any]) -> list[str]:
    """The risk register, filtered to the entries that are still ``OPEN``.

    The filter matches the read-only monitor's view, so the document and the
    monitor never disagree about what is open. Mitigated, accepted and closed
    risks stay visible in the snapshot payload and in the Excel artifact.
    """
    rows = []
    for entry in payload.get("risks") or ():
        risk = as_mapping(entry)
        if risk.get("status") != _OPEN_RISK_STATUS:
            continue
        rows.append(
            (
                risk.get("id"),
                risk.get("description"),
                risk.get("severity"),
                risk.get("probability"),
                risk.get("impact"),
                risk.get("owner"),
                risk.get("mitigation"),
                risk.get("status"),
                risk.get("created_at"),
            )
        )
    return _table(
        (
            "ID",
            "Description",
            "Severity",
            "Probability",
            "Impact",
            "Owner",
            "Mitigation",
            "Status",
            "Created at",
        ),
        rows,
    )


#: The evolution trail's columns; ``Source -> Target`` keeps both baseline
#: versions in one cell so the table stays readable.
_CHANGE_REQUEST_COLUMNS: tuple[str, ...] = (
    "Request",
    "Title",
    "Status",
    "Source -> Target",
    "ADR",
    "Rules",
    "Roadmap impact",
    "Rationale",
    "Approved by",
    "Created at",
)


def _change_requests(payload: Mapping[str, Any]) -> list[str]:
    """The evolution trail: which change moved the baseline, and to what."""
    rows = []
    for entry in payload.get("change_requests") or ():
        request = as_mapping(entry)
        rows.append(
            (
                request.get("request_id"),
                request.get("title"),
                request.get("status"),
                f"{request.get('source_version')} -> "
                f"{request.get('target_version')}",
                request.get("adr_id"),
                request.get("rule_ids"),
                request.get("roadmap_impact"),
                request.get("rationale"),
                request.get("approved_by"),
                request.get("created_at"),
            )
        )
    return _table(_CHANGE_REQUEST_COLUMNS, rows)


#: Deliberately the same columns as the Excel ``Costs`` sheet.
_COST_COLUMNS: tuple[str, ...] = (
    "Scope",
    "Total USD",
    "Records",
    "Priced records",
    "Unpriced records",
    "Input tokens",
    "Output tokens",
)


def _cost_values(scope: str, summary: Mapping[str, Any]) -> tuple[Any, ...]:
    """One cost row - the same seven values the Excel ``Costs`` sheet writes."""
    return (
        scope,
        summary.get("total_usd"),
        summary.get("record_count"),
        summary.get("priced_record_count"),
        summary.get("unpriced_record_count"),
        summary.get("input_tokens"),
        summary.get("output_tokens"),
    )


def _cost_summary(payload: Mapping[str, Any]) -> list[str]:
    """The unfiltered total first, then one row per step, ascending."""
    costs = _step_costs(payload)
    rows = [_cost_values("All", as_mapping(payload.get("cost")))]
    rows.extend(
        _cost_values(f"Step {step_no}", costs[step_no])
        for step_no in sorted(costs)
    )
    return _table(_COST_COLUMNS, rows)


def _loop_health(payload: Mapping[str, Any]) -> list[str]:
    """The loop's own read-only health values."""
    health = as_mapping(payload.get("health"))
    return _facts(
        (
            ("Project paused", bool(health.get("project_paused", False))),
            ("Step count", health.get("step_count")),
            ("Current step", health.get("current_step_no")),
            ("Current state", health.get("current_state")),
            ("Next step", health.get("next_step_no")),
            ("Complete", bool(health.get("complete", False))),
        )
    )


#: Every section builder, in document order. The renderer iterates *this* tuple,
#: so a section title is written once and :data:`SECTION_NAMES` can never drift
#: away from what the document actually contains.
_SECTION_BUILDERS: tuple[
    tuple[str, Callable[[Mapping[str, Any]], list[str]]], ...
] = (
    ("Project summary", _project_summary),
    ("Current architecture", _current_architecture),
    ("Architecture versions", _architecture_versions),
    ("ADR index", _adr_index),
    ("Step status", _step_status),
    ("Open risks", _open_risks),
    ("Architecture change requests", _change_requests),
    ("Cost summary", _cost_summary),
    ("Loop health", _loop_health),
)

#: The nine sections this export writes, in order. Step 21 requires architecture
#: versions, an ADR index, risks, the roadmap and status snapshots: the roadmap
#: is represented by the ordered Step status section plus each change request's
#: roadmap impact, never by a section of its own.
SECTION_NAMES: tuple[str, ...] = tuple(name for name, _ in _SECTION_BUILDERS)


# ---------------------------------------------------------------------------
# the document + the adapter
# ---------------------------------------------------------------------------

def _document(payload: Mapping[str, Any]) -> str:
    """The whole document: one title, then every section, in order.

    Line endings are ``\\n`` and the file ends with exactly one of them, so one
    payload produces byte-identical content on every platform.
    """
    project = as_mapping(payload.get("project"))
    title = _single_line(project.get("name") or "") or "Project"
    blocks: list[str] = [f"# {title}", "", "Architecture status report."]
    for name, build in _SECTION_BUILDERS:
        blocks.append("")
        blocks.append(f"## {name}")
        blocks.append("")
        blocks.extend(build(payload))
    return "\n".join(blocks) + "\n"


class MarkdownReportingAdapter:
    """Writes one ``.md`` document and returns where it landed.

    Implements ``ReportingPort``: ``render(payload)`` takes a
    ``ReportSnapshot.to_dict()`` mapping and returns the path of the artifact -
    the locator, never the content. The constructor takes an output directory
    and a clock and **nothing else**: no repository, no storage facade, no
    transaction, no monitor and no human override, so this adapter cannot reach
    the source of truth even by accident.
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
    def section_names(self) -> tuple[str, ...]:
        """The sections this export writes, in order."""
        return SECTION_NAMES

    def render(self, payload: Mapping[str, Any]) -> str:
        """Write the document for ``payload`` and return its absolute path."""
        if not isinstance(payload, Mapping):
            raise ValueError(
                f"payload must be a Mapping; got {type(payload).__name__}"
            )
        document = _document(payload)
        target = self._output_dir / artifact_name(
            payload, self._clock(), suffix=".md"
        )
        self._output_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(document, encoding="utf-8", newline="\n")
        return str(target.resolve())

