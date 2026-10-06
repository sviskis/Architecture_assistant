"""Global category catalog use-case - the one audited mutation surface.

The catalog is authoritative and global, so every mutation is an act that must be
traceable. This use-case is built so that **it is impossible to change a category
without its matching ``catalog_audit`` event**: the low-level repository and audit
ports are private dependencies, the only public methods are the four audited
mutations plus reads, and each mutation writes the row and its audit entry inside
one transaction (commit together, roll back together).

Nothing here touches the filesystem: whether the referenced entries of a category
exist is asked of the injected :class:`GlobalPathsPort`, and the concrete
``%LOCALAPPDATA%`` access lives in the infrastructure layer. Imports are
``domain`` and ``ports`` only.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Optional, Sequence

from ..domain.category import (
    LIBRARY_ENTRY_NAME,
    SPEC_ENTRY_NAME,
    SUPERVISOR_RULES_ENTRY_NAME,
    CatalogAuditAction,
    CatalogAuditEntry,
    Category,
    CategoryState,
    CategoryView,
    compute_registry_hash,
    is_valid_category_id,
)
from ..domain.category_spec import CategorySpec, CategorySpecError
from ..ports.catalog import CatalogAuditRepository, CategoryRepository
from ..ports.category_spec import CategorySpecPort
from ..ports.global_paths import GlobalPathsPort
from ..ports.transactions import TransactionPort

__all__ = [
    "CategoryCatalogError",
    "CategoryNotFoundError",
    "CategoryInvalidIdError",
    "CategoryExistsError",
    "CategoryNotReadyError",
    "CategoryActorRequiredError",
    "CategoryReasonRequiredError",
    "CategoryMetadataError",
    "CategoryNameConflictError",
    "CategoryCatalogUseCase",
]


class CategoryCatalogError(Exception):
    """Base class for every global category-catalog failure."""


class CategoryNotFoundError(CategoryCatalogError):
    """Raised when a referenced category is not registered."""


class CategoryInvalidIdError(CategoryCatalogError):
    """Raised when a category id is not a valid immutable slug."""


class CategoryExistsError(CategoryCatalogError):
    """Raised when registering an id that already exists (ids are immutable)."""


class CategoryNotReadyError(CategoryCatalogError):
    """Raised when a category cannot be enabled: a referenced entry is missing."""


class CategoryActorRequiredError(CategoryCatalogError):
    """Raised when a mutation carries no actor."""


class CategoryReasonRequiredError(CategoryCatalogError):
    """Raised when a mutation carries no reason."""


class CategoryMetadataError(CategoryCatalogError):
    """Raised when the supplied metadata is invalid or empty."""


class CategoryNameConflictError(CategoryCatalogError):
    """Raised when a display name is already used by another category.

    Display names are bound to their immutable id 1:1 and must stay unique across
    registered ids (compared exactly, case-sensitively), so a duplicate is refused
    by the audited register/update surface.
    """


def _snapshot(category: Category) -> dict[str, Any]:
    """Canonical audit payload of one registry row.

    Relative paths and the registry hash only - never an absolute machine path,
    never a secret - and no volatile timestamps, so ``old_value``/``new_value``
    differ only when the metadata really changed.
    """
    return {
        "id": category.id,
        "name": category.name,
        "version": category.version,
        "spec_path": category.spec_path,
        "library_path": category.library_path,
        "supervisor_rules_path": category.supervisor_rules_path,
        "registry_hash": category.registry_hash,
        "enabled": category.enabled,
    }


class CategoryCatalogUseCase:
    """The public, audited global category catalog.

    Readiness is *content* readiness: a category is only ``ENABLED`` when its
    referenced entries exist **and** its ``SPEC.md`` is a valid, hash-verified
    document that agrees with the row's identity. That check is centralized here
    and runs on every readiness entry point (listing, ``get``, ``selectable``,
    registration of an enabled row, ``enable`` - including the already-enabled
    branch - and an enabled ``update_metadata``), so a broken SPEC can never be
    selected from one path while being rejected on another. The reader is an
    injected :class:`~architecture_assistant.ports.category_spec.CategorySpecPort`;
    when it is absent, validation fails closed.
    """

    def __init__(
        self,
        categories: CategoryRepository,
        audit: CatalogAuditRepository,
        transactions: TransactionPort,
        paths: GlobalPathsPort,
        *,
        clock: Callable[[], datetime],
        specs: Optional[CategorySpecPort] = None,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._categories = categories
        self._audit = audit
        self._transactions = transactions
        self._paths = paths
        self._clock = clock
        self._specs = specs

    # -- reads -------------------------------------------------------------
    def categories(self) -> tuple[CategoryView, ...]:
        """Every registered category with its derived state and diagnostic."""
        rows = sorted(self._categories.list(), key=lambda item: item.id)
        duplicate_names = self._duplicate_names(rows)
        return tuple(
            self._view(row, duplicate_names=duplicate_names) for row in rows
        )

    def selectable(self) -> tuple[Category, ...]:
        """Only the categories a NEW project may choose (``ENABLED``)."""
        return tuple(
            view.category for view in self.categories() if view.selectable
        )

    def get(self, category_id: str) -> Optional[CategoryView]:
        """One category view, or ``None`` when it is not registered."""
        category = self._categories.get(category_id)
        return None if category is None else self._view(category)

    def missing_entries(self, category_id: str) -> Sequence[str]:
        """Which referenced entries of a registered category are missing."""
        category = self._require_category(category_id)
        return self._paths.missing_entries(category.id, category.entries())

    def load_spec(self, category_id: str) -> CategorySpec:
        """The validated ``SPEC.md`` of one registered category.

        Read-only: it never mutates the catalog and never touches a project
        database. The returned value carries the validated identity, the version,
        the description and the content hash, so a later step can persist the
        exact snapshot a selection was based on.
        """
        category = self._require_category(category_id)
        if self._specs is None:
            raise CategoryNotReadyError(
                f"category {category.id!r} has no category SPEC reader configured; "
                "refusing to trust its specification"
            )
        return self._specs.load(category)

    def audit_trail(
        self, category_id: Optional[str] = None
    ) -> tuple[CatalogAuditEntry, ...]:
        """The append-only audit trail, optionally filtered by category."""
        if category_id is None:
            return self._audit.list()
        return self._audit.list_for_category(category_id)


    # -- the four audited mutations ---------------------------------------
    def register(
        self,
        *,
        category_id: str,
        name: str,
        version: str,
        actor: str,
        reason: str,
        spec_path: str = SPEC_ENTRY_NAME,
        library_path: str = LIBRARY_ENTRY_NAME,
        supervisor_rules_path: str = SUPERVISOR_RULES_ENTRY_NAME,
        enabled: bool = False,
    ) -> CategoryView:
        """Register one category: one insert + one audit entry, atomically."""
        actor = self._require_actor(actor)
        reason = self._require_reason(reason)
        if not is_valid_category_id(category_id):
            raise CategoryInvalidIdError(
                f"category_id {category_id!r} is not a valid immutable slug"
            )
        if self._categories.get(category_id) is not None:
            raise CategoryExistsError(
                f"category {category_id!r} is already registered; "
                "a category id is immutable"
            )
        self._require_unique_name(name, excluding=category_id)
        now = self._clock()
        registry_hash = compute_registry_hash(
            category_id=category_id,
            name=name,
            version=version,
            spec_path=spec_path,
            library_path=library_path,
            supervisor_rules_path=supervisor_rules_path,
        )
        try:
            category = Category(
                id=category_id,
                name=name,
                version=version,
                spec_path=spec_path,
                library_path=library_path,
                supervisor_rules_path=supervisor_rules_path,
                registry_hash=registry_hash,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
        except ValueError as error:
            raise CategoryMetadataError(str(error)) from error
        if category.enabled:
            self._require_ready(category)
        self._commit(
            category,
            actor=actor,
            reason=reason,
            action=CatalogAuditAction.REGISTER,
            old_value={},
            new_value=_snapshot(category),
            now=now,
        )
        return self._view(category)

    def enable(
        self, category_id: str, *, actor: str, reason: str
    ) -> CategoryView:
        """Enable a category; its referenced entries and SPEC.md must be valid."""
        actor = self._require_actor(actor)
        reason = self._require_reason(reason)
        category = self._require_category(category_id)
        if category.enabled:
            # already enabled: no mutation and no audit entry, but the current
            # content is re-validated so a SPEC that broke on disk cannot be
            # confirmed as enabled silently
            self._require_ready(category)
            return self._view(category)
        self._require_ready(category)
        now = self._clock()
        updated = replace(category, enabled=True, updated_at=now)
        self._commit(
            updated,
            actor=actor,
            reason=reason,
            action=CatalogAuditAction.ENABLE,
            old_value=_snapshot(category),
            new_value=_snapshot(updated),
            now=now,
        )
        return self._view(updated)

    def disable(
        self, category_id: str, *, actor: str, reason: str
    ) -> CategoryView:
        """Disable a category - it stays registered and keeps its history."""
        actor = self._require_actor(actor)
        reason = self._require_reason(reason)
        category = self._require_category(category_id)
        if not category.enabled:
            # already disabled: no mutation, so deliberately no audit entry
            return self._view(category)
        now = self._clock()
        updated = replace(category, enabled=False, updated_at=now)
        self._commit(
            updated,
            actor=actor,
            reason=reason,
            action=CatalogAuditAction.DISABLE,
            old_value=_snapshot(category),
            new_value=_snapshot(updated),
            now=now,
        )
        return self._view(updated)


    def update_metadata(
        self,
        category_id: str,
        *,
        actor: str,
        reason: str,
        name: Optional[str] = None,
        version: Optional[str] = None,
        spec_path: Optional[str] = None,
        library_path: Optional[str] = None,
        supervisor_rules_path: Optional[str] = None,
    ) -> CategoryView:
        """Update metadata and recompute the registry hash (id never changes)."""
        actor = self._require_actor(actor)
        reason = self._require_reason(reason)
        category = self._require_category(category_id)
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if version is not None:
            changes["version"] = version
        if spec_path is not None:
            changes["spec_path"] = spec_path
        if library_path is not None:
            changes["library_path"] = library_path
        if supervisor_rules_path is not None:
            changes["supervisor_rules_path"] = supervisor_rules_path
        if not changes:
            raise CategoryMetadataError(
                "update_metadata needs at least one field to change"
            )
        if "name" in changes:
            self._require_unique_name(changes["name"], excluding=category.id)
        changes["registry_hash"] = compute_registry_hash(
            category_id=category.id,
            name=changes.get("name", category.name),
            version=changes.get("version", category.version),
            spec_path=changes.get("spec_path", category.spec_path),
            library_path=changes.get("library_path", category.library_path),
            supervisor_rules_path=changes.get(
                "supervisor_rules_path", category.supervisor_rules_path
            ),
        )
        now = self._clock()
        changes["updated_at"] = now
        try:
            updated = replace(category, **changes)
        except ValueError as error:
            raise CategoryMetadataError(str(error)) from error
        if updated.enabled:
            self._require_ready(updated)
        self._commit(
            updated,
            actor=actor,
            reason=reason,
            action=CatalogAuditAction.UPDATE,
            old_value=_snapshot(category),
            new_value=_snapshot(updated),
            now=now,
        )
        return self._view(updated)


    # -- internals ---------------------------------------------------------
    @staticmethod
    def _duplicate_names(rows: Sequence[Category]) -> frozenset[str]:
        """Display names used by more than one registered id (exact compare)."""
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.name] = counts.get(row.name, 0) + 1
        return frozenset(name for name, count in counts.items() if count > 1)

    def _spec_problem(self, category: Category) -> str:
        """Why this category's SPEC.md is not trustworthy, or an empty string.

        Always a *fresh* read: the filesystem content and the SQLite row are not
        one atomic resource, and the row may have been enabled while the file was
        already stale.
        """
        if self._specs is None:
            return (
                "no category SPEC reader is configured; refusing to trust "
                f"{SPEC_ENTRY_NAME}"
            )
        try:
            self._specs.load(category)
        except CategorySpecError as error:
            return f"invalid {SPEC_ENTRY_NAME}: {error}"
        return ""

    def _problems(
        self,
        category: Category,
        *,
        duplicate_names: frozenset[str],
    ) -> tuple[str, ...]:
        """Every reason this category cannot be offered, in a stable order."""
        problems: list[str] = []
        missing = tuple(
            self._paths.missing_entries(category.id, category.entries())
        )
        if missing:
            problems.append("missing referenced entries: " + ", ".join(missing))
        if SPEC_ENTRY_NAME not in missing:
            spec_problem = self._spec_problem(category)
            if spec_problem:
                problems.append(spec_problem)
        if category.name in duplicate_names:
            problems.append(
                f"duplicate display name {category.name!r}; rename one through the "
                "audited catalog API"
            )
        return tuple(problems)

    def _view(
        self,
        category: Category,
        *,
        duplicate_names: Optional[frozenset[str]] = None,
    ) -> CategoryView:
        """Derive the state and the diagnostic of one row.

        A category is never hidden: a row that cannot be offered is returned as
        ``DISABLED``/``INVALID`` together with the reason. Readiness covers the
        referenced entries *and* the current SPEC content.
        """
        if duplicate_names is None:
            duplicate_names = self._duplicate_names(self._categories.list())
        problems = self._problems(category, duplicate_names=duplicate_names)
        diagnostic = "; ".join(problems)
        if category.enabled and problems:
            return CategoryView(category, CategoryState.INVALID, diagnostic)
        if category.enabled:
            return CategoryView(category, CategoryState.ENABLED, "")
        return CategoryView(category, CategoryState.DISABLED, diagnostic)

    def _require_ready(self, category: Category) -> None:
        """Refuse an ``ENABLED`` state whose entries or SPEC.md are not valid."""
        problems = self._problems(
            category,
            duplicate_names=self._duplicate_names(self._categories.list()),
        )
        if problems:
            raise CategoryNotReadyError(
                f"category {category.id!r} cannot be enabled: " + "; ".join(problems)
            )

    def _require_unique_name(self, name: str, *, excluding: str) -> None:
        """Enforce exact, case-sensitive display-name uniqueness across ids."""
        for row in self._categories.list():
            if row.id != excluding and row.name == name:
                raise CategoryNameConflictError(
                    f"display name {name!r} is already used by category {row.id!r}"
                )

    def _require_category(self, category_id: str) -> Category:
        if not isinstance(category_id, str) or not category_id.strip():
            raise CategoryNotFoundError(
                f"category_id must be a non-empty string; got {category_id!r}"
            )
        category = self._categories.get(category_id)
        if category is None:
            raise CategoryNotFoundError(
                f"category {category_id!r} is not registered"
            )
        return category

    @staticmethod
    def _require_actor(actor: str) -> str:
        if not isinstance(actor, str) or not actor.strip():
            raise CategoryActorRequiredError(
                f"actor must be a non-empty string; got {actor!r}"
            )
        return actor

    @staticmethod
    def _require_reason(reason: str) -> str:
        if not isinstance(reason, str) or not reason.strip():
            raise CategoryReasonRequiredError(
                f"reason must be a non-empty string; got {reason!r}"
            )
        return reason

    def _commit(
        self,
        category: Category,
        *,
        actor: str,
        reason: str,
        action: CatalogAuditAction,
        old_value: dict[str, Any],
        new_value: dict[str, Any],
        now: datetime,
    ) -> None:
        """Write the mutation and its audit entry in **one** transaction.

        Both writes join the same explicit transaction boundary, so a crash or an
        error between them rolls back both: a catalog mutation can never exist
        without its audit entry.
        """
        with self._transactions.transaction():
            self._categories.upsert(category)
            self._audit.append(
                CatalogAuditEntry(
                    timestamp=now,
                    actor=actor,
                    action=action,
                    category_id=category.id,
                    old_value=old_value,
                    new_value=new_value,
                    reason=reason,
                )
            )
