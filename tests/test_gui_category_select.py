"""Slice tests for the GUI category surface and the mandatory/legacy contract.

The Tk dialogs are not exercised here; what is tested is the pure presentation
logic (``category_rows``/``selectable_rows``/``category_label``/``selection_for``/
``needs_category``) and the ``workspace_spec`` rule that a **new** project must
carry a category while an existing (legacy) project opens without one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")
pytest.importorskip("customtkinter")

from architecture_assistant_gui import category_select, project_setup  # noqa: E402


class _View:
    def __init__(self, category_id, state, *, diagnostic="", selectable=False, version="1.0"):
        self.category = SimpleNamespace(
            id=category_id,
            name=category_id.title(),
            version=version,
            registry_hash=f"sha256:{category_id}",
        )
        self.state = SimpleNamespace(value=state)
        self.diagnostic = diagnostic
        self.selectable = selectable


class _Catalog:
    def __init__(self, views):
        self._views = tuple(views)

    def categories(self):
        return self._views


def test_category_rows_describe_every_state() -> None:
    rows = category_select.category_rows(
        _Catalog(
            [
                _View("software", "ENABLED", selectable=True),
                _View("cnc", "DISABLED", diagnostic="missing referenced entries: LIBRARY"),
                _View("garden", "INVALID", diagnostic="missing referenced entries: SPEC.md"),
            ]
        )
    )

    assert [row["id"] for row in rows] == ["software", "cnc", "garden"]
    assert [row["selectable"] for row in rows] == [True, False, False]
    assert category_select.selectable_rows(rows) == [rows[0]]


def test_needs_category_only_when_nothing_is_selectable() -> None:
    assert category_select.needs_category([]) is True
    assert (
        category_select.needs_category([{"selectable": False}]) is True
    )
    assert category_select.needs_category([{"selectable": True}]) is False


def test_category_label_explains_why_a_row_is_not_offered() -> None:
    enabled = category_select.category_label(
        {"id": "software", "name": "Software", "version": "1.0", "state": "ENABLED"}
    )
    invalid = category_select.category_label(
        {
            "id": "garden",
            "name": "Garden",
            "version": "2.0",
            "state": "INVALID",
            "diagnostic": "missing referenced entries: SPEC.md",
        }
    )

    assert "Software" in enabled and "software" in enabled
    assert "ENABLED" not in enabled
    assert "INVALID" in invalid
    assert "SPEC.md" in invalid


def test_selection_for_is_the_traceability_contract() -> None:
    contract = category_select.selection_for(
        {"id": "software", "version": "1.0", "registry_hash": "sha256:abc"}
    )

    assert contract == {
        "category_id": "software",
        "category_version": "1.0",
        "registry_hash": "sha256:abc",
    }


def test_the_first_run_message_is_defined() -> None:
    message = category_select.FIRST_RUN_MESSAGE

    assert "category" in message.lower()
    assert "SPEC.md" in message
    assert "valid" in message


def test_a_spec_invalid_category_is_shown_but_never_offered() -> None:
    """The mandatory SPEC.md rule reaches the panel through the view alone."""
    rows = category_select.category_rows(
        _Catalog(
            [
                _View(
                    "software",
                    "INVALID",
                    diagnostic=(
                        "invalid SPEC.md: the declared spec_hash does not match the "
                        "recomputed SPEC content hash"
                    ),
                )
            ]
        )
    )

    assert rows[0]["selectable"] is False
    assert category_select.needs_category(rows) is True
    assert "invalid SPEC.md" in category_select.category_label(rows[0])
    assert category_select.selectable_rows(rows) == []


def _folders(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    return project, source


def test_a_fresh_folder_is_not_an_existing_project(tmp_path) -> None:
    project, _source = _folders(tmp_path)

    assert project_setup.is_existing_project(str(project)) is False


def test_a_saved_workspace_profile_marks_an_existing_project(tmp_path) -> None:
    project, _source = _folders(tmp_path)
    state = project / project_setup.STATE_FOLDER
    state.mkdir()
    (state / project_setup.WORKSPACE_PROFILE_FILENAME).write_text("{}", encoding="utf-8")

    assert project_setup.is_existing_project(str(project)) is True


def test_the_configured_database_path_marks_an_existing_project(tmp_path) -> None:
    project, _source = _folders(tmp_path)
    state = project / project_setup.STATE_FOLDER
    state.mkdir()
    (state / project_setup.PROJECT_DB_FILENAME).write_bytes(b"")

    assert project_setup.is_existing_project(str(project)) is True


def test_a_new_project_without_a_category_is_refused(tmp_path) -> None:
    project, source = _folders(tmp_path)

    with pytest.raises(ValueError, match="Choose a category"):
        project_setup.workspace_spec(str(project), "New", str(source))


def test_a_new_project_records_the_category_contract(tmp_path) -> None:
    project, source = _folders(tmp_path)
    contract = {
        "category_id": "software",
        "category_version": "1.0",
        "registry_hash": "sha256:abc",
    }

    spec = project_setup.workspace_spec(
        str(project), "New", str(source), category=contract
    )

    assert spec["category"] == contract
    assert spec["database_path"].endswith(project_setup.PROJECT_DB_FILENAME)


def test_an_existing_project_opens_without_a_category(tmp_path) -> None:
    project, source = _folders(tmp_path)
    state = project / project_setup.STATE_FOLDER
    state.mkdir()
    (state / project_setup.WORKSPACE_PROFILE_FILENAME).write_text("{}", encoding="utf-8")

    spec = project_setup.workspace_spec(str(project), "Legacy", str(source))

    assert spec["category"] is None
