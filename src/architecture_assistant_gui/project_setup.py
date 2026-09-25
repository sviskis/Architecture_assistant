"""Project setup dialog and local workspace preferences, without core state writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk

from .theme import COLORS, FONT, button as dark_button


def workspace_spec(folder, name, source, version="1.0", mode="MANUAL"):
    root = Path(folder).expanduser().resolve()
    source_path = Path(source).expanduser().resolve()
    if not root.is_dir() or not source_path.is_dir():
        raise ValueError("Choose existing project and source folders.")
    if not name.strip() or not version.strip():
        raise ValueError("Project name and plan version are required.")
    if mode not in ("MANUAL", "SUPERVISED", "AUTO"):
        raise ValueError("Unknown project mode.")
    state = root / ".architecture_assistant"
    return {"project_name": name.strip(), "plan_version": version.strip(), "mode": mode,
            "source_root": str(source_path), "database_path": str(state / "project.db"),
            "exchange_dir": str(state / "cline"), "report_dir": str(state / "reports"),
            "provider_settings_path": str(state / "providers.json")}


def save_preferences(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def show_project_setup(parent, launch, *, actor="", brief=""):
    window = ctk.CTkToplevel(parent, fg_color=COLORS["shell"])
    window.title("Start / open a project")
    window.geometry("820x740")
    window.minsize(640, 540)
    body = ctk.CTkFrame(window, fg_color=COLORS["shell"], corner_radius=0)
    body.pack(fill="both", expand=True, padx=16, pady=16)
    body.columnconfigure(1, weight=1)
    body.rowconfigure(7, weight=1)
    values = {key: tk.StringVar(window, value=value) for key, value in (
        ("folder", ""), ("name", ""), ("source", ""), ("version", "1.0"), ("actor", actor))}
    ctk.CTkLabel(body, text="One project · one saved workspace", text_color=COLORS["primary"],
                 font=(FONT, 20, "bold")).grid(row=0, columnspan=3, sticky="w", pady=(0, 10))

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
    ctk.CTkLabel(body, text="Initial prompt / brief (optional for an existing project)",
                 text_color=COLORS["secondary"]).grid(row=6, columnspan=3, sticky="w", pady=(12, 6))
    editor = ctk.CTkTextbox(body, height=220, wrap="word", undo=True,
                            fg_color=COLORS["field"], text_color=COLORS["primary"])
    editor.grid(row=7, columnspan=3, sticky="nsew")
    editor.insert("1.0", brief)
    ctk.CTkLabel(body, text="New projects start in MANUAL mode. Existing state is preserved.\n"
              "Settings and data live in .architecture_assistant inside the project.\n"
              "The existing gate checks Python layer rules; it does not automatically enforce an AI design.",
              text_color=COLORS["muted"], wraplength=740, justify="left").grid(row=8, columnspan=3, sticky="ew", pady=12)
    actions = ctk.CTkFrame(body, fg_color=COLORS["shell"], corner_radius=0)
    actions.grid(row=9, columnspan=3, sticky="e")
    dark_button(actions, "Cancel", window.destroy).pack(side="left", padx=6)

    def confirm():
        try:
            spec = workspace_spec(values["folder"].get(), values["name"].get(), values["source"].get(), values["version"].get())
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
