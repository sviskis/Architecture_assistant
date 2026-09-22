"""Non-modal popup windows: resizable, scrollable, presentation only.

The operator panel keeps only the primary workflow in the main window; cost
detail, logs, the audit trail, the risk register, report history, the judge, the
evidence conflicts and every "technical details" view open in one of these
windows instead.

Three rules shape this module, and they are the same rules the main window obeys:

* **presentation only.** A popup renders a *spec* - a plain dict of
  ``title``/``note``/``sections``, where a section is a table (columns + rows) or
  a block of text (lines). Nothing here reads a controller, a repository, a
  provider or the database, and nothing here decides anything;
* **no fixed layout.** Every window is resizable, every region grows with it, and
  a table longer than the window scrolls instead of forcing the window to be
  huge. An empty table is replaced by its own one-line empty state rather than by
  a large blank widget;
* **non-modal.** A popup never grabs the keyboard: the operator keeps working in
  the main window while a popup is open. Only a confirmation dialog is modal, and
  those live in the application, not here.

The Tk boundary is preserved exactly as it is in ``views.py``: this module
imports Tk, the standard library and nothing else.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "POPUP_DEFAULT_HEIGHT",
    "POPUP_DEFAULT_WIDTH",
    "POPUP_MIN_HEIGHT",
    "POPUP_MIN_WIDTH",
    "open_popup",
]

#: The size a popup starts with, and the floor it can never be dragged below.
#: Deliberately modest: it must be usable on a 1920x1080 screen beside the main
#: window, and it is the *operator* who decides whether it grows.
POPUP_DEFAULT_WIDTH = 900
POPUP_DEFAULT_HEIGHT = 620
POPUP_MIN_WIDTH = 420
POPUP_MIN_HEIGHT = 240

#: How long a note under the title may be before it is wrapped.
_NOTE_WRAP = 900



def open_popup(
    root: tk.Misc,
    spec: Mapping[str, Any],
    *,
    geometry: str = "",
    on_close: Optional[Callable[[tk.Toplevel], None]] = None,
    on_action: Optional[Callable[[str], None]] = None,
) -> tk.Toplevel:
    """Open one non-modal popup from a spec and return its window.

    ``geometry`` is an already validated ``WxH[+X+Y]`` string (or ``""`` for the
    default size): the caller owns the layout file, and this module never reads
    or writes one. ``on_close`` is called with the live window *before* it is
    destroyed, which is where a caller captures its geometry. ``on_action`` turns
    the spec's optional actions into buttons; without it no button is built.
    """
    title = str(spec.get("title") or "Details")
    window = tk.Toplevel(root)
    window.title(title)
    window.minsize(POPUP_MIN_WIDTH, POPUP_MIN_HEIGHT)
    window.geometry(str(geometry) or f"{POPUP_DEFAULT_WIDTH}x{POPUP_DEFAULT_HEIGHT}")
    window.columnconfigure(0, weight=1)
    window.rowconfigure(0, weight=1)
    # Deliberately never modal: the operator keeps the main window usable.
    try:
        window.transient(root)
    except tk.TclError:  # pragma: no cover - only without a toplevel parent
        pass

    outer = ttk.Frame(window, padding=8)
    outer.grid(row=0, column=0, sticky="nsew")
    outer.columnconfigure(0, weight=1)

    row = 0
    note = str(spec.get("note") or "")
    if note:
        ttk.Label(
            outer, text=note, wraplength=_NOTE_WRAP, justify="left"
        ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        row += 1

    sections = [
        section
        for section in (spec.get("sections") or ())
        if isinstance(section, Mapping)
    ]
    if not sections:
        sections = [spec_text("Details", (), empty="Nothing to show.")]

    split = ttk.PanedWindow(outer, orient="vertical")
    split.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(0, 6))
    outer.rowconfigure(row, weight=1)
    row += 1
    for section in sections:
        frame = ttk.LabelFrame(
            split, text=str(section.get("title") or "Details"), padding=(6, 4)
        )
        frame.columnconfigure(0, weight=1)
        split.add(frame, weight=1)
        _render_section(frame, section)

    actions = [
        action
        for action in (spec.get("actions") or ())
        if isinstance(action, Mapping) and action.get("label")
    ]
    if actions and on_action is not None:
        bar = ttk.Frame(outer)
        bar.grid(row=row, column=0, columnspan=2, sticky="ew")
        for index, action in enumerate(actions):
            ttk.Button(
                bar,
                text=str(action["label"]),
                command=lambda key=str(action.get("key") or ""): on_action(key),
            ).grid(row=0, column=index, sticky="w", padx=(0, 6))

    def _close() -> None:
        if on_close is not None:
            try:
                on_close(window)
            except BaseException:  # noqa: BLE001 - a preference never blocks a close
                pass
        window.destroy()

    window.protocol("WM_DELETE_WINDOW", _close)
    return window


def _render_section(parent: Any, section: Mapping[str, Any]) -> None:
    """Render one section inside its frame: a scrolling table, or scrolling text."""
    columns = [
        (str(column[0]), str(column[1]), int(column[2]))
        for column in (section.get("columns") or ())
        if len(tuple(column)) >= 3
    ]
    rows = [tuple(row) for row in (section.get("rows") or ())]
    if columns and rows:
        _render_table(parent, columns, rows)
        return
    if columns:
        _render_note(parent, str(section.get("empty") or "No entries."))
        return
    lines = tuple(str(line) for line in (section.get("lines") or ()))
    if not lines:
        lines = (str(section.get("empty") or "Nothing to show."),)
    text = scrolledtext.ScrolledText(parent, wrap="word", height=8)
    text.grid(row=0, column=0, sticky="nsew")
    text.insert("1.0", "\n".join(lines))
    text.configure(state="disabled")
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)


def _render_note(parent: Any, text: str) -> None:
    """A compact one-line empty state instead of a large blank widget."""
    ttk.Label(parent, text=text, wraplength=_NOTE_WRAP, justify="left").grid(
        row=0, column=0, sticky="w"
    )


def _render_table(
    parent: Any,
    columns: Sequence[tuple[str, str, int]],
    rows: Sequence[Sequence[str]],
) -> None:
    """A read-only table that scrolls in both directions."""
    keys = tuple(key for key, _heading, _width in columns)
    table = ttk.Treeview(parent, columns=keys, show="headings", height=10)
    for key, heading, width in columns:
        table.heading(key, text=heading)
        table.column(key, width=width, anchor="w")
    vertical = ttk.Scrollbar(parent, orient="vertical", command=table.yview)
    horizontal = ttk.Scrollbar(parent, orient="horizontal", command=table.xview)
    table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    table.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)
    for row in rows:
        table.insert("", "end", values=tuple(row)[: len(keys)])
