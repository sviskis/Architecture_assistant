"""Audit trail domain model.

The audit trail is an **append-only, authoritative** log: every manager mutation
records exactly one entry. ``AuditEntry.detail`` is a deterministic,
JSON-serializable mapping (never free-form prose) so later steps - report,
monitor, control - can analyse it programmatically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any, ClassVar, Mapping

from .models import DomainModel, utc_now

__all__ = [
    "AuditEntityType",
    "AuditAction",
    "AuditEntry",
]


class AuditEntityType(StrEnum):
    """Kind of entity an audit entry belongs to.

    ``ADR`` and ``RISK`` are written by the Step 5 managers, ``STEP`` by the
    Step 9 orchestrator (exactly one entry per persisted Step transition) and
    ``ACR`` by the Step 11 architecture-evolution engine (one entry per
    lifecycle transition). ``PROJECT`` is written by the Step 20 human-override
    use-case (one entry per human control of the project aggregate). ``PLAN`` is
    written by the Step 25 plan loader (exactly one entry per imported plan, its
    ``entity_id`` being the plan hash). ``PROPOSAL`` is written by the Step 27
    managed-project architecture proposal use-cases (creation, approval,
    rejection and revision request; its ``entity_id`` is the proposal id).
    ``SUPERVISION`` is written by the Step 28 supervision use-case for the
    **durable decisions only** (a directive approved, rejected, waived,
    escalated or actually sent; its ``entity_id`` is the supervision id) - a
    polling tick, a duplicate tick and a read-only status query are deliberately
    never audited. ``DELIBERATION`` is written by the Step 29 controlled
    multi-agent architecture deliberation for the **lifecycle acts only**
    (created, a round/review/synthesis completed, proposal generated, a run
    cancelled); its ``entity_id`` is the deliberation id. A stage read, a GUI
    refresh, a snapshot and a popup are deliberately never audited. Each was
    added deliberately when its writer landed - audit entries are never written
    for an entity kind that does not exist here.
    """

    ADR = "ADR"
    RISK = "RISK"
    STEP = "STEP"
    ACR = "ACR"
    PROJECT = "PROJECT"
    PLAN = "PLAN"
    PROPOSAL = "PROPOSAL"
    SUPERVISION = "SUPERVISION"
    DELIBERATION = "DELIBERATION"


class AuditAction(StrEnum):
    """Canonical actions recorded by the managers.

    ``PAUSE``/``RESUME``/``SET_MODE`` are the project-control vocabulary of the
    Step 20 human-override use-case and apply to ``AuditEntityType.PROJECT``
    only. A **step** override deliberately adds no verb: the Step entity has
    exactly one transition action (``UPDATE``), and the FSM event that was
    applied is recorded in ``detail["event"]`` - so the human marker is
    ``detail["actor"]``/``detail["operation"]``, never a duplicate verb.
    ``IMPORT`` is the Step 25 plan-loading verb and applies to
    ``AuditEntityType.PLAN`` only: a plan is imported once, in one transaction,
    and its steps are created rather than transitioned. A **proposal** decision
    deliberately adds no verb either: ``CREATE`` records the proposal row,
    ``UPDATE`` records approval and revision requests (carrying the operation,
    the actor, the reason and the status it moved between in ``detail``) and
    ``REJECT`` records the rejection - exactly like the ACR lifecycle records
    its own transitions.
    """

    CREATE = "CREATE"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    DEPRECATE = "DEPRECATE"
    SUPERSEDE = "SUPERSEDE"
    AMEND = "AMEND"
    UPDATE = "UPDATE"
    MITIGATE = "MITIGATE"
    CLOSE = "CLOSE"
    REOPEN = "REOPEN"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    SET_MODE = "SET_MODE"
    IMPORT = "IMPORT"


def _as_enum(enum_cls: type, value: Any, field_name: str) -> Enum:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            pass
    valid = ", ".join(member.value for member in enum_cls)
    raise ValueError(f"{field_name} must be one of ({valid}); got {value!r}")


@dataclass(frozen=True)
class AuditEntry(DomainModel):
    """One append-only audit record.

    Exactly one entry is produced per application-service call. ``detail`` holds
    a structured, JSON-serializable payload describing the change.
    """

    entity_type: AuditEntityType
    entity_id: str
    action: AuditAction
    detail: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    _ENUM_FIELDS: ClassVar[Mapping[str, type]] = {
        "entity_type": AuditEntityType,
        "action": AuditAction,
    }
    _DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ("created_at",)
    _MAPPING_FIELDS: ClassVar[tuple[str, ...]] = ("detail",)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entity_type",
            _as_enum(AuditEntityType, self.entity_type, "entity_type"),
        )
        object.__setattr__(
            self, "action", _as_enum(AuditAction, self.action, "action")
        )
        if not isinstance(self.entity_id, str) or not self.entity_id.strip():
            raise ValueError(
                f"entity_id must be a non-empty string; got {self.entity_id!r}"
            )
        payload = dict(self.detail or {})
        try:
            json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"detail must be JSON-serializable; got {payload!r}"
            ) from error
        object.__setattr__(self, "detail", payload)
        if not isinstance(self.created_at, datetime):
            raise ValueError(
                f"created_at must be a datetime; got {self.created_at!r}"
            )

    def detail_json(self) -> str:
        """Deterministic JSON encoding of :attr:`detail` (sorted keys)."""
        return json.dumps(dict(self.detail), sort_keys=True)
