"""Architecture evolution - the change-request lifecycle of the baseline.

An architecture change is not a code edit: it is a **request** with a lifecycle,
a reason, an approver and a version bump, and all of it is authoritative state
that survives restarts.

Lifecycle (``ArchitectureChangeRequest``, persisted)
----------------------------------------------------
::

    propose(acr, adr=...)   -> ACR PROPOSED   + ADR PROPOSED      (one tx)
    approve(id, approved_by)-> ACR APPROVED   + ADR ACCEPTED      (one tx)
    reject(id, reason)      -> ACR REJECTED   + ADR REJECTED      (one tx)
    apply(id)               -> ACR APPLIED    + version supersede (one tx)

Design rules
------------
* **History is never rewritten.** Only the *lifecycle* of a baseline row moves
  (``is_current`` / ``superseded_by``); its content is frozen forever.
* **Exactly one current baseline** - before, during (same transaction) and after
  every apply, and the apply asserts it as a postcondition.
* **Human approval is a persisted state, not a flag.** ``apply`` only ever runs
  for a stored ``APPROVED`` request; there is deliberately no ``approved_by`` /
  ``force`` parameter that could bypass the lifecycle.
* **Terminal states stay terminal.** ``APPLIED`` is idempotent on re-apply (no
  new version, no new audit entry); a ``REJECTED`` request is never revived - the
  same idea needs a new request id.
* **Structured links only.** A request links to its ADR through ``adr_id`` and to
  the baselines through ``source_version`` / ``target_version``. ADR prose is a
  human-readable explanation and is never parsed for integrity.
* **The code baseline stays authoritative.** The engine never invents a baseline:
  the canonical version records (description + rule ids) are injected by the
  composition root and a request that disagrees with them is a conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from ..domain.audit import AuditAction, AuditEntityType, AuditEntry
from ..domain.enums import ACRStatus, ADRStatus
from ..domain.models import (
    ADR,
    ArchitectureChangeRequest,
    ArchitectureVersion,
    utc_now,
)
from ..ports.storage import StoragePort
from .adr_manager import ADRManager
from .architecture_bootstrap import (
    AdrSpec,
    BootstrapOutcome,
    RiskSpec,
    risk_matches_spec,
)
from .architecture_versioning import (
    ArchitectureVersioning,
    ArchitectureVersioningConflictError,
    VersioningSummary,
)
from .risk_manager import RiskManager

__all__ = [
    "ArchitectureEvolutionError",
    "ArchitectureEvolutionNotFoundError",
    "ArchitectureEvolutionConflictError",
    "ArchitectureEvolutionNotApprovedError",
    "EvolutionSummary",
    "LinkedChange",
    "ArchitectureEvolution",
]


class ArchitectureEvolutionError(Exception):
    """Base class for architecture-evolution errors."""


class ArchitectureEvolutionNotFoundError(ArchitectureEvolutionError):
    """Raised when a change request does not exist."""


class ArchitectureEvolutionConflictError(ArchitectureEvolutionError):
    """Raised when a change would contradict authoritative state.

    The whole transaction is rolled back: nothing is written, the previous
    baseline stays the single current one and the loop must BLOCK until a human
    resolves the contradiction.
    """

    def __init__(self, conflicts: Sequence[str]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__(
            "architecture evolution conflict: " + "; ".join(self.conflicts)
        )


class ArchitectureEvolutionNotApprovedError(ArchitectureEvolutionError):
    """Raised when ``apply`` is asked to run without a stored approval."""


@dataclass(frozen=True)
class EvolutionSummary:
    """Deterministic outcome of one lifecycle operation."""

    request_id: str
    status: ACRStatus
    source_version: str
    target_version: str
    adr_id: str
    outcome: BootstrapOutcome
    version_outcome: Optional[BootstrapOutcome] = None
    risks_created: tuple[str, ...] = ()
    roadmap_impact: tuple[str, ...] = ()
    version_summary: Optional[VersioningSummary] = None

    @property
    def applied(self) -> bool:
        """Whether the request is (now) in its terminal positive state."""
        return self.status is ACRStatus.APPLIED

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "request_id": self.request_id,
            "status": self.status.value,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "adr_id": self.adr_id,
            "outcome": self.outcome.value,
            "version_outcome": (
                None if self.version_outcome is None else self.version_outcome.value
            ),
            "risks_created": list(self.risks_created),
            "roadmap_impact": list(self.roadmap_impact),
            "version_summary": (
                None
                if self.version_summary is None
                else self.version_summary.to_dict()
            ),
        }


@dataclass(frozen=True)
class LinkedChange:
    """A request plus the ADR and baseline it is structurally linked to."""

    request: ArchitectureChangeRequest
    adr: Optional[ADR]
    version: Optional[ArchitectureVersion]

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "request": self.request.to_dict(),
            "adr": None if self.adr is None else self.adr.to_dict(),
            "version": None if self.version is None else self.version.to_dict(),
        }


def _request_content_matches(
    existing: ArchitectureChangeRequest, spec: ArchitectureChangeRequest
) -> bool:
    """Content comparison - lifecycle fields are deliberately excluded."""
    return (
        existing.request_id == spec.request_id
        and existing.title == spec.title
        and existing.rationale == spec.rationale
        and existing.source_version == spec.source_version
        and existing.target_version == spec.target_version
        and existing.rule_ids == spec.rule_ids
        and existing.adr_id == spec.adr_id
        and existing.roadmap_impact == spec.roadmap_impact
    )


def _adr_content_matches(existing: ADR, spec: AdrSpec) -> bool:
    """ADR content comparison that ignores the lifecycle status."""
    return (
        existing.title == spec.title
        and existing.context == spec.context
        and existing.decision == spec.decision
        and existing.consequences == tuple(spec.consequences)
        and existing.related == tuple(spec.related)
    )


class ArchitectureEvolution:
    """Drives the architecture change-request lifecycle against storage.

    The canonical version records are **injected** (composition builds them from
    the ``architecture`` layer), so this engine never duplicates a baseline, a
    rule id or a layer permission - and never imports that layer.
    """

    def __init__(
        self,
        storage: StoragePort,
        *,
        canonical_versions: Mapping[str, ArchitectureVersion],
        clock: Callable[[], datetime] = utc_now,
        declared_risks: Optional[Mapping[str, Sequence[RiskSpec]]] = None,
        versioning: Optional[ArchitectureVersioning] = None,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        if not isinstance(canonical_versions, Mapping):
            raise ValueError(
                "canonical_versions must be a mapping of version -> "
                f"ArchitectureVersion; got {canonical_versions!r}"
            )
        for version, record in canonical_versions.items():
            if not isinstance(record, ArchitectureVersion):
                raise ValueError(
                    f"canonical_versions[{version!r}] must be an "
                    f"ArchitectureVersion; got {record!r}"
                )
        self._storage = storage
        self._canonical_versions = dict(canonical_versions)
        self._declared_risks = {
            key: tuple(value) for key, value in (declared_risks or {}).items()
        }
        self._clock = clock
        if versioning is not None and not isinstance(
            versioning, ArchitectureVersioning
        ):
            raise ValueError(
                "versioning must be an ArchitectureVersioning; got "
                f"{versioning!r}"
            )
        self._versioning = (
            versioning
            if versioning is not None
            else ArchitectureVersioning(storage, clock=clock, adr_specs=())
        )
        self._adr_manager = ADRManager(
            storage.adrs, storage.audit, storage, clock=clock
        )
        self._risk_manager = RiskManager(
            storage.risks, storage.audit, storage, clock=clock
        )

    # -- read-only queries ------------------------------------------------
    @property
    def canonical_versions(self) -> Mapping[str, ArchitectureVersion]:
        """The baseline records the code side declares, by version."""
        return dict(self._canonical_versions)

    def pending(self) -> tuple[ArchitectureChangeRequest, ...]:
        """Requests in ``PROPOSED`` (waiting for a human decision)."""
        return self._storage.change_requests.list_by_status(ACRStatus.PROPOSED)

    def approved(self) -> tuple[ArchitectureChangeRequest, ...]:
        """Requests approved but not yet applied."""
        return self._storage.change_requests.list_by_status(ACRStatus.APPROVED)

    def applied(self) -> tuple[ArchitectureChangeRequest, ...]:
        """Requests whose change is applied."""
        return self._storage.change_requests.list_by_status(ACRStatus.APPLIED)

    def rejected(self) -> tuple[ArchitectureChangeRequest, ...]:
        """Requests a human rejected."""
        return self._storage.change_requests.list_by_status(ACRStatus.REJECTED)

    def history(self) -> tuple[ArchitectureChangeRequest, ...]:
        """Every request, oldest first."""
        return self._storage.change_requests.list()

    def current_version(self) -> ArchitectureVersion:
        """The single current baseline, or a conflict (fail-closed)."""
        return self._require_single_current()

    def linked(self, request_id: str) -> LinkedChange:
        """A request with its linked ADR and ``target_version`` record."""
        request = self._require(request_id)
        return LinkedChange(
            request=request,
            adr=self._storage.adrs.get(request.adr_id),
            version=self._storage.architecture_versions.get(
                request.target_version
            ),
        )

    # -- lifecycle ---------------------------------------------------------
    def propose(
        self, request: ArchitectureChangeRequest, *, adr: AdrSpec
    ) -> EvolutionSummary:
        """Register a change request and its reason record (one transaction).

        Idempotent: re-proposing an identical request - including one that has
        already advanced - is a no-op, so a restart or a repeated bootstrap
        reconciliation never duplicates a request or an audit entry.
        """
        if not isinstance(request, ArchitectureChangeRequest):
            raise ValueError(
                f"request must be an ArchitectureChangeRequest; got {request!r}"
            )
        if not isinstance(adr, AdrSpec):
            raise ValueError(f"adr must be an AdrSpec; got {adr!r}")
        if request.status is not ACRStatus.PROPOSED:
            raise ValueError(
                "propose() takes a fresh PROPOSED request; use approve, reject "
                "or apply to advance an existing one"
            )
        if request.adr_id != adr.id:
            raise ArchitectureEvolutionConflictError(
                [
                    f"request {request.request_id} links ADR "
                    f"{request.adr_id!r}, but the specification declares "
                    f"{adr.id!r}"
                ]
            )
        existing = self._storage.change_requests.get(request.request_id)
        if existing is not None:
            if not _request_content_matches(existing, request):
                raise ArchitectureEvolutionConflictError(
                    [
                        f"change request {request.request_id} already exists "
                        "with different content"
                    ]
                )
            self._verify_linked_adr(existing.adr_id, adr)
            return self._summary(existing, BootstrapOutcome.ALREADY_PRESENT)

        with self._storage.transaction():
            self._storage.change_requests.upsert(request)
            self._audit(
                request,
                AuditAction.CREATE,
                {
                    "status_from": None,
                    "status_to": request.status.value,
                    "source_version": request.source_version,
                    "target_version": request.target_version,
                    "adr_id": request.adr_id,
                    "rule_ids": list(request.rule_ids),
                    "roadmap_impact": list(request.roadmap_impact),
                },
            )
            self._ensure_adr_proposed(adr)
        return self._summary(request, BootstrapOutcome.CREATED)

    def approve(self, request_id: str, approved_by: str) -> EvolutionSummary:
        """Record the human approval: ACR ``APPROVED`` + ADR ``ACCEPTED``.

        This engine is the only path that makes a request ``APPROVED``: an
        out-of-band ``ADRManager.accept`` cannot, because the request status is
        owned here and ``apply`` requires it.
        """
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise ValueError("approved_by must be a non-empty string")
        request = self._require(request_id)
        if request.status is ACRStatus.APPLIED:
            return self._summary(request, BootstrapOutcome.ALREADY_PRESENT)
        if request.status is ACRStatus.REJECTED:
            raise ArchitectureEvolutionConflictError(
                [
                    f"change request {request_id} is REJECTED; a rejected "
                    "request is never revived - propose a new request id"
                ]
            )
        if request.status is ACRStatus.APPROVED:
            if request.approved_by == approved_by:
                return self._summary(request, BootstrapOutcome.ALREADY_PRESENT)
            raise ArchitectureEvolutionConflictError(
                [
                    f"change request {request_id} is already APPROVED by "
                    f"{request.approved_by!r}"
                ]
            )

        now = self._clock()
        approved = replace(
            request,
            status=ACRStatus.APPROVED,
            approved_by=approved_by,
            approved_at=now,
        )
        with self._storage.transaction():
            self._storage.change_requests.upsert(approved)
            self._accept_linked_adr(request.adr_id)
            self._audit(
                approved,
                AuditAction.UPDATE,
                {
                    "status_from": request.status.value,
                    "status_to": approved.status.value,
                    "adr_id": request.adr_id,
                    "approved_by": approved_by,
                    "approved_at": now.isoformat(),
                },
            )
        return self._summary(approved, BootstrapOutcome.CREATED)

    def reject(self, request_id: str, reason: str) -> EvolutionSummary:
        """Reject a proposed change: ACR ``REJECTED`` + ADR ``REJECTED``.

        The authoritative baseline is deliberately untouched, and a rejected
        request can never be approved or applied afterwards.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        request = self._require(request_id)
        if request.status is ACRStatus.REJECTED:
            return self._summary(request, BootstrapOutcome.ALREADY_PRESENT)
        if request.status is not ACRStatus.PROPOSED:
            raise ArchitectureEvolutionConflictError(
                [
                    f"change request {request_id} is "
                    f"{request.status.value}; only a PROPOSED request can be "
                    "rejected"
                ]
            )
        rejected = replace(request, status=ACRStatus.REJECTED)
        with self._storage.transaction():
            self._storage.change_requests.upsert(rejected)
            self._reject_linked_adr(request.adr_id)
            self._audit(
                rejected,
                AuditAction.UPDATE,
                {
                    "status_from": request.status.value,
                    "status_to": rejected.status.value,
                    "adr_id": request.adr_id,
                    "reason": reason,
                },
            )
        return self._summary(rejected, BootstrapOutcome.CREATED)

    def apply(self, request_id: str) -> EvolutionSummary:
        """Apply an approved change: supersede the baseline (one transaction).

        Preconditions (all fail-closed): the request is persisted ``APPROVED``,
        its ADR is ``ACCEPTED``, the target version is declared by the code
        baseline with exactly the rules the request declares, and the source
        version is the one and only current baseline - the last two are checked
        inside the transaction, the Step 9 primitive enforces the supersession
        and the postcondition re-reads the store, so a bad write rolls the whole
        transaction back.

        There is deliberately **no** ``force`` or ``approved_by`` parameter: a
        human approval that is not persisted in the request cannot be bypassed
        into a version bump.
        """
        request = self._require(request_id)
        if request.status is ACRStatus.REJECTED:
            raise ArchitectureEvolutionNotApprovedError(
                f"change request {request_id} is REJECTED and can never be "
                "applied"
            )
        if request.status not in (ACRStatus.APPROVED, ACRStatus.APPLIED):
            raise ArchitectureEvolutionNotApprovedError(
                f"change request {request_id} is {request.status.value}; apply() "
                "requires a persisted APPROVED request"
            )

        canonical = self._canonical_versions.get(request.target_version)
        if canonical is None:
            raise ArchitectureEvolutionConflictError(
                [
                    "no canonical baseline is declared for version "
                    f"{request.target_version!r}; the code side owns the "
                    "baselines and the engine never invents one"
                ]
            )
        if request.rule_ids != canonical.rules:
            raise ArchitectureEvolutionConflictError(
                [
                    f"request {request_id} declares rules "
                    f"{tuple(request.rule_ids)!r}, but baseline "
                    f"{canonical.version} declares {tuple(canonical.rules)!r}"
                ]
            )
        self._require_accepted_adr(request.adr_id)

        now = self._clock()
        spec = ArchitectureVersion(
            version=request.target_version,
            baseline=canonical.baseline,
            rules=canonical.rules,
            is_current=True,
            created_at=now,
        )
        if request.status is ACRStatus.APPLIED:
            # APPLIED is terminal: a repeated apply only *verifies* and writes
            # absolutely nothing - no version, no risk, no audit entry, and the
            # request (status, applied_at) stays exactly as it was.
            version = self._verify_applied(request, spec)
            return self._summary(
                request,
                BootstrapOutcome.ALREADY_PRESENT,
                version_outcome=version.version_outcome,
                version_summary=version,
            )

        with self._storage.transaction():
            self._require_single_current()
            version = self._introduce(spec, request.source_version)
            risks_created = self._register_declared_risks(request.request_id)
            applied = replace(request, status=ACRStatus.APPLIED, applied_at=now)
            self._storage.change_requests.upsert(applied)
            self._audit(
                applied,
                AuditAction.UPDATE,
                {
                    "status_from": request.status.value,
                    "status_to": applied.status.value,
                    "adr_id": request.adr_id,
                    "applied_at": now.isoformat(),
                    "source_version": request.source_version,
                    "target_version": request.target_version,
                    "rules": list(request.rule_ids),
                    "risks_created": list(risks_created),
                },
            )
            self._assert_single_current(request.target_version)
        return self._summary(
            applied,
            BootstrapOutcome.CREATED,
            version_outcome=version.version_outcome,
            risks_created=risks_created,
            version_summary=version,
        )

    # -- internals ---------------------------------------------------------
    def _require(self, request_id: str) -> ArchitectureChangeRequest:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        request = self._storage.change_requests.get(request_id)
        if request is None:
            raise ArchitectureEvolutionNotFoundError(
                f"change request {request_id!r} does not exist"
            )
        return request

    def _summary(
        self,
        request: ArchitectureChangeRequest,
        outcome: BootstrapOutcome,
        *,
        version_outcome: Optional[BootstrapOutcome] = None,
        risks_created: tuple[str, ...] = (),
        version_summary: Optional[VersioningSummary] = None,
    ) -> EvolutionSummary:
        return EvolutionSummary(
            request_id=request.request_id,
            status=request.status,
            source_version=request.source_version,
            target_version=request.target_version,
            adr_id=request.adr_id,
            outcome=outcome,
            version_outcome=version_outcome,
            risks_created=risks_created,
            roadmap_impact=request.roadmap_impact,
            version_summary=version_summary,
        )

    def _audit(
        self,
        request: ArchitectureChangeRequest,
        action: AuditAction,
        detail: Mapping[str, Any],
    ) -> None:
        """One ACR audit entry per ACR transition (the managers audit their own).

        Joins the caller's transaction, so a rolled-back change leaves no trace:
        the status in storage and the status in the trail always agree.
        """
        self._storage.audit.append(
            AuditEntry(
                entity_type=AuditEntityType.ACR,
                entity_id=request.request_id,
                action=action,
                detail=dict(detail),
                created_at=self._clock(),
            )
        )

    def _introduce(
        self, spec: ArchitectureVersion, source_version: str
    ) -> VersioningSummary:
        """Delegate to the Step 9 atomic primitive, translating its errors."""
        try:
            return self._versioning.introduce(spec, supersedes=source_version)
        except ArchitectureVersioningConflictError as error:
            raise ArchitectureEvolutionConflictError(error.conflicts) from error

    @property
    def versioning(self) -> ArchitectureVersioning:
        """The Step 9 primitive this engine drives (never duplicated)."""
        return self._versioning

    def _require_single_current(self) -> ArchitectureVersion:
        current = tuple(
            version
            for version in self._storage.architecture_versions.list()
            if version.is_current
        )
        if not current:
            raise ArchitectureEvolutionConflictError(
                ["the source of truth holds no current architecture version"]
            )
        if len(current) > 1:
            listed = ", ".join(sorted(version.version for version in current))
            raise ArchitectureEvolutionConflictError(
                [
                    f"the source of truth holds {len(current)} current "
                    f"architecture versions ({listed}); refusing to evolve a "
                    "forked baseline"
                ]
            )
        return current[0]

    def _assert_single_current(self, expected_version: str) -> None:
        current = self._require_single_current()
        if current.version != expected_version:
            raise ArchitectureEvolutionConflictError(
                [
                    "postcondition failed: expected "
                    f"{expected_version!r} to be the single current baseline, "
                    f"found {current.version!r}"
                ]
            )

    def _verify_applied(
        self, request: ArchitectureChangeRequest, spec: ArchitectureVersion
    ) -> VersioningSummary:
        """Read-only re-verification behind the idempotent re-apply.

        ``APPLIED`` is terminal, so this path may not roll history forward. It
        first proves that the chain already holds (target current, source
        superseded by target) and only then asks the Step 9 primitive to
        re-verify the content - which, on a chain that already holds, writes
        nothing at all. Anything else is a conflict rather than a silent write.
        """
        self._assert_single_current(request.target_version)
        source = self._storage.architecture_versions.get(request.source_version)
        if source is None or source.superseded_by != request.target_version:
            raise ArchitectureEvolutionConflictError(
                [
                    f"change request {request.request_id} is APPLIED, but "
                    f"{request.source_version!r} is not recorded as superseded "
                    f"by {request.target_version!r}"
                ]
            )
        version = self._introduce(spec, request.source_version)
        if version.version_outcome is not BootstrapOutcome.ALREADY_PRESENT:
            raise ArchitectureEvolutionConflictError(
                [
                    f"change request {request.request_id} is APPLIED, but the "
                    f"baseline change to {request.target_version!r} is not "
                    "recorded; refusing to rewrite history from a repeated "
                    "apply"
                ]
            )
        return version

    def _register_declared_risks(self, request_id: str) -> tuple[str, ...]:
        """Register the risks the change declares; idempotent by construction."""
        created: list[str] = []
        for spec in self._declared_risks.get(request_id, ()):
            existing = self._storage.risks.get(spec.id)
            if existing is None:
                self._risk_manager.register(
                    spec.id,
                    spec.description,
                    severity=spec.severity,
                    probability=spec.probability,
                    impact=spec.impact,
                    owner=spec.owner,
                    mitigation=spec.mitigation,
                )
                created.append(spec.id)
            elif not risk_matches_spec(existing, spec):
                raise ArchitectureEvolutionConflictError(
                    [
                        f"risk {spec.id} declared by change request "
                        f"{request_id} exists but differs from the "
                        "specification"
                    ]
                )
        return tuple(created)

    def _ensure_adr_proposed(self, spec: AdrSpec) -> None:
        if self._storage.adrs.get(spec.id) is None:
            self._adr_manager.propose(
                spec.id,
                spec.title,
                context=spec.context,
                decision=spec.decision,
                consequences=spec.consequences,
                related=spec.related,
            )
            return
        self._verify_linked_adr(spec.id, spec)

    def _verify_linked_adr(self, adr_id: str, spec: AdrSpec) -> ADR:
        existing = self._require_linked_adr(adr_id)
        if not _adr_content_matches(existing, spec):
            raise ArchitectureEvolutionConflictError(
                [
                    f"ADR {adr_id} exists but differs from the change "
                    "specification"
                ]
            )
        if existing.status in (ADRStatus.REJECTED, ADRStatus.SUPERSEDED):
            raise ArchitectureEvolutionConflictError(
                [
                    f"linked ADR {adr_id} is {existing.status.value}; the change "
                    "needs a new reason record"
                ]
            )
        return existing

    def _accept_linked_adr(self, adr_id: str) -> None:
        adr = self._require_linked_adr(adr_id)
        if adr.status is ADRStatus.PROPOSED:
            self._adr_manager.accept(adr_id)
            return
        if adr.status is not ADRStatus.ACCEPTED:
            raise ArchitectureEvolutionConflictError(
                [
                    f"linked ADR {adr_id} is {adr.status.value} and can not be "
                    "accepted"
                ]
            )

    def _reject_linked_adr(self, adr_id: str) -> None:
        adr = self._require_linked_adr(adr_id)
        if adr.status is ADRStatus.REJECTED:
            return
        if adr.status is not ADRStatus.PROPOSED:
            raise ArchitectureEvolutionConflictError(
                [
                    f"linked ADR {adr_id} is {adr.status.value} and can not be "
                    "rejected"
                ]
            )
        self._adr_manager.reject(adr_id)

    def _require_accepted_adr(self, adr_id: str) -> ADR:
        adr = self._require_linked_adr(adr_id)
        if adr.status is not ADRStatus.ACCEPTED:
            raise ArchitectureEvolutionNotApprovedError(
                f"linked ADR {adr_id} is {adr.status.value}; apply() requires "
                "an ACCEPTED ADR"
            )
        return adr

    def _require_linked_adr(self, adr_id: str) -> ADR:
        adr = self._storage.adrs.get(adr_id)
        if adr is None:
            raise ArchitectureEvolutionConflictError(
                [f"linked ADR {adr_id!r} does not exist"]
            )
        return adr
