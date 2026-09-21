"""Shared, provider-neutral primitives of the reporting adapters.

This module is **private infrastructure** (the leading underscore is
deliberate). It exists for exactly one reason: two adapters - the Excel workbook
(Step 18) and the markdown document (Step 21) - write their artifacts into the
same ``reports`` directory and therefore have to obey the very same two rules:

* **the artifact name is a pure function of the payload** (project slug plus the
  report timestamp), so one snapshot always lands on one predictable path and
  two adapters can never disagree about the name of the same report;
* **the payload is traversed defensively**, because a reporting adapter may never
  fail on a missing key - it renders whatever the projection produced.

It deliberately stays narrow, because a shared layer that grows into a generic
reporting framework is worse than duplication:

* no rendering and no document model;
* no tables, no columns, no cell logic - Excel and markdown each own their
  output format completely;
* no payload knowledge beyond "it is a mapping";
* no repository, no database, no clock ownership.

Nothing here is part of the public API: this module adds **nothing** to
``architecture_assistant.infrastructure.__all__``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

__all__ = [
    "as_mapping",
    "slug",
    "utc_stamp",
    "artifact_name",
]

#: Fallback slug when a project name carries no usable character at all.
DEFAULT_SLUG = "project"


def as_mapping(value: Any) -> dict[str, Any]:
    """A nested payload object as a plain mapping (never ``None``)."""
    return dict(value) if isinstance(value, Mapping) else {}


def slug(value: str) -> str:
    """A filesystem-safe slug (letters, digits, dash and underscore only).

    Path separators, drive letters and ``..`` cannot survive this: every
    character outside ``[A-Za-z0-9_-]`` collapses to a dash, runs of dashes
    collapse to one and an empty result falls back to ``project``. An artifact
    name built from a hostile project name therefore stays inside its directory.
    """
    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value.strip().lower()
    )
    collapsed = "-".join(part for part in cleaned.split("-") if part)
    return collapsed or DEFAULT_SLUG


def utc_stamp(generated_at: Any, fallback: datetime) -> str:
    """UTC ``YYYYMMDDTHHMMSSZ`` from the payload's stamp, else from the clock."""
    moment = fallback
    if isinstance(generated_at, str) and generated_at.strip():
        try:
            moment = datetime.fromisoformat(generated_at)
        except ValueError:
            moment = fallback
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def artifact_name(
    payload: Mapping[str, Any], now: datetime, *, suffix: str
) -> str:
    """Deterministic artifact name: project slug plus the report timestamp.

    ``suffix`` is the only part an adapter chooses (``.xlsx``, ``.md``); the
    naming rule itself - and therefore the slug and the stamp - is shared, so the
    spreadsheet and the markdown document of one snapshot always agree.
    """
    project = as_mapping(payload.get("project"))
    name = slug(str(project.get("name") or DEFAULT_SLUG))
    return f"report-{name}-{utc_stamp(payload.get('generated_at'), now)}{suffix}"
