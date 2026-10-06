"""Tests for the mandatory per-category ``SPEC.md`` (universalization Step 3).

Covers the document format v1 and its framing rules, the deterministic canonical
serialization with fixed SHA-256 vectors, the stable-identity/display-name
binding, the freshness and containment rules of the filesystem reader, and the
rule that validation never repairs, rewrites or creates anything.

The canonical byte string and the digest asserted below were computed
**independently** of the implementation under test - with plain ``json`` and
``hashlib`` - and are pinned as literals.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest

from architecture_assistant.domain.category import (
    CATEGORY_ID_MAX_LENGTH,
    LIBRARY_ENTRY_NAME,
    SPEC_ENTRY_NAME,
    SUPERVISOR_RULES_ENTRY_NAME,
    Category,
    is_valid_category_id,
)
from architecture_assistant.domain.category_spec import (
    SPEC_HASH_ALGORITHM,
    SPEC_METADATA_KEYS,
    SPEC_SCHEMA_VERSION,
    CategorySpec,
    CategorySpecError,
    CategorySpecHashMismatchError,
    CategorySpecIdentityError,
    CategorySpecMalformedError,
    canonical_spec_bytes,
    canonical_spec_serialization,
    compute_spec_hash,
    normalize_spec_text,
    parse_category_spec,
)
from architecture_assistant.infrastructure.category_spec import (
    FilesystemCategorySpecReader,
)
from architecture_assistant.infrastructure.global_paths import LocalAppDataGlobalPaths
from architecture_assistant.ports.category_spec import (
    CategorySpecNotFoundError,
    CategorySpecPathError,
    CategorySpecPort,
    CategorySpecUnreadableError,
)

#: The reference description - deliberately ending with a newline, because the
#: description is whitespace-significant.
DESCRIPTION = "# Software\n\nSoftware projects.\n"

#: The canonical JSON string the reference SPEC hashes to, written out literally.
CANONICAL = (
    '{"category_id":"software","description":"# Software\\n\\nSoftware projects.\\n",'
    '"display_name":"Software","schema_version":1,"version":"1.0"}'
)

#: ``sha256(CANONICAL.encode("utf-8"))``, computed with plain ``hashlib``.
DIGEST = "9b49ab70073c83f4e2e514f0e7b503eed0f8e2f1fa859545f5153ac137ecbb95"

#: The reference declared hash of :data:`DESCRIPTION`.
HASH = f"sha256:{DIGEST}"

UNICODE_DESCRIPTION = "# Projekt\u0113\u0161ana\n\n\u013c\u0145oti \u012bpa\u0161s\n"


def metadata_for(
    *,
    category_id: str = "software",
    display_name: str = "Software",
    version: str = "1.0",
    description: str = DESCRIPTION,
    **overrides: object,
) -> dict[str, object]:
    """A complete, correctly hashed metadata object for one SPEC."""
    metadata: dict[str, object] = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "category_id": category_id,
        "display_name": display_name,
        "version": version,
        "spec_hash": compute_spec_hash(
            schema_version=SPEC_SCHEMA_VERSION,
            category_id=category_id,
            display_name=display_name,
            version=version,
            description=description,
        ),
    }
    metadata.update(overrides)
    return metadata


def document(
    metadata: dict[str, object],
    description: str = DESCRIPTION,
    *,
    indent: int | None = 2,
    fence: str = "```json",
) -> str:
    """Frame one metadata object and one description into a SPEC.md text."""
    return f"{fence}\n{json.dumps(metadata, indent=indent)}\n```\n\n{description}"


def valid_document(
    *,
    category_id: str = "software",
    display_name: str = "Software",
    version: str = "1.0",
    description: str = DESCRIPTION,
    **overrides: object,
) -> str:
    """A valid SPEC.md for the reference category (with optional overrides)."""
    metadata = metadata_for(
        category_id=category_id,
        display_name=display_name,
        version=version,
        description=description,
        **overrides,
    )
    return document(metadata, description)


class TestSlugValidation:
    """The shared id validator matches the *whole* string (Step 3 correction)."""

    def test_ids_are_validated_as_a_whole_string(self) -> None:
        assert is_valid_category_id("software") is True
        assert is_valid_category_id("cnc-2_axis") is True
        assert is_valid_category_id("x" * CATEGORY_ID_MAX_LENGTH) is True

        for bad in (
            "software\n",  # ``$`` also matches before a trailing newline
            "software ",
            " Software",
            "Soft",
            "a/b",
            "a.b",
            "",
            "x" * (CATEGORY_ID_MAX_LENGTH + 1),
            7,
            None,
        ):
            assert is_valid_category_id(bad) is False, bad


class TestCanonicalSerialization:
    """The hash is computed over one documented canonical byte string."""

    def test_the_canonical_bytes_are_the_documented_string(self) -> None:
        payload = canonical_spec_bytes(
            schema_version=SPEC_SCHEMA_VERSION,
            category_id="software",
            display_name="Software",
            version="1.0",
            description=DESCRIPTION,
        )

        assert payload == CANONICAL.encode("utf-8")
        assert not payload.startswith(b"\xef\xbb\xbf")
        # the semantic content only - the declared hash and the framing are out
        assert set(json.loads(payload)) == {
            "schema_version",
            "category_id",
            "display_name",
            "version",
            "description",
        }

    def test_the_fixed_sha256_vector(self) -> None:
        """Both the declared hash and an independent recomputation must agree."""
        assert (
            compute_spec_hash(
                schema_version=SPEC_SCHEMA_VERSION,
                category_id="software",
                display_name="Software",
                version="1.0",
                description=DESCRIPTION,
            )
            == HASH
        )
        assert (
            f"{SPEC_HASH_ALGORITHM}:"
            f"{hashlib.sha256(CANONICAL.encode('utf-8')).hexdigest()}"
            == HASH
        )
        assert HASH.startswith("sha256:")
        assert len(HASH) == len("sha256:") + 64
        assert HASH == HASH.lower()

    def test_the_declared_hash_is_excluded_from_the_hash(self) -> None:
        """The payload covers the semantics only - never the declared hash."""
        payload = canonical_spec_serialization(
            schema_version=SPEC_SCHEMA_VERSION,
            category_id="software",
            display_name="Software",
            version="1.0",
            description=DESCRIPTION,
        )

        assert "spec_hash" not in payload
        assert "spec_hash" not in inspect.signature(compute_spec_hash).parameters

    def test_unicode_is_preserved_instead_of_escaped(self) -> None:
        text = valid_document(description=UNICODE_DESCRIPTION)

        spec = parse_category_spec(text)

        assert spec.description == UNICODE_DESCRIPTION
        assert "\u0113" in canonical_spec_serialization(
            schema_version=SPEC_SCHEMA_VERSION,
            category_id="software",
            display_name="Software",
            version="1.0",
            description=UNICODE_DESCRIPTION,
        )

    def test_metadata_key_order_and_formatting_do_not_change_the_hash(self) -> None:
        ordered = document(metadata_for(), indent=2)
        shuffled_metadata = dict(reversed(list(metadata_for().items())))
        shuffled = document(shuffled_metadata, indent=4)

        assert ordered != shuffled
        assert parse_category_spec(ordered).spec_hash == parse_category_spec(
            shuffled
        ).spec_hash

    def test_bom_and_line_endings_do_not_change_the_hash(self) -> None:
        text = valid_document()
        expected = parse_category_spec(text).spec_hash

        for variant in (
            "\ufeff" + text,
            text.replace("\n", "\r\n"),
            text.replace("\n", "\r"),
        ):
            assert parse_category_spec(variant).spec_hash == expected

    def test_description_whitespace_changes_the_hash(self) -> None:
        base = parse_category_spec(valid_document()).spec_hash

        for changed in (
            DESCRIPTION + "\n",
            DESCRIPTION.rstrip("\n"),
            DESCRIPTION.replace("Software", "Software "),
            "# Software\n\nSoftware projects.",  # one newline fewer
        ):
            metadata = metadata_for(description=changed)
            assert (
                parse_category_spec(document(metadata, changed)).spec_hash != base
            )

    def test_a_trailing_whitespace_change_is_a_hash_mismatch(self) -> None:
        with pytest.raises(CategorySpecHashMismatchError):
            parse_category_spec(valid_document() + " ")

    def test_normalization_only_touches_bom_and_line_endings(self) -> None:
        assert normalize_spec_text("\ufeffa\r\nb\rc\nd") == "a\nb\nc\nd"
        assert normalize_spec_text("").startswith("")
        with pytest.raises(CategorySpecMalformedError):
            normalize_spec_text(b"bytes are not text")  # type: ignore[arg-type]


def _without(key: str) -> str:
    metadata = {k: v for k, v in metadata_for().items() if k != key}
    return document(metadata)  # type: ignore[arg-type]


#: Raw metadata with a repeated key - constructed by hand, not by ``json.dumps``.
DUPLICATE_KEY_DOCUMENT = (
    "```json\n"
    '{"schema_version":1,"category_id":"software","category_id":"software",'
    f'"display_name":"Software","version":"1.0","spec_hash":"{HASH}"}}\n'
    "```\n\n" + DESCRIPTION
)

#: Raw metadata with a JSON-escaped lone surrogate, constructed by hand rather
#: than by ``json.dumps``/``valid_document`` (whose hashing correctly refuses such
#: content). The file itself is valid ASCII/UTF-8, but ``json.loads`` turns the
#: escape back into a real surrogate that cannot be encoded as UTF-8 - a malformed
#: document, never a crash.
LONE_SURROGATE_DOCUMENT = (
    "```json\n"
    '{"schema_version":1,"category_id":"software","display_name":"Software",'
    f'"version":"\\ud800","spec_hash":"{HASH}"}}\n'
    "```\n\n" + DESCRIPTION
)

#: CPython's int/str conversion limit (``sys.get_int_max_str_digits()``); ``0``
#: means the limit is disabled for this interpreter.
INT_DIGIT_LIMIT = sys.get_int_max_str_digits()

#: A digit count beyond CPython's int/str conversion limit, so ``json.loads``
#: refuses the integer literal. The default limit is 4300, hence 5000 digits.
OVERSIZED_INTEGER_DIGITS = "9" * 5000

#: Raw metadata whose ``version`` value is an integer literal of 5000 digits.
#: The document is *syntactically valid JSON* - ``json.loads`` simply refuses to
#: convert the integer and raises a plain ``ValueError`` that is **not** a
#: ``JSONDecodeError`` - so it is built by hand, because ``json.dumps`` cannot
#: itself stringify such an int.
OVERSIZED_INTEGER_DOCUMENT = (
    "```json\n"
    '{"schema_version":1,"category_id":"software","display_name":"Software",'
    '"version":' + OVERSIZED_INTEGER_DIGITS + f',"spec_hash":"{HASH}"}}\n'
    "```\n\n" + DESCRIPTION
)

#: The metadata block of :data:`OVERSIZED_INTEGER_DOCUMENT`, for the premise test.
OVERSIZED_INTEGER_BLOCK = OVERSIZED_INTEGER_DOCUMENT.split("```json\n", 1)[1].split(
    "\n```", 1
)[0]

#: The premise of the regression only holds while the digit limit is active.
needs_int_digit_limit = pytest.mark.skipif(
    INT_DIGIT_LIMIT == 0 or INT_DIGIT_LIMIT >= len(OVERSIZED_INTEGER_DIGITS),
    reason=(
        "CPython int/str digit limit is disabled or wider than "
        f"{len(OVERSIZED_INTEGER_DIGITS)} digits"
    ),
)

MALFORMED_DOCUMENTS = (
    ("an empty document", ""),
    ("a document with no metadata fence", "# Software\n\nno fence at all\n"),
    (
        "a fence that is never closed",
        '```json\n{"schema_version":1}\n\n# Software\n',
    ),
    ("metadata that is not JSON", "```json\nnope\n```\n\n# Software\n"),
    ("metadata that is not an object", "```json\n[1, 2]\n```\n\n# Software\n"),
    ("a repeated metadata key", DUPLICATE_KEY_DOCUMENT),
    ("an unknown metadata key", valid_document(note="extra")),
    ("a missing metadata key", _without("version")),
    ("a schema version of two", valid_document(schema_version=2)),
    ("a schema version that is a string", valid_document(schema_version="1")),
    ("a schema version that is a bool", valid_document(schema_version=True)),
    ("a category id that is not a slug", valid_document(category_id="Software")),
    (
        "a category id with a trailing newline",
        valid_document(category_id="software\n"),
    ),
    ("a category id that is too long", valid_document(category_id="x" * 65)),
    ("a category id that is not a string", valid_document(category_id=7)),
    ("a blank display name", valid_document(display_name="")),
    ("a display name that is not a string", valid_document(display_name=42)),
    ("a blank version", valid_document(version="   ")),
    ("an empty spec hash", valid_document(spec_hash="")),
    (
        "a spec hash in upper case",
        valid_document(spec_hash="sha256:" + DIGEST.upper()),
    ),
    ("a spec hash without the algorithm", valid_document(spec_hash=DIGEST)),
    ("a blank description", document(metadata_for(description="   "), "   ")),
    (
        "a fence without the blank separator",
        document(metadata_for(), indent=None).replace("```\n\n", "```\n", 1),
    ),
    ("metadata with an escaped lone surrogate", LONE_SURROGATE_DOCUMENT),
    ("metadata with an oversized integer literal", OVERSIZED_INTEGER_DOCUMENT),
)


class TestDocumentFormat:
    """The format v1 framing, schema and declared hash are all mandatory."""

    def test_the_metadata_keys_are_exactly_the_documented_five(self) -> None:
        assert SPEC_METADATA_KEYS == {
            "schema_version",
            "category_id",
            "display_name",
            "version",
            "spec_hash",
        }

    def test_a_valid_document_parses_into_the_validated_value(self) -> None:
        spec = parse_category_spec(valid_document())

        assert isinstance(spec, CategorySpec)
        assert spec.schema_version == SPEC_SCHEMA_VERSION
        assert spec.category_id == "software"
        assert spec.display_name == "Software"
        assert spec.version == "1.0"
        assert spec.description == DESCRIPTION
        assert spec.content == DESCRIPTION
        assert spec.spec_hash == HASH
        assert spec.to_dict()["spec_hash"] == HASH
        assert spec.to_dict()["description"] == DESCRIPTION

    def test_the_value_is_frozen(self) -> None:
        spec = parse_category_spec(valid_document())

        with pytest.raises(Exception):
            spec.version = "9.9"  # type: ignore[misc]

    def test_a_stale_declared_hash_cannot_be_constructed(self) -> None:
        with pytest.raises(CategorySpecHashMismatchError):
            CategorySpec(
                category_id="software",
                display_name="Software",
                version="1.0",
                description="# Tampered\n",
                spec_hash=HASH,
            )

    @pytest.mark.parametrize(
        "label,text",
        MALFORMED_DOCUMENTS,
        ids=[case[0] for case in MALFORMED_DOCUMENTS],
    )
    def test_malformed_documents_are_refused(self, label: str, text: str) -> None:
        with pytest.raises(CategorySpecMalformedError):
            parse_category_spec(text)

    def test_a_tampered_description_is_a_hash_mismatch(self) -> None:
        tampered = valid_document().replace("Software projects.", "Something else.")

        with pytest.raises(CategorySpecHashMismatchError, match="declared spec_hash"):
            parse_category_spec(tampered)

    def test_every_spec_failure_shares_one_base_class(self) -> None:
        for error in (
            CategorySpecMalformedError,
            CategorySpecHashMismatchError,
            CategorySpecIdentityError,
            CategorySpecNotFoundError,
            CategorySpecUnreadableError,
            CategorySpecPathError,
        ):
            assert issubclass(error, CategorySpecError)


class TestNonUtf8EncodableContent:
    """A JSON-escaped lone surrogate is a typed malformed SPEC, never a crash.

    ``json.loads`` turns an escape such as ``\\ud800`` back into a real lone
    surrogate, which ``str.encode("utf-8")`` cannot encode. That must surface as
    :class:`CategorySpecMalformedError` - a subclass of the base class the catalog
    catches - and never leak a raw ``UnicodeEncodeError``.
    """

    #: A real lone surrogate (not the JSON escape) - only ``json.loads`` or a
    #: hand-built value can produce one, a UTF-8 file never carries it directly.
    SURROGATE = "\ud800"

    def test_a_lone_surrogate_version_is_refused_by_the_canonical_bytes(self) -> None:
        with pytest.raises(CategorySpecMalformedError, match="UTF-8"):
            canonical_spec_bytes(
                schema_version=SPEC_SCHEMA_VERSION,
                category_id="software",
                display_name="Software",
                version=self.SURROGATE,
                description=DESCRIPTION,
            )

    def test_a_lone_surrogate_description_is_refused_by_the_hash(self) -> None:
        with pytest.raises(CategorySpecMalformedError, match="UTF-8"):
            compute_spec_hash(
                schema_version=SPEC_SCHEMA_VERSION,
                category_id="software",
                display_name="Software",
                version="1.0",
                description=f"# {self.SURROGATE}\n",
            )

    def test_it_is_a_category_spec_error_not_a_raw_unicode_error(self) -> None:
        with pytest.raises(CategorySpecMalformedError) as caught:
            compute_spec_hash(
                schema_version=SPEC_SCHEMA_VERSION,
                category_id="software",
                display_name="Software",
                version=self.SURROGATE,
                description=DESCRIPTION,
            )

        assert isinstance(caught.value, CategorySpecError)
        assert not isinstance(caught.value, UnicodeError)
        assert not issubclass(CategorySpecMalformedError, UnicodeError)

    def test_an_escaped_surrogate_document_is_malformed(self) -> None:
        """The on-disk escape is ASCII; only ``json.loads`` makes it a surrogate."""
        assert "\\ud800" in LONE_SURROGATE_DOCUMENT
        assert LONE_SURROGATE_DOCUMENT.isascii()

        with pytest.raises(CategorySpecMalformedError, match="UTF-8"):
            parse_category_spec(LONE_SURROGATE_DOCUMENT)

    def test_the_reader_refuses_it_with_the_same_typed_error(self, paths) -> None:
        write_category(paths, LONE_SURROGATE_DOCUMENT)

        with pytest.raises(CategorySpecMalformedError, match="UTF-8"):
            FilesystemCategorySpecReader(paths).load(category())


class TestOversizedJsonInteger:
    """A metadata integer beyond CPython's digit limit is a typed malformed SPEC.

    ``json.loads`` refuses an integer literal with more than
    ``sys.get_int_max_str_digits()`` digits (4300 by default) and raises a plain
    ``ValueError`` that is **not** a ``JSONDecodeError``. A decode guard catching
    only ``json.JSONDecodeError`` let that raw error escape the typed SPEC
    hierarchy, so one malformed ``SPEC.md`` crashed the whole catalog listing
    instead of producing an INVALID/DISABLED diagnostic.
    """

    @needs_int_digit_limit
    def test_the_integer_literal_exceeds_cpythons_digit_limit(self) -> None:
        """The document is valid JSON; only the digit limit refuses the int."""
        assert OVERSIZED_INTEGER_DOCUMENT.isascii()

        with pytest.raises(ValueError) as caught:
            json.loads(OVERSIZED_INTEGER_BLOCK)

        assert not isinstance(caught.value, json.JSONDecodeError)
        assert "digit" in str(caught.value)

    def test_parse_refuses_it_with_the_typed_error(self) -> None:
        with pytest.raises(CategorySpecMalformedError):
            parse_category_spec(OVERSIZED_INTEGER_DOCUMENT)

    @needs_int_digit_limit
    def test_the_decode_value_error_is_translated_not_leaked(self) -> None:
        """The typed failure comes from translating the decoder's ``ValueError``.

        Without the guard the raw ``ValueError`` escaped; this pins that the
        refusal is the translation of that exact error, not an unrelated check.
        """
        with pytest.raises(CategorySpecMalformedError) as caught:
            parse_category_spec(OVERSIZED_INTEGER_DOCUMENT)

        cause = caught.value.__cause__
        assert isinstance(cause, ValueError)
        assert not isinstance(cause, json.JSONDecodeError)

    def test_the_refusal_never_leaks_a_raw_value_error(self) -> None:
        with pytest.raises(CategorySpecMalformedError) as caught:
            parse_category_spec(OVERSIZED_INTEGER_DOCUMENT)

        assert isinstance(caught.value, CategorySpecError)
        assert not isinstance(caught.value, json.JSONDecodeError)
        assert not isinstance(caught.value, UnicodeError)

    def test_the_reader_refuses_it_with_the_same_typed_error(self, paths) -> None:
        target = write_category(paths, OVERSIZED_INTEGER_DOCUMENT)
        before = target.read_bytes()

        with pytest.raises(CategorySpecMalformedError):
            FilesystemCategorySpecReader(paths).load(category())

        assert target.read_bytes() == before  # never repaired


class TestStableIdentity:
    """The SPEC identity is bound 1:1 to the catalog row it describes."""

    def test_a_matching_identity_is_accepted(self) -> None:
        spec = parse_category_spec(
            valid_document(),
            category_id="software",
            display_name="Software",
            version="1.0",
        )

        assert spec.category_id == "software"

    @pytest.mark.parametrize(
        "expectation",
        (
            {"category_id": "cnc"},
            {"display_name": "Other"},
            {"version": "2.0"},
        ),
    )
    def test_an_identity_mismatch_is_refused(self, expectation: dict) -> None:
        text = valid_document(**expectation)

        with pytest.raises(CategorySpecIdentityError, match="does not agree"):
            parse_category_spec(
                text,
                category_id="software",
                display_name="Software",
                version="1.0",
            )

    def test_the_id_may_never_be_renamed_while_the_name_may_evolve(self) -> None:
        """A new display name is a new document; the id stays the binding."""
        renamed = valid_document(display_name="Software Engineering")

        spec = parse_category_spec(
            renamed,
            category_id="software",
            display_name="Software Engineering",
            version="1.0",
        )

        assert spec.category_id == "software"
        assert spec.display_name == "Software Engineering"
        with pytest.raises(CategorySpecIdentityError):
            parse_category_spec(renamed, category_id="software-engineering")


@pytest.fixture
def paths(tmp_path) -> LocalAppDataGlobalPaths:
    return LocalAppDataGlobalPaths(base_dir=str(tmp_path / "global"))


def category(**overrides: object) -> Category:
    """One catalog row for the reference category."""
    payload: dict[str, object] = {
        "id": "software",
        "name": "Software",
        "version": "1.0",
        "spec_path": SPEC_ENTRY_NAME,
        "library_path": LIBRARY_ENTRY_NAME,
        "supervisor_rules_path": SUPERVISOR_RULES_ENTRY_NAME,
    }
    payload.update(overrides)
    return Category(**payload)  # type: ignore[arg-type]


def write_category(paths, content: object, folder: Path | None = None) -> Path:
    """Create a category content folder and write SPEC.md verbatim (as bytes).

    Written as bytes on purpose: no platform newline translation may touch a
    document whose line endings/whitespace are part of the test.
    """
    root = folder if folder is not None else Path(paths.category_root("software"))
    root.mkdir(parents=True, exist_ok=True)
    target = root / SPEC_ENTRY_NAME
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_bytes(str(content).encode("utf-8"))
    return target


class _EscapingPaths:
    """A resolver whose category folder lies outside the categories root."""

    def __init__(self, tmp_path: Path) -> None:
        self._root = tmp_path

    def catalog_database_path(self) -> str:
        return str(self._root / "catalog.db")

    def categories_root(self) -> str:
        return str(self._root / "categories")

    def category_root(self, category_id: str) -> str:
        return str(self._root / "elsewhere" / category_id)

    def missing_entries(self, category_id: str, entries) -> tuple:
        return ()

    def ensure_ready(self) -> None:
        return None


class TestFilesystemReader:
    """The one concrete reader: read-only, fresh, contained, typed failures."""

    def test_it_is_a_category_spec_port(self, paths) -> None:
        assert isinstance(FilesystemCategorySpecReader(paths), CategorySpecPort)

    def test_it_reads_the_validated_spec(self, paths) -> None:
        write_category(paths, valid_document())

        spec = FilesystemCategorySpecReader(paths).load(category())

        assert isinstance(spec, CategorySpec)
        assert spec.category_id == "software"
        assert spec.version == "1.0"
        assert spec.content == DESCRIPTION
        assert spec.spec_hash == HASH

    def test_a_bom_and_crlf_document_is_read(self, paths) -> None:
        write_category(
            paths, ("\ufeff" + valid_document()).replace("\n", "\r\n")
        )

        spec = FilesystemCategorySpecReader(paths).load(category())

        assert spec.spec_hash == HASH

    def test_a_missing_document_is_typed(self, paths) -> None:
        with pytest.raises(CategorySpecNotFoundError):
            FilesystemCategorySpecReader(paths).load(category())

        # nothing was created on the way
        assert not (
            Path(paths.category_root("software")) / SPEC_ENTRY_NAME
        ).exists()

    def test_a_directory_masquerading_as_spec_is_typed(self, paths) -> None:
        root = Path(paths.category_root("software"))
        (root / SPEC_ENTRY_NAME).mkdir(parents=True)

        with pytest.raises(CategorySpecUnreadableError):
            FilesystemCategorySpecReader(paths).load(category())

    def test_invalid_utf8_is_typed(self, paths) -> None:
        write_category(paths, b"```json\n\xff\xfe not utf-8\n```\n\n# x\n")

        with pytest.raises(CategorySpecMalformedError, match="not valid UTF-8"):
            FilesystemCategorySpecReader(paths).load(category())

    def test_a_noncanonical_spec_path_is_refused(self, paths) -> None:
        write_category(paths, valid_document())

        with pytest.raises(CategorySpecPathError, match="must reference"):
            FilesystemCategorySpecReader(paths).load(
                category(spec_path="docs/SPEC.md")
            )

    def test_a_path_escaping_the_categories_root_is_refused(self, tmp_path) -> None:
        escaping = _EscapingPaths(tmp_path)
        write_category(escaping, valid_document(), folder=tmp_path / "elsewhere" / "software")

        with pytest.raises(CategorySpecPathError, match="resolves outside"):
            FilesystemCategorySpecReader(escaping).load(category())

    def test_a_tampered_document_is_refused_and_never_repaired(self, paths) -> None:
        target = write_category(
            paths, valid_document().replace("Software projects.", "Tampered.")
        )
        before = target.read_bytes()

        with pytest.raises(CategorySpecHashMismatchError):
            FilesystemCategorySpecReader(paths).load(category())

        assert target.read_bytes() == before

    def test_it_binds_the_row_identity(self, paths) -> None:
        write_category(paths, valid_document(display_name="Other"))

        with pytest.raises(CategorySpecIdentityError):
            FilesystemCategorySpecReader(paths).load(category())

    def test_a_version_mismatch_is_refused(self, paths) -> None:
        write_category(paths, valid_document(version="2.0"))

        with pytest.raises(CategorySpecIdentityError, match="version"):
            FilesystemCategorySpecReader(paths).load(category())
