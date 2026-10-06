"""Global category catalog - pure domain models.

A *category* is a reusable, cross-project definition (software, CNC, garden,
mechatronics, publishing, electronics, buildings, ...). The catalog that stores
them is **global**: it lives outside every project and outside the repository,
and it is queried before any project database exists. Nothing in this module
knows about SQLite, a filesystem or a Tk window - it is pure, validatable state.

Two naming rules are enforced here and never negotiated:

* a category **id** is an immutable slug (``^[a-z0-9][a-z0-9_-]*$``, at most
  :data:`CATEGORY_ID_MAX_LENGTH` characters). It is the primary key and the
  content-folder name, so it can never be renamed in place;
* a category **path** is a canonical *relative* path under the category content
  root (``SPEC.md``, ``LIBRARY``, ``SUPERVISOR_RULES.md`` by default). Absolute
  machine-specific paths are rejected outright, which is what keeps
  :func:`compute_registry_hash` identical on every machine.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Optional

from .models import utc_now

__all__ = [
    "CATEGORY_ID_PATTERN",
    "CATEGORY_ID_MAX_LENGTH",
    "CATEGORY_ID_RE",
    "is_valid_category_id",
    "SPEC_ENTRY_NAME",
    "LIBRARY_ENTRY_NAME",
    "SUPERVISOR_RULES_ENTRY_NAME",
    "REQUIRED_CATEGORY_ENTRIES",
    "is_relative_category_path",
    "REGISTRY_HASH_ALGORITHM",
    "compute_registry_hash",
    "CategoryState",
    "CatalogAuditAction",
    "Category",
    "CategoryView",
    "CategorySelection",
    "CatalogAuditEntry",
]

#: An id is a lower-case slug: it names the row *and* the content folder, so it
#: may never be a path fragment and may never be renamed in place.
CATEGORY_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"

#: Reasonable maximum length of a category id (filesystem- and UI-friendly).
CATEGORY_ID_MAX_LENGTH = 64

CATEGORY_ID_RE = re.compile(CATEGORY_ID_PATTERN)

#: The three referenced entries every category content folder carries.
SPEC_ENTRY_NAME = "SPEC.md"
LIBRARY_ENTRY_NAME = "LIBRARY"
SUPERVISOR_RULES_ENTRY_NAME = "SUPERVISOR_RULES.md"

#: The entries a category must reference before it may be ``ENABLED``.
REQUIRED_CATEGORY_ENTRIES: tuple[str, ...] = (
    SPEC_ENTRY_NAME,
    LIBRARY_ENTRY_NAME,
    SUPERVISOR_RULES_ENTRY_NAME,
)

#: The algorithm prefix of every registry hash.
REGISTRY_HASH_ALGORITHM = "sha256"


def is_valid_category_id(value: Any) -> bool:
    """Whether ``value`` is a well-formed, immutable category id (a slug)."""
    if not isinstance(value, str):
        return False
    if not 1 <= len(value) <= CATEGORY_ID_MAX_LENGTH:
        return False
    # ``fullmatch``, not ``match``: ``$`` also matches *before* a trailing
    # newline, so ``"software\n"`` must be refused as a whole - the id is the
    # primary key and the folder name, and neither may carry hidden characters.
    return CATEGORY_ID_RE.fullmatch(value) is not None


def is_relative_category_path(value: Any) -> bool:
    """Whether ``value`` is a canonical *relative* path under a category root.

    Deliberately strict: a path must be non-empty, use forward slashes only, and
    never be absolute, a drive letter, a traversal or a UNC prefix. This is what
    keeps the registry hash independent of the machine.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.startswith(("/", "\\")) or "\\" in text or ":" in text:
        return False
    parts = [part for part in text.split("/") if part]
    if not parts:
        return False
    return all(part not in (".", "..") for part in parts)


def compute_registry_hash(
    *,
    category_id: str,
    name: str,
    version: str,
    spec_path: str,
    library_path: str,
    supervisor_rules_path: str,
) -> str:
    """Deterministic metadata/registry hash of one category row.

    Step 2 hashes the **registry metadata only** - the id, the display name, the
    version and the three canonical *relative* paths. Absolute
    ``%LOCALAPPDATA%`` prefixes never take part, so two machines register the
    same category with the same hash. This is deliberately **not** a SPEC/content
    hash: hashing ``SPEC.md`` content is Step 3.
    """
    canonical = {
        "category_id": category_id,
        "name": name,
        "version": version,
        "spec_path": spec_path,
        "library_path": library_path,
        "supervisor_rules_path": supervisor_rules_path,
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{REGISTRY_HASH_ALGORITHM}:{digest}"


class CategoryState(StrEnum):
    """How one registry row is presented to an operator.

    ``ENABLED`` - selectable for a new project and its referenced entries exist.
    ``DISABLED`` - registered but not offered, with a diagnostic when entries are
    missing. ``INVALID`` - claims to be enabled while a referenced entry is
    missing; it is never selectable and explains itself rather than disappearing.
    """

    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    INVALID = "INVALID"


class CatalogAuditAction(StrEnum):
    """The audited catalog mutations. One mutation is one audit entry."""

    REGISTER = "REGISTER"
    ENABLE = "ENABLE"
    DISABLE = "DISABLE"
    UPDATE = "UPDATE"


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string; got {value!r}")
    return value


def _require_dt(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime; got {value!r}")
    return value


@dataclass(frozen=True)
class Category:
    """One authoritative global catalog row.

    ``spec_path``/``library_path``/``supervisor_rules_path`` are canonical
    relative paths under the category content root - never absolute - so the row
    (and its :func:`compute_registry_hash`) is machine-independent.
    """

    id: str
    name: str
    version: str
    spec_path: str = SPEC_ENTRY_NAME
    library_path: str = LIBRARY_ENTRY_NAME
    supervisor_rules_path: str = SUPERVISOR_RULES_ENTRY_NAME
    registry_hash: str = ""
    enabled: bool = False
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not is_valid_category_id(self.id):
            raise ValueError(
                f"id must be a slug matching {CATEGORY_ID_PATTERN} "
                f"(<= {CATEGORY_ID_MAX_LENGTH} chars); got {self.id!r}"
            )
        _require_text(self.name, "name")
        _require_text(self.version, "version")
        for label, value in (
            ("spec_path", self.spec_path),
            ("library_path", self.library_path),
            ("supervisor_rules_path", self.supervisor_rules_path),
        ):
            if not is_relative_category_path(value):
                raise ValueError(
                    f"{label} must be a canonical relative path; got {value!r}"
                )
        if not isinstance(self.enabled, bool):
            raise ValueError(f"enabled must be a bool; got {self.enabled!r}")
        _require_dt(self.created_at, "created_at")
        _require_dt(self.updated_at, "updated_at")

    def entries(self) -> tuple[str, str, str]:
        """The three referenced relative entries, in canonical order."""
        return (self.spec_path, self.library_path, self.supervisor_rules_path)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation (relative paths only)."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "spec_path": self.spec_path,
            "library_path": self.library_path,
            "supervisor_rules_path": self.supervisor_rules_path,
            "registry_hash": self.registry_hash,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class CategoryView:
    """One registry row plus its derived state and diagnostic.

    Nothing is ever filtered out: a category that cannot be offered is returned
    as ``DISABLED``/``INVALID`` with a readable ``diagnostic`` instead of
    silently disappearing.
    """

    category: Category
    state: CategoryState
    diagnostic: str = ""

    @property
    def selectable(self) -> bool:
        """Whether a NEW project may choose this category."""
        return self.state is CategoryState.ENABLED

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "category": self.category.to_dict(),
            "state": self.state.value,
            "diagnostic": self.diagnostic,
            "selectable": self.selectable,
        }


@dataclass(frozen=True)
class CategorySelection:
    """The traceability contract a new project records at creation time.

    Step 2 *prepares* this contract and stores it in the project's local
    ``workspace.json`` preference; persisting it inside the project database is a
    later step, so no project schema changes here.
    """

    category_id: str
    category_version: str
    registry_hash: str

    def __post_init__(self) -> None:
        if not is_valid_category_id(self.category_id):
            raise ValueError(f"category_id must be a slug; got {self.category_id!r}")
        _require_text(self.category_version, "category_version")
        _require_text(self.registry_hash, "registry_hash")

    def to_dict(self) -> dict[str, str]:
        """Deterministic, JSON-safe representation."""
        return {
            "category_id": self.category_id,
            "category_version": self.category_version,
            "registry_hash": self.registry_hash,
        }

    @classmethod
    def from_mapping(
        cls, data: Optional[Mapping[str, Any]]
    ) -> Optional["CategorySelection"]:
        """Rebuild the contract from a persisted mapping, or ``None``."""
        if not isinstance(data, Mapping):
            return None
        category_id = data.get("category_id")
        version = data.get("category_version")
        registry_hash = data.get("registry_hash")
        if not all(
            isinstance(item, str) for item in (category_id, version, registry_hash)
        ):
            return None
        try:
            return cls(
                category_id=str(category_id),
                category_version=str(version),
                registry_hash=str(registry_hash),
            )
        except ValueError:
            return None


@dataclass(frozen=True)
class CatalogAuditEntry:
    """One append-only catalog audit record.

    Produced by the catalog use-case, exactly one per catalog mutation, inside
    the same transaction as the mutation. ``old_value``/``new_value`` are
    canonical JSON mappings of the registry row (relative paths + registry hash),
    and never contain a secret.
    """

    timestamp: datetime
    actor: str
    action: CatalogAuditAction
    category_id: Optional[str] = None
    old_value: Mapping[str, Any] = field(default_factory=dict)
    new_value: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    id: Optional[int] = None

    def __post_init__(self) -> None:
        _require_dt(self.timestamp, "timestamp")
        _require_text(self.actor, "actor")
        _require_text(self.reason, "reason")
        if not isinstance(self.action, CatalogAuditAction):
            object.__setattr__(
                self, "action", CatalogAuditAction(str(self.action))
            )
        if self.category_id is not None and (
            not isinstance(self.category_id, str) or not self.category_id.strip()
        ):
            raise ValueError(
                "category_id must be a non-empty string or None; "
                f"got {self.category_id!r}"
            )
        for label, value in (
            ("old_value", self.old_value),
            ("new_value", self.new_value),
        ):
            payload = dict(value or {})
            try:
                json.dumps(payload, sort_keys=True)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"{label} must be JSON-serializable; got {payload!r}"
                ) from error
            object.__setattr__(self, label, payload)

    def old_value_json(self) -> str:
        """Deterministic JSON encoding of :attr:`old_value` (sorted keys)."""
        return json.dumps(dict(self.old_value), sort_keys=True)

    def new_value_json(self) -> str:
        """Deterministic JSON encoding of :attr:`new_value` (sorted keys)."""
        return json.dumps(dict(self.new_value), sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "actor": self.actor,
            "action": self.action.value,
            "category_id": self.category_id,
            "old_value": dict(self.old_value),
            "new_value": dict(self.new_value),
            "reason": self.reason,
        }
