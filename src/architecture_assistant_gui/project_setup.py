"""Project setup dialog and local workspace preferences, without core state writes.

A **new** project is bound to one enabled global category (mandatory); an existing
(legacy) project opens without one. Detection uses the assistant's existing open
mechanism and its current configured paths only - the saved
``.architecture_assistant/workspace.json`` profile or the configured project
database - never a new, invented location.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

from .category_select import (
    FIRST_RUN_MESSAGE,
    category_label,
    needs_category,
    selectable_rows,
    selection_for,
)
from .theme import COLORS, FONT, button as dark_button

#: The local state folder of a project and the names inside it. The project
#: database name is defined here once, so the workspace spec and the
#: existing/legacy-project detection use the very same configured path.
STATE_FOLDER = ".architecture_assistant"
WORKSPACE_PROFILE_FILENAME = "workspace.json"
PROJECT_DB_FILENAME = "project.db"

#: The message a new project gets when no category was chosen.
NEW_PROJECT_CATEGORY_REQUIRED = "Choose a category for the new project."


def state_dir(folder):
    """The project's local ``.architecture_assistant`` folder (never created)."""
    return Path(folder).expanduser().resolve() / STATE_FOLDER


def is_existing_project(folder):
    """Whether ``folder`` already holds this assistant's project state.

    Uses the existing open mechanism and the current configured paths - the saved
    workspace profile or the configured project database - and nothing else.
    """
    state = state_dir(folder)
    return (state / WORKSPACE_PROFILE_FILENAME).is_file() or (
        state / PROJECT_DB_FILENAME
    ).is_file()


def workspace_spec(folder, name, source, version="1.0", mode="MANUAL", category=None):
    """Derive the workspace spec; a NEW project must carry a chosen category."""
    root = Path(folder).expanduser().resolve()
    source_path = Path(source).expanduser().resolve()
    if not root.is_dir() or not source_path.is_dir():
        raise ValueError("Choose existing project and source folders.")
    if not name.strip() or not version.strip():
        raise ValueError("Project name and plan version are required.")
    if mode not in ("MANUAL", "SUPERVISED", "AUTO"):
        raise ValueError("Unknown project mode.")
    if not is_existing_project(root) and category is None:
        raise ValueError(NEW_PROJECT_CATEGORY_REQUIRED)
    selection = None if category is None else dict(category)
    state = root / STATE_FOLDER
    return {"project_name": name.strip(), "plan_version": version.strip(), "mode": mode,
            "source_root": str(source_path), "database_path": str(state / PROJECT_DB_FILENAME),
            "exchange_dir": str(state / "cline"), "report_dir": str(state / "reports"),
            "provider_settings_path": str(state / "providers.json"),
            # the traceability contract: recorded for a new project (and kept for
            # an existing one when one is chosen); None marks a legacy project
            "category": selection}


def save_preferences(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _add_category_row(body, categories, chosen, on_manage):
    """Row 6 of the dialog: the category chooser, or the first-run state.

    Only ``ENABLED`` categories are offered. When none is enabled the row states
    it and points at the category manager - a new project cannot be created until
    an operator enables one.
    """
    selectable = selectable_rows(categories)
    ctk.CTkLabel(
        body, text="Category (new projects)", text_color=COLORS["secondary"]
    ).grid(row=6, column=0, sticky="w", padx=(0, 8), pady=6)

    if selectable:
        by_label = {category_label(row): row for row in selectable}
        first = selectable[0]
        chosen["selection"] = selection_for(first)

        def picked(value):
            row = by_label.get(value)
            chosen["selection"] = None if row is None else selection_for(row)

        option = ctk.CTkOptionMenu(
            body,
            values=list(by_label),
            command=picked,
            fg_color=COLORS["field"],
            button_color=COLORS["border"],
            button_hover_color=COLORS["border"],
            text_color=COLORS["primary"],
            dropdown_fg_color=COLORS["field"],
            dropdown_text_color=COLORS["primary"],
        )
        option.grid(row=6, column=1, sticky="ew")
        option.set(category_label(first))
    else:
        ctk.CTkLabel(
            body,
            text="No category is enabled yet.",
            text_color=COLORS["muted"],
            wraplength=420,
            justify="left",
        ).grid(row=6, column=1, sticky="w")

    def manage():
        if on_manage is not None:
            on_manage()

    dark_button(body, "Categories…", manage).grid(row=6, column=2, padx=(8, 0))


def show_project_setup(parent, launch, *, actor="", brief="", categories=(), on_manage=None):
    window = ctk.CTkToplevel(parent, fg_color=COLORS["shell"])
    window.title("Start / open a project")
    window.geometry("820x780")
    window.minsize(640, 560)
    body = ctk.CTkFrame(window, fg_color=COLORS["shell"], corner_radius=0)
    body.pack(fill="both", expand=True, padx=16, pady=16)
    body.columnconfigure(1, weight=1)
    body.rowconfigure(8, weight=1)
    chosen = {"selection": None}
    values = {key: tk.StringVar(window, value=value) for key, value in (
        ("folder", ""), ("name", ""), ("source", ""), ("version", "1.0"), ("actor", actor))}
    ctk.CTkLabel(body, text="One project · one saved workspace", text_color=COLORS["primary"],
                 font=(FONT, 30, "bold")).grid(row=0, columnspan=3, sticky="w", pady=(0, 10))

    def choose_folder():
        folder = filedialog.askdirectory(parent=window, title="Project folder")
        if not folder:
            return
        values["folder"].set(folder)
        values["source"].set(folder)
        values["name"].set(Path(folder).name)
        profile = Path(folder) / ".architecture_assistant" / "workspace.json"
        if profile.is_file():
            try:
                saved = json.loads(profile.read_text(encoding="utf-8"))
                values["name"].set(saved["project_name"])
                values["version"].set(saved["plan_version"])
                values["source"].set(saved["source_root"])
            except (OSError, ValueError, KeyError, TypeError):
                messagebox.showerror("Workspace", "The saved workspace settings could not be read.", parent=window)

    for row, (key, label) in enumerate((("folder", "Project folder"), ("name", "Project name"),
                                      ("source", "Source to inspect"), ("version", "Plan version"), ("actor", "Operator")), 1):
        ctk.CTkLabel(body, text=label, text_color=COLORS["secondary"]).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=6)
        ctk.CTkEntry(body, textvariable=values[key], fg_color=COLORS["field"],
                     border_color=COLORS["border"], text_color=COLORS["primary"]).grid(row=row, column=1, sticky="ew")
    dark_button(body, "Browse…", choose_folder).grid(row=1, column=2, padx=(8, 0))

    def choose_source():
        folder = filedialog.askdirectory(parent=window, title="Source folder inspected by the configured Python rules")
        if folder:
            values["source"].set(folder)
    dark_button(body, "Browse…", choose_source).grid(row=3, column=2, padx=(8, 0))
    _add_category_row(body, categories, chosen, on_manage)
    ctk.CTkLabel(body, text="Initial prompt / brief (optional for an existing project)",
                 text_color=COLORS["secondary"]).grid(row=7, columnspan=3, sticky="w", pady=(12, 6))
    editor = ctk.CTkTextbox(body, height=200, wrap="word", undo=True,
                            fg_color=COLORS["field"], text_color=COLORS["primary"])
    editor.grid(row=8, columnspan=3, sticky="nsew")
    editor.insert("1.0", brief)
    _hint = ("New projects start in MANUAL mode and need an enabled category. "
             "Existing state is preserved.\n"
             "Settings and data live in .architecture_assistant inside the project.\n"
             "The existing gate checks Python layer rules; it does not automatically enforce an AI design.")
    if needs_category(categories):
        _hint = f"{_hint}\n\n{FIRST_RUN_MESSAGE}"
    ctk.CTkLabel(body, text=_hint,
              text_color=COLORS["muted"], wraplength=740, justify="left").grid(row=9, columnspan=3, sticky="ew", pady=12)
    actions = ctk.CTkFrame(body, fg_color=COLORS["shell"], corner_radius=0)
    actions.grid(row=10, columnspan=3, sticky="e")
    dark_button(actions, "Cancel", window.destroy).pack(side="left", padx=6)

    def confirm():
        try:
            spec = workspace_spec(
                values["folder"].get(),
                values["name"].get(),
                values["source"].get(),
                values["version"].get(),
                category=chosen["selection"],
            )
            if not values["actor"].get().strip():
                raise ValueError("Enter an operator name.")
            launch(spec, values["actor"].get().strip(), editor.get("1.0", "end-1c"))
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot open project", str(error), parent=window)
            return
        window.destroy()
    dark_button(actions, "Open project workspace", confirm, primary=True).pack(side="left")
    window.bind("<Escape>", lambda _event: window.destroy())
    return window
