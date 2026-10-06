"""Composition wiring for the global category catalog.

This is the single wiring point of the global catalog (which exists **before**
any project database). It builds the one concrete
:class:`~architecture_assistant.infrastructure.global_paths.LocalAppDataGlobalPaths`
resolver, opens the catalog's own SQLite database, applies the catalog's own
migrations and hands back the audited use-case. It never calls
:func:`~architecture_assistant.composition.root.compose`, never opens a project
database and holds no business logic.

It is deliberately usable from the external host (the GUI) at startup, before any
workspace or project exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from ..application.category_catalog import CategoryCatalogUseCase
from ..domain.models import utc_now
from ..infrastructure.category_catalog import (
    SqliteCatalogAuditRepository,
    SqliteCategoryRepository,
    open_category_database,
)
from ..infrastructure.category_spec import FilesystemCategorySpecReader
from ..infrastructure.global_paths import LocalAppDataGlobalPaths
from ..infrastructure.sqlite import SqliteTransactionPort
from ..ports.category_spec import CategorySpecPort
from ..ports.global_paths import GlobalPathsPort

__all__ = ["CategoryCatalog", "open_category_catalog"]


@dataclass
class CategoryCatalog:
    """The wired global catalog: its resolver, its connection and its use-case."""

    paths: GlobalPathsPort
    #: The open catalog SQLite connection. Deliberately ``Any``: ``sqlite3`` stays
    #: confined to the infrastructure layer. It is the *catalog's* connection and
    #: never a project's.
    connection: Any
    catalog: CategoryCatalogUseCase
    #: The read-only SPEC reader the use-case validates category content with.
    specs: CategorySpecPort

    def close(self) -> None:
        """Release the catalog connection opened by :func:`open_category_catalog`."""
        self.connection.close()


def open_category_catalog(
    *,
    paths: Optional[GlobalPathsPort] = None,
    clock: Callable[[], datetime] = utc_now,
    ensure_ready: bool = True,
) -> CategoryCatalog:
    """Open, migrate and wire the global category catalog - wiring only.

    Fails closed: when ``paths`` cannot resolve or is not writable the injected
    resolver raises
    :class:`~architecture_assistant.ports.global_paths.GlobalPathsError` before
    any connection is opened.
    """
    resolved: GlobalPathsPort = paths or LocalAppDataGlobalPaths()
    if ensure_ready:
        resolved.ensure_ready()
    connection = open_category_database(resolved.catalog_database_path())
    categories = SqliteCategoryRepository(connection)
    audit = SqliteCatalogAuditRepository(connection)
    transactions = SqliteTransactionPort(connection)
    # The one SPEC reader of the catalog: it reads the authoritative SPEC.md from
    # the global category content root and validates/hash-verifies it. A category
    # is never enabled on the strength of the catalog row alone.
    specs = FilesystemCategorySpecReader(resolved)
    catalog = CategoryCatalogUseCase(
        categories,
        audit,
        transactions,
        resolved,
        clock=clock,
        specs=specs,
    )
    return CategoryCatalog(
        paths=resolved,
        connection=connection,
        catalog=catalog,
        specs=specs,
    )
