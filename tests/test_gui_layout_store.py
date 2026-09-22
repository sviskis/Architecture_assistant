"""The remembered layout file: what it stores, what it refuses, what it survives.

``data/gui_layout.json`` is a *preference*, never state: the window size and
position, one sash list per splitter and the tab that was open. These tests build
no window at all - they pin the file's own contract, which is what the panel's
safety rests on:

* only presentation is stored - no operator name, no path, no project, no step,
  and never the assistant's database;
* a missing, unreadable, truncated, wrongly encoded or hand-mangled file is
  worth the default layout and nothing else;
* a value that survives is one Tk could actually use, and an unusable value is
  dropped instead of being passed on;
* a save is atomic, is never a half-written file, and never raises.

The window side - applying a layout after the widgets are realized, and
snapshotting the layout the operator ends with - is pinned in
``test_gui_layout.py``, which needs a real display.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from architecture_assistant_gui import layout as store

#: The GUI package under test, for the static import-boundary check below.
GUI_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "architecture_assistant_gui"
)

#: A layout a real session could have produced.
GOOD: dict[str, object] = {
    "version": store.LAYOUT_VERSION,
    "geometry": "1400x900+40+20",
    "tab": "Architecture Review",
    "sashes": {
        "main": [392],
        "review": [90, 214],
        "review_advisors": [450, 901],
    },
}


class TestDefaultLocation:
    """Where the layout lives, and what that location guarantees."""

    def test_the_default_path_sits_in_the_runtime_data_directory(self) -> None:
        assert store.LAYOUT_FILENAME == "gui_layout.json"
        assert store.DEFAULT_LAYOUT_PATH == Path("data") / "gui_layout.json"

    def test_the_layout_is_not_the_remembered_operator_name(self) -> None:
        """Two preferences, two files: neither can overwrite the other."""
        from architecture_assistant_gui import app

        assert app.LAYOUT_PATH == store.DEFAULT_LAYOUT_PATH
        assert app.LAYOUT_PATH != app.SETTINGS_PATH
        assert app.SETTINGS_PATH.parent != app.LAYOUT_PATH.parent

    def test_the_layout_path_is_ignored_by_git(self) -> None:
        """The panel writes locally; a stale layout must never be committed."""
        root = Path(__file__).resolve().parents[1]
        lines = [
            line.strip()
            for line in (root / ".gitignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        wanted = str(store.DEFAULT_LAYOUT_PATH).replace("\\", "/")

        assert wanted in lines or f"{store.DEFAULT_LAYOUT_PATH.parent}/" in lines


class TestParseGeometry:
    """What Tk produces is read; what it could not produce is refused."""

    def test_a_geometry_with_a_position_is_split_into_numbers(self) -> None:
        assert store.parse_geometry("1400x900+40+20") == (1400, 900, 40, 20)

    def test_a_geometry_without_a_position_has_none(self) -> None:
        assert store.parse_geometry("1400x900") == (1400, 900, None, None)

    def test_a_negative_position_is_a_position(self) -> None:
        """A window on a monitor left of the primary one has a negative x."""
        assert store.parse_geometry("1920x1080-1920+0") == (
            1920,
            1080,
            -1920,
            0,
        )

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert store.parse_geometry(" 1000x700 ") == (1000, 700, None, None)

    def test_an_unbelievable_size_is_refused(self) -> None:
        assert store.parse_geometry("10x900") is None
        assert store.parse_geometry("900x10") is None
        assert store.parse_geometry("999999x900") is None

    def test_an_unbelievable_position_loses_the_position_not_the_size(
        self,
    ) -> None:
        assert store.parse_geometry("1400x900+999999+999999") == (
            1400,
            900,
            None,
            None,
        )

    @pytest.mark.parametrize(
        "spec",
        [
            None,
            1400,
            14.0,
            True,
            "",
            "   ",
            "1400",
            "1400X900",
            "1400x900+40",
            "1400x900+40+",
            "1400x900++40+20",
            "1400x900+40+20+5",
            "1400x900px",
            "ax900+1+1",
            "-1400x900",
        ],
    )
    def test_a_string_tk_never_produces_is_refused(self, spec: object) -> None:
        assert store.parse_geometry(spec) is None


class TestFormatGeometry:
    """The one shape written, and the only shape read back."""

    def test_a_size_and_position_round_trip(self) -> None:
        spec = store.format_geometry(1400, 900, 40, 20)

        assert spec == "1400x900+40+20"
        assert store.parse_geometry(spec) == (1400, 900, 40, 20)

    def test_a_size_alone_round_trips(self) -> None:
        assert store.format_geometry(1024, 768) == "1024x768"
        assert store.parse_geometry("1024x768") == (1024, 768, None, None)

    def test_a_negative_position_keeps_its_sign(self) -> None:
        assert store.format_geometry(900, 600, -10, 5) == "900x600-10+5"
        assert store.parse_geometry("900x600-10+5") == (900, 600, -10, 5)


class TestNormaliseLayout:
    """Only presentation survives, and only when it is usable as it stands."""

    def test_a_good_layout_survives(self) -> None:
        cleaned = store.normalise_layout(GOOD)

        assert cleaned["version"] == store.LAYOUT_VERSION
        assert cleaned["geometry"] == "1400x900+40+20"
        assert cleaned["tab"] == "Architecture Review"
        assert cleaned["sashes"] == GOOD["sashes"]

    def test_the_result_holds_presentation_keys_and_nothing_else(self) -> None:
        """No operator name, no path, no project, no step can ever get in."""
        raw = {
            "version": store.LAYOUT_VERSION,
            "geometry": "1400x900+40+20",
            "actor": "gints",
            "database": "data/architecture_assistant.db",
            "project": "youtube_to_mp3",
            "step": 12,
            "token": "sk-secret",
            "sashes": {"main": [300]},
        }

        cleaned = store.normalise_layout(raw)

        assert set(cleaned) <= {"version", "geometry", "tab", "sashes"}

    def test_a_missing_version_is_read_as_the_current_one(self) -> None:
        """A file somebody trimmed by hand is still worth its good parts."""
        cleaned = store.normalise_layout({"geometry": "1000x700"})

        assert cleaned == {
            "version": store.LAYOUT_VERSION,
            "geometry": "1000x700",
        }

    @pytest.mark.parametrize(
        "version", [2, 0, -1, "1", 1.0, True, None, [1], {"version": 1}]
    )
    def test_a_version_this_panel_does_not_know_is_refused_as_a_whole(
        self, version: object
    ) -> None:
        assert store.normalise_layout({**GOOD, "version": version}) == {}

    @pytest.mark.parametrize("raw", [None, [], (), 42, 3.5, "layout", True])
    def test_something_that_is_not_an_object_is_refused(
        self, raw: object
    ) -> None:
        assert store.normalise_layout(raw) == {}

    def test_an_object_with_no_known_key_has_nothing_to_offer(self) -> None:
        """It normalises to a version and nothing else - never to a layout."""
        assert store.normalise_layout({0: 1, "fresh": True}) == {
            "version": store.LAYOUT_VERSION
        }

    def test_an_unusable_geometry_is_dropped_with_its_own_field(self) -> None:
        cleaned = store.normalise_layout(
            {
                "geometry": "soon",
                "tab": "Monitor",
                "sashes": {"main": [10]},
            }
        )

        assert "geometry" not in cleaned
        assert cleaned["tab"] == "Monitor"

    def test_a_tab_title_is_stripped_and_bounded(self) -> None:
        assert (
            store.normalise_layout({"tab": "  Monitor  "})["tab"] == "Monitor"
        )
        assert "tab" not in store.normalise_layout({"tab": "   "})
        assert "tab" not in store.normalise_layout({"tab": "Monitor\nRisks"})
        assert "tab" not in store.normalise_layout(
            {"tab": "x" * (store.MAX_TAB_TITLE + 1)}
        )
        assert "tab" not in store.normalise_layout({"tab": 7})

    def test_one_broken_sash_list_costs_only_that_splitter(self) -> None:
        cleaned = store.normalise_layout(
            {
                "sashes": {
                    "main": [392],
                    "review": [0, 100],
                    "proposal": "wide",
                    "supervisor": [1, 2, 3, 4, 5, 6, 7, 8, 9],
                }
            }
        )

        assert cleaned["sashes"] == {"main": [392]}

    @pytest.mark.parametrize(
        "positions",
        [
            [],
            [0],
            [-5],
            [12.0],
            [True],
            ["300"],
            [None],
            [300, "x"],
            [store.MAX_SASH_POSITION + 1],
            "300",
            {1: 2},
        ],
    )
    def test_a_sash_list_is_kept_whole_or_not_at_all(
        self, positions: object
    ) -> None:
        cleaned = store.normalise_layout({"sashes": {"main": positions}})

        assert "sashes" not in cleaned

    def test_a_split_name_that_is_not_usable_is_dropped(self) -> None:
        cleaned = store.normalise_layout(
            {"sashes": {"": [10], "x" * 200: [10], 7: [10], "main": [10]}}
        )

        assert cleaned["sashes"] == {"main": [10]}

    def test_the_result_is_json_serialisable(self) -> None:
        assert json.loads(json.dumps(store.normalise_layout(GOOD))) == (
            store.normalise_layout(GOOD)
        )

    def test_it_never_rewrites_what_it_was_given(self) -> None:
        raw = json.loads(json.dumps(GOOD))
        before = json.loads(json.dumps(GOOD))

        store.normalise_layout(raw)

        assert raw == before


class TestLoadLayout:
    """Every way a layout file can be wrong ends in the default layout."""

    def test_a_missing_file_means_the_default_layout(self, tmp_path) -> None:
        assert store.load_layout(tmp_path / "gui_layout.json") == {}

    def test_a_corrupt_file_means_the_default_layout(self, tmp_path) -> None:
        path = tmp_path / "gui_layout.json"
        path.write_text("{ not json at all", encoding="utf-8")

        assert store.load_layout(path) == {}

    def test_a_truncated_file_means_the_default_layout(self, tmp_path) -> None:
        path = tmp_path / "gui_layout.json"
        path.write_text(json.dumps(GOOD)[:40], encoding="utf-8")

        assert store.load_layout(path) == {}

    @pytest.mark.parametrize("text", ["[]", "null", "42", '"layout"', "true"])
    def test_a_file_that_is_not_a_json_object_means_the_default(
        self, tmp_path, text: str
    ) -> None:
        path = tmp_path / "gui_layout.json"
        path.write_text(text, encoding="utf-8")

        assert store.load_layout(path) == {}

    def test_a_file_that_is_not_utf8_means_the_default_layout(
        self, tmp_path
    ) -> None:
        path = tmp_path / "gui_layout.json"
        path.write_bytes(b"\xff\xfe\x00layout")

        assert store.load_layout(path) == {}

    def test_a_directory_in_place_of_a_file_means_the_default_layout(
        self, tmp_path
    ) -> None:
        path = tmp_path / "gui_layout.json"
        path.mkdir()

        assert store.load_layout(path) == {}

    def test_a_path_that_cannot_be_a_path_means_the_default_layout(
        self,
    ) -> None:
        assert store.load_layout(None) == {}
        assert store.load_layout(object()) == {}

    def test_a_partly_damaged_file_keeps_its_good_parts(self, tmp_path) -> None:
        path = tmp_path / "gui_layout.json"
        path.write_text(
            json.dumps(
                {
                    "version": store.LAYOUT_VERSION,
                    "geometry": "not a geometry",
                    "tab": "Monitor",
                    "sashes": {"main": [392], "review": "wide"},
                }
            ),
            encoding="utf-8",
        )

        assert store.load_layout(path) == {
            "version": store.LAYOUT_VERSION,
            "tab": "Monitor",
            "sashes": {"main": [392]},
        }

    def test_what_was_saved_is_what_is_loaded(self, tmp_path) -> None:
        path = tmp_path / "nested" / "gui_layout.json"

        assert store.save_layout(path, GOOD) is True
        assert store.load_layout(path) == store.normalise_layout(GOOD)


class TestSaveLayout:
    """A save is atomic, honest about failure, and never raises."""

    def test_the_file_holds_a_version_and_presentation_only(
        self, tmp_path
    ) -> None:
        path = tmp_path / "gui_layout.json"

        assert store.save_layout(path, GOOD) is True

        written = json.loads(path.read_text(encoding="utf-8"))

        assert set(written) == {"version", "geometry", "tab", "sashes"}
        assert written["version"] == store.LAYOUT_VERSION

    def test_the_parent_directory_is_created(self, tmp_path) -> None:
        path = tmp_path / "data" / "gui_layout.json"

        assert store.save_layout(path, GOOD) is True
        assert path.is_file()

    def test_the_save_leaves_no_temporary_file_behind(self, tmp_path) -> None:
        path = tmp_path / "gui_layout.json"

        store.save_layout(path, GOOD)

        assert [item.name for item in tmp_path.iterdir()] == ["gui_layout.json"]

    def test_a_second_save_replaces_the_first(self, tmp_path) -> None:
        path = tmp_path / "gui_layout.json"

        store.save_layout(path, GOOD)
        store.save_layout(path, {**GOOD, "tab": "Risks"})

        assert store.load_layout(path)["tab"] == "Risks"
        assert [item.name for item in tmp_path.iterdir()] == ["gui_layout.json"]

    def test_a_location_that_cannot_be_written_is_a_false_not_an_error(
        self, tmp_path
    ) -> None:
        """``data/`` is a *file* here, so that directory cannot be created."""
        blocker = tmp_path / "data"
        blocker.write_text("not a directory", encoding="utf-8")

        assert store.save_layout(blocker / "gui_layout.json", GOOD) is False
        assert blocker.is_file()
        assert blocker.read_text(encoding="utf-8") == "not a directory"

    def test_a_layout_with_nothing_usable_is_not_written(self, tmp_path) -> None:
        path = tmp_path / "gui_layout.json"

        assert store.save_layout(path, {"actor": "gints"}) is False
        assert store.save_layout(path, "layout") is False
        assert not path.exists()

    def test_a_string_path_is_accepted(self, tmp_path) -> None:
        path = tmp_path / "gui_layout.json"

        assert store.save_layout(str(path), GOOD) is True
        assert path.is_file()

    def test_a_layout_that_is_only_a_geometry_is_still_written(
        self, tmp_path
    ) -> None:
        """A window always reports its geometry, so a save is never empty."""
        path = tmp_path / "gui_layout.json"

        assert store.save_layout(path, {"geometry": "1200x800+10+10"}) is True
        assert store.load_layout(path)["geometry"] == "1200x800+10+10"


class TestLayoutModuleBoundaries:
    """The store is standard-library only: no Tk, no core, no database."""

    def test_it_imports_nothing_but_the_standard_library(self) -> None:
        source = (GUI_ROOT / "layout.py").read_text(encoding="utf-8")
        roots: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, node.module
                roots.add((node.module or "").split(".")[0])

        assert roots == {
            "__future__",
            "collections",
            "json",
            "os",
            "pathlib",
            "re",
            "typing",
        }

    def test_it_touches_no_database_and_no_assistant_module(self) -> None:
        source = (GUI_ROOT / "layout.py").read_text(encoding="utf-8")

        assert "sqlite3" not in source
        assert "architecture_assistant." not in source
