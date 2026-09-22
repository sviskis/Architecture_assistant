"""Step 29 GUI tests: the Deliberation workbench and its plain payloads.

The tab is Agent A | Lead Agent | Agent B, the chair owns the centre and the widest
pane, and every stage control is offered only when the core says that stage is
valid. These tests pin exactly that, and they also pin the boundary the panel
keeps: the controller renders plain data and never holds a core object.
"""

from __future__ import annotations

import tkinter as tk

import pytest

from architecture_assistant_gui import views as views_module

# The Tk helpers are the ones the layout suite already built: one hidden root and
# one main window, so the real widget tree is exercised.
from tests.test_gui_layout import window  # noqa: F401


def test_the_deliberation_split_is_a_horizontal_panedwindow_with_three_panes(
    window,
) -> None:
    _root, view = window

    split = view._deliberation_split

    assert isinstance(split, tk.ttk.PanedWindow)
    assert str(split.cget("orient")) == "horizontal"
    assert tuple(view._deliberation_panes) == ("agent_a", "lead", "agent_b")
    assert len(split.panes()) == 3


def test_the_chair_pane_starts_widest(window) -> None:
    root, view = window

    split = view._deliberation_split
    root.update_idletasks()
    total = split.winfo_width()
    if total <= 1:  # pragma: no cover - no real geometry in a headless run
        pytest.skip("no real geometry available")

    first = split.sashpos(0)
    assert first / total == pytest.approx(0.27, abs=0.06)


def test_the_splitter_is_remembered_by_name(window) -> None:
    _root, view = window

    assert "deliberation" in views_module.SPLIT_KEYS
    assert "deliberation" in view._splits
    assert view._splits["deliberation"] is view._deliberation_split


def test_every_stage_control_exists(window) -> None:
    _root, view = window

    assert set(view._deliberation_buttons) == {
        "deliberation_round1",
        "deliberation_lead_review",
        "deliberation_round2",
        "deliberation_synthesis",
        "deliberation_proposal",
        "deliberation_cancel",
    }


def test_the_tab_renders_a_plain_payload(window) -> None:
    _root, view = window

    view.render(
        {
            "deliberation": {
                "status": "Deliberation d-1 is ROUND1_COMPLETE",
                "requirement": "MP3 -> TXT",
                "cost": "Total deliberation cost: unavailable",
                "agent_a_rows": [["Round 1", "COMPLETE"]],
                "lead_rows": [["Stage", "ROUND1_COMPLETE"]],
                "agent_b_rows": [["Round 1", "COMPLETE"]],
                "agent_a_summary": "independent",
                "lead_summary": "Agreements 1",
                "agent_b_summary": "independent",
                "buttons": {"deliberation_lead_review": True},
            }
        }
    )

    assert view._var("deliberation_status").get().startswith("Deliberation d-1")
    assert view._var("deliberation_requirement").get() == "MP3 -> TXT"
    rows = view._deliberation_panes["lead"]["rows"]
    assert [tuple(rows.item(item, "values")) for item in rows.get_children()] == [
        ("Stage", "ROUND1_COMPLETE")
    ]
    # only the valid control is enabled
    assert "disabled" not in view._deliberation_buttons[
        "deliberation_lead_review"
    ].state()
    assert "disabled" in view._deliberation_buttons[
        "deliberation_round1"
    ].state()
