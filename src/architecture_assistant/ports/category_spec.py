"""Category SPEC port - the read-only boundary of one category's ``SPEC.md``.

A category's specification lives in the *global* category content root (outside
every project and outside the repository), so loading it is a filesystem concern.
This port is the abstraction the application layer depends on: the concrete
adapter belongs to the infrastructure layer and the composition root wires the
single implementation, exactly like
:class:`~architecture_assistant.ports.global_paths.GlobalPathsPort`.

The port is deliberately read-only - Step 3 validates documents, it never
creates, rewrites or repairs one - and it returns the *validated*
:class:`~architecture_assistant.domain.category_spec.CategorySpec` or raises a
typed error:

* :class:`CategorySpecNotFoundError` - there is no ``SPEC.md`` at the canonical
  location;
* :class:`CategorySpecUnreadableError` - it exists but is not a readable regular
  file;
* :class:`CategorySpecPathError` - the referenced entry is not the canonical
  ``SPEC.md`` or resolves outside the category folder;
* the domain's own failures -
  :class:`~architecture_assistant.domain.category_spec.CategorySpecMalformedError`,
  :class:`~architecture_assistant.domain.category_spec.CategorySpecHashMismatchError`
  and
  :class:`~architecture_assistant.domain.category_spec.CategorySpecIdentityError` -
  because the content itself is not trustworthy.

Every one of them derives from
:class:`~architecture_assistant.domain.category_spec.CategorySpecError`, so a
caller that only wants to fail closed can catch that single base class.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.category import Category
from ..domain.category_spec import CategorySpec, CategorySpecError

__all__ = [
    "CategorySpecNotFoundError",
    "CategorySpecPathError",
    "CategorySpecPort",
    "CategorySpecUnreadableError",
]


class CategorySpecNotFoundError(CategorySpecError):
    """Raised when a category has no SPEC.md at its canonical location."""


class CategorySpecUnreadableError(CategorySpecError):
    """Raised when SPEC.md is present but cannot be read as a regular file."""


class CategorySpecPathError(CategorySpecError):
    """Raised when spec_path is not the canonical entry or escapes its folder."""


@runtime_checkable
class CategorySpecPort(Protocol):
    """Reads and validates the SPEC.md of one registered category."""

    def load(self, category: Category) -> CategorySpec:
        """The validated SPEC of ``category``, or a typed error.

        ``category`` carries the catalog row's stable identity: the reader is
        expected to resolve the category's content folder from its immutable id,
        require the canonical ``SPEC.md`` entry, and refuse a document whose
        identity does not agree with the row.
        """
        ...
