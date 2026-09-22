"""The one window every popup uses: standard, resizable, non-modal, plain data.

The operator panel keeps only the primary workflow in the main window; the cost
detail, the logs, the audit trail, the risk register, the report history, the
judge, the evidence conflicts, the supervisor's technical details, the project
details, the architecture details, the monitor snapshot and every "provider
details" view open in one of these windows instead.

One standard, and it is the same for all of them:

* a **title**, an optional one-line **note**, a **content area** of sections and
  one **action bar** at the bottom - ``[ Copy ] [ Refresh ] [ ... ] [ Close ]``
  for a view, ``[ Cancel ] [ Confirm ]`` for a decision - with the same padding,
  the same spacing and the same button order everywhere;
* **resizable**, with a sensible minimum, scrollable content and a size
  *category* (small / medium / large) instead of one fixed size: it has to be
  usable on a 1920x1080 screen and on a smaller one, and it is the operator who
  decides whether a window grows;
* **non-modal** unless the operator's answer *is* the decision (a confirmation),
  and never stealing the keyboard from a window that already holds the grab;
* **the same three ways out**: Escape, the window's own X and ``[ Close ]`` all
  close it, and on a decision window Escape means Cancel;
* **presentation only.** A window renders a *spec* - a plain dict of
  ``title``/``note``/``sections``/``actions`` - and nothing here reads a
  controller, a repository, a provider or the database. Geometry is a number the
  caller hands in, never a file this module opens, and a remembered position that
  would land off screen is clamped back onto it instead of being trusted.

The Tk boundary is the one ``views.py`` already obeys: this module imports Tk,
the standard library and nothing else.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Any, Callable, Mapping, NamedTuple, Optional, Sequence

__all__ = [
    "CANCEL_ACTION",
    "CONFIRM_ACTION",
    "POPUP_ACTION_CLOSE",
    "POPUP_ACTION_COPY",
    "POPUP_ACTION_DETAILS",
    "POPUP_ACTION_REFRESH",
    "POPUP_DEFAULT_HEIGHT",
    "POPUP_DEFAULT_SIZE",
    "POPUP_DEFAULT_WIDTH",
    "POPUP_MIN_HEIGHT",
    "POPUP_MIN_ON_SCREEN_EDGE",
    "POPUP_MIN_WIDTH",
    "POPUP_PADDING",
    "POPUP_ROW_GAP",
    "POPUP_SIZE_LARGE",
    "POPUP_SIZE_MEDIUM",
    "POPUP_SIZE_SMALL",
    "POPUP_SIZES",
    "ROLE_CANCEL",
    "ROLE_PRIMARY",
    "ROLE_SECONDARY",
    "PopupSize",
    "PopupWindow",
    "confirm_dialog",
    "open_popup",
]

#: The size categories. A window names one and gets the same starting size and
#: the same floor every time, so two windows of the same kind cannot disagree.
POPUP_SIZE_SMALL = "small"
POPUP_SIZE_MEDIUM = "medium"
POPUP_SIZE_LARGE = "large"


class PopupSize(NamedTuple):
    """One category: the size a window starts with, and the floor below it.

    ``note_height`` is the height a window uses while it has *no* content
    sections - a confirmation or a one-line notice. It grows to ``height`` as
    soon as a section appears, so a window that says one line is one line tall
    and a window that shows a table gets the room a table needs.
    """

    width: int
    height: int
    note_height: int
    min_width: int
    min_height: int


#: small: a confirmation or a short note; medium: a detail view; large: a long
#: table (logs, audit, risks, a decision on a plan). Deliberately modest - the
#: operator resizes, and nothing here is allowed to open fullscreen.
POPUP_SIZES: Mapping[str, PopupSize] = {
    POPUP_SIZE_SMALL: PopupSize(520, 380, 240, 360, 200),
    POPUP_SIZE_MEDIUM: PopupSize(840, 620, 300, 460, 260),
    POPUP_SIZE_LARGE: PopupSize(1080, 740, 360, 520, 300),
}

#: The category a window gets when it names none.
POPUP_DEFAULT_SIZE = POPUP_SIZE_MEDIUM

#: The absolute floor: no popup may be dragged below this, whatever it renders.
POPUP_MIN_WIDTH = 320
POPUP_MIN_HEIGHT = 180

#: The size the default category starts with (the documented default).
POPUP_DEFAULT_WIDTH = POPUP_SIZES[POPUP_DEFAULT_SIZE].width
POPUP_DEFAULT_HEIGHT = POPUP_SIZES[POPUP_DEFAULT_SIZE].height

#: How much of a popup must stay on screen when a remembered position is reused:
#: a stale preference must never be able to hide the window it belongs to.
POPUP_MIN_ON_SCREEN_EDGE = 60

#: One padding and one gap everywhere, so no two windows drift apart.
POPUP_PADDING = 8
POPUP_ROW_GAP = 6

#: The window's own actions - the framework knows these and builds the buttons
#: for them itself. A spec may declare more (an export, a decision), and those
#: travel back to the caller as their own key.
POPUP_ACTION_CLOSE = "close"
POPUP_ACTION_COPY = "copy"
POPUP_ACTION_REFRESH = "refresh"
POPUP_ACTION_DETAILS = "details"

#: Which end of the bar an action belongs to. ``secondary`` sits on the left
#: (Copy, Refresh, Export), ``cancel``/``primary`` form the decision pair on the
#: right - always in that order, in every window.
ROLE_SECONDARY = "secondary"
ROLE_CANCEL = "cancel"
ROLE_PRIMARY = "primary"

#: How tall a section's own content may ask to be, in lines/rows: a three-line
#: note must not reserve a screenful, and a thousand-row table must scroll
#: instead of demanding a huge window.
_SECTION_MIN_LINES = 3
_SECTION_MAX_LINES = 18
_SECTION_MIN_ROWS = 3
_SECTION_MAX_ROWS = 16

#: How long a note under the title may be before it is wrapped.
_NOTE_WRAP = 900

#: How many idle passes a modal window gets to become viewable before it gives up
#: on its grab. A grab is a hint about modality, never a reason to spin.
_GRAB_ATTEMPTS = 3

#: How long the Copy button says "Copied" before it says "Copy" again.
_COPY_FEEDBACK_MS = 1200


def open_popup(
    root: tk.Misc,
    spec: Mapping[str, Any],
    *,
    size: str = POPUP_DEFAULT_SIZE,
    width: Optional[int] = None,
    height: Optional[int] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    modal: bool = False,
    include_copy: bool = True,
    on_close: Optional[Callable[[tk.Toplevel], None]] = None,
    on_action: Optional[Callable[[str], None]] = None,
    on_refresh: Optional[Callable[[tk.Toplevel], None]] = None,
) -> "PopupWindow":
    """Open one popup from a spec and return its window.

    ``size`` names the category (``small`` / ``medium`` / ``large``), and
    ``width`` / ``height`` / ``x`` / ``y`` are an already validated *remembered*
    geometry in pixels - the caller owns the layout file, and this module never
    reads or writes one. A size outside the category's floor or larger than the
    screen is clamped; a position that would land off screen is clamped back onto
    it; and no position at all means "centred on the window that owns this
    popup".

    ``on_close`` is called with the live window *before* it is destroyed, which
    is where a caller captures its geometry. ``on_action`` receives the key of any
    spec action that is not one of the framework's own. ``on_refresh`` adds a
    Refresh button that re-reads the caller's last payload.
    """
    return PopupWindow(
        root,
        spec,
        size=size,
        width=width,
        height=height,
        x=x,
        y=y,
        modal=modal,
        include_copy=include_copy,
        on_close=on_close,
        on_action=on_action,
        on_refresh=on_refresh,
    )


class PopupWindow(tk.Toplevel):
    """The one window every popup in the panel is: a spec, drawn and resizable.

    The window keeps its own spec, so Copy and Refresh always work on exactly what
    is on screen, and ``render`` can redraw the same window from a newer spec
    without the caller ever touching a widget.
    """

    def __init__(
        self,
        root: tk.Misc,
        spec: Mapping[str, Any],
        *,
        size: str = POPUP_DEFAULT_SIZE,
        width: Optional[int] = None,
        height: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modal: bool = False,
        include_copy: bool = True,
        on_close: Optional[Callable[[tk.Toplevel], None]] = None,
        on_action: Optional[Callable[[str], None]] = None,
        on_refresh: Optional[Callable[[tk.Toplevel], None]] = None,
        focus: bool = True,
    ) -> None:
        super().__init__(root)
        self._parent = root
        self._spec: dict[str, Any] = {}
        self._sections: tuple[Mapping[str, Any], ...] = ()
        self._buttons: dict[str, Any] = {}
        self._primary: Optional[Mapping[str, Any]] = None
        self._cancel: Optional[Mapping[str, Any]] = None
        self._modal = bool(modal)
        self._include_copy = bool(include_copy)
        self._on_close = on_close if callable(on_close) else None
        self._on_action = on_action if callable(on_action) else None
        self._on_refresh = on_refresh if callable(on_refresh) else None
        self._closed = False
        self._closing = False
        self._grabs = 0
        self._size = size
        #: Whether this window is still at the height it was given for a note with
        #: no content, in which case the first section it draws grows it.
        self._compact = False
        #: Every label whose text must wrap to the window's own width: a long note
        #: in a narrow window is wrapped, never cut off.
        self._wrapping: list[Any] = []

        self._body = ttk.Frame(self, padding=POPUP_PADDING)
        self._body.grid(row=0, column=0, sticky="nsew")
        self._body.columnconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.resizable(True, True)

        category = _size_of(size)
        self.minsize(category.min_width, category.min_height)
        self._place(category, width, height, x, y, has_content=bool(_sections_of(spec)))
        try:
            self.transient(root)
        except tk.TclError:  # pragma: no cover - only without a toplevel parent
            pass
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Return>", self._on_return)
        self.bind("<Configure>", self._wrap_text, add="+")

        self.render(spec)
        self._raise_to_front()
        if self._modal:
            self._grab()
            self._focus()
        elif focus and not self._grabbed_elsewhere():
            self._focus()

    # -- drawing -----------------------------------------------------------

    def render(self, spec: Mapping[str, Any]) -> None:
        """Draw the window from a spec - the same window, a fresh body.

        Called once when the window opens, and again on Refresh, on Details... and
        whenever the caller holds newer data: the window, its size and its
        position stay exactly where the operator put them.
        """
        self._spec = dict(spec) if isinstance(spec, Mapping) else {}
        self._buttons = {}
        self._primary = None
        self._cancel = None
        for child in self._body.winfo_children():
            child.destroy()
        try:
            self.title(str(self._spec.get("title") or "Details"))
        except tk.TclError:  # pragma: no cover - a window being torn down
            return
        row = 0
        note = str(self._spec.get("note") or "")
        self._wrapping = []
        if note:
            label = ttk.Label(
                self._body, text=note, wraplength=_NOTE_WRAP, justify="left"
            )
            label.grid(row=row, column=0, sticky="ew", pady=(0, POPUP_ROW_GAP))
            self._wrapping.append(label)
            self._body.rowconfigure(row, weight=0)
            row += 1
        sections = _sections_of(self._spec)
        self._sections = sections
        if sections:
            self._draw_sections(sections, row)
            self._body.rowconfigure(row, weight=1)
            self._grow_for_content()
        elif note:
            # A note-only window (a confirmation, a notice): the note is the whole
            # content, so the note row is what grows - no empty area is reserved.
            self._body.rowconfigure(0, weight=1)
        row += 1
        self._draw_bar(row, _actions_of(self._spec))
        self._wrap_text()

    def content_text(self) -> str:
        """The window's own content as plain text - what Copy puts on a clipboard.

        Title, note and every section in reading order; a table is tab-separated,
        so a pasted result stays a table.
        """
        spec = self._spec
        parts = [str(spec.get("title") or "")]
        note = str(spec.get("note") or "")
        if note:
            parts.append(note)
        for section in self._sections:
            parts.append("")
            parts.append(str(section.get("title") or "Details"))
            columns = [
                column
                for column in (section.get("columns") or ())
                if len(tuple(column)) >= 3
            ]
            rows = [
                tuple(str(cell) for cell in row)
                for row in (section.get("rows") or ())
            ]
            if columns and rows:
                parts.append("\t".join(str(column[1]) for column in columns))
                parts.extend("\t".join(row[: len(columns)]) for row in rows)
                continue
            lines = [str(line) for line in (section.get("lines") or ())]
            parts.extend(lines or [str(section.get("empty") or "")])
        return "\n".join(parts).strip("\n")

    def _draw_sections(
        self, sections: Sequence[Mapping[str, Any]], row: int
    ) -> None:
        """The content area: one frame per section, each scrolling on its own."""
        if len(sections) > 1:
            host: Any = ttk.PanedWindow(self._body, orient="vertical")
        else:
            host = ttk.Frame(self._body)
        host.grid(row=row, column=0, sticky="nsew")
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        frames: list[Any] = []
        sizes: list[int] = []
        for section in sections:
            frame = ttk.LabelFrame(
                host, text=str(section.get("title") or "Details"), padding=(6, 4)
            )
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
            sizes.append(self._draw_section(frame, section))
            frames.append(frame)
        if len(frames) == 1:
            frames[0].grid(row=0, column=0, sticky="nsew")
            return
        # Only the fullest section takes the leftover space: a short section keeps
        # the height its own content asked for instead of opening a blank area.
        grow = max(range(len(frames)), key=lambda index: sizes[index])
        for index, frame in enumerate(frames):
            host.add(frame, weight=1 if index == grow else 0)

    def _draw_section(self, parent: Any, section: Mapping[str, Any]) -> int:
        """Draw one section in its frame and answer how big its content is.

        The answer is the amount of *content* the section holds - rows or lines -
        and it is what decides which section grows when the window is resized, so
        a short section never opens a large blank area beside a long one.
        """
        columns = [
            (str(column[0]), str(column[1]), int(column[2]))
            for column in (section.get("columns") or ())
            if len(tuple(column)) >= 3
        ]
        rows = [tuple(row) for row in (section.get("rows") or ())]
        if columns and rows:
            _draw_table(parent, columns, rows)
            return min(max(len(rows), _SECTION_MIN_ROWS), _SECTION_MAX_ROWS)
        if columns:
            self._draw_note(parent, str(section.get("empty") or "No entries."))
            return 1
        lines = tuple(str(line) for line in (section.get("lines") or ()))
        if not lines:
            self._draw_note(
                parent, str(section.get("empty") or "Nothing to show.")
            )
            return 1
        _draw_text(parent, lines)
        return min(max(len(lines), _SECTION_MIN_LINES), _SECTION_MAX_LINES)

    def _draw_note(self, parent: Any, text: str) -> None:
        """A compact one-line empty state instead of a large blank widget."""
        label = ttk.Label(parent, text=text, wraplength=_NOTE_WRAP, justify="left")
        label.grid(row=0, column=0, sticky="nw")
        self._wrapping.append(label)

    # -- the width the text is wrapped to ----------------------------------

    def _wrap_text(self, _event: Any = None) -> None:
        """Wrap every note to this window's own width, so nothing is cut off.

        Tk wraps a label at a fixed ``wraplength``, and a window is resizable, so
        the length is recomputed from the width the window actually has - on every
        configure, and again as soon as a spec is drawn.
        """
        try:
            width = int(self.winfo_width())
        except tk.TclError:  # pragma: no cover - a window being torn down
            return
        if width <= 1:
            return  # not laid out yet: the next <Configure> comes with a width
        wraplength = max(width - 2 * POPUP_PADDING - 24, 200)
        for label in self._wrapping:
            try:
                label.configure(wraplength=wraplength)
            except tk.TclError:  # pragma: no cover - the widget is gone
                continue

    def _draw_bar(
        self, row: int, actions: Sequence[Mapping[str, Any]]
    ) -> None:
        """The action bar: the view's buttons left, the closing pair right.

        The order never changes between windows: ``[ Copy ] [ Refresh ] [ ... ]``
        on the left, then the spacer, then ``[ Close ]`` - or, on a decision
        window, ``[ Cancel ] [ Primary ]`` in exactly that order.
        """
        bar = ttk.Frame(self._body)
        bar.grid(row=row, column=0, sticky="ew", pady=(POPUP_ROW_GAP, 0))
        left: list[tuple[str, str, Mapping[str, Any]]] = []
        if self._include_copy:
            left.append((POPUP_ACTION_COPY, "Copy", {"key": POPUP_ACTION_COPY}))
        if self._on_refresh is not None:
            left.append(
                (POPUP_ACTION_REFRESH, "Refresh", {"key": POPUP_ACTION_REFRESH})
            )
        for action in actions:
            role = str(action.get("role") or ROLE_SECONDARY)
            if role == ROLE_PRIMARY:
                if self._primary is None:
                    self._primary = action
                continue
            if role == ROLE_CANCEL:
                if self._cancel is None:
                    self._cancel = action
                continue
            left.append((str(action.get("key") or ""), _label_of(action), action))
        right: list[tuple[str, str, Mapping[str, Any]]] = []
        if self._primary is None and self._cancel is None:
            right.append((POPUP_ACTION_CLOSE, "Close", {"key": POPUP_ACTION_CLOSE}))
        else:
            if self._cancel is not None:
                right.append(
                    (
                        str(self._cancel.get("key") or ""),
                        _label_of(self._cancel),
                        self._cancel,
                    )
                )
            if self._primary is not None:
                right.append(
                    (
                        str(self._primary.get("key") or ""),
                        _label_of(self._primary),
                        self._primary,
                    )
                )
        spacer = len(left)
        bar.columnconfigure(spacer, weight=1)
        for index, (key, label, action) in enumerate(left):
            self._button(
                bar, index, key, label, action, sticky="w", padx=(0, POPUP_ROW_GAP)
            )
        for offset, (key, label, action) in enumerate(right):
            self._button(
                bar,
                spacer + 1 + offset,
                key,
                label,
                action,
                sticky="e",
                padx=(POPUP_ROW_GAP, 0),
            )

    def _button(
        self,
        parent: Any,
        column: int,
        key: str,
        label: str,
        action: Mapping[str, Any],
        *,
        sticky: str,
        padx: tuple[int, int],
    ) -> None:
        """One button, in the one style the bar uses, disabled when told so."""
        button = ttk.Button(
            parent, text=label, command=lambda item=action: self._press(item)
        )
        button.grid(row=0, column=column, sticky=sticky, padx=padx)
        if action.get("enabled") is False:
            button.state(["disabled"])
        if key:
            self._buttons[key] = button

    def _press(self, action: Mapping[str, Any]) -> None:
        """Handle one press: the framework's own keys first, then the caller's."""
        key = str(action.get("key") or "")
        if key == POPUP_ACTION_CLOSE:
            self.close()
            return
        if key == POPUP_ACTION_COPY:
            self._copy()
            return
        if key == POPUP_ACTION_REFRESH:
            if self._on_refresh is not None:
                self._on_refresh(self)
            return
        if key and self._on_action is not None:
            self._on_action(key)
        if bool(action.get("closes")):
            # Straight out: an action that closes must never fall through to the
            # cancel side of the window it is confirming.
            self._finish()

    def _copy(self) -> None:
        """Put the window's content on the clipboard; never fatal."""
        try:
            self.clipboard_clear()
            self.clipboard_append(self.content_text())
        except tk.TclError:  # pragma: no cover - no clipboard on this display
            return
        self._flash(POPUP_ACTION_COPY, "Copied")

    def _flash(self, key: str, text: str) -> None:
        """Say what just happened on the button itself, for a moment."""
        button = self._buttons.get(key)
        if button is None:
            return
        try:
            original = str(button.cget("text"))
            button.configure(text=text)
            self.after(
                _COPY_FEEDBACK_MS, lambda: self._unflash(button, original)
            )
        except tk.TclError:  # pragma: no cover - a window being torn down
            pass

    def _unflash(self, button: Any, text: str) -> None:
        """Put a button's own label back after a flash."""
        if self._closed:
            return
        try:
            button.configure(text=text)
        except tk.TclError:  # pragma: no cover - the widget is gone
            pass

    # -- closing -----------------------------------------------------------

    def close(self) -> None:
        """Close the window: X, Escape and ``[ Close ]`` all land here.

        On a decision window Escape *is* Cancel - closing must never look like the
        primary answer - so the cancel side is pressed first, and the window is
        destroyed afterwards unless that press already did it.
        """
        if self._closed or self._closing:
            return
        if self._cancel is not None:
            self._closing = True
            try:
                self._press(self._cancel)
            finally:
                self._closing = False
            if self._closed:
                return
        self._finish()

    def _finish(self) -> None:
        """Tell the owner - it captures the geometry - and destroy the window."""
        self._closed = True
        callback, self._on_close = self._on_close, None
        if callback is not None:
            try:
                callback(self)
            except BaseException:  # noqa: BLE001 - a preference never blocks a close
                pass
        try:
            self.destroy()
        except tk.TclError:  # pragma: no cover - already gone
            pass

    # -- placement, focus and the keyboard ----------------------------------

    def _place(
        self,
        category: PopupSize,
        width: Optional[int],
        height: Optional[int],
        x: Optional[int],
        y: Optional[int],
        *,
        has_content: bool = True,
    ) -> None:
        """Give the window a size and a position that are on this screen.

        A window that opens with nothing but a note starts at the category's
        ``note_height`` - a one-line question does not need a 380-pixel window -
        and grows to the full height as soon as it draws a section.
        """
        try:
            screen_width = int(self.winfo_screenwidth())
            screen_height = int(self.winfo_screenheight())
        except tk.TclError:  # pragma: no cover - Tk always knows its screen
            screen_width, screen_height = POPUP_DEFAULT_WIDTH, POPUP_DEFAULT_HEIGHT
        default_height = category.height if has_content else category.note_height
        width = _pixel(width, category.width)
        height = _pixel(height, default_height)
        self._compact = not has_content and height == default_height
        width = max(min(width, screen_width), min(category.min_width, screen_width))
        height = max(
            min(height, screen_height), min(category.min_height, screen_height)
        )
        left, top = self._corner(
            width,
            height,
            _pixel(x, None),
            _pixel(y, None),
            screen_width,
            screen_height,
        )
        try:
            self.geometry(f"{width}x{height}{left:+d}{top:+d}")
        except tk.TclError:  # pragma: no cover - Tk accepts its own format
            pass

    def _grow_for_content(self) -> None:
        """Give a window that was sized for a note the room its content needs.

        An error window opens compact - one line of text does not need 380 pixels -
        and grows to its category size the moment the operator reveals its
        details. Only a height this window chose for itself is replaced: the
        operator's own size and a remembered one are never touched.
        """
        if not self._compact:
            return
        self._compact = False
        category = _size_of(self._size)
        try:
            width = int(self.winfo_width())
            height = max(int(self.winfo_height()), category.height)
            left = int(self.winfo_x())
            top = int(self.winfo_y())
            self.geometry(f"{width}x{height}{left:+d}{top:+d}")
        except tk.TclError:  # pragma: no cover - a window being torn down
            return

    def _corner(
        self,
        width: int,
        height: int,
        x: Optional[int],
        y: Optional[int],
        screen_width: int,
        screen_height: int,
    ) -> tuple[int, int]:
        """A visible top-left corner: the remembered one clamped, else centred.

        A remembered position is honoured as far as it can be - at least
        ``POPUP_MIN_ON_SCREEN_EDGE`` pixels of the window stay reachable - and a
        window with no remembered position is centred on the window that owns it,
        which is where an operator expects a details window to appear.
        """
        edge = POPUP_MIN_ON_SCREEN_EDGE
        if x is not None and y is not None:
            return (
                min(max(x, edge - width), screen_width - edge),
                min(max(y, 0), screen_height - edge),
            )
        try:
            parent_x = int(self._parent.winfo_rootx())
            parent_y = int(self._parent.winfo_rooty())
            parent_width = int(self._parent.winfo_width())
            parent_height = int(self._parent.winfo_height())
        except (tk.TclError, AttributeError):  # pragma: no cover - no parent yet
            parent_x = parent_y = parent_width = parent_height = 0
        if parent_width <= 1 or parent_height <= 1:
            left = (screen_width - width) // 2
            top = (screen_height - height) // 2
        else:
            left = parent_x + (parent_width - width) // 2
            top = parent_y + (parent_height - height) // 3
        return (
            min(max(left, 0), max(screen_width - width, 0)),
            min(max(top, 0), max(screen_height - height, 0)),
        )

    def _raise_to_front(self) -> None:
        """Show the window above the one that owns it - never fullscreen."""
        try:
            self.deiconify()
            self.lift()
        except tk.TclError:  # pragma: no cover - a window being torn down
            pass

    def _focus(self) -> None:
        """Give the window the keyboard (the first useful control is the window)."""
        try:
            self.focus_set()
        except tk.TclError:  # pragma: no cover - a window being torn down
            pass

    def _grabbed_elsewhere(self) -> bool:
        """Whether another window already holds the grab - its flow comes first."""
        try:
            holder = self._parent.grab_current()
        except (tk.TclError, AttributeError):  # pragma: no cover - no grab support
            return False
        return bool(holder) and holder is not self

    def _grab(self) -> None:
        """Take the keyboard for a decision window, once Tk has mapped it."""
        if self._closed or self._grabs >= _GRAB_ATTEMPTS:
            return
        self._grabs += 1
        try:
            self.grab_set()
        except tk.TclError:
            # A window that is not viewable yet cannot hold a grab: try again at
            # idle and then give up quietly. A lost grab costs the *modal* hint,
            # never the window.
            try:
                self.after_idle(self._grab)
            except tk.TclError:  # pragma: no cover - a window being torn down
                pass

    def _on_return(self, _event: Any = None) -> None:
        """Enter presses the primary action - but only when that is unambiguous.

        A text area or an entry that holds the focus owns Enter itself, so a
        decision window is never confirmed from inside a widget the operator is
        reading or typing in.
        """
        if self._primary is None:
            return
        try:
            focused = self.focus_get()
        except tk.TclError:  # pragma: no cover - no focus at all
            focused = None
        if isinstance(focused, (tk.Entry, tk.Text, ttk.Entry)):
            return
        self._press(self._primary)


# ---------------------------------------------------------------------------
# the decision dialog: the one popup that is modal
# ---------------------------------------------------------------------------


#: The two keys a confirmation dialog's buttons carry, so the answer is a value
#: this module owns and not a string a caller has to agree on.
CONFIRM_ACTION = "confirm"
CANCEL_ACTION = "cancel"


def confirm_dialog(
    root: tk.Misc,
    *,
    title: str,
    message: str,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    details: str = "",
) -> bool:
    """Ask one modal question and answer it: ``True`` for the confirm side.

    Modal on purpose - this is the one dialog whose answer *is* the decision, and
    nothing else in the panel may be touched until it is given. It is also the
    same window as every other popup: a title, one readable line, an optional
    detail area and ``[ Cancel ] [ Confirm ]`` in that order.

    Escape and the window's own X mean **Cancel**, and so does every path that
    does not press the confirm button: a dialog that was closed by accident must
    never have confirmed anything.
    """
    confirmed = {"value": False}
    spec: dict[str, Any] = {
        "title": str(title),
        "note": str(message),
        # An explicit empty list: a confirmation is its question plus its two
        # buttons, so it opens at the compact "note" height instead of reserving
        # a content area it will never fill.
        "sections": [],
        "actions": [
            {
                "key": CANCEL_ACTION,
                "label": str(cancel_label),
                "role": ROLE_CANCEL,
                "closes": True,
            },
            {
                "key": CONFIRM_ACTION,
                "label": str(confirm_label),
                "role": ROLE_PRIMARY,
                "closes": True,
            },
        ],
    }
    if details:
        spec["sections"] = (_text_section("Details", details.splitlines()),)
    window = open_popup(
        root,
        spec,
        size=POPUP_SIZE_SMALL,
        modal=True,
        include_copy=False,
        on_action=lambda key: confirmed.__setitem__(
            "value", key == CONFIRM_ACTION
        ),
    )
    window.wait_window()
    return bool(confirmed["value"])


# ---------------------------------------------------------------------------
# what a spec is, and how one section is drawn
# ---------------------------------------------------------------------------


def _size_of(size: Any) -> PopupSize:
    """The named category, or the default one when the name is unknown."""
    return POPUP_SIZES.get(str(size), POPUP_SIZES[POPUP_DEFAULT_SIZE])


def _pixel(value: Any, fallback: Optional[int]) -> Optional[int]:
    """A pixel count from an int, or the fallback for anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _sections_of(spec: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """A spec's sections, or one honest empty state when it names none.

    A spec that carries *no* ``sections`` key still gets one visible section, so a
    window opened without content says so instead of showing a blank pane; a spec
    that carries an empty list renders its note alone - that is a confirmation or
    a notice, and its note *is* its whole content.
    """
    declared = spec.get("sections")
    if declared is None:
        return ({"title": "Details", "empty": "Nothing to show."},)
    if isinstance(declared, (str, bytes)) or not isinstance(declared, Sequence):
        return ()
    return tuple(section for section in declared if isinstance(section, Mapping))


def _actions_of(spec: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """A spec's usable actions: a mapping with a label, in the order given."""
    declared = spec.get("actions")
    if isinstance(declared, (str, bytes)) or not isinstance(declared, Sequence):
        return ()
    return tuple(
        action
        for action in declared
        if isinstance(action, Mapping) and str(action.get("label") or "")
    )


def _label_of(action: Mapping[str, Any]) -> str:
    """One action's button label."""
    return str(action.get("label") or action.get("key") or "")


def _text_section(
    title: str, lines: Sequence[str], *, empty: str = ""
) -> dict[str, Any]:
    """One text section in the popup contract's own shape."""
    cleaned = [str(line) for line in lines]
    if not any(line.strip() for line in cleaned):
        cleaned = []
    return {
        "title": str(title),
        "columns": [],
        "rows": [],
        "lines": cleaned,
        "empty": str(empty or "Nothing to show."),
    }


def _draw_text(parent: Any, lines: Sequence[str]) -> None:
    """A read-only text block that scrolls when it is longer than it is tall.

    Its height follows its own content (within bounds), so a three-line section is
    three lines tall and a paragraph is not squeezed into a two-line slit.
    """
    height = min(max(len(lines), _SECTION_MIN_LINES), _SECTION_MAX_LINES)
    text = scrolledtext.ScrolledText(parent, wrap="word", height=height)
    text.grid(row=0, column=0, sticky="nsew")
    text.insert("1.0", "\n".join(lines))
    text.configure(state="disabled")


def _draw_table(
    parent: Any,
    columns: Sequence[tuple[str, str, int]],
    rows: Sequence[Sequence[str]],
) -> None:
    """A read-only table that scrolls in both directions."""
    keys = tuple(key for key, _heading, _width in columns)
    height = min(max(len(rows), _SECTION_MIN_ROWS), _SECTION_MAX_ROWS)
    table = ttk.Treeview(parent, columns=keys, show="headings", height=height)
    for key, heading, width in columns:
        table.heading(key, text=heading)
        table.column(key, width=width, anchor="w")
    vertical = ttk.Scrollbar(parent, orient="vertical", command=table.yview)
    horizontal = ttk.Scrollbar(parent, orient="horizontal", command=table.xview)
    table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    table.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    for row in rows:
        table.insert("", "end", values=tuple(row)[: len(keys)])

