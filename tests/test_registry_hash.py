"""Registry/metadata hash semantics of the global catalog (Step 2).

The Step 2 hash is a **registry** hash: it covers the stable canonical metadata of
a category (id, name, version and the three *relative* paths). It is deliberately
not a SPEC/content hash - that is Step 3, and the two hashes stay separate: even
a *valid* SPEC.md whose content (and therefore its own ``spec_hash``) changes
leaves the registry hash untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

from architecture_assistant.composition import open_category_catalog
from architecture_assistant.domain.category import (
    LIBRARY_ENTRY_NAME,
    SPEC_ENTRY_NAME,
    SUPERVISOR_RULES_ENTRY_NAME,
    CategoryState,
    compute_registry_hash,
    is_relative_category_path,
)
from architecture_assistant.domain.category_spec import compute_spec_hash
from architecture_assistant.infrastructure import LocalAppDataGlobalPaths

METADATA = dict(
    category_id="software",
    name="Software",
    version="1.0",
    spec_path=SPEC_ENTRY_NAME,
    library_path=LIBRARY_ENTRY_NAME,
    supervisor_rules_path=SUPERVISOR_RULES_ENTRY_NAME,
)


def write_spec(root: Path, description: str) -> Path:
    """Write a valid SPEC.md (name/version of the Step 2 fixture) into ``root``."""
    metadata = {
        "schema_version": 1,
        "category_id": "software",
        "display_name": "Software",
        "version": "1.0",
        "spec_hash": compute_spec_hash(
            schema_version=1,
            category_id="software",
            display_name="Software",
            version="1.0",
            description=description,
        ),
    }
    root.mkdir(parents=True, exist_ok=True)
    target = root / SPEC_ENTRY_NAME
    target.write_bytes(
        f"```json\n{json.dumps(metadata, indent=2)}\n```\n\n{description}".encode("utf-8")
    )
    return target


def test_the_hash_is_deterministic() -> None:
    assert compute_registry_hash(**METADATA) == compute_registry_hash(**METADATA)
    assert compute_registry_hash(**METADATA).startswith("sha256:")


def test_every_metadata_field_changes_the_hash() -> None:
    base = compute_registry_hash(**METADATA)

    assert compute_registry_hash(**{**METADATA, "name": "Other"}) != base
    assert compute_registry_hash(**{**METADATA, "version": "2.0"}) != base
    assert compute_registry_hash(**{**METADATA, "spec_path": "SPEC.md"}) == base
    assert (
        compute_registry_hash(**{**METADATA, "spec_path": "docs/SPEC.md"}) != base
    )


def test_absolute_machine_paths_are_never_accepted() -> None:
    for bad in (
        r"C:\Users\x\AppData\Local\ArchitectureAssistant\categories\software\SPEC.md",
        "/home/x/SPEC.md",
        r"..\software\SPEC.md",
        r"software\SPEC.md",
    ):
        assert is_relative_category_path(bad) is False


def test_the_hash_is_the_same_on_every_machine(tmp_path) -> None:
    """Two different %LOCALAPPDATA%-style roots register the same hash."""
    first_root = tmp_path / "machine-a" / "ArchitectureAssistant"
    second_root = tmp_path / "machine-b" / "ArchitectureAssistant"

    def register(root: Path) -> str:
        paths = LocalAppDataGlobalPaths(base_dir=str(root))
        wired = open_category_catalog(paths=paths)
        try:
            view = wired.catalog.register(
                category_id="software",
                name="Software",
                version="1.0",
                actor="operator",
                reason="portable",
            )
        finally:
            wired.close()
        return view.category.registry_hash

    assert register(first_root) == register(second_root)
    assert str(first_root) != str(second_root)


def test_it_is_not_a_content_hash(tmp_path) -> None:
    """A changed but valid SPEC.md moves the SPEC hash, never the registry hash."""
    paths = LocalAppDataGlobalPaths(base_dir=str(tmp_path / "global"))
    root = Path(paths.category_root("software"))
    (root / LIBRARY_ENTRY_NAME).mkdir(parents=True)
    (root / SUPERVISOR_RULES_ENTRY_NAME).write_text("# rules\n", encoding="utf-8")
    write_spec(root, "# Software\n\nVersion one.\n")

    wired = open_category_catalog(paths=paths)
    try:
        registered = wired.catalog.register(
            category_id="software",
            name="Software",
            version="1.0",
            actor="operator",
            reason="first",
        )
        wired.catalog.enable("software", actor="operator", reason="ready")
        first_spec = wired.catalog.load_spec("software")

        write_spec(root, "# Software\n\nVersion two - different content.\n")

        after = wired.catalog.get("software")
        second_spec = wired.catalog.load_spec("software")
    finally:
        wired.close()

    assert after.state is CategoryState.ENABLED
    assert after.category.registry_hash == registered.category.registry_hash
    assert second_spec.spec_hash != first_spec.spec_hash
