"""Tests for the operator panel's popup windows (v1.1).

Logs, audit, risks, reports, the cost detail, the judge, the evidence conflicts
and every "technical details" view open in their own resizable, non-modal window
instead of owning a permanent tab. These tests pin the four things that make that
safe:

* the **buttons** exist where the workflow needs them, and the sections that have
  nothing to show stay compact;
* the **payloads** are plain data - dicts, lists and scalars - built by the
  controller from the last projection, so no widget ever reaches the core;
* no **secret** can be in a popup, because none is in the data it renders;
* the **windows** behave: resizable, non-modal, scrollable, and remembered in the
  same layout preference file as the main window.
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest

pytest.importorskip("tkinter")

from tkinter import ttk  # noqa: E402

from architecture_assistant_gui import layout as store  # noqa: E402
from architecture_assistant_gui import views  # noqa: E402
from architecture_assistant_gui.controller import (  # noqa: E402
    POPUP_ADVISOR_KEYS,
    POPUP_INTENTS,
    POPUP_RISKS,
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


# ---------------------------------------------------------------------------
# the window itself (a real Tk root, no display assumed)
# ---------------------------------------------------------------------------


def _review_view(*, judge_used: bool, conflicts: int) -> dict[str, Any]:
    """A minimal review payload that exercises the compact sections."""
    return {
        "review": {
            "conflicts_summary": f"Conflicts: {conflicts}",
            "conflicts_available": bool(conflicts),
            "judge_summary": "Judge used: ALLOW" if judge_used else "Judge not used",
            "judge_used": judge_used,
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

    def test_the_cost_area_is_one_line_and_one_button(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        _root, view, actions = window

        assert isinstance(view._cost, ttk.Frame)
        assert view._cost.winfo_exists()
        assert str(view._cost_button.cget("text")) == "Cost Details"
        view._cost_button.invoke()
        assert actions[-1] == "cost_details"

    def test_the_judge_button_is_hidden_until_the_judge_was_used(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        root, view, actions = window

        assert view._var("review_judge_summary").get() == "Judge not used"
        assert view._judge_button.winfo_ismapped() == 0

        view.render(_review_view(judge_used=True, conflicts=0))
        _settle(root)

        assert view._var("review_judge_summary").get() == "Judge used: ALLOW"
        assert view._judge_button.winfo_ismapped() == 1
        view._judge_button.invoke()
        assert actions[-1] == "review_judge_details"

    def test_the_conflict_button_appears_only_when_there_are_conflicts(
        self, window: tuple[tk.Tk, views.MainWindow, list[str]]
    ) -> None:
        root, view, actions = window

        assert view._var("review_conflicts_summary").get() == "Conflicts: 0"
        assert view._conflicts_button.winfo_ismapped() == 0

        view.render(_review_view(judge_used=False, conflicts=2))
        _settle(root)

        assert view._var("review_conflicts_summary").get() == "Conflicts: 2"
        assert view._conflicts_button.winfo_ismapped() == 1
        view._conflicts_button.invoke()
        assert actions[-1] == "review_conflict_details"

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
        """The compact panes keep the splitters the operator drags."""
        root, view, _actions = window

        assert isinstance(view._panes_frame, ttk.PanedWindow)
        assert len(view._panes_frame.panes()) == 3
        assert view._panes_frame.sashpos(0) > 0
        assert isinstance(view._review_bottom_split, ttk.PanedWindow)
        assert len(view._review_bottom_split.panes()) == 2
        assert view._review_bottom_split.sashpos(0) > 0
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

            assert len(panel._popups) == 1
            window = next(iter(panel._popups.values()))
            assert isinstance(window, tk.Toplevel)
            assert str(window.title()) == "Logs"
            # resizable, and never modal: the main window stays usable
            assert window.resizable() == (True, True)
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
            first = panel._popups["Audit History"]
            panel._on_action("open_audit")

            assert panel._popups["Audit History"] is first
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

            assert sorted(panel._popups) == ["Judge Details"]
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

            titles = sorted(panel._popups)

            assert len(titles) == len(POPUP_INTENTS)
            assert all(_window_alive(panel._popups[title]) for title in titles)
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
            window = panel._popups["Risk Register"]
            window.geometry("700x480+40+60")
            _settle(panel._root)
            # close the window the way the window manager does
            handler = window.protocol("WM_DELETE_WINDOW")
            window.tk.call(handler)
            _settle(panel._root)

            assert _window_alive(window) is False
            # the panel writes its layout - main window and popups - on the way
            # out, exactly as it does when the operator closes the window
            panel._on_close()
        finally:
            self._discard(panel)

        written = store.load_layout(path)

        assert written["windows"]["Risk Register"] == "700x480+40+60"

    def test_a_remembered_popup_geometry_comes_back(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "data" / "gui_layout.json"
        assert (
            store.save_layout(path, {"windows": {"Logs": "760x520+12+24"}})
            is True
        )
        panel = self._start_panel(tmp_path, monkeypatch, path)
        try:
            assert panel._saved_popup_geometry("Logs") == "760x520+12+24"
            # A title that was never remembered, and a damaged value, both fall
            # back to the default size instead of an impossible geometry.
            assert panel._saved_popup_geometry("Reports") == ""
            panel._layout["windows"]["Logs"] = "not-a-geometry"
            assert panel._saved_popup_geometry("Logs") == ""

            panel._on_action("open_logs")
            _settle(panel._root)

            assert panel._popups["Logs"].winfo_width() > 1
        finally:
            self._discard(panel)




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
        # The rows the tab used to draw are still built, for the popups.
        assert view["conflict_rows"] == [["a", "f1", "-"]]
        assert view["cost_rows"][-1][0] == "total"
        assert view["judge_lines"]
