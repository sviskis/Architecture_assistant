"""Transaction boundary port.

Application services group several adapter writes (domain state + audit entry)
into one atomic unit through this port, without importing any SQLite code.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

__all__ = ["TransactionPort"]


@runtime_checkable
class TransactionPort(Protocol):
    """Provides an atomic transaction scope for write operations.

    ``with transactions.transaction():`` must commit everything written inside
    the block on success and roll all of it back on any exception.
    """

    def transaction(self) -> AbstractContextManager[None]:
        """Return the context manager owning the commit/rollback boundary."""
        ...
