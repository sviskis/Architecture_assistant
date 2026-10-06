"""Per-category ``SPEC.md`` - the authoritative category specification format.

Every category content folder carries exactly one human-readable specification
document, ``SPEC.md`` (the entry named
:data:`~architecture_assistant.domain.category.SPEC_ENTRY_NAME`). It is the
*authoritative* description of a category: the global catalog only indexes it, and
later steps derive a project strategy from it. Because it is authoritative it is
validated before it is trusted, and that validation - the document format, the
deterministic content hash and the stable-identity binding - is owned by this
module.

Document format (version 1)
---------------------------

A ``SPEC.md`` file is UTF-8 Markdown (one optional leading BOM is tolerated and
CRLF/CR line endings are normalized to LF) made of exactly three parts:

1. a fenced JSON **metadata object**: the opening fence is the line of three
   backticks followed by ``json`` and the closing fence is a line of three
   backticks;
2. one **blank separator line**;
3. the nonblank Markdown **description** - everything that follows, with every
   whitespace character preserved.

The metadata object declares exactly five keys: ``schema_version`` (always
``1``), ``category_id``, ``display_name``, ``version`` and ``spec_hash``. An
unknown key, a duplicate key, a missing key, a wrong type, an unsupported
``schema_version`` and a malformed hash are all refused.

Stable identity
---------------

``category_id`` is the immutable slug (``^[a-z0-9][a-z0-9_-]*$``) that names the
category row *and* its content folder: it can never be renamed in place.
``display_name`` is the human-readable name bound to that id (it may evolve
through the audited catalog API, the id may not) and ``version`` is the
category's own version string, compatible with existing values such as ``1.0``.

Canonical hash
--------------

``spec_hash`` is ``sha256:`` followed by 64 lower-case hexadecimal characters and
is computed over the *canonical serialization* of the semantic content::

    json.dumps(canonical, sort_keys=True, ensure_ascii=False,
               separators=(",", ":"), allow_nan=False).encode("utf-8")

so key order, metadata formatting and line-ending style do not change the hash
while any change of the description's whitespace does. The declared hash itself
and the document framing (the fences, the separator) are excluded. Loading never
repairs a mismatch silently: a declared hash that differs from the recomputed one
is refused.

Only content that can actually be encoded as UTF-8 is hashable: an escaped lone
surrogate such as ``"\\ud800"`` - which ``json.loads`` turns back into a real
surrogate - is refused with :class:`CategorySpecMalformedError` instead of
leaking a raw ``UnicodeEncodeError`` to a caller that only expects SPEC failures.
A metadata document that ``json.loads`` itself refuses without raising a
``JSONDecodeError`` - an integer literal longer than CPython's int/str conversion
limit (``sys.get_int_max_str_digits()``) is the documented case - is refused the
same way, as a malformed document rather than a raw ``ValueError``.

This is deliberately **not** the Step 2 *registry* hash
(:func:`~architecture_assistant.domain.category.compute_registry_hash`), which
covers the catalog row only; the two hashes stay separate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from .category import is_valid_category_id

__all__ = [
    "SPEC_SCHEMA_VERSION",
    "SPEC_METADATA_KEYS",
    "SPEC_METADATA_ORDER",
    "SPEC_METADATA_FENCE_OPEN",
    "SPEC_METADATA_FENCE_CLOSE",
    "SPEC_HASH_ALGORITHM",
    "SPEC_HASH_PATTERN",
    "SPEC_HASH_RE",
    "CategorySpecError",
    "CategorySpecMalformedError",
    "CategorySpecHashMismatchError",
    "CategorySpecIdentityError",
    "CategorySpec",
    "normalize_spec_text",
    "canonical_spec_serialization",
    "canonical_spec_bytes",
    "compute_spec_hash",
    "parse_category_spec",
]

#: The only supported SPEC.md schema version in this build.
SPEC_SCHEMA_VERSION = 1

#: The metadata keys, in the documented declaration order (all five are required).
SPEC_METADATA_ORDER: tuple[str, ...] = (
    "schema_version",
    "category_id",
    "display_name",
    "version",
    "spec_hash",
)

#: The metadata key set: unknown and duplicate keys are refused.
SPEC_METADATA_KEYS = frozenset(SPEC_METADATA_ORDER)

#: The opening fence line of the metadata block (``json`` after three backticks).
SPEC_METADATA_FENCE_OPEN = "```json"

#: The closing fence line of the metadata block.
SPEC_METADATA_FENCE_CLOSE = "```"

#: The algorithm prefix of every SPEC content hash.
SPEC_HASH_ALGORITHM = "sha256"

#: A declared hash: ``sha256:`` plus 64 lower-case hexadecimal characters.
SPEC_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"

SPEC_HASH_RE = re.compile(SPEC_HASH_PATTERN)


class CategorySpecError(Exception):
    """Base class for every category-SPEC failure."""


class CategorySpecMalformedError(CategorySpecError):
    """Raised when a SPEC.md document violates the format or the schema."""


class CategorySpecHashMismatchError(CategorySpecError):
    """Raised when the declared hash differs from the recomputed content hash."""


class CategorySpecIdentityError(CategorySpecError):
    """Raised when the SPEC identity does not agree with the catalog row."""


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CategorySpecMalformedError(
            f"{field_name} must be a non-empty string; got {value!r}"
        )
    return value


def normalize_spec_text(text: Any) -> str:
    """Strip one optional leading UTF-8 BOM and normalize CRLF/CR to LF.

    The result is the *framing-normalized* document: everything that must not
    influence the hash (a byte-order mark and the line-ending style) is removed
    here, and nothing else is touched.
    """
    if not isinstance(text, str):
        raise CategorySpecMalformedError(
            f"a SPEC.md document must be text; got {type(text).__name__}"
        )
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def canonical_spec_serialization(
    *,
    schema_version: int,
    category_id: str,
    display_name: str,
    version: str,
    description: str,
) -> str:
    """The canonical JSON string the SPEC content hash is computed over.

    Only the *semantic* content takes part: the metadata fields and the
    description. The declared hash and the document framing are excluded by
    construction, and sorted keys plus ``ensure_ascii=False`` make the result
    identical regardless of metadata formatting, key order or Unicode escaping.
    """
    payload = {
        "schema_version": schema_version,
        "category_id": category_id,
        "display_name": display_name,
        "version": version,
        "description": description,
    }
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise CategorySpecMalformedError(
            f"the SPEC content cannot be canonicalized: {error}"
        ) from error


def canonical_spec_bytes(
    *,
    schema_version: int,
    category_id: str,
    display_name: str,
    version: str,
    description: str,
) -> bytes:
    """The canonical UTF-8 bytes (no BOM) the SPEC hash is computed over.

    Content that cannot be encoded as UTF-8 - a lone surrogate, typically the
    result of an escaped ``"\\ud800"`` in the JSON metadata or the description -
    is refused with :class:`CategorySpecMalformedError`. ``str.encode`` raises
    ``UnicodeEncodeError`` for it, and that raw error must never escape: the
    catalog only catches :class:`CategorySpecError`, so a single malformed
    document would otherwise crash the whole category listing.
    """
    serialized = canonical_spec_serialization(
        schema_version=schema_version,
        category_id=category_id,
        display_name=display_name,
        version=version,
        description=description,
    )
    try:
        return serialized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CategorySpecMalformedError(
            f"the SPEC content cannot be encoded as UTF-8: {error}"
        ) from error


def compute_spec_hash(
    *,
    schema_version: int,
    category_id: str,
    display_name: str,
    version: str,
    description: str,
) -> str:
    """Deterministic ``sha256:<64 hex>`` content hash of one category SPEC."""
    digest = hashlib.sha256(
        canonical_spec_bytes(
            schema_version=schema_version,
            category_id=category_id,
            display_name=display_name,
            version=version,
            description=description,
        )
    ).hexdigest()
    return f"{SPEC_HASH_ALGORITHM}:{digest}"


@dataclass(frozen=True)
class CategorySpec:
    """One validated SPEC.md: identity, version, description and content hash.

    Instances are produced by :func:`parse_category_spec` (or by the catalog's
    SPEC reader); constructing one directly validates the metadata *and* verifies
    that :attr:`spec_hash` matches the recomputed content hash.
    """

    category_id: str
    display_name: str
    version: str
    description: str
    spec_hash: str
    schema_version: int = SPEC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise CategorySpecMalformedError(
                f"schema_version must be an integer; got {self.schema_version!r}"
            )
        if self.schema_version != SPEC_SCHEMA_VERSION:
            raise CategorySpecMalformedError(
                f"unsupported schema_version {self.schema_version!r}; this build "
                f"understands {SPEC_SCHEMA_VERSION}"
            )
        if not is_valid_category_id(self.category_id):
            raise CategorySpecMalformedError(
                f"category_id must be an immutable slug; got {self.category_id!r}"
            )
        _require_text(self.display_name, "display_name")
        _require_text(self.version, "version")
        if not isinstance(self.description, str) or not self.description.strip():
            raise CategorySpecMalformedError(
                "description must not be blank; a SPEC.md needs a real category "
                "description"
            )
        if (
            not isinstance(self.spec_hash, str)
            or SPEC_HASH_RE.fullmatch(self.spec_hash) is None
        ):
            raise CategorySpecMalformedError(
                "spec_hash must be 'sha256:' followed by 64 lower-case hexadecimal "
                f"characters; got {self.spec_hash!r}"
            )
        recomputed = compute_spec_hash(
            schema_version=self.schema_version,
            category_id=self.category_id,
            display_name=self.display_name,
            version=self.version,
            description=self.description,
        )
        if recomputed != self.spec_hash:
            raise CategorySpecHashMismatchError(
                "the declared spec_hash does not match the recomputed SPEC content "
                f"hash ({self.spec_hash} != {recomputed})"
            )

    @property
    def content(self) -> str:
        """The Markdown description - the category's authoritative content."""
        return self.description

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-safe representation."""
        return {
            "schema_version": self.schema_version,
            "category_id": self.category_id,
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "spec_hash": self.spec_hash,
        }


def _load_metadata(metadata_text: str) -> dict[str, Any]:
    """Parse the metadata JSON object, refusing duplicates and unknown keys."""

    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                raise CategorySpecMalformedError(
                    f"duplicate SPEC.md metadata key {key!r}"
                )
            seen.add(key)
        return dict(pairs)

    try:
        metadata = json.loads(metadata_text, object_pairs_hook=_reject_duplicate_keys)
    except CategorySpecMalformedError:
        raise
    except json.JSONDecodeError as error:
        raise CategorySpecMalformedError(
            f"the SPEC.md metadata block is not valid JSON ({error})"
        ) from error
    except ValueError as error:
        # ``json.loads`` can raise a plain ``ValueError`` that is *not* a
        # ``JSONDecodeError``: an integer literal longer than CPython's int/str
        # conversion limit (``sys.get_int_max_str_digits()``) is the documented
        # case, and the document is still syntactically valid JSON. The catalog
        # only handles :class:`CategorySpecError`, so the raw ``ValueError`` is
        # translated here - one malformed SPEC must never crash the listing.
        raise CategorySpecMalformedError(
            f"the SPEC.md metadata block cannot be decoded ({error})"
        ) from error
    if not isinstance(metadata, dict):
        raise CategorySpecMalformedError("the SPEC.md metadata must be a JSON object")
    unknown = sorted(set(metadata) - SPEC_METADATA_KEYS)
    if unknown:
        raise CategorySpecMalformedError(
            "unknown SPEC.md metadata keys: " + ", ".join(unknown)
        )
    missing = sorted(SPEC_METADATA_KEYS - set(metadata))
    if missing:
        raise CategorySpecMalformedError(
            "missing SPEC.md metadata keys: " + ", ".join(missing)
        )
    return metadata


def _check_identity(
    spec: CategorySpec,
    *,
    category_id: Optional[str],
    display_name: Optional[str],
    version: Optional[str],
) -> None:
    """Bind the SPEC's stable identity to the catalog row it claims to describe."""
    problems: list[str] = []
    if category_id is not None and spec.category_id != category_id:
        problems.append(
            f"SPEC category_id {spec.category_id!r} does not match {category_id!r}"
        )
    if display_name is not None and spec.display_name != display_name:
        problems.append(
            f"SPEC display_name {spec.display_name!r} does not match "
            f"{display_name!r}"
        )
    if version is not None and spec.version != version:
        problems.append(f"SPEC version {spec.version!r} does not match {version!r}")
    if problems:
        raise CategorySpecIdentityError(
            "the SPEC.md identity does not agree with the catalog row: "
            + "; ".join(problems)
        )


def parse_category_spec(
    text: Any,
    *,
    category_id: Optional[str] = None,
    display_name: Optional[str] = None,
    version: Optional[str] = None,
) -> CategorySpec:
    """Parse, validate and hash-verify one SPEC.md document.

    ``category_id``/``display_name``/``version`` are the expectations of the
    catalog row the document claims to describe; when given they must match the
    document exactly. A missing, malformed or hash-mismatching document raises a
    :class:`CategorySpecError` subclass and is never repaired.
    """
    normalized = normalize_spec_text(text)
    opening = SPEC_METADATA_FENCE_OPEN + "\n"
    if not normalized.startswith(opening):
        raise CategorySpecMalformedError(
            "a SPEC.md document must begin with the metadata fence "
            f"{SPEC_METADATA_FENCE_OPEN!r} on its first line"
        )
    rest = normalized[len(opening) :]
    closing = "\n" + SPEC_METADATA_FENCE_CLOSE + "\n"
    close_at = rest.find(closing)
    if close_at < 0:
        raise CategorySpecMalformedError(
            "the SPEC.md metadata fence is not closed (expected a line "
            f"{SPEC_METADATA_FENCE_CLOSE!r}, then a blank line and the description)"
        )
    metadata = _load_metadata(rest[:close_at])
    after = rest[close_at + len(closing) :]
    if not after.startswith("\n"):
        raise CategorySpecMalformedError(
            "a SPEC.md document needs exactly one blank separator line between the "
            "metadata fence and the description"
        )
    description = after[1:]
    spec = CategorySpec(
        schema_version=metadata["schema_version"],
        category_id=metadata["category_id"],
        display_name=metadata["display_name"],
        version=metadata["version"],
        description=description,
        spec_hash=metadata["spec_hash"],
    )
    _check_identity(
        spec,
        category_id=category_id,
        display_name=display_name,
        version=version,
    )
    return spec
