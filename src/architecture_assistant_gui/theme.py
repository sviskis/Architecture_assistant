"""One dark visual language for CustomTkinter and the remaining ttk widgets."""
from __future__ import annotations

import customtkinter as ctk
from tkinter import ttk

COLORS = {
    "shell": "#0f1117",
    "sidebar": "#0d1018",
    "card": "#161925",
    "card_hover": "#1d2231",
    "field": "#111521",
    "primary": "#e2e8f0",
    "secondary": "#a0aec0",
    "muted": "#4a5568",
    "accent": "#63b3ed",
    "accent_hover": "#4299e1",
    "border": "#2d3748",
    "green": "#68d391",
    "amber": "#f6ad55",
    "purple": "#b794f4",
    "danger": "#fc8181",
    "selection": "#26364a",
}

FONT = "Segoe UI"


def configure(root) -> None:
    """Apply the theme before the first window paint."""
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    ctk.set_window_scaling(1.0)
    ctk.set_widget_scaling(1.0)
    root.configure(fg_color=COLORS["shell"])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=COLORS["shell"], foreground=COLORS["primary"],
                    fieldbackground=COLORS["field"], bordercolor=COLORS["border"],
                    lightcolor=COLORS["border"], darkcolor=COLORS["border"], font=(FONT, 10))
    style.configure("TFrame", background=COLORS["shell"])
    style.configure("Card.TFrame", background=COLORS["card"], relief="flat")
    style.configure("TLabel", background=COLORS["shell"], foreground=COLORS["primary"])
    style.configure("Muted.TLabel", background=COLORS["shell"], foreground=COLORS["secondary"])
    style.configure("TLabelframe", background=COLORS["card"], bordercolor=COLORS["border"], relief="solid")
    style.configure("TLabelframe.Label", background=COLORS["card"], foreground=COLORS["secondary"], font=(FONT, 9, "bold"))
    style.configure("TButton", background=COLORS["card"], foreground=COLORS["primary"],
                    bordercolor=COLORS["border"], padding=(9, 6), relief="flat")
    style.map("TButton", background=[("active", COLORS["card_hover"]), ("disabled", COLORS["shell"])],
              foreground=[("disabled", COLORS["muted"])] )
    style.configure("Accent.TButton", background=COLORS["card"], foreground=COLORS["accent"], bordercolor=COLORS["accent"])
    style.configure("Treeview", background=COLORS["card"], fieldbackground=COLORS["card"],
                    foreground=COLORS["primary"], bordercolor=COLORS["border"], rowheight=27)
    style.map("Treeview", background=[("selected", COLORS["selection"])], foreground=[("selected", COLORS["primary"])])
    style.configure("Treeview.Heading", background=COLORS["field"], foreground=COLORS["secondary"],
                    bordercolor=COLORS["border"], relief="flat", padding=6)
    style.configure("TNotebook", background=COLORS["shell"], borderwidth=0)
    style.configure("TNotebook.Tab", background=COLORS["shell"], foreground=COLORS["secondary"], padding=(12, 7), borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", COLORS["shell"])], foreground=[("selected", COLORS["accent"])])
    style.configure("Workspace.TNotebook", background=COLORS["shell"], borderwidth=0)
    style.configure("Workspace.TNotebook.Tab", background=COLORS["shell"], foreground=COLORS["secondary"], padding=(13, 7))
    style.map("Workspace.TNotebook.Tab", foreground=[("selected", COLORS["accent"])])
    style.configure("TEntry", fieldbackground=COLORS["field"], foreground=COLORS["primary"], insertcolor=COLORS["primary"])
    style.configure("TCombobox", fieldbackground=COLORS["field"], background=COLORS["field"], foreground=COLORS["primary"], arrowcolor=COLORS["secondary"])
    style.configure("Vertical.TScrollbar", background=COLORS["card"], troughcolor=COLORS["shell"], arrowcolor=COLORS["secondary"])


class DarkButton(ctk.CTkButton):
    """CTk button with the tiny ttk state surface used by the existing view."""
    def state(self, flags=None):
        if flags is None:
            return ("disabled",) if self.cget("state") == "disabled" else ()
        disabled = "disabled" in flags and "!disabled" not in flags
        self.configure(state="disabled" if disabled else "normal")
        return ()


def button(parent, text, command, *, primary=False, width=0):
    return DarkButton(parent, text=text, command=command, width=width,
                         height=34, corner_radius=5, border_width=1,
                         fg_color=COLORS["card"], hover_color=COLORS["card_hover"],
                         border_color=COLORS["accent"] if primary else COLORS["border"],
                         text_color=COLORS["accent"] if primary else COLORS["primary"],
                         font=(FONT, 12))
