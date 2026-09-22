"""Tests for the operator panel's dialog standard (v1.1).

Logs, audit, risks, reports, the cost detail, the judge, the evidence conflicts,
the monitor snapshot, the plan preview and every "technical details" view open in
the same kind of window instead of owning a permanent tab. These tests pin the
five things that make that safe:

* the **standard** holds: one title, one optional note, one content area and one
  action bar, with the same button **order** everywhere (`[ Copy ] [ Refresh ]`
  left, `[ Close ]` - or `[ Cancel ] [ Primary ]` - right), one **size category**
  per kind of window, and Escape / X / Close all closing it;
* the **buttons** exist where the workflow needs them, and the sections that have
  nothing to show stay compact (a window with only a note is only as tall as its
  note);
* the **payloads** are plain data - dicts, lists and scalars - built by the
  controller from the last projection, so no widget ever reaches the core;
* no **secret** can be in a popup, or in an error dialog's details, because the
  text is sanitized by the log contract and nothing else is shown;
* the **windows** behave: resizable, non-modal (modal only for a decision),
  scrollable, de-duplicated by a stable window key, and remembered in the same
  layout preference file as the main window - with an off-screen geometry clamped
  back onto the screen.
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest

pytest.importorskip("tkinter")

from tkinter import scrolledtext, ttk  # noqa: E402

from architecture_assistant_gui import layout as store  # noqa: E402
from architecture_assistant_gui import views, windows  # noqa: E402
from architecture_assistant_gui.controller import (  # noqa: E402
    POPUP_ADVISOR_KEYS,
    POPUP_DECISION,
    POPUP_EVIDENCE,
    POPUP_INTENTS,
    POPUP_RISKS,
    POPUP_SNAPSHOT_INTENT,
    POPUP_WINDOW_KEYS,
    GuiController,
)

#: A window big enough for every region to be visible.
WINDOW_GEOMETRY = "1400x900"

#: A secret that must never appear in a popup payload.
KEY = "sk-test-not-a-real-secret"


class BlockedRunner:
    """A runner that records jobs instead of running them (no core here)."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    @property
    def is_busy(self) -> bool:
        return False

    def submit(self, label: str, action: Any) -> bool:
        self.submitted.append(label)
        return True


def _controller() -> GuiController:
    """A controller with no core behind it: the popups only read the payload."""
    return GuiController(BlockedRunner(), actor="operator")


def _popup_rows(spec: Mapping[str, Any], title: str) -> list[tuple[str, ...]]:
    """One popup section's rows, addressed by its own title.

    Every cell is a string, and a row keeps the width of its own table, so a
    two-column fact table can be read with ``dict(...)`` and a wider one by
    position.
    """
    for section in spec["sections"]:
        if section["title"] == title:
            return [tuple(str(cell) for cell in row) for row in section["rows"]]
    raise AssertionError(f"the popup has no {title!r} section")


def _popup_text(spec: Mapping[str, Any], title: str) -> list[str]:
    """One popup text section's lines, addressed by its own title."""
    for section in spec["sections"]:
        if section["title"] == title:
            return [str(line) for line in section["lines"]]
    raise AssertionError(f"the popup has no {title!r} section")


def _plain(value: Any) -> bool:
    """Whether a payload is dicts, lists, strings, numbers, booleans or ``None``."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _plain(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_plain(item) for item in value)
    return False


class TestThePopupVocabulary:
    """Every popup is one display action with one plain-data window."""

    def test_every_popup_has_a_window_and_a_title(self) -> None:
        controller = _controller()

        for key in sorted(POPUP_INTENTS):
            spec = controller.popup_view(key)

            assert spec, key
            assert str(spec["title"]).strip(), key
            assert spec["sections"], key

    def test_an_unknown_key_opens_nothing(self) -> None:
        controller = _controller()

        assert controller.popup_view("nope") == {}
        assert controller.popup_view("") == {}

    def test_the_popup_payload_is_plain_data_only(self) -> None:
        """Dict, list and scalar - a popup can never carry a core object."""
        controller = _controller()

        for key in sorted(POPUP_INTENTS):
            spec = controller.popup_view(key)
            for section in spec["sections"]:
                assert isinstance(section["title"], str), key
                assert _plain(section["columns"]), key
                assert _plain(section["rows"]), key
                assert _plain(section["lines"]), key
                assert isinstance(section["empty"], str), key
                assert all(
                    isinstance(cell, str)
                    for row in section["rows"]
                    for cell in row
                ), key

    def test_every_popup_survives_a_json_round_trip(self) -> None:
        controller = _controller()

        for key in sorted(POPUP_INTENTS):
            spec = controller.popup_view(key)

            assert json.loads(json.dumps(list(spec["sections"]))) == list(
                spec["sections"]
            ), key

    def test_the_popups_never_reach_the_core(self) -> None:
        """Opening a window queues no core work: they are display actions."""
        runner = BlockedRunner()
        controller = GuiController(runner, actor="operator")

        for key in sorted(POPUP_INTENTS):
            assert controller.enabled(controller.intent(key)) is True
            controller.popup_view(key)

        assert runner.submitted == []

    def test_every_window_has_a_stable_key_and_a_size_category(self) -> None:
        """One window, one key, one category - never a title a label can change."""
        controller = _controller()
        keys = []

        for key in sorted(POPUP_INTENTS | {POPUP_SNAPSHOT_INTENT}):
            spec = controller.popup_view(key)
            window_key = str(spec["window"])
            keys.append(window_key)

            assert window_key.startswith("popup."), key
            assert window_key == POPUP_WINDOW_KEYS[key], key
            assert spec["size"] in windows.POPUP_SIZES, key

        # distinct keys: no two windows can ever share a remembered geometry
        assert len(set(keys)) == len(keys)

    def test_the_snapshot_opens_like_every_other_window(self) -> None:
        """The monitor snapshot is a window in the standard, not a special case."""
        controller = _controller()

        spec = controller.popup_view(POPUP_SNAPSHOT_INTENT)

        assert spec["window"] == "popup.snapshot"
        assert spec["title"] == "Monitor Snapshot"
        assert spec["sections"][0]["lines"]

    def test_the_titles_are_concise_labels(self) -> None:
        """No technical class name and no leftover "X - technical ..." title."""
        controller = _controller()

        for key in sorted(POPUP_INTENTS | {POPUP_SNAPSHOT_INTENT}):
            title = str(controller.popup_view(key)["title"])

            assert title == title.strip(), key
            assert 0 < len(title) <= 40, (key, title)
            assert "-" not in title, (key, title)
            assert "Error" not in title, (key, title)
            assert "Exception" not in title, (key, title)


# ---------------------------------------------------------------------------
# the window itself (a real Tk root, no display assumed)
# ---------------------------------------------------------------------------


def _review_view(
    *,
    judge_used: bool,
    conflicts: int,
    evidence: int = 3,
    decision: str = "ACCEPTED",
    cost: str = "Cost: unavailable (no price table)",
) -> dict[str, Any]:
    """A minimal review payload that exercises the compact summary line."""
    return {
        "review": {
            "available": True,
            "status": "Review finished.",
            # the five short values the tab itself shows
            "evidence_line": f"Evidence: {evidence}",
            "evidence_count": evidence,
            "conflicts_line": f"Conflicts: {conflicts}",
            "judge_line": (
                "Judge: used (ALLOW)" if judge_used else "Judge: not used"
            ),
            "judge_used": judge_used,
            "decision_line": f"Decision: {decision}",
            "cost_line": cost,
            "conflicts_summary": f"Conflicts: {conflicts}",
            "conflicts_available": bool(conflicts),
            "judge_summary": "Judge used: ALLOW" if judge_used else "Judge not used",
            "total_cost": "unavailable (no price table)",
            "provider_panels": [
                {
                    "name": name,
                    "status_text": "FINDING",
                    "level": "INFO",
                    "severity": "HIGH",
                    "relation": "supports",
                    "summary": "a short claim",
                    "facts": [["Finding id", "finding-1"], ["Cost", "0.1 USD"]],
                    "body_lines": ["the full claim"],
                }
                for name in ("OpenAI", "Claude", "Grok")
            ],
        }
    }


def _empty_review_view() -> dict[str, Any]:
    """A panel whose review has not been run yet: the tab's empty state."""
    return {
        "review": {
            "available": False,
            "status": "No architecture review has been run in this session.",
            "evidence_line": "Evidence: -",
            "conflicts_line": "Conflicts: 0",
            "judge_line": "Judge: not used",
            "decision_line": "Decision: -",
            "cost_line": "Cost: -",
            "provider_panels": [
                {
                    "name": name,
                    "status_text": "not run yet",
                    "level": "INFO",
                    "severity": "-",
                    "relation": "-",
                    "summary": "",
                    "facts": [],
                    "body_lines": ["No result for this advisor in this session."],
                }
                for name in ("OpenAI", "Claude", "Grok")
            ],
        }
    }


def _settle(root: tk.Misc, passes: int = 3) -> None:
    """Let Tk finish the layout."""
    for _ in range(passes):
        root.update()
        root.update_idletasks()


@pytest.fixture
def window() -> Iterator[tuple[tk.Tk, views.MainWindow, list[str]]]:
    """A real panel window with the actions recorded instead of performed."""
    try:
        root = tk.Tk()
    except tk.TclError as error:  # pragma: no cover - machine without a display
        pytest.skip(f"Tk cannot open a window here: {error}")
    actions: list[str] = []
    root.geometry(WINDOW_GEOMETRY)
    view = views.MainWindow(
        root,
        on_action=actions.append,
        on_actor=lambda _text: None,
        on_question=lambda _text: None,
    )
    view.render(_review_view(judge_used=False, conflicts=0))
    view.select_tab("Architecture Review")
    _settle(root)
    try:
        yield root, view, actions
    finally:
        root.destroy()


class TestTheMainWindowStaysCompact:
    """The primary workflow keeps the tabs, the rest is one button away."""

    def test_only_the_primary_workflow_has_a_tab(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        _root, view, _actions = window

        titles = [
            str(view._notebook.tab(tab, "text")) for tab in view._notebook.tabs()
        ]

        assert titles == [
            "Monitor",
            "Architecture Review",
            "Deliberation",
            "Architecture Proposal",
            "Supervisor",
        ]

    def test_the_utility_bar_has_one_button_per_secondary_window(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        _root, view, actions = window

        assert [key for key, _label in views.UTILITY_BUTTONS]
        for key, label in views.UTILITY_BUTTONS:
            button = view.buttons[key]

            assert str(button.cget("text")) == label
            button.invoke()
            assert actions[-1] == key

        assert {key for key, _label in views.UTILITY_BUTTONS} <= set(
            POPUP_INTENTS | {"clear_logs"}
        )

    def test_the_review_summary_is_one_line_of_values_and_one_row_of_buttons(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        _root, view, actions = window

        assert isinstance(view._review_summary, ttk.LabelFrame)
        assert str(view._review_summary.cget("text")) == "Review summary"
        # the five values of one review, in the order the tab shows them
        assert [
            view._var(variable).get() for variable, _key in views.REVIEW_SUMMARY_ROWS
        ] == [
            "Evidence: 3",
            "Conflicts: 0",
            "Judge: not used",
            "Decision: ACCEPTED",
            "Cost: unavailable (no price table)",
        ]
        # one button per value, each opening its own window through the panel
        assert [key for key, _label in views.REVIEW_SUMMARY_BUTTONS] == [
            "review_evidence_details",
            "review_conflict_details",
            "review_judge_details",
            "review_decision_details",
            "cost_details",
        ]
        for key, label in views.REVIEW_SUMMARY_BUTTONS:
            button = view._review_buttons[key]

            assert str(button.cget("text")) == label
            button.invoke()
            assert actions[-1] == key

    def test_the_summary_renders_no_table_and_no_text_panel(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        """The lower half of the tab is values and buttons - nothing else."""
        _root, view, _actions = window
        panel = view._review_summary

        assert _widgets_of(panel, ttk.Treeview) == []
        assert _widgets_of(panel, tk.Text) == []
        assert _widgets_of(panel, ttk.PanedWindow) == []

    def test_the_summary_values_follow_the_review(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        root, view, _actions = window

        view.render(
            _review_view(
                judge_used=True,
                conflicts=2,
                evidence=5,
                decision="REVISE",
                cost="Cost: 0.2500 USD",
            )
        )
        _settle(root)

        assert view._var("review_evidence_line").get() == "Evidence: 5"
        assert view._var("review_conflicts_line").get() == "Conflicts: 2"
        assert view._var("review_judge_line").get() == "Judge: used (ALLOW)"
        assert view._var("review_decision_line").get() == "Decision: REVISE"
        assert view._var("review_cost_line").get() == "Cost: 0.2500 USD"
        # every button is there: the judge is a line and a window, not a section
        for key, _label in views.REVIEW_SUMMARY_BUTTONS:
            assert view._review_buttons[key].winfo_ismapped() == 1

    def test_a_review_that_has_not_been_run_shows_one_small_empty_state(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        """No review: one honest line, no values, no buttons, no empty table."""
        root, view, _actions = window

        view.render(_empty_review_view())
        _settle(root)

        assert view._review_empty.winfo_ismapped() == 1
        assert view._review_values.winfo_ismapped() == 0
        assert view._review_buttons_frame.winfo_ismapped() == 0
        assert "No architecture review has been run yet" in str(
            view._review_empty.cget("text")
        )

    def test_the_advisor_pane_shows_the_compact_summary(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        _root, view, actions = window
        pane = view._panes[0]

        rows = [
            tuple(pane["facts"].item(item, "values"))
            for item in pane["facts"].get_children()
        ]

        assert [row[0] for row in rows] == [
            "Provider",
            "Model",
            "Severity",
            "Relation",
        ]
        assert rows[2][1] == "HIGH"
        assert rows[3][1] == "supports"
        # The finding id, the cost and the full text are not in the main pane.
        assert "body" not in pane
        assert "finding-1" not in json.dumps(rows)
        assert str(pane["details"].cget("text")) == "Details..."
        pane["details"].invoke()
        assert actions[-1] == "advisor_details_1"

    def test_the_supervisor_tab_keeps_a_button_to_its_technical_window(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        root, view, actions = window

        assert str(view._supervisor_details.cget("text")) == "Technical Details"
        view._supervisor_details.invoke()
        assert actions[-1] == "supervisor_technical"
        view.select_tab("Supervisor")
        _settle(root)

        assert view._supervisor_details.winfo_ismapped() == 1
        assert not hasattr(view, "_supervisor_history")

    def test_the_panes_are_still_resizable(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        """The compact panes keep the splitter the operator drags."""
        root, view, _actions = window

        assert isinstance(view._panes_frame, ttk.PanedWindow)
        assert len(view._panes_frame.panes()) == 3
        assert view._panes_frame.sashpos(0) > 0
        # the summary is a panel under the advisors, never a splitter of its own
        assert isinstance(view._review_summary, ttk.LabelFrame)
        assert not isinstance(view._review_summary, ttk.PanedWindow)
        # every advisor pane is visible side by side
        assert all(
            root.nametowidget(pane).winfo_width() > 1
            for pane in view._panes_frame.panes()
        )

    def test_a_popup_intent_queues_no_core_action(self) -> None:
        controller = _controller()

        with pytest.raises(ValueError):
            controller.submit("cost_details")


class TestTheEmptyStates:
    """A window with nothing to show says so in one line."""

    def test_the_cost_and_risk_windows_state_their_empty_state(self) -> None:
        controller = _controller()

        assert "Cost unavailable" in str(
            controller.popup_view("cost_details")["note"]
        )
        assert "No open risks" in str(controller.popup_view(POPUP_RISKS)["note"])

    def test_the_log_audit_and_conflict_windows_are_empty_and_honest(self) -> None:
        controller = _controller()

        logs = controller.popup_view("open_logs")
        assert _popup_rows(logs, "Runtime events") == []
        assert logs["sections"][0]["empty"] == "No events"

        audit = controller.popup_view("open_audit")
        assert _popup_rows(audit, "Audit entries") == []
        assert "No audit entries" in str(audit["sections"][0]["empty"])

        conflicts = controller.popup_view("review_conflict_details")
        assert _popup_rows(conflicts, "Conflicts") == []
        assert "Conflicts: 0" in str(conflicts["sections"][0]["empty"])

    def test_the_judge_window_explains_that_it_was_not_consulted(self) -> None:
        controller = _controller()

        spec = controller.popup_view("review_judge_details")

        assert "not consulted" in str(spec["note"])

    def test_the_advisor_window_says_which_advisor_it_belongs_to(self) -> None:
        controller = _controller()

        for index, key in enumerate(POPUP_ADVISOR_KEYS):
            spec = controller.popup_view(key)

            assert "Advisor" in str(spec["title"]), key
            assert index < len(spec["sections"])


# ---------------------------------------------------------------------------
# the application opens the windows (real Toplevels, real layout file)
# ---------------------------------------------------------------------------


def _window_alive(window: Any) -> bool:
    """Whether a Toplevel is still alive."""
    try:
        return bool(window.winfo_exists())
    except BaseException:  # pragma: no cover - a destroyed window
        return False


class TestTheApplicationOpensTheWindows:
    """The host - never a widget - turns a display action into a window."""

    @staticmethod
    def _config(tmp_path: Path) -> Any:
        from architecture_assistant_gui import app as gui_app

        return gui_app.build_config(
            gui_app.parse_args(
                [
                    "--database",
                    str(tmp_path / "assistant.db"),
                    "--exchange-dir",
                    str(tmp_path / "cline"),
                    "--report-dir",
                    str(tmp_path / "reports"),
                ]
            )
        )

    def _start_panel(self, tmp_path: Path, monkeypatch: Any, layout: Path) -> Any:
        """A panel with a real window, but no core thread and no mainloop.

        What is under test is the popup lifecycle, never Tk's own startup, so a
        machine where Tk cannot open a window *skips* these tests exactly as
        every other display test in this repository does - a Tk that cannot
        start is an environment, not a defect in the panel.
        """
        from architecture_assistant_gui import app as gui_app
        from architecture_assistant_gui import core

        monkeypatch.setattr(core.BackgroundRunner, "start", lambda self: None)
        monkeypatch.setattr(tk.Tk, "mainloop", lambda self: None)
        panel = gui_app.GuiApp(self._config(tmp_path), layout_path=layout)

        try:
            assert panel.run() == 0
        except tk.TclError as error:  # pragma: no cover - machine without Tk
            pytest.skip(f"Tk cannot open a window here: {error}")
        _settle(panel._root)
        return panel

    @staticmethod
    def _discard(panel: Any) -> None:
        try:
            panel._root.destroy()
        except (AttributeError, tk.TclError):  # pragma: no cover - closed
            pass

    def test_a_display_action_opens_a_non_modal_resizable_window(
        self, tmp_path, monkeypatch
    ) -> None:
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel._on_action("open_logs")
            _settle(panel._root)

            # one window, remembered under its *stable* key - never its title
            assert sorted(panel._popups) == ["popup.logs"]
            window = panel._popups["popup.logs"]
            assert isinstance(window, tk.Toplevel)
            assert str(window.title()) == "Logs"
            # resizable, with a floor, and never modal: the main window stays usable
            assert window.resizable() == (True, True)
            minimum = window.minsize()
            assert minimum[0] > 0 and minimum[1] > 0
            assert window.winfo_ismapped() == 1
            assert window.grab_current() is None
        finally:
            self._discard(panel)

    def test_clicking_the_same_button_twice_raises_the_same_window(
        self, tmp_path, monkeypatch
    ) -> None:
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel._on_action("open_audit")
            first = panel._popups["popup.audit"]
            panel._on_action("open_audit")

            assert panel._popups["popup.audit"] is first
            assert len(panel._popups) == 1
        finally:
            self._discard(panel)

    def test_a_window_with_nothing_to_show_states_that_honestly(
        self, tmp_path, monkeypatch
    ) -> None:
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            # no review was run, so the judge was not consulted - and the window
            # says exactly that instead of pretending to have a result
            panel._on_action("review_judge_details")

            assert sorted(panel._popups) == ["popup.judge_details"]
            spec = panel.controller.popup_view("review_judge_details")
            assert "not consulted" in str(spec["note"])
        finally:
            self._discard(panel)

    def test_an_unknown_window_is_reported_instead_of_opening_empty(
        self, tmp_path, monkeypatch
    ) -> None:
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel._open_popup("not_a_window")

            assert panel._popups == {}
            assert any(
                "not_a_window" in str(entry.get("message"))
                for entry in panel.controller.log_entries()
            )
        finally:
            self._discard(panel)

    def test_clear_logs_empties_the_view_and_deletes_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel.controller.log("one panel entry")
            assert panel.controller.log_count == 1

            panel._on_action("clear_logs")

            assert panel.controller.log_count == 0
            assert "Nothing was deleted" in panel.controller.status
        finally:
            self._discard(panel)

    def test_every_display_action_opens_exactly_one_window(
        self, tmp_path, monkeypatch
    ) -> None:
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            for key in sorted(POPUP_INTENTS):
                panel._on_action(key)
                _settle(panel._root, 1)
            panel._on_action("view_snapshot")
            _settle(panel._root, 1)

            keys = sorted(panel._popups)

            # one window per action, each under a stable key of its own
            assert len(keys) == len(POPUP_INTENTS) + 1
            assert len(set(keys)) == len(keys)
            assert all(key.startswith("popup.") for key in keys)
            assert all(_window_alive(panel._popups[key]) for key in keys)
        finally:
            self._discard(panel)

    def test_the_popup_size_is_remembered_in_the_layout_file(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "data" / "gui_layout.json"
        panel = self._start_panel(tmp_path, monkeypatch, path)
        try:
            panel._on_action("open_risks")
            _settle(panel._root)
            window = panel._popups["popup.risks"]
            window.geometry("700x480+40+60")
            _settle(panel._root)
            # close the window the way the window manager does
            handler = window.protocol("WM_DELETE_WINDOW")
            window.tk.call(handler)
            _settle(panel._root)

            assert _window_alive(window) is False
            # the registry drops the window it just closed, under the same key
            assert panel._popups == {}
            # the panel writes its layout - main window and popups - on the way
            # out, exactly as it does when the operator closes the window
            panel._on_close()
        finally:
            self._discard(panel)

        written = store.load_layout(path)

        assert written["windows"]["popup.risks"] == "700x480+40+60"

    def test_a_remembered_popup_geometry_comes_back(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "data" / "gui_layout.json"
        assert (
            store.save_layout(path, {"windows": {"popup.logs": "760x520+12+24"}})
            is True
        )
        panel = self._start_panel(tmp_path, monkeypatch, path)
        try:
            assert panel._saved_popup_placement("popup.logs") == (760, 520, 12, 24)
            # A window that was never remembered has nothing to place.
            assert panel._saved_popup_placement("popup.reports") is None

            panel._on_action("open_logs")
            _settle(panel._root)

            window = panel._popups["popup.logs"]
            assert (window.winfo_width(), window.winfo_height()) == (760, 520)
            # the remembered position is used as well, not only the size
            assert window.geometry().startswith("760x520")
            assert window.winfo_x() >= 0 and window.winfo_y() >= 0
        finally:
            self._discard(panel)

    def test_a_damaged_or_off_screen_geometry_fails_safely(
        self, tmp_path, monkeypatch
    ) -> None:
        """A hand-mangled or impossible geometry costs a position, never a window."""
        path = tmp_path / "data" / "gui_layout.json"
        assert (
            store.save_layout(
                path,
                {
                    "windows": {
                        "popup.logs": "not-a-geometry",
                        # plausible to the file, off this screen to the screen
                        "popup.audit": "600x400+90000+90000",
                    }
                },
            )
            is True
        )
        panel = self._start_panel(tmp_path, monkeypatch, path)
        try:
            # the file's own validator drops what is not a geometry at all
            assert panel._saved_popup_placement("popup.logs") is None
            assert panel._saved_popup_placement("popup.audit") == (
                600,
                400,
                90000,
                90000,
            )

            panel._on_action("open_logs")
            panel._on_action("open_audit")
            _settle(panel._root)

            logs = panel._popups["popup.logs"]
            assert logs.winfo_width() >= windows.POPUP_MIN_WIDTH
            assert logs.winfo_height() >= windows.POPUP_MIN_HEIGHT
            # the off-screen position was clamped back onto this screen
            audit = panel._popups["popup.audit"]
            screen_width = panel._root.winfo_screenwidth()
            screen_height = panel._root.winfo_screenheight()
            assert logs.winfo_x() < screen_width
            assert logs.winfo_y() < screen_height
            assert -audit.winfo_width() < audit.winfo_x() < screen_width
            assert 0 <= audit.winfo_y() < screen_height
        finally:
            self._discard(panel)

    def test_a_failure_opens_one_sanitized_error_window(
        self, tmp_path, monkeypatch
    ) -> None:
        """One readable line, the technical detail behind Details..., no secret."""
        from architecture_assistant_gui import app as gui_app
        from architecture_assistant_gui.core import ErrorReport, JobResult

        secret = "sk-live-this-must-never-be-shown"
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel._report_error(
                JobResult(
                    label="Run Architecture Review",
                    error=ErrorReport(
                        error_name="OpenAiAdvisorHttpError",
                        message=(
                            "OpenAI request failed with HTTP 401: "
                            f"Authorization: Bearer {secret} token"
                        ),
                        critical=False,
                    ),
                )
            )
            _settle(panel._root)

            assert sorted(panel._popups) == ["popup.error"]
            window = panel._popups["popup.error"]

            assert str(window.title()) == gui_app.ERROR_TITLE
            # an error is information, not a decision: it never blocks the panel
            assert window.grab_current() is None
            shown = window.content_text()
            assert secret not in shown
            assert "Bearer " not in shown
            assert "Authorization:" not in shown
            assert "Traceback" not in shown
            # the raw message is not the headline either
            assert secret not in str(window._spec.get("note") or "")

            # Details... reveals the sanitized technical data in the same window
            panel._on_popup_action("details")
            _settle(panel._root)

            assert sorted(panel._popups) == ["popup.error"]
            revealed = panel._popups["popup.error"].content_text()
            assert "Error type: OpenAiAdvisorHttpError" in revealed
            assert secret not in revealed

            # a second failure replaces the report instead of opening a window
            panel._report_error(
                JobResult(
                    label="Export Markdown",
                    error=ErrorReport(
                        error_name="ReportExportError",
                        message="the report directory is not writable",
                        critical=False,
                    ),
                )
            )
            _settle(panel._root)

            assert sorted(panel._popups) == ["popup.error"]
            assert "not writable" in panel._popups["popup.error"].content_text()
        finally:
            self._discard(panel)

    def test_a_critical_failure_says_so_in_one_readable_title(
        self, tmp_path, monkeypatch
    ) -> None:
        from architecture_assistant_gui import app as gui_app
        from architecture_assistant_gui.core import ErrorReport, JobResult

        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel._report_error(
                JobResult(
                    label="Run Until Idle",
                    error=ErrorReport(
                        error_name="LoopInvariantError",
                        message="the loop reached an impossible state",
                        critical=True,
                    ),
                )
            )
            _settle(panel._root)

            window = panel._popups["popup.error"]
            assert str(window.title()) == gui_app.ERROR_TITLE_CRITICAL
            assert "class " not in window.content_text()
        finally:
            self._discard(panel)

    def test_a_notice_is_small_non_modal_and_unique(
        self, tmp_path, monkeypatch
    ) -> None:
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel._notice("Reason Required", "A reason is required.")
            panel._notice("Reason Required", "A reason is still required.")
            _settle(panel._root)

            assert sorted(panel._popups) == ["popup.notice"]
            window = panel._popups["popup.notice"]
            assert window.grab_current() is None
            assert window.resizable() == (True, True)
            assert "still required" in window.content_text()
            assert windows.POPUP_ACTION_COPY not in window._buttons
            # a one-line notice is one line tall, not a screenful
            assert window.winfo_height() == windows.POPUP_SIZES[
                windows.POPUP_SIZE_SMALL
            ].note_height
        finally:
            self._discard(panel)

    def test_the_plan_preview_is_a_modal_decision_window(
        self, tmp_path, monkeypatch
    ) -> None:
        """Confirm Import / Cancel, and Escape means Cancel - which writes nothing."""
        from architecture_assistant_gui import app as gui_app

        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel.controller.set_plan('{"project": {"name": "p"}}')
            panel._show_plan_preview()
            _settle(panel._root)

            assert sorted(panel._popups) == [gui_app.POPUP_PLAN_WINDOW]
            window = panel._popups[gui_app.POPUP_PLAN_WINDOW]
            assert str(window.title()) == "Plan Preview"
            assert window.resizable() == (True, True)
            assert window.grab_current() is window

            buttons = {
                str(button.cget("text")): button
                for button in _widgets_of(window, ttk.Button)
            }
            assert (
                int(buttons["Cancel"].grid_info()["column"])
                < int(buttons["Confirm Import"].grid_info()["column"])
            )

            # Escape means Cancel: the pending plan is dropped, nothing is sent
            _press_key(window, "<Escape>")
            _settle(panel._root)

            assert _window_alive(window) is False
            assert panel._popups == {}
            assert panel.controller.plan_pending is False
        finally:
            self._discard(panel)

    def test_a_second_click_on_the_snapshot_raises_the_same_window(
        self, tmp_path, monkeypatch
    ) -> None:
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel._on_action("view_snapshot")
            first = panel._popups["popup.snapshot"]
            panel._on_action("view_snapshot")
            _settle(panel._root)

            assert panel._popups["popup.snapshot"] is first
            assert len(panel._popups) == 1
        finally:
            self._discard(panel)

    def test_refresh_re_reads_the_last_payload_in_the_open_window(
        self, tmp_path, monkeypatch
    ) -> None:
        """Refresh redraws the same window from the last payload - no core call."""
        panel = self._start_panel(tmp_path, monkeypatch, tmp_path / "layout.json")
        try:
            panel._on_action("open_logs")
            _settle(panel._root)
            window = panel._popups["popup.logs"]
            assert "from the operator" not in window.content_text()

            panel.controller.log("a line from the operator")
            window._buttons[windows.POPUP_ACTION_REFRESH].invoke()
            _settle(panel._root)

            assert "from the operator" in window.content_text()
            # the very same window, in the very same place
            assert panel._popups["popup.logs"] is window
        finally:
            self._discard(panel)

# ---------------------------------------------------------------------------
# the window standard itself (a bare Tk root, no panel)
# ---------------------------------------------------------------------------


@pytest.fixture
def popup_root() -> Iterator[tk.Tk]:
    """A real Tk root with no panel: the dialog standard on its own."""
    try:
        root = tk.Tk()
    except tk.TclError as error:  # pragma: no cover - machine without a display
        pytest.skip(f"Tk cannot open a window here: {error}")
    root.geometry(WINDOW_GEOMETRY)
    _settle(root)
    try:
        yield root
    finally:
        root.destroy()


def _widgets_of(window: Any, kind: type) -> list[Any]:
    """Every widget of one kind inside a window (a small, honest tree walk)."""
    found: list[Any] = []
    stack = [window]
    while stack:
        for child in stack.pop().winfo_children():
            if isinstance(child, kind):
                found.append(child)
            stack.append(child)
    return found


def _text_spec(lines: int, *, title: str = "Section") -> dict[str, Any]:
    """One spec with a single text section of ``lines`` lines."""
    return {
        "title": title,
        "note": "One line of description.",
        "sections": [
            {
                "title": "Content",
                "columns": [],
                "rows": [],
                "lines": [f"line {index}" for index in range(lines)],
                "empty": "Nothing to show.",
            }
        ],
    }


def _rows_spec(rows: int) -> dict[str, Any]:
    """One spec with a single table section of ``rows`` rows."""
    return {
        "title": "Table",
        "sections": [
            {
                "title": "Rows",
                "columns": [["key", "Key", 120], ["value", "Value", 240]],
                "rows": [[f"k{index}", f"v{index}"] for index in range(rows)],
                "lines": [],
                "empty": "No entries.",
            }
        ],
    }


def _press_key(window: Any, keysym: str) -> None:
    """Send one key to a window the way the operator's keyboard would.

    ``when="now"`` after taking the focus: Tk directs a queued key event to the
    *focused* window, which in a test is not necessarily the one under test.
    """
    window.focus_force()
    window.update()
    window.event_generate(keysym, when="now")
    window.update()


def _decision_spec() -> dict[str, Any]:
    """The two-sided action bar a decision window uses."""
    spec = _text_spec(2)
    spec["actions"] = [
        {"key": "cancel", "label": "Cancel", "role": "cancel", "closes": True},
        {"key": "confirm", "label": "Confirm", "role": "primary", "closes": True},
    ]
    return spec


class TestTheOneDialogStandard:
    """One structure, one button order and the same three ways out."""

    def test_a_window_is_resizable_with_a_floor_and_one_bar(
        self, popup_root: tk.Tk
    ) -> None:
        window = windows.open_popup(
            popup_root, _text_spec(3), size=windows.POPUP_SIZE_SMALL
        )
        _settle(popup_root)
        try:
            assert str(window.title()) == "Section"
            assert window.resizable() == (True, True)
            floor = windows.POPUP_SIZES[windows.POPUP_SIZE_SMALL]
            assert window.minsize() == (floor.min_width, floor.min_height)
            assert window.winfo_ismapped() == 1
            # the bar: Copy on the left, Close on the right, both present
            buttons = {
                str(button.cget("text")): button
                for button in _widgets_of(window, ttk.Button)
            }
            assert sorted(buttons) == ["Close", "Copy"]
            assert (
                int(buttons["Copy"].grid_info()["column"])
                < int(buttons["Close"].grid_info()["column"])
            )
        finally:
            window.close()

    def test_a_decision_window_puts_cancel_before_its_primary_action(
        self, popup_root: tk.Tk
    ) -> None:
        window = windows.open_popup(popup_root, _decision_spec())
        _settle(popup_root)
        try:
            buttons = {
                str(button.cget("text")): button
                for button in _widgets_of(window, ttk.Button)
            }
            # the same order everywhere: [ Cancel ] [ Confirm ]
            assert (
                int(buttons["Cancel"].grid_info()["column"])
                < int(buttons["Confirm"].grid_info()["column"])
            )
            assert "Close" not in buttons
        finally:
            window.close()

    def test_escape_the_close_button_and_the_x_all_close(
        self, popup_root: tk.Tk
    ) -> None:
        closed: list[str] = []
        window = windows.open_popup(
            popup_root,
            _text_spec(2),
            on_close=lambda open_window: closed.append(str(open_window.title())),
        )
        _settle(popup_root)

        _press_key(window, "<Escape>")
        _settle(popup_root)

        assert _window_alive(window) is False
        assert closed == ["Section"]  # the caller heard about it before the destroy

        button_window = windows.open_popup(popup_root, _text_spec(2))
        _settle(popup_root)
        button_window._buttons[windows.POPUP_ACTION_CLOSE].invoke()
        _settle(popup_root)
        assert _window_alive(button_window) is False

        x_window = windows.open_popup(popup_root, _text_spec(2))
        _settle(popup_root)
        x_window.tk.call(x_window.protocol("WM_DELETE_WINDOW"))
        _settle(popup_root)
        assert _window_alive(x_window) is False

    def test_escape_on_a_decision_window_means_cancel(
        self, popup_root: tk.Tk
    ) -> None:
        pressed: list[str] = []
        window = windows.open_popup(
            popup_root, _decision_spec(), on_action=pressed.append
        )
        _settle(popup_root)

        _press_key(window, "<Escape>")
        _settle(popup_root)

        assert pressed == ["cancel"]
        assert _window_alive(window) is False

    def test_enter_presses_the_primary_action_only(
        self, popup_root: tk.Tk
    ) -> None:
        pressed: list[str] = []
        window = windows.open_popup(
            popup_root, _decision_spec(), on_action=pressed.append
        )
        _settle(popup_root)

        _press_key(window, "<Return>")
        _settle(popup_root)

        assert pressed == ["confirm"]
        assert _window_alive(window) is False


class TestThePopupKeepsItsStandard:
    """Sizes, modality, Copy, Refresh and scrolling - the same in every window."""

    def test_the_size_categories_are_ordered_and_modest(self) -> None:
        small = windows.POPUP_SIZES[windows.POPUP_SIZE_SMALL]
        medium = windows.POPUP_SIZES[windows.POPUP_SIZE_MEDIUM]
        large = windows.POPUP_SIZES[windows.POPUP_SIZE_LARGE]

        assert small.width < medium.width < large.width
        assert small.height < medium.height < large.height
        assert small.min_width < medium.min_width < large.min_width
        # a window with nothing but a note is shorter than one with content
        for category in (small, medium, large):
            assert category.min_height <= category.note_height < category.height
        # usable on a 1920x1080 screen without ever being a fullscreen window
        assert large.width < 1920 and large.height < 1080
        assert windows.POPUP_DEFAULT_WIDTH == medium.width
        assert windows.POPUP_DEFAULT_HEIGHT == medium.height

    def test_a_window_with_only_a_note_opens_compact_and_grows_for_content(
        self, popup_root: tk.Tk
    ) -> None:
        """A one-line window is one line tall - until it has something to show."""
        note_only = windows.open_popup(
            popup_root, {"title": "Confirm", "note": "One line.", "sections": []},
            size=windows.POPUP_SIZE_SMALL,
        )
        _settle(popup_root)
        category = windows.POPUP_SIZES[windows.POPUP_SIZE_SMALL]
        try:
            assert note_only.winfo_height() == category.note_height

            # the details arrive: the window grows to the room its content needs
            note_only.render(
                {
                    "title": "Confirm",
                    "note": "One line.",
                    "sections": _text_spec(6)["sections"],
                }
            )
            _settle(popup_root)

            assert note_only.winfo_height() == category.height
            assert note_only.winfo_width() == category.width
        finally:
            note_only.close()

    def test_a_window_opens_at_the_size_its_category_names(
        self, popup_root: tk.Tk
    ) -> None:
        screen_width = popup_root.winfo_screenwidth()
        screen_height = popup_root.winfo_screenheight()
        window = windows.open_popup(
            popup_root, _text_spec(3), size=windows.POPUP_SIZE_SMALL
        )
        _settle(popup_root)
        floor = windows.POPUP_SIZES[windows.POPUP_SIZE_SMALL]
        try:
            assert window.winfo_width() == min(floor.width, screen_width)
            assert window.winfo_height() == min(floor.height, screen_height)
        finally:
            window.close()

    def test_a_view_is_never_modal_and_a_decision_always_is(
        self, popup_root: tk.Tk
    ) -> None:
        view = windows.open_popup(popup_root, _text_spec(3))
        _settle(popup_root)
        assert view.grab_current() is None
        view.close()

        decision = windows.open_popup(popup_root, _decision_spec(), modal=True)
        _settle(popup_root)
        try:
            assert decision.grab_current() is decision
        finally:
            decision.close()

    def test_a_new_window_does_not_steal_the_keyboard_of_a_decision(
        self, popup_root: tk.Tk
    ) -> None:
        decision = windows.open_popup(popup_root, _decision_spec(), modal=True)
        _settle(popup_root)
        try:
            other = windows.open_popup(popup_root, _text_spec(2))
            _settle(popup_root)
            try:
                assert decision.grab_current() is decision
                assert other.focus_get() is not other
            finally:
                other.close()
        finally:
            decision.close()

    def test_copy_puts_the_whole_window_on_the_clipboard(
        self, popup_root: tk.Tk
    ) -> None:
        window = windows.open_popup(popup_root, _rows_spec(2))
        _settle(popup_root)
        try:
            window._buttons[windows.POPUP_ACTION_COPY].invoke()
            _settle(popup_root)

            copied = popup_root.clipboard_get()

            assert "Table" in copied
            assert "k0" in copied and "v1" in copied
        finally:
            window.close()

    def test_refresh_asks_the_caller_for_newer_content(
        self, popup_root: tk.Tk
    ) -> None:
        asked: list[Any] = []
        window = windows.open_popup(
            popup_root, _text_spec(2), on_refresh=asked.append
        )
        _settle(popup_root)
        try:
            assert windows.POPUP_ACTION_REFRESH in window._buttons
            window._buttons[windows.POPUP_ACTION_REFRESH].invoke()

            assert asked == [window]

            # a window with nothing to re-read has no Refresh button at all
            plain = windows.open_popup(popup_root, _text_spec(2))
            try:
                assert windows.POPUP_ACTION_REFRESH not in plain._buttons
            finally:
                plain.close()
        finally:
            window.close()

    def test_a_window_redraws_from_a_newer_spec(self, popup_root: tk.Tk) -> None:
        window = windows.open_popup(popup_root, _text_spec(2))
        _settle(popup_root)
        try:
            window.render(_text_spec(2, title="Renamed"))
            _settle(popup_root)

            assert str(window.title()) == "Renamed"
            assert "line 1" in window.content_text()
        finally:
            window.close()

    def test_long_content_scrolls_instead_of_growing_the_window(
        self, popup_root: tk.Tk
    ) -> None:
        window = windows.open_popup(
            popup_root, _text_spec(200), size=windows.POPUP_SIZE_LARGE
        )
        _settle(popup_root)
        try:
            texts = _widgets_of(window, scrolledtext.ScrolledText)

            assert texts, "the text section is not scrollable"
            assert int(texts[0].cget("height")) <= 18
            assert texts[0].yview() != (0.0, 1.0)
            assert window.winfo_height() <= popup_root.winfo_screenheight()
        finally:
            window.close()

    def test_a_long_table_scrolls_in_both_directions(
        self, popup_root: tk.Tk
    ) -> None:
        window = windows.open_popup(
            popup_root, _rows_spec(200), size=windows.POPUP_SIZE_LARGE
        )
        _settle(popup_root)
        try:
            tables = _widgets_of(window, ttk.Treeview)

            assert len(tables) == 1
            assert len(tables[0].get_children()) == 200
            # two scrollbars, because a cell can be wider than the window
            assert len(_widgets_of(window, ttk.Scrollbar)) == 2
            tables[0].xview_moveto(1.0)
            tables[0].yview_moveto(1.0)
            _settle(popup_root)

            assert tables[0].yview() != (0.0, 1.0)
        finally:
            window.close()

    def test_a_short_section_does_not_reserve_a_blank_screen(
        self, popup_root: tk.Tk
    ) -> None:
        window = windows.open_popup(
            popup_root, _text_spec(1), size=windows.POPUP_SIZE_LARGE
        )
        _settle(popup_root)
        try:
            texts = _widgets_of(window, scrolledtext.ScrolledText)

            # three lines is the floor: a one-line section stays a small widget
            assert int(texts[0].cget("height")) == 3
        finally:
            window.close()

    def test_an_empty_spec_says_so_instead_of_showing_a_blank_pane(
        self, popup_root: tk.Tk
    ) -> None:
        window = windows.open_popup(popup_root, {"title": "Nothing"})
        _settle(popup_root)
        try:
            assert "Nothing to show." in window.content_text()
        finally:
            window.close()

    def test_a_damaged_spec_never_breaks_the_window(self, popup_root: tk.Tk) -> None:
        """A spec is data from the controller - a bad one must not raise."""
        window = windows.open_popup(
            popup_root,
            {
                "title": "Odd",
                "note": None,
                "sections": ["not a section", {"title": None, "rows": ["x"]}],
                "actions": [{"label": ""}, "not an action"],
            },
        )
        _settle(popup_root)
        try:
            assert str(window.title()) == "Odd"
            assert window.winfo_exists()
        finally:
            window.close()


class TestTheDialogsThatAskSomething:
    """A confirmation is modal, explicit and closed by Escape as *Cancel*."""

    @staticmethod
    def _first_window(root: tk.Tk) -> Any:
        """The one Toplevel the dialog under test opened."""
        for child in root.winfo_children():
            if isinstance(child, tk.Toplevel):
                return child
        return None

    def test_the_confirm_side_answers_true(self, popup_root: tk.Tk) -> None:
        answer: list[bool] = []

        def press_confirm() -> None:
            window = self._first_window(popup_root)
            if window is not None:
                _press_key(window, "<Return>")

        popup_root.after(10, press_confirm)
        answer.append(
            windows.confirm_dialog(
                popup_root,
                title="Abort",
                message="Aborting is terminal for this step. Continue?",
                confirm_label="Abort",
            )
        )

        assert answer == [True]

    def test_escape_and_the_x_both_answer_false(self, popup_root: tk.Tk) -> None:
        for how in ("escape", "x"):
            answer: list[bool] = []

            def close_it(mode: str = how) -> None:
                window = self._first_window(popup_root)
                if window is None:
                    return
                if mode == "escape":
                    _press_key(window, "<Escape>")
                else:
                    window.tk.call(window.protocol("WM_DELETE_WINDOW"))

            popup_root.after(10, close_it)
            answer.append(
                windows.confirm_dialog(
                    popup_root,
                    title="Reject",
                    message="Reject this proposal?",
                )
            )

            assert answer == [False], how

    def test_a_confirmation_is_modal_and_carries_no_copy_button(
        self, popup_root: tk.Tk
    ) -> None:
        seen: list[Any] = []

        def inspect() -> None:
            window = self._first_window(popup_root)
            if window is None:
                return
            seen.append(window)
            assert window.grab_current() is window
            window.close()

        popup_root.after(10, inspect)
        windows.confirm_dialog(
            popup_root,
            title="Clear state",
            message="This cannot be undone.",
        )

        assert seen
        assert windows.POPUP_ACTION_COPY not in seen[0]._buttons
        assert sorted(seen[0]._buttons) == ["cancel", "confirm"]


class TestTheConfirmationPolicy:
    """Only a decision with a consequence is confirmed - never a read-only view."""

    def test_every_confirmed_action_names_what_will_happen(self) -> None:
        from architecture_assistant_gui import app as gui_app
        from architecture_assistant_gui.controller import INTENTS

        confirmed = [intent for intent in INTENTS if intent.confirmation]

        assert confirmed
        for intent in confirmed:
            label = gui_app.CONFIRM_LABELS.get(intent.key, "Confirm")

            assert label and label not in {"Yes", "OK"}, intent.key
            # the question states the consequence, it never asks "are you sure?"
            assert len(intent.confirmation) > 20, intent.key
            assert intent.confirmation.strip().endswith("?"), intent.key

        # the destructive and the approving families get their own verb
        assert gui_app.CONFIRM_LABELS["abort"] == "Abort"
        assert gui_app.CONFIRM_LABELS["reject"] == "Reject"
        assert gui_app.CONFIRM_LABELS["approve"] == "Approve"

    def test_a_read_only_action_is_never_confirmed(self) -> None:
        from architecture_assistant_gui.controller import (
            DISPLAY_INTENTS,
            INTENTS,
        )

        for intent in INTENTS:
            if intent.key not in DISPLAY_INTENTS:
                continue
            assert intent.confirmation == "", intent.key
            assert intent.human is False, intent.key
            assert intent.mutating is False, intent.key


    def test_a_long_note_wraps_inside_a_narrow_window(
        self, popup_root: tk.Tk
    ) -> None:
        """A note is wrapped to the window's own width, never cut off."""
        long_note = (
            "Running the review asks the three configured AI advisors and may "
            "incur provider cost. It changes no workflow state and writes "
            "nothing. Continue?"
        )
        window = windows.open_popup(
            popup_root,
            {"title": "Confirm", "note": long_note, "sections": []},
            size=windows.POPUP_SIZE_SMALL,
        )
        _settle(popup_root)
        try:
            labels = [
                widget
                for widget in _widgets_of(window, ttk.Label)
                if str(widget.cget("text")) == long_note
            ]

            assert labels
            wraplength = int(labels[0].cget("wraplength"))
            assert wraplength <= window.winfo_width()
            assert wraplength >= 200
        finally:
            window.close()


class TestThePopupPayloads:
    """What each window says - and, just as important, what it refuses to say."""

    def test_the_cost_popup_moves_the_records_out_of_the_tab(self) -> None:
        controller = _controller()
        controller._review = {

            "cost": {
                "total": {
                    "available": True,
                    "total_usd": 0.25,
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "record_count": 4,
                    "priced_record_count": 3,
                    "unpriced_record_count": 1,
                },
                "providers": [
                    {
                        "source": "OpenAI",
                        "available": True,
                        "total_usd": 0.2,
                        "input_tokens": 80,
                        "output_tokens": 30,
                        "priced_record_count": 2,
                        "unpriced_record_count": 1,
                    }
                ],
                "judge": {
                    "available": True,
                    "total_usd": 0.05,
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "priced_record_count": 1,
                    "unpriced_record_count": 0,
                },
            }
        }

        spec = controller.cost_popup()
        total = dict(_popup_rows(spec, "Total (this review)"))
        per_provider = {
            row[0]: row for row in _popup_rows(spec, "Per advisor and judge")
        }

        assert total["Availability"] == "available"
        assert total["Total cost"] == "0.2500 USD"
        assert total["Total tokens"] == "100 in / 40 out"
        assert total["Cost records"] == "4 (3 priced, 1 unpriced)"
        assert per_provider["OpenAI"][1].startswith("0.2000 USD")
        assert per_provider["OpenAI"][2] == "80 in / 30 out"
        assert per_provider["judge"][1].startswith("0.0500 USD")
        assert per_provider["total"][1].startswith("0.2500 USD")

    def test_unavailable_cost_is_never_printed_as_zero(self) -> None:
        controller = _controller()
        controller._review = {
            "cost": {
                "total": {"available": False, "reason": "no price table"},
                "providers": [],
            }
        }

        spec = controller.cost_popup()

        assert "Cost unavailable: no price table." in str(spec["note"])
        assert _popup_rows(spec, "Total (this review)") == [
            ("Availability", "unavailable (no price table)")
        ]
        assert "0.0000" not in json.dumps(list(spec["sections"]))

    def test_the_risk_popup_carries_every_register_field(self) -> None:
        controller = _controller()
        controller._payload = {
            "open_risks": [
                {
                    "id": "risk-001",
                    "severity": "HIGH",
                    "probability": 0.5,
                    "impact": "MEDIUM",
                    "owner": "operator",
                    "status": "OPEN",
                    "description": "a pilot risk",
                    "mitigation": "pilot first",
                }
            ]
        }

        assert _popup_rows(controller.risks_popup(), "Open risks") == [
            (
                "risk-001",
                "HIGH",
                "0.5",
                "MEDIUM",
                "operator",
                "OPEN",
                "a pilot risk",
                "pilot first",
            )
        ]

    def test_the_audit_popup_pulls_the_recorded_reason_out(self) -> None:
        controller = _controller()
        controller._audit = [
            {
                "created_at": "2026-09-22T10:00:00+00:00",
                "entity_type": "Step",
                "entity_id": "9",
                "action": "approve",
                "event": "HUMAN",
                "actor": "operator",
                "step_no": 9,
                "detail": json.dumps({"reason": "reviewed", "step_no": 9}),
            }
        ]

        rows = _popup_rows(controller.audit_popup(), "Audit entries")

        assert rows[0][0] == "2026-09-22 10:00:00"
        assert rows[0][1] == "Step 9"
        assert rows[0][2] == "approve"
        assert rows[0][4] == "operator"
        assert rows[0][5] == "9"
        assert rows[0][6] == "reviewed"
        assert json.loads(rows[0][7])["step_no"] == 9

    def test_the_audit_popup_survives_a_detail_that_is_not_json(self) -> None:
        controller = _controller()
        controller._audit = [{"detail": "not json at all", "action": "x"}]

        assert _popup_rows(controller.audit_popup(), "Audit entries")[0][6] == "-"


    def test_the_advisor_popup_carries_every_field_the_pane_dropped(self) -> None:
        controller = _controller()
        controller._review = {
            "providers": [
                {
                    "source": "OpenAI",
                    "status": "FINDING",
                    "relation": "supports",
                    "relation_target": "application/core.py:12",
                    "finding": {
                        "id": "finding-1",
                        "claim": "the loop is re-entrant",
                        "severity": "HIGH",
                        "confidence": 0.8,
                        "step_no": 9,
                        "evidence": ["application/core.py:12"],
                    },
                    "cost": {
                        "available": True,
                        "total_usd": 0.1,
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "priced_record_count": 1,
                        "unpriced_record_count": 0,
                    },
                }
            ]
        }

        spec = controller.advisor_popup(0)
        facts = dict(_popup_rows(spec, "Advisor"))

        assert spec["title"].startswith("OpenAI")
        assert facts["Finding id"] == "finding-1"
        assert facts["Confidence"] == "0.8"
        assert facts["Anchor"] == "application/core.py:12"
        assert facts["Step"] == "9"
        assert facts["Evidence refs"] == "1"
        assert "0.1000 USD" in facts["Cost"]
        assert _popup_text(spec, "Finding text") == ["the loop is re-entrant"]
        assert _popup_rows(spec, "Evidence references") == [
            ("Evidence 1", "application/core.py:12")
        ]

    def test_an_advisor_popup_for_a_pane_that_does_not_exist_says_so(self) -> None:
        controller = _controller()

        spec = controller.advisor_popup(len(POPUP_ADVISOR_KEYS) + 5)

        assert "does not exist" in str(spec["note"])
        assert spec["sections"]

    def test_the_project_popup_shows_the_runtime_configuration(self) -> None:
        controller = _controller()
        controller._payload = {
            "project": {
                "name": "Project",
                "plan_version": "1.0",
                "plan_hash": "abc123",
                "mode": "MANUAL",
                "paused": False,
                "current_step_no_snapshot": 9,
                "created_at": "2026-09-22T09:00:00+00:00",
            },
            "paths": {
                "database_path": "data/assistant.db",
                "exchange_dir": "data/cline",
                "report_dir": "reports",
                "source_root": "src",
            },
            "architecture_version": "1.1",
        }

        spec = controller.project_popup()
        project = dict(_popup_rows(spec, "Project"))
        runtime = dict(_popup_rows(spec, "Paths and runtime"))

        assert project["Project"] == "Project"
        assert project["Plan version"] == "1.0"
        assert project["Plan hash"] == "abc123"
        assert project["Paused"] == "no"
        assert project["Mode"] == "MANUAL"
        assert runtime["Database path"] == "data/assistant.db"
        assert runtime["Worker channel"] == "data/cline"
        assert runtime["Source root"] == "src"
        assert runtime["Architecture baseline"] == "1.1"

    def test_the_popups_never_carry_a_secret(self) -> None:
        """No key, no header and no provider body can be in a popup's data."""
        controller = _controller()
        controller.log(
            f"rejected: Authorization: Bearer abcdef123456 with {KEY}",
            level="WARN",
            action="auth",
        )
        controller._provider_note = f"key {KEY} stored"
        controller._exports = [{"kind": "export_markdown", "path": "reports/r.md"}]

        for key in sorted(POPUP_INTENTS):
            blob = json.dumps(controller.popup_view(key))

            assert KEY not in blob, key
            assert "abcdef123456" not in blob, key
            # No header and no bearer token ever reaches a payload. (The words
            # may appear in a note that *says* no header is shown - the value
            # and the header syntax are what must never be there.)
            assert "Authorization:" not in blob, key
            assert "Bearer " not in blob, key

    def test_the_evidence_and_decision_windows_carry_what_left_the_tab(self) -> None:
        """The two results the tab stopped rendering are windows of their own."""
        controller = _controller()
        controller._review = {
            "evidence": {
                "findings": ["f1", "f2"],
                "supporting_ids": ["f1"],
                "conflicting_ids": ["f2"],
                "unresolved_ids": [],
                "conflicts": [{"target": "unknown-layer"}],
                "gate_status": "ACCEPTED",
                "gate_anchors": ["unknown-layer"],
            },
            "decision": {
                "status": "ACCEPTED",
                "rationale": "the gate accepted the tree",
            },
            "judge": {"available": True, "consulted": True, "status": "ALLOW"},
            "cost": {"total": {"available": True, "total_usd": 0.25}},
        }

        evidence = controller.popup_view(POPUP_EVIDENCE)
        decision = controller.popup_view(POPUP_DECISION)

        assert evidence["title"] == "Merged Evidence"
        assert evidence["window"] == "popup.evidence_details"
        assert evidence["size"] == "large"
        assert "2 finding(s)" in evidence["note"]
        assert dict(_popup_rows(evidence, "Merged evidence"))["Findings"] == "2"
        assert dict(_popup_rows(evidence, "Merged evidence"))["Gate status"] == (
            "ACCEPTED"
        )
        assert len(_popup_rows(evidence, "What each advisor contributed")) == 3

        assert decision["title"] == "Review Decision"
        assert decision["window"] == "popup.decision_details"
        assert "only verdict" in decision["note"]
        assert "Status:      ACCEPTED" in "\n".join(
            _popup_text(decision, "Advisory decision")
        )

    def test_the_evidence_and_decision_windows_are_honest_before_a_review(
        self,
    ) -> None:
        controller = _controller()

        evidence = controller.popup_view(POPUP_EVIDENCE)
        decision = controller.popup_view(POPUP_DECISION)

        assert "no architecture review" in evidence["note"]
        assert _popup_rows(evidence, "Merged evidence") == [
            ("Merged evidence", "no architecture review yet")
        ]
        assert _popup_text(decision, "Advisory decision") == [
            "No architecture review has been run in this session."
        ]

    def test_the_review_highlights_and_the_manual_sections_still_work(self) -> None:
        """The compact sections come from the same payload the tab always had."""
        controller = _controller()
        controller._review = {
            "conflicts": [{"target": "a", "supporting_ids": ["f1"]}],
            "judge": {"available": True, "consulted": True, "status": "ALLOW"},
            "cost": {
                "total": {
                    "available": True,
                    "total_usd": 0.5,
                    "input_tokens": 5,
                    "output_tokens": 5,
                    "record_count": 1,
                    "priced_record_count": 1,
                    "unpriced_record_count": 0,
                }
            },
        }

        view = controller.view_model()["review"]

        assert view["conflicts_count"] == 1
        assert view["conflicts_summary"] == "Conflicts: 1"
        assert view["conflicts_available"] is True
        assert view["judge_used"] is True
        assert view["judge_summary"] == "Judge used: ALLOW"
        assert view["cost_available"] is True
        assert view["total_cost"].startswith("0.5000 USD")
        # the five short values the tab itself shows, from the same payload
        assert view["evidence_line"] == "Evidence: -"
        assert view["conflicts_line"] == "Conflicts: 1"
        assert view["judge_line"] == "Judge: used (ALLOW)"
        assert view["decision_line"] == "Decision: -"
        assert view["cost_line"] == "Cost: 0.5000 USD"
        # The rows the tab used to draw are still built, for the popups.
        assert view["conflict_rows"] == [["a", "f1", "-"]]
        assert view["cost_rows"][-1][0] == "total"
        assert view["judge_lines"]
