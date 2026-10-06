"""Tests for the global category catalog (universalization Step 2).

Covers the global-path resolver (fail-closed, %LOCALAPPDATA%-based), the
authoritative SQL registry with its derived states/diagnostics, the audited
mutation surface (one mutation + one audit entry, atomic, no unaudited path), the
immutable slug id and the fact that the catalog is a database of its own - never a
project's.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from architecture_assistant.application.category_catalog import (
    CategoryActorRequiredError,
    CategoryCatalogUseCase,
    CategoryExistsError,
    CategoryInvalidIdError,
    CategoryMetadataError,
    CategoryNameConflictError,
    CategoryNotFoundError,
    CategoryNotReadyError,
    CategoryReasonRequiredError,
)
from architecture_assistant.composition import (
    CategorySelection,
    GlobalPathsError,
    open_category_catalog,
)
from architecture_assistant.domain.category import (
    LIBRARY_ENTRY_NAME,
    SPEC_ENTRY_NAME,
    SUPERVISOR_RULES_ENTRY_NAME,
    CatalogAuditAction,
    Category,
    CategoryState,
)
from architecture_assistant.domain.category_spec import (
    CategorySpec,
    CategorySpecHashMismatchError,
    CategorySpecMalformedError,
    compute_spec_hash,
)
from architecture_assistant.infrastructure import (
    CATEGORIES_FOLDER,
    CATALOG_AUDIT_TABLE_NAME,
    CATEGORY_TABLE_NAME,
    FilesystemCategorySpecReader,
    LocalAppDataGlobalPaths,
    SqliteCatalogAuditRepository,
    SqliteCategoryRepository,
    open_category_database,
)
from architecture_assistant.infrastructure.sqlite import (
    DEFAULT_DATABASE_PATH,
    SqliteTransactionPort,
)

FIXED = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def fixed_clock() -> datetime:
    return FIXED


@pytest.fixture
def paths(tmp_path) -> LocalAppDataGlobalPaths:
    return LocalAppDataGlobalPaths(base_dir=str(tmp_path / "global"))


@pytest.fixture
def catalog(paths):
    wired = open_category_catalog(paths=paths, clock=fixed_clock)
    yield wired
    wired.close()


def spec_text(
    *,
    category_id: str,
    display_name: str,
    version: str,
    description: str,
) -> str:
    """A valid SPEC.md document: metadata + separator + description."""
    metadata = {
        "schema_version": 1,
        "category_id": category_id,
        "display_name": display_name,
        "version": version,
        "spec_hash": compute_spec_hash(
            schema_version=1,
            category_id=category_id,
            display_name=display_name,
            version=version,
            description=description,
        ),
    }
    # deliberately pretty-printed and key-sorted: formatting must not matter
    block = json.dumps(metadata, sort_keys=True, indent=2)
    return f"```json\n{block}\n```\n\n{description}"


def write_spec(
    paths: LocalAppDataGlobalPaths,
    category_id: str,
    *,
    name: str = "Software",
    version: str = "1.0",
    description: str | None = None,
) -> Path:
    """Write a valid SPEC.md for one category content folder."""
    description = (
        description
        if description is not None
        else f"# {name}\n\nA {name} category.\n"
    )
    root = Path(paths.category_root(category_id))
    root.mkdir(parents=True, exist_ok=True)
    target = root / SPEC_ENTRY_NAME
    # written as bytes: no platform newline translation may alter the document
    target.write_bytes(
        spec_text(
            category_id=category_id,
            display_name=name,
            version=version,
            description=description,
        ).encode("utf-8")
    )
    return target


def make_entries(
    paths: LocalAppDataGlobalPaths,
    category_id: str,
    *,
    name: str = "Software",
    version: str = "1.0",
) -> None:
    """Create the three referenced entries of one category content folder."""
    write_spec(paths, category_id, name=name, version=version)
    root = Path(paths.category_root(category_id))
    (root / LIBRARY_ENTRY_NAME).mkdir(parents=True, exist_ok=True)
    (root / SUPERVISOR_RULES_ENTRY_NAME).write_text("# rules\n", encoding="utf-8")


def register_disabled(wired, category_id: str = "software"):
    """Register one category (disabled) through the public, audited API."""
    return wired.catalog.register(
        category_id=category_id,
        name="Software",
        version="1.0",
        actor="operator",
        reason="first registration",
    )


class TestGlobalPaths:
    """One resolver, %LOCALAPPDATA%-based, fail-closed."""

    def test_the_layout_is_under_the_app_folder(self, tmp_path) -> None:
        default = LocalAppDataGlobalPaths(
            environ={"LOCALAPPDATA": str(tmp_path / "Local")}
        )
        db = Path(default.catalog_database_path())
        root = Path(default.categories_root())

        assert db.name == "catalog.db"
        assert db.parent.name == "ArchitectureAssistant"
        assert db.parent.parent == tmp_path / "Local"
        assert root.name == CATEGORIES_FOLDER
        assert root.parent == db.parent
        assert Path(default.category_root("software")).name == "software"

    def test_a_missing_localappdata_fails_closed(self) -> None:
        silent = LocalAppDataGlobalPaths(environ={})

        with pytest.raises(GlobalPathsError, match="LOCALAPPDATA is not set"):
            silent.catalog_database_path()
        with pytest.raises(GlobalPathsError, match="LOCALAPPDATA is not set"):
            silent.ensure_ready()

    def test_an_invalid_id_never_resolves_a_folder(self, paths) -> None:
        for bad in ("../evil", "a/b", "a\\b", "", ".."):
            with pytest.raises(GlobalPathsError):
                paths.category_root(bad)

    def test_missing_entries_reports_exactly_what_is_absent(self, paths) -> None:
        entries = (SPEC_ENTRY_NAME, LIBRARY_ENTRY_NAME, SUPERVISOR_RULES_ENTRY_NAME)

        assert paths.missing_entries("software", entries) == entries
        make_entries(paths, "software")
        assert paths.missing_entries("software", entries) == ()


class TestRegistryAndStates:
    """The SQL registry is authoritative; states are derived and surfaced."""

    def test_an_empty_catalog_lists_nothing(self, catalog) -> None:
        assert catalog.catalog.categories() == ()
        assert catalog.catalog.selectable() == ()

    def test_register_starts_disabled_with_one_audit_entry(self, catalog) -> None:
        view = register_disabled(catalog)

        assert view.state is CategoryState.DISABLED
        assert view.category.enabled is False
        assert view.category.registry_hash.startswith("sha256:")
        assert catalog.catalog.selectable() == ()

        trail = catalog.catalog.audit_trail()
        assert len(trail) == 1
        assert trail[0].action is CatalogAuditAction.REGISTER
        assert trail[0].old_value == {}
        assert trail[0].new_value["registry_hash"] == view.category.registry_hash
        assert trail[0].new_value["spec_path"] == SPEC_ENTRY_NAME

    def test_enable_without_the_referenced_entries_is_refused(self, catalog) -> None:
        register_disabled(catalog)

        with pytest.raises(CategoryNotReadyError, match="missing referenced entries"):
            catalog.catalog.enable("software", actor="operator", reason="ready")

        # refused before any write: still disabled, still exactly one audit entry
        assert catalog.catalog.get("software").state is CategoryState.DISABLED
        assert len(catalog.catalog.audit_trail()) == 1

    def test_enable_after_creating_the_entries_succeeds(self, catalog, paths) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")

        view = catalog.catalog.enable("software", actor="operator", reason="ready")

        assert view.state is CategoryState.ENABLED
        assert [item.id for item in catalog.catalog.selectable()] == ["software"]
        assert [entry.action for entry in catalog.catalog.audit_trail()] == [
            CatalogAuditAction.REGISTER,
            CatalogAuditAction.ENABLE,
        ]

    def test_disable_keeps_the_row_and_audits_it(self, catalog, paths) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")

        view = catalog.catalog.disable("software", actor="operator", reason="pause")

        assert view.state is CategoryState.DISABLED
        assert catalog.catalog.selectable() == ()
        assert catalog.catalog.audit_trail()[-1].action is CatalogAuditAction.DISABLE

    def test_vanished_content_becomes_invalid_not_invisible(
        self, catalog, paths
    ) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")

        (Path(paths.category_root("software")) / SPEC_ENTRY_NAME).unlink()

        view = catalog.catalog.categories()[0]
        assert view.state is CategoryState.INVALID
        assert SPEC_ENTRY_NAME in view.diagnostic
        assert catalog.catalog.selectable() == ()

    def test_unknown_categories_are_reported_honestly(self, catalog) -> None:
        assert catalog.catalog.get("nope") is None
        with pytest.raises(CategoryNotFoundError):
            catalog.catalog.enable("nope", actor="operator", reason="why")
        with pytest.raises(CategoryNotFoundError):
            catalog.catalog.missing_entries("nope")


class _ExplodingAudit:
    """An audit sink that fails after the category write - to prove rollback."""

    action_raised = False

    def __init__(self, inner) -> None:
        self._inner = inner

    def append(self, entry) -> None:
        type(self).action_raised = True
        raise RuntimeError("audit sink exploded")

    def list(self):
        return self._inner.list()

    def list_for_category(self, category_id):
        return self._inner.list_for_category(category_id)


def _manual_use_case(paths, audit_wrapper=None):
    connection = open_category_database(paths.catalog_database_path())
    repository = SqliteCategoryRepository(connection)
    audit = SqliteCatalogAuditRepository(connection)
    wrapped = audit if audit_wrapper is None else audit_wrapper(audit)
    transactions = SqliteTransactionPort(connection)
    use_case = CategoryCatalogUseCase(
        repository,
        wrapped,
        transactions,
        paths,
        clock=fixed_clock,
        specs=FilesystemCategorySpecReader(paths),
    )
    return connection, use_case


class TestAuditAtomicity:
    """No catalog mutation can exist without its matching audit entry."""

    def test_every_mutation_writes_exactly_one_audit_entry(
        self, catalog, paths
    ) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")
        # the documented controlled update: disable, revise the SPEC, update the
        # row through the audited API, re-enable
        catalog.catalog.disable("software", actor="operator", reason="revise")
        write_spec(paths, "software", version="2.0")
        catalog.catalog.update_metadata(
            "software", actor="operator", reason="bump", version="2.0"
        )
        catalog.catalog.enable("software", actor="operator", reason="ready again")

        trail = catalog.catalog.audit_trail("software")
        assert [entry.action for entry in trail] == [
            CatalogAuditAction.REGISTER,
            CatalogAuditAction.ENABLE,
            CatalogAuditAction.DISABLE,
            CatalogAuditAction.UPDATE,
            CatalogAuditAction.ENABLE,
        ]
        assert all(entry.actor == "operator" for entry in trail)
        assert all(entry.reason for entry in trail)
        # the UPDATE moved the registry hash (1.0 -> 2.0) and nothing else did
        assert trail[3].old_value["registry_hash"] != trail[3].new_value["registry_hash"]
        assert trail[3].old_value["version"] == "1.0"
        assert trail[3].new_value["version"] == "2.0"

    def test_no_public_method_mutates_without_the_audit(self, catalog) -> None:
        public = {
            name
            for name, value in inspect.getmembers(catalog.catalog, callable)
            if not name.startswith("_")
        }
        mutators = {
            name
            for name in public
            if any(
                word in name
                for word in ("register", "enable", "disable", "update")
            )
        }

        assert mutators == {"register", "enable", "disable", "update_metadata"}
        assert "load_spec" in public
        assert "load_spec" not in mutators
        assert not {"upsert", "save", "put", "delete"} & public

    def test_a_failing_audit_write_rolls_the_category_back(self, paths) -> None:
        connection, use_case = _manual_use_case(paths, _ExplodingAudit)

        with pytest.raises(RuntimeError, match="audit sink exploded"):
            use_case.register(
                category_id="software",
                name="Software",
                version="1.0",
                actor="operator",
                reason="atomic",
            )

        assert _ExplodingAudit.action_raised is True
        rows = connection.execute(
            f"SELECT COUNT(*) FROM {CATEGORY_TABLE_NAME}"
        ).fetchone()[0]
        audits = connection.execute(
            f"SELECT COUNT(*) FROM {CATALOG_AUDIT_TABLE_NAME}"
        ).fetchone()[0]
        assert rows == 0
        assert audits == 0
        connection.close()

    def test_the_mutation_and_the_audit_row_land_together(self, paths) -> None:
        connection, use_case = _manual_use_case(paths)
        use_case.register(
            category_id="software",
            name="Software",
            version="1.0",
            actor="operator",
            reason="atomic",
        )

        assert (
            connection.execute(
                f"SELECT COUNT(*) FROM {CATEGORY_TABLE_NAME}"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                f"SELECT COUNT(*) FROM {CATALOG_AUDIT_TABLE_NAME}"
            ).fetchone()[0]
            == 1
        )
        connection.close()


class TestImmutabilityAndSlug:
    """The id is an immutable slug; a row is never replaced."""

    def test_invalid_slugs_are_refused(self, catalog) -> None:
        for bad in ("", "Bad", "A B", "x/y", "../x", "x" * 65):
            with pytest.raises(CategoryInvalidIdError):
                catalog.catalog.register(
                    category_id=bad,
                    name="N",
                    version="1",
                    actor="operator",
                    reason="why",
                )

    def test_registering_an_existing_id_is_refused(self, catalog) -> None:
        register_disabled(catalog)

        with pytest.raises(CategoryExistsError, match="immutable"):
            register_disabled(catalog)

    def test_update_never_changes_the_id(self, catalog) -> None:
        register_disabled(catalog)

        view = catalog.catalog.update_metadata(
            "software", actor="operator", reason="rename", name="Software 2"
        )

        assert view.category.id == "software"
        assert view.category.name == "Software 2"

    def test_update_without_a_change_is_refused(self, catalog) -> None:
        register_disabled(catalog)

        with pytest.raises(CategoryMetadataError, match="at least one field"):
            catalog.catalog.update_metadata(
                "software", actor="operator", reason="nothing"
            )

    def test_actor_and_reason_are_mandatory(self, catalog) -> None:
        with pytest.raises(CategoryActorRequiredError):
            catalog.catalog.register(
                category_id="software",
                name="N",
                version="1",
                actor=" ",
                reason="r",
            )
        register_disabled(catalog)
        with pytest.raises(CategoryReasonRequiredError):
            catalog.catalog.enable("software", actor="operator", reason="")


class TestSelectionContract:
    """The traceability contract a new project records at creation."""

    def test_the_selection_carries_id_version_and_hash(self, catalog) -> None:
        view = register_disabled(catalog)

        selection = CategorySelection(
            category_id=view.category.id,
            category_version=view.category.version,
            registry_hash=view.category.registry_hash,
        )

        assert CategorySelection.from_mapping(selection.to_dict()) == selection
        assert selection.to_dict()["registry_hash"] == view.category.registry_hash


class TestNoProjectDatabase:
    """The global catalog is a database of its own - never a project's."""

    def test_the_catalog_holds_only_catalog_tables(self, catalog) -> None:
        register_disabled(catalog)

        names = {
            row[0]
            for row in catalog.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        assert CATEGORY_TABLE_NAME in names
        assert CATALOG_AUDIT_TABLE_NAME in names
        assert "catalog_schema_migrations" in names
        assert not {"project", "steps", "tasks", "audit_entries"} & names

    def test_it_is_not_the_project_database(self, paths, catalog) -> None:
        catalog_path = Path(paths.catalog_database_path())

        assert catalog_path.is_file()
        assert catalog_path.resolve() != Path(DEFAULT_DATABASE_PATH).resolve()


def tamper_spec(paths: LocalAppDataGlobalPaths, category_id: str = "software") -> None:
    """Change the SPEC description without touching its declared hash."""
    target = Path(paths.category_root(category_id)) / SPEC_ENTRY_NAME
    text = target.read_bytes().decode("utf-8")
    target.write_bytes(text.replace("A Software category.", "Tampered.").encode("utf-8"))


def write_lone_surrogate_spec(
    paths: LocalAppDataGlobalPaths,
    category_id: str,
    *,
    name: str = "Broken",
) -> Path:
    """Write one category's SPEC.md with a JSON-escaped lone surrogate version.

    The file itself stays valid ASCII/UTF-8 because ``json.dumps(ensure_ascii=True)``
    writes the ``\\ud800`` escape verbatim; ``json.loads`` turns it back into a real
    lone surrogate that cannot be UTF-8 encoded. Reading such a document must fail
    closed with a typed SPEC diagnostic instead of leaking ``UnicodeEncodeError``.
    """
    block = json.dumps(
        {
            "schema_version": 1,
            "category_id": category_id,
            "display_name": name,
            "version": "\ud800",
            # syntactically valid: the content is refused before the hash is compared
            "spec_hash": "sha256:" + "0" * 64,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    root = Path(paths.category_root(category_id))
    root.mkdir(parents=True, exist_ok=True)
    target = root / SPEC_ENTRY_NAME
    target.write_bytes(f"```json\n{block}\n```\n\n# {name}\n\nBroken.\n".encode("ascii"))
    return target


#: CPython's int/str conversion limit (``sys.get_int_max_str_digits()``); ``0``
#: means the limit is disabled for this interpreter.
INT_DIGIT_LIMIT = sys.get_int_max_str_digits()

#: A digit count beyond CPython's int/str conversion limit (4300 by default).
OVERSIZED_INTEGER_DIGITS = "9" * 5000


def write_oversized_integer_spec(
    paths: LocalAppDataGlobalPaths,
    category_id: str,
    *,
    name: str = "Broken",
) -> Path:
    """Write one category's SPEC.md whose metadata has an oversized integer.

    The document is *syntactically valid JSON* - the ``version`` literal is simply
    longer than CPython's int/str conversion limit - so ``json.loads`` refuses it
    with a plain ``ValueError`` that is **not** a ``JSONDecodeError``. It is built
    by string concatenation because ``json.dumps`` cannot itself stringify such an
    int. Reading such a document must fail closed with a typed SPEC diagnostic
    instead of leaking the raw ``ValueError`` and crashing the listing.
    """
    block = (
        '{"schema_version": 1, "category_id": '
        + json.dumps(category_id)
        + ', "display_name": '
        + json.dumps(name)
        + ', "version": '
        + OVERSIZED_INTEGER_DIGITS
        + ', "spec_hash": "sha256:'
        + "0" * 64
        + '"}'
    )
    root = Path(paths.category_root(category_id))
    root.mkdir(parents=True, exist_ok=True)
    target = root / SPEC_ENTRY_NAME
    target.write_bytes(f"```json\n{block}\n```\n\n# {name}\n\nBroken.\n".encode("ascii"))
    return target


class TestSpecReadiness:
    """A category is ENABLED only while its SPEC.md is valid **and** fresh."""

    def test_a_placeholder_spec_cannot_be_enabled(self, catalog, paths) -> None:
        register_disabled(catalog)
        root = Path(paths.category_root("software"))
        (root / LIBRARY_ENTRY_NAME).mkdir(parents=True)
        (root / SPEC_ENTRY_NAME).write_text("# spec\n", encoding="utf-8")
        (root / SUPERVISOR_RULES_ENTRY_NAME).write_text("# rules\n", encoding="utf-8")

        with pytest.raises(CategoryNotReadyError, match="invalid SPEC.md"):
            catalog.catalog.enable("software", actor="operator", reason="ready")

        assert catalog.catalog.get("software").state is CategoryState.DISABLED
        assert len(catalog.catalog.audit_trail()) == 1

    def test_a_disabled_row_reports_the_spec_problem(self, catalog, paths) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        write_spec(paths, "software", name="Other")

        view = catalog.catalog.get("software")

        assert view.state is CategoryState.DISABLED
        assert "SPEC.md" in view.diagnostic
        assert "does not agree" in view.diagnostic

    def test_a_tampered_spec_turns_an_enabled_row_invalid(
        self, catalog, paths
    ) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")

        tamper_spec(paths)

        view = catalog.catalog.categories()[0]
        assert view.state is CategoryState.INVALID
        assert "invalid SPEC.md" in view.diagnostic
        assert catalog.catalog.selectable() == ()

    def test_enabling_an_already_enabled_row_revalidates_the_spec(
        self, catalog, paths
    ) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")
        tamper_spec(paths)

        with pytest.raises(CategoryNotReadyError, match="invalid SPEC.md"):
            catalog.catalog.enable("software", actor="operator", reason="still ready")

        # refused without a mutation: no extra audit entry, row still enabled
        assert len(catalog.catalog.audit_trail("software")) == 2
        assert catalog.catalog.get("software").category.enabled is True

    def test_an_enabled_metadata_update_is_refused_when_the_spec_is_invalid(
        self, catalog, paths
    ) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")
        tamper_spec(paths)

        with pytest.raises(CategoryNotReadyError, match="invalid SPEC.md"):
            catalog.catalog.update_metadata(
                "software", actor="operator", reason="bump", version="2.0"
            )

        assert catalog.catalog.get("software").category.version == "1.0"
        assert len(catalog.catalog.audit_trail("software")) == 2

    def test_a_noncanonical_spec_path_is_never_ready(self, catalog, paths) -> None:
        catalog.catalog.register(
            category_id="software",
            name="Software",
            version="1.0",
            actor="operator",
            reason="first",
            spec_path="docs/SPEC.md",
        )
        root = Path(paths.category_root("software"))
        (root / LIBRARY_ENTRY_NAME).mkdir(parents=True)
        (root / SUPERVISOR_RULES_ENTRY_NAME).write_text("# rules\n", encoding="utf-8")
        (root / "docs").mkdir()
        (root / "docs" / SPEC_ENTRY_NAME).write_text(
            spec_text(
                category_id="software",
                display_name="Software",
                version="1.0",
                description="# Software\n\nDoc.\n",
            ),
            encoding="utf-8",
        )

        view = catalog.catalog.get("software")
        assert view.state is CategoryState.DISABLED
        assert "must reference" in view.diagnostic
        with pytest.raises(CategoryNotReadyError, match="must reference"):
            catalog.catalog.enable("software", actor="operator", reason="ready")

    def test_a_missing_spec_reader_fails_closed(self, paths) -> None:
        connection = open_category_database(paths.catalog_database_path())
        use_case = CategoryCatalogUseCase(
            SqliteCategoryRepository(connection),
            SqliteCatalogAuditRepository(connection),
            SqliteTransactionPort(connection),
            paths,
            clock=fixed_clock,
        )
        try:
            use_case.register(
                category_id="software",
                name="Software",
                version="1.0",
                actor="operator",
                reason="first",
            )
            make_entries(paths, "software")

            assert "no category SPEC reader" in use_case.get("software").diagnostic
            with pytest.raises(CategoryNotReadyError, match="no category SPEC reader"):
                use_case.enable("software", actor="operator", reason="ready")
            with pytest.raises(CategoryNotReadyError, match="no category SPEC reader"):
                use_case.load_spec("software")
        finally:
            connection.close()


class TestNonUtf8EncodableSpec:
    """An escaped lone surrogate is one more *content* diagnostic, never a crash.

    ``json.loads`` turns the escaped ``\\ud800`` back into a real lone surrogate
    that cannot be UTF-8 encoded. The failure must stay inside the typed SPEC
    errors the catalog handles: the row stays visible as DISABLED/INVALID, every
    other category stays accessible and selectable, and no catalog row, audit entry
    or SPEC.md byte changes.
    """

    def _register_broken(self, catalog, paths) -> None:
        """A valid, enabled ``software`` plus a broken ``broken`` row."""
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.register(
            category_id="broken",
            name="Broken",
            version="1.0",
            actor="operator",
            reason="second registration",
        )
        make_entries(paths, "broken", name="Broken")
        write_lone_surrogate_spec(paths, "broken")

    def test_a_lone_surrogate_spec_reports_a_diagnostic(self, catalog, paths) -> None:
        self._register_broken(catalog, paths)

        view = catalog.catalog.get("broken")

        assert view.state is CategoryState.DISABLED
        assert "invalid SPEC.md" in view.diagnostic
        assert "UTF-8" in view.diagnostic

    def test_it_cannot_be_enabled_and_leaves_no_trace(self, catalog, paths) -> None:
        self._register_broken(catalog, paths)
        target = Path(paths.category_root("broken")) / SPEC_ENTRY_NAME
        before = target.read_bytes()

        with pytest.raises(CategoryNotReadyError, match="invalid SPEC.md"):
            catalog.catalog.enable("broken", actor="operator", reason="ready")

        # refused before any write: still disabled, still exactly one audit entry
        assert catalog.catalog.get("broken").category.enabled is False
        assert len(catalog.catalog.audit_trail("broken")) == 1
        assert target.read_bytes() == before  # never repaired

    def test_listing_survives_and_keeps_other_categories_accessible(
        self, catalog, paths
    ) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")
        catalog.catalog.register(
            category_id="broken",
            name="Broken",
            version="1.0",
            actor="operator",
            reason="second registration",
        )
        make_entries(paths, "broken", name="Broken")
        write_lone_surrogate_spec(paths, "broken")

        views = {view.category.id: view for view in catalog.catalog.categories()}

        assert set(views) == {"software", "broken"}
        assert views["software"].state is CategoryState.ENABLED
        assert views["broken"].state is CategoryState.DISABLED
        assert "invalid SPEC.md" in views["broken"].diagnostic
        assert [row.id for row in catalog.catalog.selectable()] == ["software"]

    def test_reads_never_mutate_the_catalog_or_the_audit(self, catalog, paths) -> None:
        self._register_broken(catalog, paths)
        before_rows = catalog.catalog.categories()
        before_audit = catalog.catalog.audit_trail()

        catalog.catalog.categories()
        catalog.catalog.selectable()
        catalog.catalog.get("broken")
        catalog.catalog.missing_entries("broken")
        with pytest.raises(CategorySpecMalformedError):
            catalog.catalog.load_spec("broken")

        assert catalog.catalog.categories() == before_rows
        assert catalog.catalog.audit_trail() == before_audit
        assert len(catalog.catalog.audit_trail("broken")) == 1

    def test_an_enabled_row_broken_into_a_surrogate_turns_invalid(
        self, catalog, paths
    ) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")

        write_lone_surrogate_spec(paths, "software", name="Software")

        view = catalog.catalog.categories()[0]

        assert view.state is CategoryState.INVALID
        assert "invalid SPEC.md" in view.diagnostic
        assert catalog.catalog.selectable() == ()
        # the brokenness is derived on read: no mutation and no extra audit entry
        assert len(catalog.catalog.audit_trail("software")) == 2


class TestOversizedJsonIntegerSpec:
    """An oversized metadata integer is one more *content* diagnostic, never a crash.

    ``json.loads`` raises a plain ``ValueError`` - **not** a ``JSONDecodeError`` -
    for an integer literal beyond CPython's int/str conversion limit, so a decode
    guard catching only ``JSONDecodeError`` let that raw error escape and crash the
    catalog listing. The failure must stay inside the typed SPEC errors the catalog
    handles: the row stays visible as DISABLED/INVALID, every other category stays
    accessible and selectable, and no catalog row, audit entry or SPEC.md byte
    changes.
    """

    def _register_broken(self, catalog, paths) -> None:
        """A registered ``software`` plus a ``broken`` row with an oversized int."""
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.register(
            category_id="broken",
            name="Broken",
            version="1.0",
            actor="operator",
            reason="second registration",
        )
        make_entries(paths, "broken", name="Broken")
        write_oversized_integer_spec(paths, "broken")

    def test_an_oversized_integer_spec_reports_a_diagnostic(
        self, catalog, paths
    ) -> None:
        self._register_broken(catalog, paths)

        view = catalog.catalog.get("broken")

        assert view.state is CategoryState.DISABLED
        assert "invalid SPEC.md" in view.diagnostic

    def test_it_cannot_be_enabled_and_leaves_no_trace(self, catalog, paths) -> None:
        self._register_broken(catalog, paths)
        target = Path(paths.category_root("broken")) / SPEC_ENTRY_NAME
        before = target.read_bytes()

        with pytest.raises(CategoryNotReadyError, match="invalid SPEC.md"):
            catalog.catalog.enable("broken", actor="operator", reason="ready")

        # refused before any write: still disabled, still exactly one audit entry
        assert catalog.catalog.get("broken").category.enabled is False
        assert len(catalog.catalog.audit_trail("broken")) == 1
        assert target.read_bytes() == before  # never repaired

    def test_listing_survives_and_keeps_other_categories_accessible(
        self, catalog, paths
    ) -> None:
        self._register_broken(catalog, paths)
        catalog.catalog.enable("software", actor="operator", reason="ready")

        views = {view.category.id: view for view in catalog.catalog.categories()}

        assert set(views) == {"software", "broken"}
        assert views["software"].state is CategoryState.ENABLED
        assert views["broken"].state is CategoryState.DISABLED
        assert "invalid SPEC.md" in views["broken"].diagnostic
        assert [row.id for row in catalog.catalog.selectable()] == ["software"]

    def test_reads_never_mutate_the_catalog_or_the_audit(self, catalog, paths) -> None:
        self._register_broken(catalog, paths)
        before_rows = catalog.catalog.categories()
        before_audit = catalog.catalog.audit_trail()

        catalog.catalog.categories()
        catalog.catalog.selectable()
        catalog.catalog.get("broken")
        catalog.catalog.missing_entries("broken")
        with pytest.raises(CategorySpecMalformedError):
            catalog.catalog.load_spec("broken")

        assert catalog.catalog.categories() == before_rows
        assert catalog.catalog.audit_trail() == before_audit
        assert len(catalog.catalog.audit_trail("broken")) == 1

    def test_an_enabled_row_broken_into_an_oversized_integer_turns_invalid(
        self, catalog, paths
    ) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")

        write_oversized_integer_spec(paths, "software", name="Software")

        view = catalog.catalog.categories()[0]

        assert view.state is CategoryState.INVALID
        assert "invalid SPEC.md" in view.diagnostic
        assert catalog.catalog.selectable() == ()
        # the brokenness is derived on read: no mutation and no extra audit entry
        assert len(catalog.catalog.audit_trail("software")) == 2


class TestLoadSpec:
    """The read-only SPEC snapshot later steps consume."""

    def test_it_returns_the_validated_snapshot(self, catalog, paths) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")

        spec = catalog.catalog.load_spec("software")

        assert isinstance(spec, CategorySpec)
        assert spec.category_id == "software"
        assert spec.display_name == "Software"
        assert spec.version == "1.0"
        assert spec.schema_version == 1
        assert spec.content == "# Software\n\nA Software category.\n"
        assert spec.spec_hash.startswith("sha256:")
        # the SPEC content hash stays separate from the Step 2 registry hash
        assert (
            spec.spec_hash
            != catalog.catalog.get("software").category.registry_hash
        )

    def test_an_unknown_category_is_refused(self, catalog) -> None:
        with pytest.raises(CategoryNotFoundError):
            catalog.catalog.load_spec("nope")

    def test_an_invalid_spec_cannot_be_loaded(self, catalog, paths) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        tamper_spec(paths)

        with pytest.raises(CategorySpecHashMismatchError):
            catalog.catalog.load_spec("software")

    def test_reads_never_mutate_the_catalog(self, catalog, paths) -> None:
        register_disabled(catalog)
        make_entries(paths, "software")
        catalog.catalog.enable("software", actor="operator", reason="ready")
        before = catalog.catalog.audit_trail()
        rows = tuple(catalog.catalog.categories())

        catalog.catalog.categories()
        catalog.catalog.selectable()
        catalog.catalog.get("software")
        catalog.catalog.missing_entries("software")
        catalog.catalog.load_spec("software")

        assert catalog.catalog.audit_trail() == before
        assert tuple(catalog.catalog.categories()) == rows


class TestDisplayNameUniqueness:
    """The display name is bound to its id 1:1 and stays unique across ids."""

    def test_a_duplicate_name_is_refused_on_register(self, catalog) -> None:
        register_disabled(catalog)

        with pytest.raises(CategoryNameConflictError, match="already used"):
            catalog.catalog.register(
                category_id="cnc",
                name="Software",
                version="1.0",
                actor="operator",
                reason="duplicate",
            )

        assert [
            view.category.id for view in catalog.catalog.categories()
        ] == ["software"]
        assert len(catalog.catalog.audit_trail()) == 1

    def test_a_duplicate_name_is_refused_on_update(self, catalog) -> None:
        register_disabled(catalog)
        catalog.catalog.register(
            category_id="cnc",
            name="CNC",
            version="1.0",
            actor="operator",
            reason="second",
        )

        with pytest.raises(CategoryNameConflictError, match="already used"):
            catalog.catalog.update_metadata(
                "cnc", actor="operator", reason="rename", name="Software"
            )

        assert catalog.catalog.get("cnc").category.name == "CNC"
        assert len(catalog.catalog.audit_trail()) == 2

    def test_names_are_compared_case_sensitively(self, catalog) -> None:
        register_disabled(catalog)

        view = catalog.catalog.register(
            category_id="cnc",
            name="software",
            version="1.0",
            actor="operator",
            reason="case",
        )

        assert view.category.name == "software"

    def test_existing_duplicates_are_unselectable_until_renamed(self, paths) -> None:
        """A legacy duplicate stays visible, is refused by every enable path and
        is resolved through the audited update surface - never by renaming an id."""
        connection, use_case = _manual_use_case(paths)
        try:
            repository = SqliteCategoryRepository(connection)
            repository.upsert(
                Category(id="software", name="Software", version="1.0")
            )
            repository.upsert(Category(id="cnc", name="Software", version="1.0"))
            make_entries(paths, "software")
            make_entries(paths, "cnc")

            assert use_case.get("software").state is CategoryState.DISABLED
            assert "duplicate display name" in use_case.get("software").diagnostic
            assert use_case.selectable() == ()
            with pytest.raises(CategoryNotReadyError, match="duplicate display name"):
                use_case.enable("software", actor="operator", reason="ready")

            use_case.update_metadata(
                "cnc", actor="operator", reason="resolve", name="CNC"
            )
            write_spec(paths, "cnc", name="CNC")

            assert "duplicate" not in use_case.get("software").diagnostic
            assert (
                use_case.enable("cnc", actor="operator", reason="ready").state
                is CategoryState.ENABLED
            )
            assert (
                use_case.enable("software", actor="operator", reason="ready").state
                is CategoryState.ENABLED
            )
            assert [row.id for row in use_case.selectable()] == ["cnc", "software"]
        finally:
            connection.close()

