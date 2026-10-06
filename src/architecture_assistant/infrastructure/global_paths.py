"""Concrete global-path resolver - ``%LOCALAPPDATA%`` and the filesystem.

The global category catalog lives outside every project and outside the
repository, at a user-level location:

    %LOCALAPPDATA%\\ArchitectureAssistant\\catalog.db
    %LOCALAPPDATA%\\ArchitectureAssistant\\categories\\<category_id>\\

This module is the **only** place that reads the Windows environment, builds
those paths and touches the filesystem for them; the application layer only sees
:class:`~architecture_assistant.ports.global_paths.GlobalPathsPort`. It is
fail-closed: a missing ``LOCALAPPDATA`` or a non-writable root raises
:class:`GlobalPathsError` and nothing is relocated silently. (A ``--catalog-root``
override is deliberately *not* implemented in Step 2.)

``base_dir``/``environ`` are injectable so tests never depend on the machine they
run on.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..domain.category import is_valid_category_id
from ..ports.global_paths import GlobalPathsError

__all__ = [
    "LOCALAPPDATA_ENV_VAR",
    "GLOBAL_APP_FOLDER",
    "CATALOG_DB_FILENAME",
    "CATEGORIES_FOLDER",
    "LocalAppDataGlobalPaths",
]

#: The environment variable that names the user's local application-data folder.
LOCALAPPDATA_ENV_VAR = "LOCALAPPDATA"

#: The assistant's own folder under it - shared by every project, owned by none.
GLOBAL_APP_FOLDER = "ArchitectureAssistant"

#: The authoritative global catalog database file name.
CATALOG_DB_FILENAME = "catalog.db"

#: The folder that holds one content folder per category.
CATEGORIES_FOLDER = "categories"


class LocalAppDataGlobalPaths:
    """The one authoritative resolver of the global catalog location."""

    def __init__(
        self,
        *,
        environ: Optional[Mapping[str, str]] = None,
        base_dir: Optional[object] = None,
    ) -> None:
        self._environ = dict(os.environ) if environ is None else dict(environ)
        self._base_dir = None if base_dir is None else Path(str(base_dir))

    @property
    def base_dir(self) -> Path:
        """The assistant's global folder, resolved on demand (fail closed)."""
        if self._base_dir is not None:
            return self._base_dir
        raw = str(self._environ.get(LOCALAPPDATA_ENV_VAR) or "").strip()
        if not raw:
            raise GlobalPathsError(
                f"{LOCALAPPDATA_ENV_VAR} is not set, so the global category "
                "catalog cannot be located; refusing to guess a location"
            )
        return Path(raw) / GLOBAL_APP_FOLDER

    def catalog_database_path(self) -> str:
        """Absolute path of the global catalog SQLite database."""
        return str(self.base_dir / CATALOG_DB_FILENAME)

    def categories_root(self) -> str:
        """Absolute path of the global category content root."""
        return str(self.base_dir / CATEGORIES_FOLDER)

    def category_root(self, category_id: str) -> str:
        """Absolute path of one category's content folder."""
        if not is_valid_category_id(category_id):
            raise GlobalPathsError(
                f"category_id {category_id!r} is not a valid slug, so no "
                "category folder can be resolved"
            )
        return str(self.base_dir / CATEGORIES_FOLDER / category_id)

    def missing_entries(
        self, category_id: str, entries: Sequence[str]
    ) -> tuple[str, ...]:
        """Which of ``entries`` do not exist under the category root."""
        root = Path(self.category_root(category_id))
        missing: list[str] = []
        for relative in entries:
            if not (root / relative).exists():
                missing.append(relative)
        return tuple(missing)

    def ensure_ready(self) -> None:
        """Create the global root and prove it is writable, or fail closed."""
        base = self.base_dir
        try:
            (base / CATEGORIES_FOLDER).mkdir(parents=True, exist_ok=True)
            probe = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(base),
                prefix=".write_probe.",
                suffix=".tmp",
                delete=False,
            )
            try:
                probe.write("ok")
            finally:
                probe.close()
            Path(probe.name).unlink(missing_ok=True)
        except OSError as error:
            raise GlobalPathsError(
                "the global catalog directory is not writable: "
                f"{base} ({type(error).__name__})"
            ) from error
