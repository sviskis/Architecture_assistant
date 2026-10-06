"""Filesystem adapter of the category SPEC port.

It reads ``SPEC.md`` under the **global** category content root resolved by the
existing :class:`~architecture_assistant.ports.global_paths.GlobalPathsPort` -
never a project folder, never the repository - and returns only the validated
:class:`~architecture_assistant.domain.category_spec.CategorySpec`.

Everything that could smuggle content past the catalog is checked here:

* the referenced entry must be the canonical ``SPEC.md``
  (:data:`~architecture_assistant.domain.category.SPEC_ENTRY_NAME`), so a
  noncanonical ``spec_path`` is never "ready";
* the entry must exist, be a *regular* file (a directory of that name is
  refused), be readable and decode as UTF-8;
* the resolved path must stay inside the category folder *and* inside the global
  categories root, so a symlink or a crafted resolver cannot escape;
* the document must parse, hash-verify and agree with the catalog row's id,
  display name and version.

This module never writes anything: ``SPEC.md`` is authored by a human operator
and Step 3 validates (or refuses) what is on disk instead of repairing it. It
also never opens a database of any kind - not the catalog's and not a project's.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.category import SPEC_ENTRY_NAME, Category
from ..domain.category_spec import (
    CategorySpec,
    CategorySpecMalformedError,
    parse_category_spec,
)
from ..ports.category_spec import (
    CategorySpecNotFoundError,
    CategorySpecPathError,
    CategorySpecUnreadableError,
)
from ..ports.global_paths import GlobalPathsPort

__all__ = ["FilesystemCategorySpecReader"]


def _resolved(path: Path) -> Path:
    """Absolute, symlink-resolved path - never raising on a broken link."""
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - defensive: a dangling link/junction
        return path.absolute()


def _is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` itself or lives below it."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class FilesystemCategorySpecReader:
    """The one concrete :class:`CategorySpecPort`: ``SPEC.md`` of a category."""

    def __init__(self, paths: GlobalPathsPort) -> None:
        self._paths = paths

    def load(self, category: Category) -> CategorySpec:
        """Read, validate and return ``SPEC.md`` of one catalog row."""
        if category.spec_path != SPEC_ENTRY_NAME:
            raise CategorySpecPathError(
                f"category {category.id!r} must reference {SPEC_ENTRY_NAME!r} as its "
                f"SPEC entry; got {category.spec_path!r}"
            )
        folder = Path(self._paths.category_root(category.id))
        categories_root = _resolved(Path(self._paths.categories_root()))
        target = folder / category.spec_path
        resolved = _resolved(target)
        if not _is_within(resolved, _resolved(folder)) or not _is_within(
            resolved, categories_root
        ):
            raise CategorySpecPathError(
                f"{SPEC_ENTRY_NAME!r} of category {category.id!r} resolves outside "
                "its category folder"
            )
        if not target.exists():
            raise CategorySpecNotFoundError(
                f"category {category.id!r} has no {SPEC_ENTRY_NAME}"
            )
        if not target.is_file():
            raise CategorySpecUnreadableError(
                f"{SPEC_ENTRY_NAME} of category {category.id!r} is not a regular file"
            )
        try:
            raw = target.read_bytes()
        except OSError as error:
            raise CategorySpecUnreadableError(
                f"{SPEC_ENTRY_NAME} of category {category.id!r} cannot be read "
                f"({type(error).__name__})"
            ) from error
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CategorySpecMalformedError(
                f"{SPEC_ENTRY_NAME} of category {category.id!r} is not valid UTF-8"
            ) from error
        return parse_category_spec(
            text,
            category_id=category.id,
            display_name=category.name,
            version=category.version,
        )
