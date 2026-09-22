"""Layout tests for the operator panel: every major region is resizable by hand.

The panel is laid out with ``ttk.PanedWindow`` splitters, so the *operator* - not
the code - decides how much room the header, the notebook, the three advisor
panes, the review results and the supervisor panes get. A fake widget cannot
prove that, so these tests build a real ``tk.Tk`` window (and skip when Tk or a
display is missing) and pin:

* a vertical splitter between the work area and the tab notebook, with the
  notebook owning most of the height;
* three *separate* advisor panes inside one horizontal splitter in the
  Architecture Review tab, starting equally wide;
* a draggable split between the review question/header and the advisor area, and
  **no** splitter below the advisors: the shared results are one compact summary
  panel at the bottom of that same pane, so the advisors keep the height;
* three draggable panes in the Supervisor tab;
* a draggable split between the proposal summary and the proposal detail;
* a drag that would collapse a pane being stopped at the pane floor, and a
  notebook that grows when the window grows.

The layout is the operator's *and stays theirs*: a second part of these tests
builds windows from a remembered layout and pins that the saved window geometry,
sash positions and open tab come back exactly - and that a remembered value Tk
could not use costs the operator that one value and never the window.

The last class is static: it keeps the Tk/core boundary this task must not
touch - the layout module still imports nothing but Tk, the standard library and
its own controller, positions no widget in pixels, and declares real floors.
"""

from __future__ import annotations

import ast
import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any, Iterator, Mapping

import pytest

pytest.importorskip("tkinter")

from architecture_assistant_gui import layout as store  # noqa: E402
from architecture_assistant_gui import views  # noqa: E402

#: The GUI package under test.
GUI_ROOT = Path(__file__).resolve().parents[1] / "src" / "architecture_assistant_gui"

#: A window big enough for every region to be visible before a drag.
WINDOW_GEOMETRY = "1400x900"

#: The advisor names the review payload carries in these tests.
ADVISORS = ("OpenAI", "Claude", "Grok")


def _panel(name: str) -> dict[str, Any]:
    """One advisor panel, exactly the shape the controller's view model has."""
    return {
        "name": name,
        "status_text": "DONE",
        "level": "INFO",
        "facts": (("Severity", "LOW"), ("Confidence", "0.9")),
        "body_lines": ("claim", "evidence"),
    }


def _review_view(*names: str) -> dict[str, Any]:
    """A minimal view model whose review section carries these advisor panels."""
    return {"review": {"provider_panels": [_panel(name) for name in names]}}


def _settle(root: tk.Misc, passes: int = 3) -> None:
    """Let Tk finish the layout.

    Sashes are placed on ``<Configure>`` and the pane floors on idle, so a single
    ``update()`` is not enough to see the final sizes.
    """
    for _ in range(passes):
        root.update()
        root.update_idletasks()


def _panes(root: tk.Misc, split: Any) -> list[Any]:
    """The pane widgets of one splitter, in order."""
    return [root.nametowidget(pane) for pane in split.panes()]


def _sizes(root: tk.Misc, split: Any) -> list[int]:
    """The size of every pane along the splitter's own axis."""
    horizontal = str(split.cget("orient")) == "horizontal"
    return [
        widget.winfo_width() if horizontal else widget.winfo_height()
        for widget in _panes(root, split)
    ]


def _parents(widget: Any) -> list[str]:
    """The Tk names of every ancestor of one widget, closest first."""
    names: list[str] = []
    parent = widget.winfo_parent()
    while parent:
        names.append(parent)
        parent = widget.nametowidget(parent).winfo_parent()
    return names


def _inside(widget: Any, pane: Any) -> bool:
    """Whether ``widget`` lives inside the pane widget ``pane``."""
    return str(pane) in _parents(widget)


def _of_kind(widget: Any, kind: type) -> list[Any]:
    """Every widget of one kind below ``widget`` (inclusive of its own children)."""
    found: list[Any] = []
    stack = list(widget.winfo_children())
    while stack:
        child = stack.pop()
        if isinstance(child, kind):
            found.append(child)
        stack.extend(child.winfo_children())
    return found


def _drag(root: tk.Misc, split: Any, index: int, position: int) -> None:
    """Move one sash to ``position`` and finish the drag as a mouse release does."""
    split.sashpos(index, position)
    split.event_generate("<ButtonRelease-1>", x=8, y=8)
    _settle(root)


def _select_tab(root: tk.Misc, view: views.MainWindow, title: str) -> None:
    """Open one tab and let the splitters inside it place themselves."""
    assert view.select_tab(title), f"the notebook has no {title!r} tab"
    _settle(root)


@pytest.fixture
def window() -> Iterator[tuple[tk.Tk, views.MainWindow]]:
    """A real panel window, laid out and rendering three advisor panels."""
    try:
        root = tk.Tk()
    except tk.TclError as error:  # pragma: no cover - machine without a display
        pytest.skip(f"Tk cannot open a window here: {error}")
    root.geometry(WINDOW_GEOMETRY)
    view = views.MainWindow(
        root,
        on_action=lambda _key: None,
        on_actor=lambda _text: None,
        on_question=lambda _text: None,
    )
    view.render(_review_view(*ADVISORS))
    _select_tab(root, view, "Architecture Review")
    try:
        yield root, view
    finally:
        root.destroy()


@pytest.fixture
def restored() -> Iterator[Any]:
    """Builds real windows from a remembered layout, and closes them after.

    The builder repeats the two steps the application performs - build the
    window with the layout, then hand it the remembered geometry - in the same
    order, so a test exercises the real startup sequence and not a model of it.
    """
    roots: list[tk.Tk] = []

    def build(
        layout: Mapping[str, Any],
        *,
        panels: tuple[str, ...] = ADVISORS,
        geometry: str = WINDOW_GEOMETRY,
    ) -> tuple[tk.Tk, views.MainWindow]:
        try:
            root = tk.Tk()
        except tk.TclError as error:  # pragma: no cover - no display
            pytest.skip(f"Tk cannot open a window here: {error}")
        roots.append(root)
        root.geometry(geometry)
        view = views.MainWindow(
            root,
            on_action=lambda _key: None,
            on_actor=lambda _text: None,
            on_question=lambda _text: None,
            layout=layout,
        )
        view.render(_review_view(*panels))
        parsed = store.parse_geometry(layout.get("geometry"))
        if parsed is not None:
            view.place_window(*parsed)
        _settle(root)
        return root, view

    try:
        yield build
    finally:
        for root in roots:
            root.destroy()


class TestMainWindowSplit:
    """The work area and the tab notebook are two draggable panes."""

    def test_the_main_splitter_is_a_vertical_panedwindow(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window

        split = view._main_split

        assert isinstance(split, ttk.PanedWindow)
        assert str(split.cget("orient")) == "vertical"
        assert len(split.panes()) == 2
        top, bottom = _panes(root, split)

        # the top pane is the header, the current work and the actions
        assert _inside(view._banner, top)
        assert _inside(view.buttons["refresh"], top)
        # the bottom pane is the tab notebook
        assert _inside(view._notebook, bottom)
        assert _inside(view._monitor, bottom)

    def test_the_notebook_starts_with_more_height_than_the_top_area(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        _root, view = window

        assert view._top_area.winfo_height() > 0
        # the notebook receives most of the vertical space at launch
        assert view._notebook.winfo_height() > view._top_area.winfo_height()

    def test_dragging_the_main_splitter_resizes_both_areas(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        split = view._main_split
        before = _sizes(root, split)

        _drag(root, split, 0, before[0] + 80)
        taller = _sizes(root, split)

        assert taller[0] > before[0]
        assert taller[1] < before[1]
        assert all(size > 0 for size in taller)

        _drag(root, split, 0, taller[0] - 80)
        back = _sizes(root, split)

        assert back[0] < taller[0]
        assert back[1] > taller[1]

    def test_a_drag_that_would_collapse_a_pane_stops_at_its_floor(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        split = view._main_split
        floor = int(views.MAIN_SPLIT_MIN_SIZE * 0.9)  # one sash width of slack

        _drag(root, split, 0, 0)

        assert min(_sizes(root, split)) >= floor

        _drag(root, split, 0, 20_000)

        assert min(_sizes(root, split)) >= floor

    def test_the_notebook_expands_when_the_window_grows(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        before_notebook = view._notebook.winfo_height()
        before_top = view._top_area.winfo_height()

        root.geometry("1400x1120")
        _settle(root)

        assert view._notebook.winfo_height() > before_notebook + 100
        assert view._notebook.winfo_height() > view._top_area.winfo_height()
        assert view._top_area.winfo_height() >= before_top


class TestReviewTabSplitters:
    """The Architecture Review tab is a splitter over the advisors and a summary."""

    def test_the_review_tab_has_a_vertical_splitter_with_two_regions(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        split = view._review_split

        assert isinstance(split, ttk.PanedWindow)
        assert str(split.cget("orient")) == "vertical"
        # two regions, not three: the shared results are one compact panel inside
        # the lower one, so there is no second, lower splitter left to drag
        assert len(split.panes()) == 2
        summary, advisors = _panes(root, split)

        # question and header on top, the advisors *and* the review summary below
        assert _inside(view._question, summary)
        assert _inside(view._review_header, summary)
        assert _inside(view._panes_frame, advisors)
        assert _inside(view._review_summary, advisors)

    def test_the_three_advisor_panes_are_separate_panes_of_one_splitter(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        split = view._panes_frame

        assert isinstance(split, ttk.PanedWindow)
        assert str(split.cget("orient")) == "horizontal"
        assert len(view._panes) == 3
        assert split.panes() == tuple(str(pane["frame"]) for pane in view._panes)

        panes = _panes(root, split)

        # every pane is its own labelled frame with its own compact summary and
        # its own controls: one advisor can be widened without touching the others
        assert [str(pane.cget("text")) for pane in panes] == list(ADVISORS)
        for pane, key in zip(panes, view._panes):
            assert isinstance(pane, ttk.LabelFrame)
            assert _inside(key["facts"], pane)
            assert _inside(key["summary"], pane)
            assert _inside(key["details"], pane)
            assert _inside(key["provider"], pane)

    def test_the_advisor_panes_start_equally_wide(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window

        sizes = _sizes(root, view._panes_frame)
        even = sum(sizes) / len(sizes)

        assert all(size > 0 for size in sizes)
        assert all(abs(size - even) <= even * 0.15 for size in sizes)

    def test_dragging_one_advisor_sash_resizes_only_that_pair(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        split = view._panes_frame
        before = _sizes(root, split)

        _drag(root, split, 0, split.sashpos(0) + 120)
        after = _sizes(root, split)

        assert after[0] > before[0]
        assert after[1] < before[1]
        assert after[2] == before[2]

    def test_an_advisor_sash_cannot_collapse_a_pane(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        split = view._panes_frame
        floor = int(views.ADVISOR_SPLIT_MIN_SIZE * 0.9)

        _drag(root, split, 0, 0)

        assert min(_sizes(root, split)) >= floor

        _drag(root, split, 1, 20_000)

        assert min(_sizes(root, split)) >= floor

    def test_a_rebuilt_advisor_split_gets_one_pane_per_advisor(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        split = view._panes_frame
        replaced = _panes(root, split)

        view.render(_review_view("OpenAI", "Claude", "Grok", "fourth"))
        _settle(root)

        assert len(view._panes) == 4
        assert len(split.panes()) == 4
        assert root.nametowidget(split.panes()[3]).winfo_exists() == 1

        view.render(_review_view(*ADVISORS))
        _settle(root)

        assert len(view._panes) == 3
        assert len(split.panes()) == 3
        # the panes that were replaced are gone, not merely hidden
        assert all(widget.winfo_exists() == 0 for widget in replaced)


    def test_the_review_summary_is_a_panel_not_a_splitter(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        """What replaced the five permanent sections: one frame, two short rows."""
        root, view = window
        root.geometry("1400x1150")
        _settle(root)
        panel = view._review_summary

        assert isinstance(panel, ttk.LabelFrame)
        assert _inside(panel, view._panes_frame) is False
        # the values and their five buttons are the panel's whole content
        assert _inside(view._review_values, panel)
        assert _inside(view._review_buttons_frame, panel)
        for key, _label in views.REVIEW_SUMMARY_BUTTONS:
            assert _inside(view._review_buttons[key], panel)
            assert _inside(view._review_buttons[key], view._review_buttons_frame)
        # and nothing in it is a splitter or a table any more
        assert _of_kind(panel, ttk.PanedWindow) == []
        assert _of_kind(panel, ttk.Treeview) == []

    def test_the_advisor_area_owns_the_height_of_the_tab(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        """Removing the lower splitter gave the advisor panes their room back."""
        root, view = window
        root.geometry("1400x1150")
        _settle(root)

        advisors = view._panes_frame.winfo_height()
        summary = view._review_summary.winfo_height()

        assert advisors > 0
        assert summary > 0
        # the compact panel is two short rows: the advisors get everything else
        assert advisors > summary * 3
        assert advisors > view._review_header.winfo_height()

    def test_dragging_the_review_sash_resizes_the_regions(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        split = view._review_split
        before = _sizes(root, split)

        _drag(root, split, 0, before[0] + 40)
        after = _sizes(root, split)

        assert after[0] > before[0]
        assert after[1] < before[1]
        assert all(size > 0 for size in after)


class TestSupervisorTabSplit:
    """The Supervisor tab's three panes are draggable, one sash each."""

    def test_the_supervisor_tab_has_three_draggable_panes(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        _select_tab(root, view, "Supervisor")
        split = view._supervisor_split

        assert isinstance(split, ttk.PanedWindow)
        assert str(split.cget("orient")) == "horizontal"
        assert len(split.panes()) == 3
        panes = _panes(root, split)

        for pane, key in zip(
            panes, ("assistant_pane", "cline_pane", "supervisor_pane")
        ):
            assert _inside(view._supervisor_panes[key], pane)
            assert _inside(view._supervisor_details, pane) is False

        assert [str(pane.cget("text")) for pane in panes] == [
            "Assistant",
            "Cline",
            "Supervisor",
        ]

    def test_the_supervisor_panes_start_equally_wide(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        _select_tab(root, view, "Supervisor")

        sizes = _sizes(root, view._supervisor_split)
        even = sum(sizes) / len(sizes)

        assert all(size > 0 for size in sizes)
        assert all(abs(size - even) <= even * 0.15 for size in sizes)

    def test_dragging_a_supervisor_sash_resizes_that_pair(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        _select_tab(root, view, "Supervisor")
        split = view._supervisor_split
        before = _sizes(root, split)

        _drag(root, split, 0, before[0] + 100)
        after = _sizes(root, split)

        assert after[0] > before[0]
        assert after[1] < before[1]
        assert after[2] == before[2]

    def test_a_supervisor_sash_cannot_collapse_a_pane(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        _select_tab(root, view, "Supervisor")
        split = view._supervisor_split
        floor = int(views.SUPERVISOR_SPLIT_MIN_SIZE * 0.9)

        _drag(root, split, 0, 0)

        assert min(_sizes(root, split)) >= floor

        _drag(root, split, 1, 20_000)

        assert min(_sizes(root, split)) >= floor

    def test_the_pane_titles_come_from_the_view_model(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        _select_tab(root, view, "Supervisor")

        view.render(
            {
                "supervisor": {
                    key: {"title": title, "rows": ()}
                    for key, title in (
                        ("assistant_pane", "Architecture Assistant"),
                        ("cline_pane", "Cline"),
                        ("supervisor_pane", "Supervisor"),
                    )
                }
            }
        )
        _settle(root)

        assert [
            str(root.nametowidget(pane).cget("text"))
            for pane in view._supervisor_split.panes()
        ] == ["Architecture Assistant", "Cline", "Supervisor"]


class TestProposalTabSplit:
    """The proposal summary and the proposal detail are draggable panes."""

    def test_the_proposal_tab_splits_the_summary_from_the_detail(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        _select_tab(root, view, "Architecture Proposal")
        split = view._proposal_split

        assert isinstance(split, ttk.PanedWindow)
        assert str(split.cget("orient")) == "vertical"
        assert len(split.panes()) == 2
        summary, detail = _panes(root, split)

        # the summary: the proposal header, its status, the facts and the digest
        assert _inside(view._proposal_header, summary)
        assert _inside(view._proposal_facts, summary)
        assert _inside(view._proposal_digest, summary)
        # the detail: the read-only architecture sections and the history
        assert _inside(view._proposal_sections["modules"], detail)
        assert _inside(view._proposal_history, detail)

    def test_the_two_proposal_panes_start_with_the_detail_larger(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        _select_tab(root, view, "Architecture Proposal")

        sizes = _sizes(root, view._proposal_split)

        assert all(size > 0 for size in sizes)
        assert sizes[1] > sizes[0]

    def test_dragging_the_proposal_splitter_resizes_the_summary(
        self, window: tuple[tk.Tk, views.MainWindow]
    ) -> None:
        root, view = window
        _select_tab(root, view, "Architecture Proposal")
        split = view._proposal_split
        before = _sizes(root, split)

        _drag(root, split, 0, before[0] + 60)
        after = _sizes(root, split)

        assert after[0] > before[0]
        assert after[1] < before[1]
        assert min(after) >= int(views.PROPOSAL_SPLIT_MIN_SIZE * 0.9)


class TestTheRememberedSashes:
    """Sash positions the operator left behind come back - within the floors."""

    def test_a_remembered_main_sash_is_placed_once_the_window_exists(
        self, restored
    ) -> None:
        _root, view = restored({"sashes": {"main": [300]}})

        assert view._main_split.sashpos(0) == 300

    def test_a_remembered_advisor_sash_is_placed_when_its_tab_is_opened(
        self, restored
    ) -> None:
        """The advisor panes only have a size once their tab is shown."""
        root, view = restored({"sashes": {"review_advisors": [400, 900]}})

        _select_tab(root, view, "Architecture Review")

        assert [view._panes_frame.sashpos(i) for i in (0, 1)] == [400, 900]

    def test_a_remembered_supervisor_sash_is_placed_when_its_tab_is_opened(
        self, restored
    ) -> None:
        root, view = restored({"sashes": {"supervisor": [380, 760]}})

        _select_tab(root, view, "Supervisor")

        assert [
            view._supervisor_split.sashpos(index) for index in (0, 1)
        ] == [380, 760]

    def test_a_remembered_proposal_sash_is_placed_when_its_tab_is_opened(
        self, restored
    ) -> None:
        """The proposal splitter comes back where the operator left it.

        The value is one this splitter itself produced, exactly what a previous
        session would have written: a splitter inside a tab whose height depends
        on its content is only restored faithfully for a position it can hold.
        """
        root, view = restored({})
        _select_tab(root, view, "Architecture Proposal")
        remembered = view._proposal_split.sashpos(0)

        root2, view2 = restored({"sashes": {"proposal": [remembered]}})
        _select_tab(root2, view2, "Architecture Proposal")

        assert remembered > 0
        assert view2._proposal_split.sashpos(0) == remembered

    def test_a_remembered_sash_that_does_not_fit_stops_at_the_pane_floor(
        self, restored
    ) -> None:
        """An impossible position is clamped, never obeyed - and never fatal."""
        root, view = restored({"sashes": {"proposal": [10_000]}})

        _select_tab(root, view, "Architecture Proposal")
        split = view._proposal_split
        heights = [pane.winfo_height() for pane in _panes(root, split)]
        floor = int(views.PROPOSAL_SPLIT_MIN_SIZE * 0.9)  # one sash of slack

        assert max(heights) < 10_000
        assert min(heights) >= floor

    def test_a_remembered_sash_comes_back_exactly_after_the_panels_settle(
        self, restored
    ) -> None:
        """A tab that was never shown is laid out at a smaller size first.

        A remembered position placed during that first, smaller layout would
        stay clamped to it, so the operator's own position is placed again until
        the splitter has the size it was measured at.
        """
        root, view = restored({})
        _select_tab(root, view, "Architecture Proposal")
        _drag(
            root,
            view._proposal_split,
            0,
            view._proposal_split.sashpos(0) + 40,
        )
        saved = store.normalise_layout(view.layout_snapshot())

        root2, view2 = restored(saved)
        _select_tab(root2, view2, "Architecture Proposal")

        assert view2._proposal_split.sashpos(0) == saved["sashes"]["proposal"][0]

    def test_a_remembered_position_is_kept_when_the_window_is_resized(
        self, restored
    ) -> None:
        """Until the operator drags, the remembered position stays where it was."""
        root, view = restored({"sashes": {"main": [300]}})

        root.geometry("1400x1120")
        _settle(root)

        assert view._main_split.sashpos(0) == 300

    def test_a_drag_hands_that_splitter_back_to_tk(self, restored) -> None:
        """A drag is never undone - not even by the next window resize."""
        root, view = restored({"sashes": {"main": [150]}})
        _drag(root, view._main_split, 0, 320)

        assert "main" in view._operator_splits

        root.geometry("1400x1120")
        _settle(root)
        taller = view._main_split.sashpos(0)

        assert taller != 150  # the remembered position was not re-placed
        assert taller >= 320  # and Tk grew the pane with the window

    def test_a_remembered_sash_that_would_collapse_a_pane_stops_at_its_floor(
        self, restored
    ) -> None:
        root, view = restored({"sashes": {"main": [20_000]}})
        floor = int(views.MAIN_SPLIT_MIN_SIZE * 0.9)  # one sash of slack

        assert min(_sizes(root, view._main_split)) >= floor

    def test_a_remembered_sash_for_a_different_pane_count_is_not_used(
        self, restored
    ) -> None:
        """Two advisors must not be given the sash shape of three."""
        root, view = restored(
            {"sashes": {"review_advisors": [400, 900]}},
            panels=("OpenAI", "Claude"),
        )

        _select_tab(root, view, "Architecture Review")
        sizes = _sizes(root, view._panes_frame)
        even = sum(sizes) / len(sizes)

        assert len(view._panes) == 2
        assert all(abs(size - even) <= even * 0.15 for size in sizes)

    def test_a_partly_usable_layout_keeps_its_usable_part(
        self, restored
    ) -> None:
        _root, view = restored(
            {"sashes": {"main": [280], "supervisor": "wide"}}
        )

        assert view._main_split.sashpos(0) == 280

    @pytest.mark.parametrize(
        "sashes",
        [
            {"main": "300"},
            {"main": []},
            {"main": [0]},
            {"main": [-40]},
            {"main": [None]},
            {"main": [True]},
            {"main": [12.5]},
            {"main": [1, 2, 3]},
            {"main": {"0": 300}},
            "300",
            None,
            [300],
            42,
        ],
    )
    def test_a_remembered_value_tk_could_not_use_costs_only_itself(
        self, restored, sashes: Any
    ) -> None:
        root, view = restored({"sashes": sashes})

        assert view._main_split.sashpos(0) > 0
        assert all(size > 0 for size in _sizes(root, view._main_split))
        assert view._notebook.winfo_height() > view._top_area.winfo_height()


class TestTheRememberedWindow:
    """The window itself: its size and its position on this screen."""

    def test_the_remembered_geometry_is_applied(self, restored) -> None:
        root, _view = restored(
            {"geometry": "1000x700+120+80"}, geometry="1400x900"
        )

        assert root.winfo_geometry() == "1000x700+120+80"

    def test_a_position_off_this_screen_is_dropped_and_the_size_kept(
        self, restored
    ) -> None:
        """A monitor that is gone must not decide that the panel is hidden."""
        root, view = restored({})

        view.place_window(1000, 700, 50_000, 50_000)
        _settle(root)

        after = root.winfo_geometry()

        assert after.startswith("1000x700")
        assert "50000" not in after

    def test_a_size_larger_than_the_screen_is_capped(self, restored) -> None:
        root, view = restored({})
        screen = root.winfo_screenwidth()

        view.place_window(screen + 5_000, 700)
        _settle(root)

        assert root.winfo_width() <= screen
        assert root.winfo_height() > 0


class TestTheRememberedTab:
    """The tab that was open is the tab that opens."""

    def test_the_remembered_tab_is_opened(self, restored) -> None:
        _root, view = restored({"tab": "Supervisor"})

        assert view._current_tab() == "Supervisor"

    def test_a_tab_that_no_longer_exists_is_ignored(self, restored) -> None:
        _root, view = restored({"tab": "Risks (v2)"})

        assert view._current_tab() == "Monitor"
        assert view.select_tab("Risks (v2)") is False

    def test_every_built_in_tab_can_be_opened_by_name(self, restored) -> None:
        _root, view = restored({})
        # Only the primary workflow owns a tab; logs, audit, risks and reports
        # open in their own windows instead (see the utility bar).
        titles = (
            "Monitor",
            "Architecture Review",
            "Architecture Proposal",
            "Supervisor",
        )

        assert [view.select_tab(title) for title in titles] == [True] * len(
            titles
        )
        assert view._current_tab() == titles[-1]

    def test_the_secondary_views_are_no_longer_tabs(self, restored) -> None:
        """Logs, Audit, Risks and Reports are popups now - not tabs."""
        _root, view = restored({})

        for title in ("Logs", "Audit", "Risks", "Reports / Log", "Reports"):
            assert view.select_tab(title) is False


class TestTheLayoutSnapshot:
    """What the host writes is what the operator actually left on screen."""

    def test_a_snapshot_carries_the_geometry_the_tab_and_the_sashes(
        self, restored
    ) -> None:
        root, view = restored({})
        _select_tab(root, view, "Architecture Review")
        _drag(root, view._main_split, 0, 320)
        _drag(root, view._panes_frame, 0, view._panes_frame.sashpos(0) + 90)
        view.select_tab("Architecture Proposal")
        _settle(root)

        snapshot = view.layout_snapshot()

        assert snapshot["geometry"] == root.winfo_geometry()
        assert snapshot["tab"] == "Architecture Proposal"
        assert snapshot["sashes"]["main"] == [view._main_split.sashpos(0)]
        assert snapshot["sashes"]["review_advisors"] == [
            view._panes_frame.sashpos(0),
            view._panes_frame.sashpos(1),
        ]
        # a tab that was never opened has no shape to report
        assert "supervisor" not in snapshot["sashes"]
        # and the whole thing is plain JSON data, ready for the file
        assert json.loads(json.dumps(snapshot)) == snapshot

    def test_a_snapshot_reports_only_splitters_it_could_measure(
        self, restored
    ) -> None:
        _root, view = restored({})

        snapshot = view.layout_snapshot()

        assert "main" in snapshot["sashes"]
        assert "proposal" not in snapshot["sashes"]
        assert "supervisor" not in snapshot["sashes"]

    def test_a_session_that_changes_nothing_keeps_what_it_restored(
        self, restored
    ) -> None:
        """The operator's layout is not lost by simply closing the panel."""
        _root, view = restored(
            {
                "geometry": "1400x900+40+40",
                "tab": "Architecture Review",
                # a splitter in a tab nobody opens must survive a snapshot too
                "sashes": {"main": [300], "supervisor": [380, 760]},
            }
        )

        snapshot = view.layout_snapshot()

        assert snapshot["sashes"]["main"] == [300]
        assert snapshot["sashes"]["supervisor"] == [380, 760]
        assert snapshot["tab"] == "Architecture Review"
        assert snapshot["geometry"].startswith("1400x900")

    def test_a_snapshot_round_trips_into_a_second_window(
        self, restored
    ) -> None:
        """Close the panel, start it again: everything is where it was."""
        root, view = restored({})
        _select_tab(root, view, "Architecture Review")
        _drag(root, view._main_split, 0, 320)
        _drag(root, view._panes_frame, 0, view._panes_frame.sashpos(0) + 90)
        saved = store.normalise_layout(view.layout_snapshot())

        root2, view2 = restored(saved)

        assert root2.winfo_geometry() == saved["geometry"]
        assert view2._main_split.sashpos(0) == saved["sashes"]["main"][0]
        assert view2._current_tab() == "Architecture Review"
        _select_tab(root2, view2, "Architecture Review")
        assert [
            view2._panes_frame.sashpos(index) for index in (0, 1)
        ] == saved["sashes"]["review_advisors"]


class TestTheLayoutVocabulary:
    """Every splitter has exactly one stable name, and it is the one stored."""

    def test_every_named_splitter_is_a_real_splitter(self, window) -> None:
        _root, view = window

        assert set(view._splits) == set(views.SPLIT_KEYS)
        assert len(views.SPLIT_KEYS) == len(set(views.SPLIT_KEYS))
        for split in view._splits.values():
            assert isinstance(split, ttk.PanedWindow)

    def test_the_names_point_at_the_regions_the_layout_remembers(
        self, window
    ) -> None:
        _root, view = window
        named = view._splits

        assert named["main"] is view._main_split
        assert named["review"] is view._review_split
        assert named["review_advisors"] is view._panes_frame
        assert named["supervisor"] is view._supervisor_split
        assert named["proposal"] is view._proposal_split
        # the review results are a panel inside the advisor pane now, so they
        # have no splitter name of their own any more
        assert "review_results" not in named
        assert "review_bottom" not in named
        assert "review_merged" not in named
        assert "review_judge" not in named


class TestTheApplicationRemembersTheWindow:
    """Startup reads the layout file; a normal close writes it back."""

    @staticmethod
    def _config(tmp_path) -> Any:
        """A configuration whose runtime files all live in the test's tmp dir."""
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

    def _start_panel(self, tmp_path, monkeypatch, layout_path) -> Any:
        """A panel with a real window, but no core thread and no mainloop.

        The runner and the loop are replaced so the test can drive the startup
        and the close path itself: what is under test here is the layout file,
        never the workflow behind it.
        """
        from architecture_assistant_gui import app as gui_app
        from architecture_assistant_gui import core

        monkeypatch.setattr(core.BackgroundRunner, "start", lambda self: None)
        monkeypatch.setattr(tk.Tk, "mainloop", lambda self: None)
        panel = gui_app.GuiApp(
            self._config(tmp_path), layout_path=layout_path
        )

        assert panel.run() == 0
        _settle(panel._root)
        return panel

    @staticmethod
    def _discard(panel: Any) -> None:
        """Close a window the test did not close itself - never twice."""
        try:
            panel._root.destroy()
        except (AttributeError, tk.TclError):  # pragma: no cover - closed
            pass

    def test_the_panel_starts_from_the_file_and_saves_on_close(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "data" / "gui_layout.json"
        assert (
            store.save_layout(
                path,
                {
                    "geometry": "1200x820+30+40",
                    "tab": "Supervisor",
                    "sashes": {"main": [260]},
                },
            )
            is True
        )
        panel = self._start_panel(tmp_path, monkeypatch, path)
        try:
            window = panel._window

            assert panel._root.winfo_geometry().startswith("1200x820")
            assert window._current_tab() == "Supervisor"
            assert window._main_split.sashpos(0) == 260

            # the operator drags one sash and closes the window
            _drag(panel._root, window._main_split, 0, 310)
            panel._on_close()
        finally:
            self._discard(panel)

        written = store.load_layout(path)

        assert written["sashes"]["main"] == [310]
        assert written["geometry"].startswith("1200x820")
        assert written["tab"] == "Supervisor"

    def test_a_damaged_file_means_the_default_layout_and_is_replaced(
        self, tmp_path, monkeypatch
    ) -> None:
        path = tmp_path / "gui_layout.json"
        path.write_text("{ this is not json", encoding="utf-8")
        panel = self._start_panel(tmp_path, monkeypatch, path)
        try:
            window = panel._window

            assert window._main_split.sashpos(0) > 0
            assert window._current_tab() == "Monitor"

            panel._on_close()
        finally:
            self._discard(panel)

        written = store.load_layout(path)

        assert store.parse_geometry(written["geometry"]) is not None
        assert written["tab"] == "Monitor"

    def test_a_layout_path_that_is_not_a_path_is_refused(self) -> None:
        from architecture_assistant_gui import app as gui_app

        with pytest.raises(ValueError):
            gui_app.GuiApp(self._config(Path.cwd()), layout_path=7)


class TestLayoutBoundaries:
    """The layout code stays a presentation module - static checks, no display."""

    def test_the_layout_module_imports_only_tk_the_stdlib_and_its_controller(
        self,
    ) -> None:
        source = (GUI_ROOT / "views.py").read_text(encoding="utf-8")
        roots: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    roots.add(f".{node.module or ''}")
                elif node.module:
                    roots.add(node.module.split(".")[0])

        assert roots == {".controller", "__future__", "tkinter", "typing"}
        # the frozen core is never imported by a widget module
        assert not any(
            name.startswith("architecture_assistant.") for name in roots
        )

    def test_no_widget_is_positioned_in_pixels(self) -> None:
        source = (GUI_ROOT / "views.py").read_text(encoding="utf-8")

        assert "ttk.PanedWindow" in source
        assert ".place(" not in source
        assert "minsize=" not in source  # Tk 8.6 rejects the pane option

    def test_every_split_is_added_through_the_pane_helper(self) -> None:
        """No split is filled with a fixed grid again: every pane is added."""
        source = (GUI_ROOT / "views.py").read_text(encoding="utf-8")

        splits = [
            line
            for line in source.splitlines()
            if "ttk.PanedWindow(" in line and "def " not in line
        ]

        assert len(splits) >= 6
        assert source.count("_add_pane(") >= len(splits) + 1
        assert "_clamp_split(" in source
