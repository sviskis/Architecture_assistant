"""Global category selection and management - the panel's startup surface.

The global category catalog exists **before** any project, so this module is the
first thing an operator meets when they start a *new* project: choose an enabled
category (mandatory), or - when none is enabled yet - enter the first-run
category-management state and register/enable one. An existing (legacy) project
opens without a category.

It reaches the core exclusively through
:mod:`architecture_assistant.composition` (``open_category_catalog``): the panel
itself writes nothing and holds no repository. Every mutation is an audited core
call that needs an explicit actor and reason. All the presentation logic is pure
and lives in the ``*_rows``/``category_label``/``selection_for`` helpers, so it
is testable without a display.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox

from architecture_assistant.composition import open_category_catalog

from .theme import COLORS, FONT, button as dark_button

__all__ = [
    "FIRST_RUN_MESSAGE",
    "category_rows",
    "category_label",
    "selectable_rows",
    "selection_for",
    "needs_category",
    "load_category_rows",
    "show_category_manager",
]


#: What the panel says when a new project cannot be created yet.
FIRST_RUN_MESSAGE = (
    "No category is enabled yet. A new project must start from an enabled "
    "category: its SPEC.md must be present and valid (a fenced JSON metadata "
    "block whose declared sha256 content hash matches the document) and its "
    "LIBRARY and SUPERVISOR_RULES.md must exist.\n"
    "Register and enable one in the category manager, then try again."
)


def category_rows(catalog: Any) -> list[dict[str, Any]]:
    """Plain, presentation-ready rows for every registered category."""
    rows: list[dict[str, Any]] = []
    for view in catalog.categories():
        rows.append(
            {
                "id": view.category.id,
                "name": view.category.name,
                "version": view.category.version,
                "state": view.state.value,
                "diagnostic": view.diagnostic,
                "selectable": view.selectable,
                "registry_hash": view.category.registry_hash,
            }
        )
    return rows


def selectable_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Only the rows a new project may choose."""
    return [dict(row) for row in rows if row.get("selectable")]


def needs_category(rows: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the first-run/category-management state is required."""
    return not selectable_rows(rows)


def category_label(row: Mapping[str, Any]) -> str:
    """One readable line: name, id, version and - when not enabled - why."""
    label = (
        f"{row.get('name') or row.get('id')}  "
        f"({row.get('id')} v{row.get('version')})"
    )
    state = str(row.get("state") or "")
    if state and state != "ENABLED":
        label = f"{label}  -  {state}"
    diagnostic = str(row.get("diagnostic") or "")
    if diagnostic:
        label = f"{label}: {diagnostic}"
    return label


def selection_for(row: Mapping[str, Any]) -> dict[str, str]:
    """The traceability contract a new project records for one chosen row."""
    return {
        "category_id": str(row["id"]),
        "category_version": str(row["version"]),
        "registry_hash": str(row["registry_hash"]),
    }


def load_category_rows() -> list[dict[str, Any]]:
    """Open the global catalog, read every row, close it again.

    Fail-closed by design: a
    :class:`~architecture_assistant.ports.global_paths.GlobalPathsError` travels
    to the caller, which refuses to continue rather than guessing a location.
    """
    wired = open_category_catalog()
    try:
        return category_rows(wired.catalog)
    finally:
        wired.close()


def _run_catalog_action(
    action: Callable[[Any, str, str], Any],
    *,
    actor: str,
    reason: str,
) -> None:
    """Run one audited catalog mutation (actor + reason), then close the catalog."""
    wired = open_category_catalog()
    try:
        action(wired.catalog, actor, reason)
    finally:
        wired.close()


def _apply(catalog: Any, action_key: str, target: str, actor: str, reason: str) -> Any:
    """Map one management verb onto the audited catalog API."""
    if action_key == "enable":
        return catalog.enable(target, actor=actor, reason=reason)
    if action_key == "disable":
        return catalog.disable(target, actor=actor, reason=reason)
    raise ValueError(f"unknown management action {action_key!r}")


def show_category_manager(
    parent: Any,
    *,
    actor: str = "",
    on_change: Optional[Callable[[list], None]] = None,
) -> Any:
    """The minimal category-management surface: register / list / enable / disable.

    Every action is an audited call that needs the actor and the reason typed in
    the header; the panel never writes anything itself.
    """
    window = ctk.CTkToplevel(parent, fg_color=COLORS["shell"])
    window.title("Global categories")
    window.geometry("940x660")
    window.minsize(720, 500)

    body = ctk.CTkFrame(window, fg_color=COLORS["shell"], corner_radius=0)
    body.pack(fill="both", expand=True, padx=16, pady=16)

    ctk.CTkLabel(
        body,
        text="Global categories · register, enable, disable",
        text_color=COLORS["primary"],
        font=(FONT, 27, "bold"),
        anchor="w",
    ).pack(fill="x")

    header = ctk.CTkFrame(body, fg_color=COLORS["shell"], corner_radius=0)
    header.pack(fill="x", pady=(10, 6))
    fields: dict[str, tk.StringVar] = {
        key: tk.StringVar(window, value=value)
        for key, value in (("actor", actor), ("reason", ""))
    }
    for key, label in (("actor", "Operator"), ("reason", "Reason")):
        ctk.CTkLabel(header, text=label, text_color=COLORS["secondary"]).pack(
            side="left", padx=(0, 6)
        )
        ctk.CTkEntry(
            header,
            textvariable=fields[key],
            fg_color=COLORS["field"],
            border_color=COLORS["border"],
            text_color=COLORS["primary"],
            width=240,
        ).pack(side="left", padx=(0, 14))

    listing = tk.Listbox(
        body,
        height=12,
        bg=COLORS["field"],
        fg=COLORS["primary"],
        selectbackground=COLORS["primary"],
        highlightthickness=0,
    )
    listing.pack(fill="both", expand=True, pady=(6, 6))

    status = ctk.CTkLabel(body, text="", text_color=COLORS["muted"], anchor="w")
    status.pack(fill="x")

    state: dict[str, list] = {"rows": []}
    values: dict[str, tk.StringVar] = {
        key: tk.StringVar(window, value=value)
        for key, value in (("id", ""), ("name", ""), ("version", "1.0"))
    }

    def refresh(rows: Optional[list] = None) -> None:
        try:
            current = load_category_rows() if rows is None else rows
        except Exception as error:  # noqa: BLE001 - surfaced, never fatal
            messagebox.showerror("Categories", str(error), parent=window)
            return
        state["rows"] = current
        listing.delete(0, "end")
        for row in current:
            listing.insert("end", category_label(row))
        status.configure(text=f"{len(current)} registered")
        if on_change is not None:
            on_change(current)

    def selected_id() -> Optional[str]:
        picked = listing.curselection()
        if not picked:
            return None
        return str(state["rows"][picked[0]]["id"])

    def mutate(action_key: str) -> None:
        who = fields["actor"].get().strip()
        why = fields["reason"].get().strip()
        if not who or not why:
            messagebox.showerror(
                "Categories", "Enter the operator and a reason.", parent=window
            )
            return
        target = selected_id()
        if target is None:
            messagebox.showerror(
                "Categories", "Select a category first.", parent=window
            )
            return
        try:
            _run_catalog_action(
                lambda catalog, a, r: _apply(catalog, action_key, target, a, r),
                actor=who,
                reason=why,
            )
        except Exception as error:  # noqa: BLE001 - fail closed, say why
            messagebox.showerror("Categories", str(error), parent=window)
            return
        fields["reason"].set("")
        refresh()


    def register() -> None:
        who = fields["actor"].get().strip()
        why = fields["reason"].get().strip()
        category_id = values["id"].get().strip()
        if not who or not why:
            messagebox.showerror(
                "Categories", "Enter the operator and a reason.", parent=window
            )
            return
        try:
            _run_catalog_action(
                lambda catalog, a, r: catalog.register(
                    category_id=category_id,
                    name=values["name"].get().strip() or category_id,
                    version=values["version"].get().strip() or "1.0",
                    actor=a,
                    reason=r,
                ),
                actor=who,
                reason=why,
            )
        except Exception as error:  # noqa: BLE001 - fail closed, say why
            messagebox.showerror("Categories", str(error), parent=window)
            return
        fields["reason"].set("")
        values["id"].set("")
        values["name"].set("")
        refresh()

    creator = ctk.CTkFrame(body, fg_color=COLORS["shell"], corner_radius=0)
    creator.pack(fill="x", pady=(6, 6))
    for key, label in (
        ("id", "New id"),
        ("name", "Display name"),
        ("version", "Version"),
    ):
        ctk.CTkLabel(creator, text=label, text_color=COLORS["secondary"]).pack(
            side="left", padx=(0, 6)
        )
        ctk.CTkEntry(
            creator,
            textvariable=values[key],
            fg_color=COLORS["field"],
            border_color=COLORS["border"],
            text_color=COLORS["primary"],
            width=150,
        ).pack(side="left", padx=(0, 14))
    dark_button(creator, "Register", register).pack(side="left")

    actions = ctk.CTkFrame(body, fg_color=COLORS["shell"], corner_radius=0)
    actions.pack(fill="x", pady=(6, 0))
    dark_button(actions, "Enable", lambda: mutate("enable")).pack(side="left", padx=6)
    dark_button(actions, "Disable", lambda: mutate("disable")).pack(side="left", padx=6)
    dark_button(actions, "Refresh", lambda: refresh(None)).pack(side="left", padx=6)
    dark_button(actions, "Close", window.destroy).pack(side="right", padx=6)

    window.bind("<Escape>", lambda _event: window.destroy())
    refresh(None)
    return window
