"""Global path port - where the global (non-project) catalog lives.

The global category catalog exists **before** any project database and outside
every project and the repository, so its location is a first-class concern of its
own. This port is the single abstraction the application layer depends on;
concrete ``%LOCALAPPDATA%``/filesystem/writability access belongs to the
infrastructure layer, and the composition root wires the one implementation.

Fail-closed: when the global root cannot be located or is not writable, every
method raises :class:`GlobalPathsError`. There is no silent relocation and (in
Step 2) no CLI override.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

__all__ = ["GlobalPathsError", "GlobalPathsPort"]


class GlobalPathsError(Exception):
    """Raised when the global catalog location cannot be used (fail closed)."""


@runtime_checkable
class GlobalPathsPort(Protocol):
    """Absolute locations of the global catalog, resolved on demand."""

    def catalog_database_path(self) -> str:
        """Absolute path of the global catalog SQLite database."""
        ...

    def categories_root(self) -> str:
        """Absolute path of the global category content root."""
        ...

    def category_root(self, category_id: str) -> str:
        """Absolute path of one category's content folder."""
        ...

    def missing_entries(
        self, category_id: str, entries: Sequence[str]
    ) -> tuple[str, ...]:
        """Which of ``entries`` do not exist under the category root.

        The filesystem check lives here (infrastructure), never in the
        application layer: the caller only receives the relative names that are
        missing, in the order it asked for them.
        """
        ...

    def ensure_ready(self) -> None:
        """Create the global root and verify it is writable, or fail closed."""
        ...
