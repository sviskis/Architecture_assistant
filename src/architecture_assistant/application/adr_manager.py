"""ADR Manager - deterministic, versioned Architecture Decision Records.

Every mutating call runs in **one** transaction: the ADR write and **exactly one**
audit entry commit together or roll back together. Status changes follow a fixed
transition table; ADR content changes increment the ADR ``version``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import ADRStatus
from ..domain.models import ADR
from ..ports.repositories import ADRRepository, AuditRepository
from ..ports.transactions import TransactionPort

__all__ = [
    "AdrManagerError",
    "AdrNotFoundError",
    "InvalidAdrStatusTransitionError",
    "InvalidSupersessionError",
    "ADR_TRANSITIONS",
    "ADRManager",
]


class AdrManagerError(Exception):
    """Base class for ADR manager errors."""


class AdrNotFoundError(AdrManagerError):
    """Raised when the referenced ADR does not exist."""


class InvalidAdrStatusTransitionError(AdrManagerError):
    """Raised when a status transition is not allowed."""


class InvalidSupersessionError(AdrManagerError):
    """Raised when a supersession would be inconsistent."""


#: Allowed ADR status transitions (deterministic lifecycle).
ADR_TRANSITIONS: Mapping[ADRStatus, frozenset] = {
    ADRStatus.PROPOSED: frozenset({ADRStatus.ACCEPTED, ADRStatus.REJECTED}),
    ADRStatus.ACCEPTED: frozenset({ADRStatus.DEPRECATED, ADRStatus.SUPERSEDED}),
    ADRStatus.DEPRECATED: frozenset({ADRStatus.SUPERSEDED}),
    ADRStatus.REJECTED: frozenset(),
    ADRStatus.SUPERSEDED: frozenset(),
}


class ADRManager:
    """Versioned ADR lifecycle with an atomic append-only audit trail."""

    def __init__(
        self,
        adrs: ADRRepository,
        audit: AuditRepository,
        transactions: TransactionPort,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._adrs = adrs
        self._audit = audit
        self._transactions = transactions
        self._clock = clock

    # -- lifecycle -------------------------------------------------------
    def propose(
        self,
        adr_id: str,
        title: str,
        *,
        context: str = "",
        decision: str = "",
        consequences: Sequence[str] = (),
        related: Sequence[str] = (),
    ) -> ADR:
        """Create a new ADR at version 1 in status ``PROPOSED``."""
        if self._adrs.get(adr_id) is not None:
            raise AdrManagerError(f"ADR {adr_id!r} already exists")

        now = self._clock()
        adr = ADR(
            id=adr_id,
            title=title,
            status=ADRStatus.PROPOSED,
            context=context,
            decision=decision,
            consequences=tuple(consequences),
            version=1,
            superseded_by=None,
            related=tuple(related),
            created_at=now,
            updated_at=now,
        )
        self._commit(
            (adr,),
            adr.id,
            AuditAction.CREATE,
            {
                "id": adr.id,
                "title": adr.title,
                "version": adr.version,
                "status": adr.status.value,
            },
            now,
        )
        return adr

    def accept(self, adr_id: str) -> ADR:
        """``PROPOSED`` -> ``ACCEPTED``."""
        return self._change_status(
            adr_id, ADRStatus.ACCEPTED, AuditAction.ACCEPT
        )

    def reject(self, adr_id: str) -> ADR:
        """``PROPOSED`` -> ``REJECTED``."""
        return self._change_status(
            adr_id, ADRStatus.REJECTED, AuditAction.REJECT
        )

    def deprecate(self, adr_id: str) -> ADR:
        """``ACCEPTED`` -> ``DEPRECATED``."""
        return self._change_status(
            adr_id, ADRStatus.DEPRECATED, AuditAction.DEPRECATE
        )

    def amend(
        self,
        adr_id: str,
        *,
        title: Optional[str] = None,
        context: Optional[str] = None,
        decision: Optional[str] = None,
        consequences: Optional[Sequence[str]] = None,
        related: Optional[Sequence[str]] = None,
    ) -> ADR:
        """Amend ADR content and increment its ``version``.

        Status is a lifecycle concern, so ``amend`` leaves it untouched.
        """
        adr = self._require(adr_id)
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title
        if context is not None:
            changes["context"] = context
        if decision is not None:
            changes["decision"] = decision
        if consequences is not None:
            changes["consequences"] = tuple(consequences)
        if related is not None:
            changes["related"] = tuple(related)
        if not changes:
            raise AdrManagerError("amend requires at least one changed field")

        now = self._clock()
        updated = replace(adr, version=adr.version + 1, updated_at=now, **changes)
        self._commit(
            (updated,),
            adr.id,
            AuditAction.AMEND,
            {
                "adr_id": adr.id,
                "previous_version": adr.version,
                "new_version": updated.version,
                "changed_fields": sorted(changes),
            },
            now,
        )
        return updated

    def supersede(self, adr_id: str, successor_id: str) -> tuple[ADR, ADR]:
        """Supersede ``adr_id`` with ``successor_id``.

        Produces exactly **one** audit entry, recorded against the superseded ADR,
        whose detail captures both sides of the relationship.

        Returns ``(superseded_adr, successor_adr)``.
        """
        old = self._require(adr_id)
        successor = self._require(successor_id)
        if old.id == successor.id:
            raise InvalidSupersessionError(
                f"ADR {old.id!r} cannot supersede itself"
            )
        if old.superseded_by is not None:
            raise InvalidSupersessionError(
                f"ADR {old.id!r} is already superseded by {old.superseded_by!r}"
            )
        self._ensure_transition(old, ADRStatus.SUPERSEDED)
        if successor.status not in (ADRStatus.PROPOSED, ADRStatus.ACCEPTED):
            raise InvalidSupersessionError(
                f"successor {successor.id!r} must be PROPOSED or ACCEPTED; "
                f"got {successor.status.value}"
            )

        now = self._clock()
        updated_old = replace(
            old,
            status=ADRStatus.SUPERSEDED,
            superseded_by=successor.id,
            updated_at=now,
        )
        updated_successor = (
            replace(successor, status=ADRStatus.ACCEPTED, updated_at=now)
            if successor.status is ADRStatus.PROPOSED
            else successor
        )
        self._commit(
            (updated_old, updated_successor),
            updated_old.id,
            AuditAction.SUPERSEDE,
            {
                "old_id": updated_old.id,
                "successor_id": updated_successor.id,
                "old_status": old.status.value,
                "old_status_after": updated_old.status.value,
                "successor_status": updated_successor.status.value,
            },
            now,
        )
        return updated_old, updated_successor

    # -- internals -------------------------------------------------------
    def _require(self, adr_id: str) -> ADR:
        adr = self._adrs.get(adr_id)
        if adr is None:
            raise AdrNotFoundError(f"ADR {adr_id!r} does not exist")
        return adr

    def _ensure_transition(self, adr: ADR, target: ADRStatus) -> None:
        allowed = ADR_TRANSITIONS.get(adr.status, frozenset())
        if target not in allowed:
            options = (
                ", ".join(sorted(status.value for status in allowed)) or "<none>"
            )
            raise InvalidAdrStatusTransitionError(
                f"ADR {adr.id!r} cannot move from {adr.status.value} to "
                f"{target.value}; allowed from {adr.status.value}: {options}"
            )

    def _change_status(
        self, adr_id: str, target: ADRStatus, action: AuditAction
    ) -> ADR:
        adr = self._require(adr_id)
        self._ensure_transition(adr, target)
        now = self._clock()
        updated = replace(adr, status=target, updated_at=now)
        self._commit(
            (updated,),
            updated.id,
            action,
            {
                "adr_id": updated.id,
                "from": adr.status.value,
                "to": target.value,
            },
            now,
        )
        return updated

    def _commit(
        self,
        adrs: Sequence[ADR],
        audit_entity_id: str,
        action: AuditAction,
        detail: Mapping[str, Any],
        now: datetime,
    ) -> None:
        """Persist the ADR write(s) and exactly one audit entry atomically."""
        entry = AuditEntry(
            entity_type=AuditEntityType.ADR,
            entity_id=audit_entity_id,
            action=action,
            detail=dict(detail),
            created_at=now,
        )
        with self._transactions.transaction():
            for adr in adrs:
                self._adrs.upsert(adr)
            self._audit.append(entry)

