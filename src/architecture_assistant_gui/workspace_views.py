"""The guided workspace widgets. They display data and emit intent keys only."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
import customtkinter as ctk

from .workspace import BRIEF_TEMPLATE, SEATS, SEAT_LABELS
from .theme import COLORS, FONT, button as dark_button


class WorkspaceViews:
    def _document(self, parent, *, editable=False, height=12):
        frame = ctk.CTkFrame(parent, fg_color=COLORS["card"], corner_radius=6,
                             border_width=1, border_color=COLORS["border"])
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = ctk.CTkTextbox(frame, wrap="word", font=(FONT, 18), fg_color=COLORS["field"],
                              text_color=COLORS["primary"], border_width=0,
                              scrollbar_button_color=COLORS["border"], undo=editable)
        text.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        text.configure(state="normal" if editable else "disabled")
        return frame, text

    @staticmethod
    def _set_document(widget, value):
        if widget.get("1.0", "end-1c") == value:
            return
        position = widget.yview()
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")
        widget.yview_moveto(position[0])

    def _build_navigation(self, parent):
        rail = ctk.CTkFrame(parent, width=210, fg_color=COLORS["sidebar"], corner_radius=0,
                            border_width=1, border_color=COLORS["border"])
        rail.grid(row=0, column=0, sticky="ns")
        rail.grid_propagate(False)
        ctk.CTkLabel(rail, text="PROJECT", text_color=COLORS["secondary"],
                     font=(FONT, 17, "bold")).pack(anchor="w", padx=14, pady=(15, 10))
        self._nav_buttons = {}
        for title, label in (("Brief", "1  Project brief"), ("Claude Chat", "Consultation (Claude)"), ("Deliberation", "2  Discussion"),
                             ("Architecture Proposal", "3  Architecture"), ("Execution Plan", "4  Execution plan"),
                             ("Cline", "5  Coding / Cline"), ("Monitor", "6  Progress & checks"),
                             ("Agents", "Agent settings"), ("Architecture Review", "Quick review"),
                             ("Supervisor", "Supervisor")):
            button = dark_button(rail, label, lambda t=title: self.select_tab(t), width=180)
            button.configure(anchor="w", fg_color=COLORS["sidebar"], border_color=COLORS["sidebar"])
            button.pack(fill="x", padx=9, pady=2)
            self._nav_buttons[title] = button
        ctk.CTkFrame(rail, height=1, fg_color=COLORS["border"]).pack(fill="x", padx=12, pady=12)
        dark_button(rail, "New / Open Project", lambda: self._on_action("new_project"), width=180).pack(fill="x", padx=9)
        dark_button(rail, "User guide", lambda: self._on_action("open_guide"), width=180).pack(fill="x", padx=9, pady=5)

    def _build_brief_tab(self, notebook):
        frame = ctk.CTkFrame(notebook, fg_color=COLORS["shell"], corner_radius=0)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        ctk.CTkLabel(frame, text="What would you like to build?", text_color=COLORS["primary"],
                     font=(FONT, 30, "bold")).grid(row=0, sticky="w", padx=14, pady=(14, 0))
        ctk.CTkLabel(frame, text="Describe the goal, existing project, constraints and acceptance criteria. The architects receive this text.",
                     text_color=COLORS["secondary"], wraplength=720).grid(row=1, sticky="ew", padx=14, pady=8)
        editor, self._brief_editor = self._document(frame, editable=True)
        editor.grid(row=2, column=0, sticky="nsew", padx=14)
        self._brief_editor.bind("<FocusOut>", lambda _event: self._on_proposal_requirement(self.brief_value()))
        bar = ctk.CTkFrame(frame, fg_color=COLORS["shell"], corner_radius=0)
        bar.grid(row=3, sticky="ew", padx=14, pady=8)
        dark_button(bar, "Insert template", self._insert_brief_template).pack(side="left")
        dark_button(bar, "Save brief", lambda: self._on_action("save_brief")).pack(side="left", padx=6)
        dark_button(bar, "Choose architects →", lambda: self._on_action("nav:Agents"), primary=True).pack(side="right")
        self._brief_rendered = ""
        notebook.add(frame, text="Brief")

    def _insert_brief_template(self):
        if not self.brief_value().strip():
            self._brief_editor.insert("1.0", BRIEF_TEMPLATE)
            self._on_proposal_requirement(self.brief_value())

    def brief_value(self):
        return self._brief_editor.get("1.0", "end-1c")

    def execution_plan_value(self):
        return self._execution_editor.get("1.0", "end-1c")

    def _build_chat_tab(self, notebook):
        frame = ctk.CTkFrame(notebook, fg_color=COLORS["shell"], corner_radius=0)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        header = ctk.CTkFrame(frame, fg_color=COLORS["shell"], corner_radius=0)
        header.grid(row=0, sticky="ew", padx=14, pady=(14, 0))
        ctk.CTkLabel(header, text="Consultation with Claude", text_color=COLORS["primary"],
                     font=(FONT, 30, "bold")).pack(side="left")
        dark_button(header, "New conversation", lambda: self._on_action("chat_new")).pack(side="right")
        ctk.CTkLabel(frame, text="Ask Claude about your project, constraints or risks. Hand over chosen answers to the lead architect.",
                     text_color=COLORS["secondary"], wraplength=720).grid(row=1, sticky="ew", padx=14, pady=8)
        history_frame = ctk.CTkFrame(frame, fg_color=COLORS["card"], corner_radius=6,
                                     border_width=1, border_color=COLORS["border"])
        history_frame.grid(row=2, column=0, sticky="nsew", padx=14)
        history_frame.columnconfigure(0, weight=1)
        history_frame.rowconfigure(0, weight=1)
        self._chat_history_text = ctk.CTkTextbox(history_frame, wrap="word", font=(FONT, 16),
                                                 fg_color=COLORS["field"],
                                                 text_color=COLORS["primary"], border_width=0,
                                                 scrollbar_button_color=COLORS["border"])
        self._chat_history_text.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        self._chat_history_text.configure(state="disabled")
        input_bar = ctk.CTkFrame(frame, fg_color=COLORS["shell"], corner_radius=0)
        input_bar.grid(row=3, sticky="ew", padx=14, pady=12)
        input_bar.columnconfigure(0, weight=1)
        self._chat_input_field = ctk.CTkEntry(input_bar, placeholder_text="Ask a question...",
                                              font=(FONT, 16), fg_color=COLORS["field"],
                                              text_color=COLORS["primary"], border_width=1,
                                              border_color=COLORS["border"])
        self._chat_input_field.grid(row=0, column=0, sticky="ew")
        self._chat_input_field.bind("<Return>", lambda _event: self._on_action("chat_ask"))
        self._chat_send_button = dark_button(input_bar, "Send", lambda: self._on_action("chat_ask"), primary=True)
        self._chat_send_button.grid(row=0, column=1, padx=(10, 0))
        
        self._chat_message_selector = ttk.Combobox(input_bar, state="readonly", width=40)
        self._chat_message_selector.grid(row=0, column=2, padx=(10, 0))
        self._chat_message_selector.bind("<<ComboboxSelected>>", self._on_chat_message_selected)

        self._chat_handover_button = dark_button(input_bar, "Nodot galvenajam aģentam",
                                                 lambda: self._on_action("chat_handover"))
        self._chat_handover_button.grid(row=0, column=3, padx=(10, 0))
        notebook.add(frame, text="Claude Chat")

    def _on_chat_message_selected(self, _event):
        selection = self._chat_message_selector.get()
        if not selection or " - " not in selection:
             self._on_chat_selection(None)
             return
        msg_id = selection.split(" - ", 1)[0]
        self._on_chat_selection(msg_id)

    def chat_input_value(self):
        return self._chat_input_field.get()

    def set_chat_input_value(self, value):
        self._chat_input_field.delete(0, "end")
        self._chat_input_field.insert(0, value)


    def _build_agents_tab(self, notebook):
        frame = ctk.CTkFrame(notebook, fg_color=COLORS["shell"], corner_radius=0)
        frame.columnconfigure(0, weight=1)
        ctk.CTkLabel(frame, text="Architecture discussion team", text_color=COLORS["primary"], font=(FONT, 29, "bold")).grid(row=0, sticky="w")
        ctk.CTkLabel(frame, text="Separate from the three Quick Review advisors. Blank key keeps the stored credential; model may be left at its provider default.",
                     text_color=COLORS["secondary"], wraplength=760).grid(row=1, sticky="ew", pady=(6, 12))
        self._agent_fields = {}
        for index, slot in enumerate(SEATS):
            card = ctk.CTkFrame(frame, fg_color=COLORS["card"], corner_radius=7,
                                border_width=1, border_color=COLORS["border"])
            card.grid(row=index + 2, column=0, sticky="ew", pady=5)
            card.columnconfigure(1, weight=1)
            card.columnconfigure(3, weight=1)
            ctk.CTkLabel(card, text=SEAT_LABELS[slot], text_color=COLORS["primary"],
                         font=(FONT, 20, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 4))
            ctk.CTkLabel(card, text="Provider", text_color=COLORS["secondary"]).grid(row=1, column=0, sticky="w", padx=10)
            provider = ttk.Combobox(card, state="readonly", width=14, style="Agent.TCombobox")
            provider.grid(row=1, column=1, sticky="ew")
            ctk.CTkLabel(card, text="Model", text_color=COLORS["secondary"]).grid(row=1, column=2, padx=8)
            model = ttk.Entry(card, width=18)
            model.grid(row=1, column=3, sticky="ew", padx=(0, 10))
            ctk.CTkLabel(card, text="API key", text_color=COLORS["secondary"]).grid(row=2, column=0, sticky="w", padx=10, pady=6)
            key = ctk.CTkEntry(card, show="*", placeholder_text="Ievadi API atslēgu",
                              fg_color=COLORS["field"], text_color=COLORS["primary"],
                              border_color=COLORS["border"])
            key.grid(row=2, column=1, columnspan=3, sticky="ew", pady=6, padx=(0, 10))
            label = ctk.CTkLabel(card, text="", text_color=COLORS["muted"])
            label.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))
            button = dark_button(card, "Test Connection", lambda s=slot: self._on_action(f"test_{s}"))
            button.grid(row=3, column=3, sticky="e", padx=10, pady=(0, 8))
            self.buttons[f"test_{slot}"] = button
            self._agent_fields[slot] = {"provider": provider, "model": model, "key": key, "label": label}
        bar = ctk.CTkFrame(frame, fg_color=COLORS["shell"], corner_radius=0)
        bar.grid(row=5, sticky="ew", pady=10)
        dark_button(bar, "Save team settings", lambda: self._on_action("save_settings"), primary=True).pack(side="left")
        dark_button(bar, "Go to discussion →", lambda: self.select_tab("Deliberation")).pack(side="right")
        notebook.add(frame, text="Agents")

    def _build_plan_tab(self, notebook):
        frame = ctk.CTkFrame(notebook, fg_color=COLORS["shell"], corner_radius=0)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        self._build_chat_tab(notebook)

        ctk.CTkLabel(frame, text="Execution plan · editable draft", text_color=COLORS["primary"],
                     font=(FONT, 29, "bold")).grid(row=0, sticky="w", padx=14, pady=(14, 0))
        ctk.CTkLabel(frame, text="Generated from your approved architecture, in dependency order. Review scope and acceptance criteria before importing. Validation changes no workflow state.",
                     text_color=COLORS["secondary"], wraplength=760).grid(row=1, sticky="ew", padx=14, pady=8)
        editor, self._execution_editor = self._document(frame, editable=True)
        editor.grid(row=2, sticky="nsew", padx=14)
        self._draft_rendered = ""
        bar = ctk.CTkFrame(frame, fg_color=COLORS["shell"], corner_radius=0)
        bar.grid(row=3, sticky="ew", padx=14, pady=8)
        dark_button(bar, "Validate & Import…", lambda: self._on_action("validate_draft"), primary=True).pack(side="right")
        dark_button(bar, "Save JSON…", lambda: self._on_action("save_plan_draft")).pack(side="left")
        notebook.add(frame, text="Execution Plan")

    def _build_cline_tab(self, notebook):
        frame = ctk.CTkFrame(notebook, fg_color=COLORS["shell"], corner_radius=0)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        ctk.CTkLabel(frame, text="Cline · task handoff", text_color=COLORS["primary"],
                     font=(FONT, 29, "bold")).grid(row=0, sticky="w", padx=14, pady=(14, 0))
        ctk.CTkLabel(frame, textvariable=self._var("channel_status"), text_color=COLORS["accent"],
                     wraplength=760).grid(row=1, sticky="ew", padx=14, pady=10)
        doc, self._channel_document = self._document(frame)
        doc.grid(row=2, sticky="nsew", padx=14)
        bar = ctk.CTkFrame(frame, fg_color=COLORS["shell"], corner_radius=0)
        bar.grid(row=3, sticky="ew", padx=14, pady=8)
        for key, label in (("copy_cline_handoff", "Copy instruction for Cline"), ("open_exchange", "Open exchange folder"), ("refresh", "Check report")):
            dark_button(bar, label, lambda k=key: self._on_action(k), primary=key == "copy_cline_handoff").pack(side="left", padx=(0, 6))
        notebook.add(frame, text="Cline")

    def _build_proposal_tab(self, notebook):
        frame = ctk.CTkFrame(notebook, fg_color=COLORS["shell"], corner_radius=0)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tabs = ttk.Notebook(frame)
        tabs.grid(row=0, sticky="nsew")
        document, self._architecture_document = self._document(tabs)
        tabs.add(document, text="Architecture document")
        self._build_proposal_details(tabs)
        tabs.tab(1, text="Technical details & history")
        # Revision feedback stays beside the readable document as well.
        feedback = ctk.CTkFrame(frame, fg_color=COLORS["card"], corner_radius=6,
                                border_width=1, border_color=COLORS["border"])
        # Claude Chat rendering
        chat_history = view.get("chat_history") or []
        chat_content = ""
        # Populate assistant message selector for handover
        assistant_msgs = [m for m in chat_history if m.get("role") == "assistant"]
        selector_values = [f"{m.get('id')} - {str(m.get('text'))[:50]}..." for m in assistant_msgs]
        if selector_values != list(self._chat_message_selector["values"]):
            self._chat_message_selector["values"] = selector_values
            if selector_values:
                # If current selection is invalid or missing, select the last one
                current_sel = self._chat_message_selector.get()
                if not current_sel or not any(v.startswith(current_sel.split(" - ")[0]) for v in selector_values):
                     self._chat_message_selector.set(selector_values[-1])
                     self._on_chat_message_selected(None)


        for msg in chat_history:
            role = str(msg.get("role", "")).upper()
            text = str(msg.get("text", ""))
            chat_content += f"{role}:\n{text}\n\n"
        
        if chat_content != self._chat_history_text.get("1.0", "end-1c"):
            self._chat_history_text.configure(state="normal")
            self._chat_history_text.delete("1.0", "end")
            self._chat_history_text.insert("1.0", chat_content)
            self._chat_history_text.see("end")
            self._chat_history_text.configure(state="disabled")
        
        chat_input = str(view.get("chat_input") or "")
        if chat_input != self.chat_input_value():
            self.set_chat_input_value(chat_input)


        feedback.grid(row=1, sticky="ew", pady=6)
        ctk.CTkLabel(feedback, text="REVISION FEEDBACK", text_color=COLORS["muted"],
                     font=(FONT, 14, "bold")).pack(anchor="w", padx=10, pady=(6, 2))
        self._revision_feedback_field = ctk.CTkTextbox(feedback, height=74, wrap="word",
                                                       fg_color=COLORS["field"], text_color=COLORS["primary"])
        self._revision_feedback_field.pack(fill="x", padx=6, pady=(0, 6))
        self._revision_feedback_field.bind("<FocusOut>", lambda _event: self._on_revision_feedback(self.revision_feedback_value()))
        notebook.add(frame, text="Architecture Proposal")

    def _render_workspace(self, view):
        workspace = view.get("workspace") or {}
        self._next_action = workspace.get("action", "nav:Brief")
        self._var("next_step_guidance").set(f"{workspace.get('stage', 'Brief')}  ·  {workspace.get('text', 'Start with your project brief.')}")
        self._next_button.configure(text=workspace.get("label", "Write project brief"))
        self._next_button.state(["disabled"] if workspace.get("busy") else ["!disabled"])
        requirement = str(workspace.get("requirement") or "")
        current = self.brief_value()
        if current in ("", self._brief_rendered):
            if current != requirement:
                self._brief_editor.delete("1.0", "end")
                self._brief_editor.insert("1.0", requirement)
        self._brief_rendered = requirement
        draft = str(view.get("execution_draft") or "")
        if draft and draft != self._draft_rendered:
            self._execution_editor.delete("1.0", "end")
            self._execution_editor.insert("1.0", draft)
            self._draft_rendered = draft
        self._set_document(self._architecture_document, str((view.get("proposal") or {}).get("document") or "No architecture yet. Start with the project brief."))
        channel = view.get("channel") or {}
        self._var("channel_status").set(channel.get("status", "No task dispatched yet."))
        content = f"{channel.get('handoff', 'Import a plan and advance the workflow to publish a task.')}\n\n"
        content += f"Exchange folder: {channel.get('exchange_dir', '—')}\n\n{channel.get('supervisor', '')}"
        self._set_document(self._channel_document, content)
        settings = view.get("agent_settings") or {}
        options = tuple(str(x.get("provider")) for x in settings.get("catalog", ()))
        for slot, fields in self._agent_fields.items():
            row = (settings.get("rows") or {}).get(slot) or {}
            fields["provider"].configure(values=options)
            for key in ("provider", "model"):
                self._refresh_field(fields, key, str(row.get(key) or ""))
            fields["key"].configure(placeholder_text="API atslēga saglabāta" if row.get("key_set") else "Ievadi API atslēgu")
            fields["label"].configure(text=("Key stored" if row.get("key_set") else "No stored key / environment fallback") + "  " + str(row.get("connection") or ""))
        for key, spec in (view.get("buttons") or {}).items():
            button = self.buttons.get(key)
            if button is not None:
                button.bind("<Enter>", lambda _event, text=spec.get("hint", ""): self._var("action_hint").set(text))
                button.bind("<Leave>", lambda _event: self._var("action_hint").set(""))
