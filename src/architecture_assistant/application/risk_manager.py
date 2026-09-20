"""Risk Manager - the deterministic risk register.

Every mutating call runs in **one** transaction: the risk write and **exactly
one** audit entry commit together or roll back together. Status changes follow a
fixed transition table.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import RiskStatus, Severity
from ..domain.models import Risk
from ..ports.repositories import AuditRepository, RiskRepository
from ..ports.transactions import TransactionPort

__all__ = [
    "RiskManagerError",
    "RiskNotFoundError",
    "InvalidRiskStatusTransitionError",
    "RISK_TRANSITIONS",
    "RiskManager",
]


class RiskManagerError(Exception):
    """Base class for risk manager errors."""


class RiskNotFoundError(RiskManagerError):
    """Raised when the referenced risk does not exist."""


class InvalidRiskStatusTransitionError(RiskManagerError):
    """Raised when a risk status transition is not allowed."""


#: Allowed risk status transitions (deterministic lifecycle).
RISK_TRANSITIONS: Mapping[RiskStatus, frozenset] = {
    RiskStatus.OPEN: frozenset(
        {RiskStatus.MITIGATED, RiskStatus.ACCEPTED, RiskStatus.CLOSED}
    ),
    RiskStatus.MITIGATED: frozenset({RiskStatus.CLOSED}),
    RiskStatus.ACCEPTED: frozenset({RiskStatus.CLOSED}),
    RiskStatus.CLOSED: frozenset({RiskStatus.OPEN}),
}


class RiskManager:
    """Risk register lifecycle with an atomic append-only audit trail."""

    def __init__(
        self,
        risks: RiskRepository,
        audit: AuditRepository,
        transactions: TransactionPort,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._risks = risks
        self._audit = audit
        self._transactions = transactions
        self._clock = clock

    # -- lifecycle -------------------------------------------------------
    def register(
        self,
        risk_id: str,
        description: str,
        *,
        severity: Severity = Severity.MEDIUM,
        probability: float = 0.5,
        impact: Severity = Severity.MEDIUM,
        owner: str = "",
        mitigation: str = "",
    ) -> Risk:
        """Register a new risk in status ``OPEN``."""
        if self._risks.get(risk_id) is not None:
            raise RiskManagerError(f"risk {risk_id!r} already exists")

        now = self._clock()
        risk = Risk(
            id=risk_id,
            description=description,
            severity=severity,
            probability=probability,
            impact=impact,
            owner=owner,
            mitigation=mitigation,
            status=RiskStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
        self._commit(
            risk,
            AuditAction.CREATE,
            {
                "id": risk.id,
                "severity": risk.severity.value,
                "probability": float(risk.probability),
                "impact": risk.impact.value,
                "owner": risk.owner,
                "status": risk.status.value,
            },
            now,
        )
        return risk

    def update(
        self,
        risk_id: str,
        *,
        description: Optional[str] = None,
        severity: Optional[Severity] = None,
        probability: Optional[float] = None,
        impact: Optional[Severity] = None,
        owner: Optional[str] = None,
        mitigation: Optional[str] = None,
    ) -> Risk:
        """Update risk fields; the status is left untouched."""
        risk = self._require(risk_id)
        changes: dict[str, Any] = {}
        if description is not None:
            changes["description"] = description
        if severity is not None:
            changes["severity"] = severity
        if probability is not None:
            changes["probability"] = float(probability)
        if impact is not None:
            changes["impact"] = impact
        if owner is not None:
            changes["owner"] = owner
        if mitigation is not None:
            changes["mitigation"] = mitigation
        if not changes:
            raise RiskManagerError("update requires at least one changed field")

        now = self._clock()
        updated = replace(risk, updated_at=now, **changes)
        self._commit(
            updated,
            AuditAction.UPDATE,
            {"risk_id": risk.id, "changed_fields": sorted(changes)},
            now,
        )
        return updated

    def mitigate(self, risk_id: str) -> Risk:
        """``OPEN`` -> ``MITIGATED``."""
        return self._change_status(
            risk_id, RiskStatus.MITIGATED, AuditAction.MITIGATE
        )

    def accept(self, risk_id: str) -> Risk:
        """``OPEN`` -> ``ACCEPTED`` (the risk is accepted as it is)."""
        return self._change_status(
            risk_id, RiskStatus.ACCEPTED, AuditAction.ACCEPT
        )

    def close(self, risk_id: str) -> Risk:
        """``OPEN``/``MITIGATED``/``ACCEPTED`` -> ``CLOSED``."""
        return self._change_status(risk_id, RiskStatus.CLOSED, AuditAction.CLOSE)

    def reopen(self, risk_id: str) -> Risk:
        """``CLOSED`` -> ``OPEN``."""
        return self._change_status(risk_id, RiskStatus.OPEN, AuditAction.REOPEN)

    # -- internals -------------------------------------------------------
    def _require(self, risk_id: str) -> Risk:
        risk = self._risks.get(risk_id)
        if risk is None:
            raise RiskNotFoundError(f"risk {risk_id!r} does not exist")
        return risk

    def _ensure_transition(self, risk: Risk, target: RiskStatus) -> None:
        allowed = RISK_TRANSITIONS.get(risk.status, frozenset())
        if target not in allowed:
            options = (
                ", ".join(sorted(status.value for status in allowed)) or "<none>"
            )
            raise InvalidRiskStatusTransitionError(
                f"risk {risk.id!r} cannot move from {risk.status.value} to "
                f"{target.value}; allowed from {risk.status.value}: {options}"
            )

    def _change_status(
        self, risk_id: str, target: RiskStatus, action: AuditAction
    ) -> Risk:
        risk = self._require(risk_id)
        self._ensure_transition(risk, target)
        now = self._clock()
        updated = replace(risk, status=target, updated_at=now)
        self._commit(
            updated,
            action,
            {
                "risk_id": updated.id,
                "from": risk.status.value,
                "to": target.value,
            },
            now,
        )
        return updated

    def _commit(
        self,
        risk: Risk,
        action: AuditAction,
        detail: Mapping[str, Any],
        now: datetime,
    ) -> None:
        """Persist the risk write and exactly one audit entry atomically."""
        entry = AuditEntry(
            entity_type=AuditEntityType.RISK,
            entity_id=risk.id,
            action=action,
            detail=dict(detail),
            created_at=now,
        )
        with self._transactions.transaction():
            self._risks.upsert(risk)
            self._audit.append(entry)

