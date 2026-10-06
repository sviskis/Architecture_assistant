"""Category catalog ports.

Three contracts, deliberately separated:

* :class:`CategoryRepository` / :class:`CatalogAuditRepository` - the low-level
  persistence boundaries the application use-case composes inside one
  transaction. They are plumbing and are never the public API;
* :class:`CategoryCatalogPort` - the **public, audited** catalog API an external
  host (the GUI) uses. It exposes reads plus exactly four audited mutations
  (``register`` / ``enable`` / ``disable`` / ``update_metadata``), each of which
  requires an ``actor`` and a ``reason``. There is deliberately no public
  mutation that could bypass the audit trail.
"""

from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from ..domain.category import CatalogAuditEntry, Category, CategoryView
from ..domain.category_spec import CategorySpec

__all__ = [
    "CategoryRepository",
    "CatalogAuditRepository",
    "CategoryCatalogPort",
]


@runtime_checkable
class CategoryRepository(Protocol):
    """Low-level persistence of category rows (one aggregate, key: id)."""

    def list(self) -> tuple[Category, ...]: ...

    def get(self, category_id: str) -> Optional[Category]: ...

    def upsert(self, category: Category) -> None: ...


@runtime_checkable
class CatalogAuditRepository(Protocol):
    """Append-only persistence of catalog audit entries."""

    def append(self, entry: CatalogAuditEntry) -> None: ...

    def list(self) -> tuple[CatalogAuditEntry, ...]: ...

    def list_for_category(
        self, category_id: str
    ) -> tuple[CatalogAuditEntry, ...]: ...


@runtime_checkable
class CategoryCatalogPort(Protocol):
    """The public, audited global category-catalog API."""

    def categories(self) -> tuple[CategoryView, ...]:
        """Every registered category with its state and diagnostic."""
        ...

    def selectable(self) -> tuple[Category, ...]:
        """Only the categories a NEW project may choose (``ENABLED``)."""
        ...

    def get(self, category_id: str) -> Optional[CategoryView]:
        """One category view, or ``None`` when it is not registered."""
        ...

    def register(
        self,
        *,
        category_id: str,
        name: str,
        version: str,
        actor: str,
        reason: str,
        spec_path: str = ...,
        library_path: str = ...,
        supervisor_rules_path: str = ...,
        enabled: bool = False,
    ) -> CategoryView:
        """Register one category (mutation + audit in one transaction)."""
        ...

    def enable(self, category_id: str, *, actor: str, reason: str) -> CategoryView:
        """Enable a category; requires its referenced entries to exist."""
        ...

    def disable(self, category_id: str, *, actor: str, reason: str) -> CategoryView:
        """Disable a category (never deletes it)."""
        ...

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
        """Update metadata without changing the immutable id."""
        ...

    def audit_trail(
        self, category_id: Optional[str] = None
    ) -> tuple[CatalogAuditEntry, ...]:
        """The append-only audit trail, optionally filtered by category."""
        ...

    def missing_entries(self, category_id: str) -> Sequence[str]:
        """Which referenced entries of a registered category are missing."""
        ...

    def load_spec(self, category_id: str) -> CategorySpec:
        """The validated SPEC.md of a registered category (read-only, no writes)."""
        ...
