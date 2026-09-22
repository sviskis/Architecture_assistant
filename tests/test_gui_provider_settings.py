"""Tests for the per-advisor provider settings UI.

The panel must let the operator choose a provider and a model and enter a key -
and it must never hold, render, log or echo that key. There are two halves here:

* the **view** half, which needs a real window (skipped when Tk is unavailable):
  the provider dropdown, the model selector, a masked key entry, a Show/Hide
  toggle, and the fact that a saved key never comes back into a widget;
* the **controller** half, which is pure logic: one save action carrying exactly
  what the panes show, one Test Connection action per pane, a key-free view
  model, the explicit "provider settings invalid" status, and the guarantee that
  a typed key never reaches a log entry.
"""

from __future__ import annotations

import json
import tkinter as tk
from typing import Any, Iterator, Optional

import pytest

from architecture_assistant_gui.controller import (
    CONNECTION_INTENT_KEYS,
    INTENTS,
    SAVE_SETTINGS_INTENT,
    GuiController,
)
from architecture_assistant_gui.core import JobResult, describe_error
from architecture_assistant_gui.views import MainWindow

#: A literal that is not, and never was, a real credential.
KEY = "sk-gui-provider-settings-not-a-real-key"

CATALOG = (
    {
        "provider": "openai",
        "label": "OpenAI",
        "available": True,
        "default_model": "gpt-4.1-mini",
    },
    {
        "provider": "claude",
        "label": "Claude",
        "available": True,
        "default_model": "claude-sonnet",
    },
    {
        "provider": "grok",
        "label": "Grok",
        "available": True,
        "default_model": "grok-4.6",
    },
    {
        "provider": "deepseek",
        "label": "DeepSeek",
        "available": True,
        "default_model": "deepseek-chat",
    },
    {
        "provider": "disabled",
        "label": "Disabled",
        "available": True,
        "default_model": "",
    },
)

PROVIDERS = ("openai", "claude", "grok")
LABELS = {"openai": "OpenAI", "claude": "Claude", "grok": "Grok"}
MODELS = {"openai": "gpt-4.1-mini", "claude": "claude-sonnet", "grok": "grok-4.6"}


def settings_rows(*, key_set: tuple[bool, bool, bool] = (False, False, False)):
    """The provider-settings rows of a view model, in pane order."""
    rows = []
    for index, provider in enumerate(PROVIDERS):
        rows.append(
            {
                "slot": f"advisor_{index + 1}",
                "pane": index,
                "action": CONNECTION_INTENT_KEYS[index],
                "provider": provider,
                "provider_label": LABELS[provider],
                "model": MODELS[provider],
                "key_set": key_set[index],
                "key_masked": "********" if key_set[index] else "",
                "connection": "",
            }
        )
    return rows


def core_settings_payload(
    *,
    status: str = "loaded",
    status_text: str = "provider settings loaded",
    valid: bool = True,
    key_set: tuple[bool, bool, bool] = (False, False, False),
) -> dict[str, Any]:
    """The key-free ``provider_settings`` payload the *core* sends the panel."""
    return {
        "advisors": {
            f"advisor_{index + 1}": {
                "provider": provider,
                "provider_label": LABELS[provider],
                "model": MODELS[provider],
                "key_set": key_set[index],
                "key_masked": "********" if key_set[index] else "",
            }
            for index, provider in enumerate(PROVIDERS)
        },
        "order": ["advisor_1", "advisor_2", "advisor_3"],
        "catalog": [dict(entry) for entry in CATALOG],
        "status": status,
        "status_text": status_text,
        "valid": valid,
        "path": "data/provider_settings.json",
    }


def settings_view(
    *,
    status: str = "loaded",
    status_text: str = "provider settings loaded",
    valid: bool = True,
    key_set: tuple[bool, bool, bool] = (False, False, False),
    note: str = "",
) -> dict[str, Any]:
    """The ``provider_settings`` section of a *controller* view model."""
    return {
        "rows": settings_rows(key_set=key_set),
        "catalog": [dict(entry) for entry in CATALOG],
        "status": status,
        "status_text": status_text,
        "valid": valid,
        "path": "data/provider_settings.json",
        "note": note,
    }


def review_view(**kwargs: Any) -> dict[str, Any]:
    """A view model whose review section carries the provider settings."""
    return {
        "review": {
            "provider_panels": [
                {
                    "name": LABELS[provider],
                    "status_text": "not run yet",
                    "level": "INFO",
                    "facts": (),
                    "body_lines": (),
                }
                for provider in PROVIDERS
            ],
            "provider_settings": settings_view(**kwargs),
        }
    }


# ---------------------------------------------------------------------------
# the view half (a real window)
# ---------------------------------------------------------------------------


@pytest.fixture
def window() -> Iterator[tuple[tk.Tk, MainWindow]]:
    """A real panel window rendering the provider settings of three advisors."""
    try:
        root = tk.Tk()
    except tk.TclError as error:  # pragma: no cover - machine without a display
        pytest.skip(f"Tk cannot open a window here: {error}")
    view = MainWindow(
        root,
        on_action=lambda _key: None,
        on_actor=lambda _text: None,
        on_question=lambda _text: None,
    )
    view.render(review_view(key_set=(True, False, False)))
    root.update_idletasks()
    try:
        yield root, view
    finally:
        root.destroy()


class TestTheSettingsHeader:
    """Provider, model, key and the two buttons - one set per advisor pane."""

    def test_every_advisor_pane_has_a_provider_dropdown(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        assert len(view._panes) == 3
        for pane in view._panes:
            assert pane["provider"].winfo_exists()
            assert tuple(pane["provider"].cget("values")) == (
                "OpenAI",
                "Claude",
                "Grok",
                "DeepSeek",
                "Disabled",
            )

    def test_the_provider_dropdown_is_not_free_text(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        for pane in view._panes:
            assert str(pane["provider"].cget("state")) == "readonly"

    def test_every_advisor_pane_has_a_model_selector(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        for index, pane in enumerate(view._panes):
            assert pane["model"].winfo_exists()
            # the offered model is the *configured* default of that provider
            assert tuple(pane["model"].cget("values")) == (MODELS[PROVIDERS[index]],)
            assert pane["model"].get() == MODELS[PROVIDERS[index]]

    def test_the_model_selector_allows_manual_entry(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        pane = view._panes[0]
        pane["model"].delete(0, "end")
        pane["model"].insert(0, "a-model-the-operator-typed")

        assert view.provider_settings_values()["advisor_1"]["model"] == (
            "a-model-the-operator-typed"
        )

    def test_the_key_field_is_masked_and_never_prefilled(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        for pane in view._panes:
            assert pane["key"].winfo_exists()
            assert str(pane["key"].cget("show")) == "*"
            # a stored key is never written back into a widget
            assert pane["key"].get() == ""

    def test_the_show_toggle_reveals_only_what_is_typed(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window
        pane = view._panes[0]
        pane["key"].insert(0, KEY)

        pane["reveal"].set(True)
        view._toggle_key_reveal(0)
        assert str(pane["key"].cget("show")) == ""

        pane["reveal"].set(False)
        view._toggle_key_reveal(0)
        assert str(pane["key"].cget("show")) == "*"

    def test_the_status_line_reports_the_stored_key_without_it(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        line = view._var("pane_settings_0").get()
        assert line.startswith("key: stored")
        assert view._var("pane_settings_1").get().startswith("key: not set")
        assert KEY not in line

    def test_the_provider_status_line_shows_the_load_status(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        assert view._var("review_provider_status").get() == (
            "provider settings loaded"
        )

    def test_a_malformed_file_is_said_out_loud(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        view.render(
            review_view(
                status="invalid",
                status_text="provider settings invalid",
                valid=False,
            )
        )

        status = view._var("review_provider_status").get()
        assert "provider settings invalid" in status
        assert "provider settings invalid" in view._var("pane_settings_0").get()

    def test_the_pane_reads_back_the_provider_tokens_not_the_labels(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        assert view.provider_settings_values() == {
            "advisor_1": {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "api_key": None,
            },
            "advisor_2": {
                "provider": "claude",
                "model": "claude-sonnet",
                "api_key": None,
            },
            "advisor_3": {
                "provider": "grok",
                "model": "grok-4.6",
                "api_key": None,
            },
        }

    def test_a_typed_key_is_read_back_but_a_stored_one_is_not(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window
        view._panes[0]["key"].insert(0, KEY)

        values = view.provider_settings_values()

        assert values["advisor_1"]["api_key"] == KEY
        assert values["advisor_2"]["api_key"] is None

    def test_a_new_provider_offers_its_own_default_model(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window
        pane = view._panes[0]

        pane["provider"].set("DeepSeek")
        view._on_provider_changed(0)

        assert pane["model"].get() == "deepseek-chat"
        assert view.provider_settings_values()["advisor_1"]["provider"] == "deepseek"

    def test_clearing_the_key_entries_re_masks_everything(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window
        for pane in view._panes:
            pane["key"].insert(0, KEY)
        view._panes[1]["reveal"].set(True)
        view._toggle_key_reveal(1)

        view.clear_key_entries()

        for pane in view._panes:
            assert pane["key"].get() == ""
            assert str(pane["key"].cget("show")) == "*"
            assert pane["reveal"].get() is False

    def test_a_render_never_overwrites_a_value_the_operator_typed(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        """The pump re-renders on a timer; it must not clobber a half-typed model."""
        _root, view = window
        pane = view._panes[0]
        pane["model"].delete(0, "end")
        pane["model"].insert(0, "half-typed")

        view.render(review_view())

        assert pane["model"].get() == "half-typed"
        # ... but a field nobody touched is still refreshed from the core
        assert view._panes[1]["model"].get() == MODELS["claude"]

    def test_a_render_does_refresh_an_untouched_field(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        view.render(
            review_view(status="loaded", status_text="provider settings loaded")
        )
        assert view._panes[0]["model"].get() == "gpt-4.1-mini"

        changed = review_view()
        changed["review"]["provider_settings"]["rows"][0]["model"] = "gpt-5"
        view.render(changed)

        assert view._panes[0]["model"].get() == "gpt-5"

    def test_a_render_never_writes_a_key_into_a_widget(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        _root, view = window

        for pane in view._panes:
            assert pane["key"].get() == ""
        rendered = json.dumps(
            {name: var.get() for name, var in view._text.items()}, default=str
        )
        assert KEY not in rendered

    def test_the_settings_header_stays_compact(
        self, window: tuple[tk.Tk, MainWindow]
    ) -> None:
        """The result still owns the pane: the header is a few rows and a button.

        The pane shows the compact summary - the four rows in ``facts``, the
        summary line and the two configuration buttons plus ``Details...`` - so
        the header can never push the result it exists to compare off the screen.
        """
        _root, view = window
        pane = view._panes[0]

        assert pane["frame"].rowconfigure(2)["weight"] >= 1
        # The full finding text is not in the pane any more: it is in the popup.
        assert "body" not in pane
        assert pane["facts"].winfo_exists()
        assert pane["summary"].winfo_exists()
        assert pane["details"].winfo_exists()
        assert pane["details_key"] == "advisor_details_1"


# ---------------------------------------------------------------------------
# the controller half (no display needed)
# ---------------------------------------------------------------------------


class FakeRunner:
    """Records jobs instead of running them, like the real runner would."""

    def __init__(self, *, busy: bool = False) -> None:
        self.jobs: list[tuple[str, Any]] = []
        self.busy = busy

    @property
    def is_busy(self) -> bool:
        return self.busy

    def submit(self, label: str, action: Any) -> bool:
        if self.busy:
            return False
        self.jobs.append((label, action))
        return True

    def run_next(self, worker: Any) -> JobResult:
        label, action = self.jobs.pop(0)
        try:
            payload = action(worker)
        except Exception as error:  # mirrors the real runner exactly
            return JobResult(label=label, error=describe_error(error))
        return JobResult(label=label, payload=payload)


class FakeWorker:
    """A fake core worker that records the provider-settings calls it receives."""

    def __init__(
        self,
        *,
        saved: bool = True,
        verdict: str = "CONNECTED",
        payload: Optional[dict[str, Any]] = None,
        failure: Optional[BaseException] = None,
    ) -> None:
        self.saved = saved
        self.verdict = verdict
        self.failure = failure
        self.requests: list[tuple[str, Any]] = []
        self._payload = (
            dict(payload)
            if payload is not None
            else {"provider_settings": core_settings_payload()}
        )

    def payload(self) -> dict[str, Any]:
        return self._payload

    def save_provider_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(("save", payload))
        if self.failure is not None:
            # the core validates the configuration before anything is written
            raise self.failure
        return {
            "saved": self.saved,
            "path": "data/provider_settings.json",
            "provider_settings": self._payload["provider_settings"],
        }

    def test_connection(
        self,
        advisor: str,
        provider: str,
        model: str = "",
        api_key: Optional[str] = None,
    ) -> dict[str, Any]:
        self.requests.append(
            (
                "test",
                {
                    "advisor": advisor,
                    "provider": provider,
                    "model": model,
                    "api_key": api_key,
                },
            )
        )
        return {"advisor": advisor, "provider": provider, "status": self.verdict}


def make_controller(
    *,
    payload: Optional[dict[str, Any]] = None,
    runner: Optional[FakeRunner] = None,
) -> tuple[FakeRunner, GuiController]:
    """A controller showing a provider-settings payload, with a recording runner."""
    resolved = runner if runner is not None else FakeRunner()
    controller = GuiController(resolved, actor="operator", reports_dir="reports")
    controller.apply_result(
        JobResult(
            label="refresh",
            payload={
                "payload": dict(payload)
                if payload is not None
                else {"provider_settings": core_settings_payload()}
            },
        )
    )
    return resolved, controller


class TestTheController:
    """One save action, one probe per pane, and no key anywhere in the panel."""

    def test_the_provider_actions_exist_and_are_available(self) -> None:
        _runner, controller = make_controller()

        keys = {intent.key for intent in INTENTS}
        assert SAVE_SETTINGS_INTENT in keys
        for key in CONNECTION_INTENT_KEYS:
            assert key in keys
        for key in (SAVE_SETTINGS_INTENT, *CONNECTION_INTENT_KEYS):
            assert controller.enabled(controller.intent(key)) is True

    def test_the_provider_actions_are_disabled_while_another_action_runs(
        self,
    ) -> None:
        runner, controller = make_controller()
        runner.busy = True

        for key in (SAVE_SETTINGS_INTENT, *CONNECTION_INTENT_KEYS):
            assert controller.enabled(controller.intent(key)) is False
        assert controller.submit(SAVE_SETTINGS_INTENT) is False

    def test_the_settings_are_key_free_and_plain_data(self) -> None:
        _runner, controller = make_controller(
            payload={
                "provider_settings": core_settings_payload(
                    key_set=(True, False, False)
                )
            }
        )

        view = controller.view_model()["review"]["provider_settings"]

        assert json.loads(json.dumps(view)) == view
        assert KEY not in json.dumps(view)
        assert "api_key" not in json.dumps(view)
        assert [row["provider"] for row in view["rows"]] == [
            "openai",
            "claude",
            "grok",
        ]
        assert view["rows"][0]["key_set"] is True
        assert view["rows"][0]["key_masked"] == "********"
        assert [entry["provider"] for entry in view["catalog"]] == [
            "openai",
            "claude",
            "grok",
            "deepseek",
            "disabled",
        ]
        # nothing has been saved in this session yet
        assert view["note"] == ""

    def test_the_load_status_reaches_the_panel(self) -> None:
        _runner, controller = make_controller(
            payload={
                "provider_settings": core_settings_payload(
                    status="invalid",
                    status_text="provider settings invalid",
                    valid=False,
                )
            }
        )

        view = controller.view_model()["review"]["provider_settings"]

        assert view["status"] == "invalid"
        assert view["status_text"] == "provider settings invalid"
        assert view["valid"] is False

    def test_a_save_submits_exactly_one_core_call(self) -> None:
        runner, controller = make_controller()
        controller.set_provider_selection(
            {
                "advisor_1": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "api_key": KEY,
                },
                "advisor_2": {"provider": "disabled", "model": ""},
                "advisor_3": {"provider": "openai", "model": "gpt-4.1-mini"},
            }
        )

        assert controller.submit(SAVE_SETTINGS_INTENT) is True

        assert [label for label, _action in runner.jobs] == [SAVE_SETTINGS_INTENT]

    def test_the_saved_payload_is_exactly_what_the_panes_show(self) -> None:
        runner, controller = make_controller()
        worker = FakeWorker()
        controller.set_provider_selection(
            {
                "advisor_1": {
                    "provider": "deepseek",
                    "model": "deepseek-reasoner",
                    "api_key": KEY,
                },
                "advisor_2": {"provider": "disabled", "model": "", "api_key": None},
                "advisor_3": {
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "api_key": "",
                },
            }
        )

        controller.submit(SAVE_SETTINGS_INTENT)
        controller.apply_result(runner.run_next(worker))

        assert worker.requests == [
            (
                "save",
                {
                    "advisor_1": {
                        "provider": "deepseek",
                        "model": "deepseek-reasoner",
                        "api_key": KEY,
                    },
                    # a blank entry means "keep the stored key", never an empty one
                    "advisor_2": {
                        "provider": "disabled",
                        "model": "",
                        "api_key": None,
                    },
                    "advisor_3": {
                        "provider": "openai",
                        "model": "gpt-4.1-mini",
                        "api_key": "",
                    },
                },
            )
        ]

    def test_a_pane_that_was_never_touched_saves_what_is_stored(self) -> None:
        runner, controller = make_controller()
        worker = FakeWorker()

        controller.submit(SAVE_SETTINGS_INTENT)
        controller.apply_result(runner.run_next(worker))

        assert worker.requests[0][1] == {
            "advisor_1": {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "api_key": None,
            },
            "advisor_2": {
                "provider": "claude",
                "model": "claude-sonnet",
                "api_key": None,
            },
            "advisor_3": {
                "provider": "grok",
                "model": "grok-4.6",
                "api_key": None,
            },
        }

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_each_pane_probes_only_its_own_advisor(self, index: int) -> None:
        runner, controller = make_controller()
        worker = FakeWorker(verdict="AUTH ERROR")
        slot = f"advisor_{index + 1}"

        assert controller.submit(CONNECTION_INTENT_KEYS[index]) is True
        assert [label for label, _action in runner.jobs] == [
            CONNECTION_INTENT_KEYS[index]
        ]
        controller.apply_result(runner.run_next(worker))

        assert worker.requests == [
            (
                "test",
                {
                    "advisor": slot,
                    "provider": PROVIDERS[index],
                    "model": MODELS[PROVIDERS[index]],
                    "api_key": None,
                },
            )
        ]

    def test_a_probe_sends_the_typed_key_and_never_the_stored_one(self) -> None:
        runner, controller = make_controller()
        worker = FakeWorker()
        controller.set_provider_selection(
            {"advisor_2": {"provider": "deepseek", "model": "m", "api_key": KEY}}
        )

        controller.submit(CONNECTION_INTENT_KEYS[1])
        controller.apply_result(runner.run_next(worker))

        assert worker.requests[0][1] == {
            "advisor": "advisor_2",
            "provider": "deepseek",
            "model": "m",
            "api_key": KEY,
        }

    def test_the_verdict_reaches_the_pane_and_the_log(self) -> None:
        runner, controller = make_controller()
        worker = FakeWorker(verdict="NETWORK ERROR")

        controller.submit(CONNECTION_INTENT_KEYS[0])
        controller.apply_result(runner.run_next(worker))

        view = controller.view_model()["review"]["provider_settings"]
        assert view["rows"][0]["connection"] == "NETWORK ERROR"
        assert "NETWORK ERROR" in controller.log_rows()[0][5]

    def test_a_saved_configuration_is_logged_without_a_key(self) -> None:
        runner, controller = make_controller()
        worker = FakeWorker()
        controller.set_provider_selection(
            {"advisor_1": {"provider": "openai", "model": "m", "api_key": KEY}}
        )

        controller.submit(SAVE_SETTINGS_INTENT)
        controller.apply_result(runner.run_next(worker))

        view = controller.view_model()["review"]["provider_settings"]
        assert view["note"] == "provider settings saved"
        rendered = json.dumps(controller.log_rows()) + json.dumps(view)
        assert KEY not in rendered
        assert "api_key" not in rendered

    def test_a_save_that_could_not_be_written_says_so(self) -> None:
        runner, controller = make_controller()
        worker = FakeWorker(saved=False)

        controller.submit(SAVE_SETTINGS_INTENT)
        controller.apply_result(runner.run_next(worker))

        view = controller.view_model()["review"]["provider_settings"]
        assert view["note"] == "provider settings not saved"
        assert "NOT saved" in controller.log_rows()[0][5]

    def test_a_rejected_save_is_reported_next_to_the_fields(self) -> None:
        runner, controller = make_controller()
        controller.set_provider_selection(
            {"advisor_1": {"provider": "gemini", "model": "", "api_key": None}}
        )

        controller.submit(SAVE_SETTINGS_INTENT)
        result = runner.run_next(
            FakeWorker(failure=ValueError("provider must be one of ..."))
        )
        assert result.error is not None  # the core refused the configuration
        controller.apply_result(result)

        view = controller.view_model()["review"]["provider_settings"]
        assert view["note"].startswith("provider settings rejected")

    def test_a_probe_result_never_carries_a_secret(self) -> None:
        runner, controller = make_controller()
        worker = FakeWorker(verdict="AUTH ERROR")
        controller.set_provider_selection(
            {"advisor_1": {"provider": "openai", "model": "m", "api_key": KEY}}
        )

        controller.submit(CONNECTION_INTENT_KEYS[0])
        controller.apply_result(runner.run_next(worker))

        rendered = json.dumps(controller.view_model(), default=str)
        assert KEY not in rendered
        assert "api_key" not in rendered
