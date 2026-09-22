"""The remembered window layout: one small JSON file, never the database.

The operator panel is laid out with splitters, so *how* it looks is the
operator's decision: the window size and position, the sash position of every
splitter, the tab that was open, and the size and position of each popup window.
This module is the only place that reads or writes those preferences.

Three rules shape it:

* **presentation only.** A layout holds numbers, one tab title and one geometry
  per popup window - nothing an action needs. It never holds an operator name, a
  token, a path, a project, a step or any other secret or domain state, and it
  never goes into the assistant's SQLite source of truth: a *preference* is not
  domain state, the core must be able to run with no layout file at all, and a
  corrupt file must never be able to reach the workflow.
* **fail-safe.** A missing, unreadable or hand-mangled file is worth exactly
  one thing: the panel's default layout. Every function here either returns
  validated data or answers "nothing to restore"; nothing in this module can
  raise into the GUI.
* **self-contained.** The file is written to a temporary neighbour and then
  moved into place, so an interrupted save can never leave a half-written file
  that the next start would have to guess about.

``data/gui_layout.json`` is the default location: the assistant's runtime
``data/`` directory (ignored by Git, like the default database next to it), in
a file of its own so that the remembered layout and the remembered operator
name can never overwrite each other.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "DEFAULT_LAYOUT_PATH",
    "LAYOUT_FILENAME",
    "LAYOUT_VERSION",
    "MAX_POPUPS",
    "MAX_SASH_POSITION",
    "MAX_SASHES",
    "MAX_SPLIT_NAME",
    "MAX_TAB_TITLE",
    "MAX_WINDOW_OFFSET",
    "MAX_WINDOW_SIZE",
    "MIN_WINDOW_SIZE",
    "format_geometry",
    "load_layout",
    "normalise_layout",
    "parse_geometry",
    "save_layout",
]

#: The file name of the remembered layout.
LAYOUT_FILENAME = "gui_layout.json"

#: Where the remembered layout lives: the assistant's runtime data directory,
#: which is already ignored by Git (``data/`` in ``.gitignore``).
DEFAULT_LAYOUT_PATH: Path = Path("data") / LAYOUT_FILENAME

#: Schema version of the file. A file that declares a *different* version is
#: refused as a whole: this panel cannot know what a future layout means, and
#: guessing would be worse than starting with the default layout.
LAYOUT_VERSION = 1

#: Plausible window bounds, in pixels. A size outside these is not a layout a
#: human produced on a screen, so it is treated as a damaged value.
MIN_WINDOW_SIZE = 200
MAX_WINDOW_SIZE = 20_000

#: How far a remembered window position may sit from the screen origin. A
#: larger offset is dropped (the size is still used; see the window itself).
MAX_WINDOW_OFFSET = 100_000

#: The most sashes one splitter can have, and the largest believable position.
MAX_SASHES = 8
MAX_SASH_POSITION = 100_000

#: How many popup windows are remembered. The cap is deliberately larger than the
#: number of windows the panel can open (sixteen: logs, audit, risks, reports,
#: cost, the judge, the conflicts, three advisor details, the supervisor, the
#: project, the architecture details, the monitor snapshot, the plan preview and
#: the error window), so opening every one of them still remembers every one of
#: them. The *names* are the stable window keys the panel hands in
#: (``popup.logs``, ``popup.cost``, ...): a label that gets reworded never moves a
#: window, and a window is matched to its remembered size by that key alone.
MAX_POPUPS = 24

#: The longest splitter name and tab title the file may carry.
MAX_SPLIT_NAME = 64
MAX_TAB_TITLE = 120

#: Exactly what Tk's own ``winfo_geometry`` produces: ``WIDTHxHEIGHT`` with an
#: optional ``+X+Y`` / ``-X+Y``. Nothing else is ever handed to Tk.
_GEOMETRY = re.compile(
    r"^(?P<width>[0-9]{1,6})x(?P<height>[0-9]{1,6})"
    r"(?:(?P<x>[+-][0-9]{1,6})(?P<y>[+-][0-9]{1,6}))?$"
)


def parse_geometry(
    spec: Any,
) -> Optional[tuple[int, int, Optional[int], Optional[int]]]:
    """Split a Tk geometry string into ``(width, height, x, y)`` or ``None``.

    Only what Tk itself produces is accepted, and only within plausible bounds,
    so an untrusted string is never handed to Tk's own parser. A position that
    is beyond ``MAX_WINDOW_OFFSET`` costs the position, not the size: the
    operator keeps the window they sized, the window manager places it.
    """
    if not isinstance(spec, str):
        return None
    match = _GEOMETRY.match(spec.strip())
    if match is None:
        return None
    width = int(match.group("width"))
    height = int(match.group("height"))
    if not MIN_WINDOW_SIZE <= width <= MAX_WINDOW_SIZE:
        return None
    if not MIN_WINDOW_SIZE <= height <= MAX_WINDOW_SIZE:
        return None
    if match.group("x") is None or match.group("y") is None:
        return (width, height, None, None)
    x = int(match.group("x"))
    y = int(match.group("y"))
    if abs(x) > MAX_WINDOW_OFFSET or abs(y) > MAX_WINDOW_OFFSET:
        return (width, height, None, None)
    return (width, height, x, y)


def format_geometry(
    width: int, height: int, x: Optional[int] = None, y: Optional[int] = None
) -> str:
    """The geometry string for these numbers: ``"WxH"`` or ``"WxH+X+Y"``.

    The exact shape :func:`parse_geometry` reads back, which is also the shape
    Tk's ``geometry()`` accepts - sign included, so a window on a second screen
    left of the primary one keeps its negative position.
    """
    spec = f"{int(width)}x{int(height)}"
    if x is None or y is None:
        return spec
    return f"{spec}{int(x):+d}{int(y):+d}"


def normalise_layout(raw: Any) -> dict[str, Any]:
    """Keep only what a layout file may legitimately hold; ``{}`` means "none".

    Everything is validated by *shape*, never by trust: unknown keys are
    dropped, a wrongly typed value is dropped, and a value Tk could not use is
    dropped with it. The result is always safe to hand to the window and safe
    to write back, so a hand-edited or truncated file can only ever cost the
    operator the part that was broken.
    """
    if not isinstance(raw, Mapping):
        return {}
    version = raw.get("version", LAYOUT_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        return {}
    if version != LAYOUT_VERSION:
        return {}
    cleaned: dict[str, Any] = {"version": LAYOUT_VERSION}
    geometry = raw.get("geometry")
    parsed = parse_geometry(geometry)
    if parsed is not None:
        cleaned["geometry"] = format_geometry(*parsed)
    tab = _clean_tab(raw.get("tab"))
    if tab:
        cleaned["tab"] = tab
    sashes = _clean_sashes(raw.get("sashes"))
    if sashes:
        cleaned["sashes"] = sashes
    windows = _clean_windows(raw.get("windows"))
    if windows:
        cleaned["windows"] = windows
    return cleaned


def _clean_windows(value: Any) -> dict[str, str]:
    """Every remembered popup geometry that is usable as it is.

    A popup is remembered by its *stable window key* - ``popup.logs``, never a
    widget path and never a title - and only by the same validated
    ``WxH[+X+Y]`` shape the main window uses. A key that is not printable, a
    geometry Tk could not accept, or more entries than the cap are all simply
    dropped: a damaged preference costs the operator one popup's position and
    nothing else.
    """
    if not isinstance(value, Mapping):
        return {}
    cleaned: dict[str, str] = {}
    for window_key, geometry in value.items():
        if not isinstance(window_key, str) or not window_key:
            continue
        if len(window_key) > MAX_TAB_TITLE or not window_key.isprintable():
            continue
        parsed = parse_geometry(geometry)
        if parsed is None:
            continue
        if len(cleaned) >= MAX_POPUPS:
            break
        cleaned[window_key] = format_geometry(*parsed)
    return cleaned


def _clean_tab(value: Any) -> str:
    """The remembered tab title, or ``""`` when it is not a usable title."""
    if not isinstance(value, str):
        return ""
    title = value.strip()
    if not title or len(title) > MAX_TAB_TITLE:
        return ""
    if any(character in title for character in "\r\n\t"):
        return ""
    return title


def _clean_sashes(value: Any) -> dict[str, list[int]]:
    """Every splitter's sash positions that are usable as they are.

    One splitter's list is kept whole or not at all: a partly readable list
    would move a sash the operator never touched, which is worse than leaving
    that splitter on its default.
    """
    if not isinstance(value, Mapping):
        return {}
    cleaned: dict[str, list[int]] = {}
    for name, positions in value.items():
        if not isinstance(name, str) or not name:
            continue
        if len(name) > MAX_SPLIT_NAME or not name.isprintable():
            continue
        if isinstance(positions, (str, bytes)) or not isinstance(
            positions, Sequence
        ):
            continue
        if not 1 <= len(positions) <= MAX_SASHES:
            continue
        values: list[int] = []
        for position in positions:
            if isinstance(position, bool) or not isinstance(position, int):
                values = []
                break
            if not 0 < position <= MAX_SASH_POSITION:
                values = []
                break
            values.append(int(position))
        if values:
            cleaned[name] = values
    return cleaned


def load_layout(path: Any = DEFAULT_LAYOUT_PATH) -> dict[str, Any]:
    """The remembered layout, or ``{}`` for "use the panel's own defaults".

    Every failure has the same answer - the file is missing, unreadable, not
    JSON, not a JSON object, or declares a version this panel does not know -
    because a preference must never be able to stop the panel from starting.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return {}
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    return normalise_layout(raw)


def save_layout(path: Any, layout: Any) -> bool:
    """Write the layout; ``False`` when there was nothing (or no way) to write.

    The write is atomic: a temporary neighbour is written first and then moved
    over the target, so a crash or a full disk can never leave a truncated file
    behind. Nothing here raises either - a read-only or full disk simply means
    the operator keeps the layout they had, and the panel keeps running.
    """
    data = normalise_layout(layout)
    if not {"geometry", "tab", "sashes", "windows"} & data.keys():
        return False  # nothing worth a file: no window to remember yet
    target = Path(path)
    temporary = target.with_name(f"{target.name}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    except (OSError, ValueError, TypeError):
        try:
            temporary.unlink()
        except (OSError, ValueError, TypeError):
            pass
        return False
    return True
