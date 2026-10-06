"""The global catalog must work with no project database and no composition.

Category selection is the first step of the startup flow and happens *before* any
project folder or project database exists, so the catalog must open, list and
select categories without :func:`architecture_assistant.composition.compose` and
without touching the project runtime ``data/`` directory.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from architecture_assistant.domain.category_spec import compute_spec_hash

from architecture_assistant.composition import (
    open_category_catalog,
    compose,
)
from architecture_assistant.infrastructure import LocalAppDataGlobalPaths


def _base(tmp_path) -> Path:
    return tmp_path / "global"


def _catalog(tmp_path):
    return open_category_catalog(
        paths=LocalAppDataGlobalPaths(base_dir=str(_base(tmp_path)))
    )


def _spec_document(
    *,
    category_id: str,
    display_name: str,
    version: str,
    description: str = "# Software\n\nA Software category.\n",
) -> str:
    """A valid SPEC.md document for the notebook-level fixture."""
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
    block = json.dumps(metadata, sort_keys=True, indent=2)
    return f"```json\n{block}\n```\n\n{description}"


def test_list_and_select_need_no_project_database(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    wired = _catalog(tmp_path)
    try:
        assert wired.catalog.categories() == ()

        root = Path(wired.paths.category_root("software"))
        (root / "LIBRARY").mkdir(parents=True)
        (root / "SPEC.md").write_bytes(
            _spec_document(
                category_id="software", display_name="Software", version="1.0"
            ).encode("utf-8")
        )
        (root / "SUPERVISOR_RULES.md").write_text("# rules\n", encoding="utf-8")

        wired.catalog.register(
            category_id="software",
            name="Software",
            version="1.0",
            actor="operator",
            reason="seed",
        )
        wired.catalog.enable("software", actor="operator", reason="ready")

        assert [item.id for item in wired.catalog.selectable()] == ["software"]
        # the validated SPEC snapshot is available without any project database
        spec = wired.catalog.load_spec("software")
        assert spec.category_id == "software"
        assert spec.spec_hash.startswith("sha256:")
    finally:
        wired.close()

    # nothing under the project runtime data dir was created by the catalog
    assert not (tmp_path / "data").exists()


def test_the_catalog_wiring_never_composes_a_project() -> None:
    """`open_category_catalog` is wiring only: it never calls `compose`."""
    from architecture_assistant.composition import category_catalog as module

    source = inspect.getsource(module)

    assert "compose(" not in source
    assert callable(compose)  # the project root stays available, just unused


def test_the_project_default_database_is_never_opened(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    wired = _catalog(tmp_path)
    try:
        wired.catalog.register(
            category_id="cnc",
            name="CNC",
            version="0.1",
            actor="operator",
            reason="seed",
        )
    finally:
        wired.close()

    assert (Path(str(_base(tmp_path))) / "catalog.db").is_file()
    assert not Path("data/architecture_assistant.db").exists()
