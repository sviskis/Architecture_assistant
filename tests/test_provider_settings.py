"""Tests for the per-advisor provider settings file.

Two families of property are pinned here, and they are the two that matter for a
file that holds a credential:

* **fail-safe loading.** A missing file means the documented defaults (OpenAI,
  Claude, Grok - the wiring this assistant had before the file existed); a
  malformed one means the same defaults *plus* an explicit "provider settings
  invalid"; nothing in this module can raise into the panel;
* **secret safety.** An API key is a field of exactly one file. It is never
  rendered by ``repr``/``str``, never part of a view model, never echoed in an
  error message, and ``to_file_dict`` - the one method that carries it - exists
  for :func:`save_provider_settings` alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from architecture_assistant.composition import (
    ADVISOR_KEYS,
    DEFAULT_PROVIDER_BY_ADVISOR,
    MASKED_KEY,
    SETTINGS_STATUS_DEFAULTS,
    SETTINGS_STATUS_INVALID,
    SETTINGS_STATUS_LOADED,
    SUPPORTED_PROVIDERS,
    ProviderSelection,
    ProviderSettings,
    default_provider_settings,
    load_provider_settings,
    save_provider_settings,
    settings_from_mapping,
)

#: A value that must never appear in a message, a repr or a view.
KEY = "sk-test-provider-settings-key"


def write_settings(path: Path, data: object) -> Path:
    """Write one settings file verbatim (valid or not) and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestDefaults:
    """The documented defaults are the wiring this panel had before the file."""

    def test_the_default_mapping_is_openai_claude_grok(self) -> None:
        settings = default_provider_settings()

        assert DEFAULT_PROVIDER_BY_ADVISOR == {
            "advisor_1": "openai",
            "advisor_2": "claude",
            "advisor_3": "grok",
        }
        assert [settings.selection(key).provider for key in ADVISOR_KEYS] == [
            "openai",
            "claude",
            "grok",
        ]
        assert ADVISOR_KEYS == ("advisor_1", "advisor_2", "advisor_3")

    def test_a_missing_file_means_the_defaults(self, tmp_path) -> None:
        loaded = load_provider_settings(tmp_path / "absent.json")

        assert loaded.status == SETTINGS_STATUS_DEFAULTS
        assert loaded.valid is True
        assert loaded.settings == default_provider_settings()
        assert loaded.text == "provider settings missing; using defaults"

    def test_the_supported_providers_are_the_documented_five(self) -> None:
        assert SUPPORTED_PROVIDERS == (
            "openai",
            "claude",
            "grok",
            "deepseek",
            "disabled",
        )


class TestSaveAndReload:
    """A saved configuration is read back exactly - provider, model and key."""

    def test_a_saved_configuration_round_trips(self, tmp_path) -> None:
        path = tmp_path / "provider_settings.json"
        settings = settings_from_mapping(
            {
                "advisor_1": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "api_key": KEY,
                },
                "advisor_2": {"provider": "disabled"},
                "advisor_3": {
                    "provider": "claude",
                    "model": "claude-from-config",
                    "api_key": "",
                },
            }
        )

        assert save_provider_settings(path, settings) is True

        loaded = load_provider_settings(path)
        assert loaded.status == SETTINGS_STATUS_LOADED
        assert loaded.settings == settings
        assert loaded.settings.selection("advisor_1").api_key == KEY
        assert loaded.settings.selection("advisor_3").api_key == ""
        assert loaded.settings.selection("advisor_3").key_set is False

    def test_the_file_is_the_shaped_object_the_spec_names(self, tmp_path) -> None:
        path = tmp_path / "provider_settings.json"
        save_provider_settings(
            path,
            settings_from_mapping(
                {
                    "advisor_1": {
                        "provider": "openai",
                        "model": "m1",
                        "api_key": KEY,
                    },
                    "advisor_2": {"provider": "claude", "model": "m2"},
                    "advisor_3": {"provider": "grok", "model": "m3"},
                }
            ),
        )

        data = json.loads(path.read_text(encoding="utf-8"))

        assert data == {
            "advisor_1": {"provider": "openai", "model": "m1", "api_key": KEY},
            "advisor_2": {"provider": "claude", "model": "m2", "api_key": ""},
            "advisor_3": {"provider": "grok", "model": "m3", "api_key": ""},
            # Step 29 added the deliberation seats (two architects and a chair) to
            # the same file, so one save covers both workflows.
            "agent_a": {"provider": "deepseek", "model": "", "api_key": ""},
            "agent_b": {"provider": "claude", "model": "", "api_key": ""},
            "lead": {"provider": "openai", "model": "", "api_key": ""},
        }

    def test_a_write_that_cannot_land_answers_false_and_never_raises(
        self, tmp_path
    ) -> None:
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")

        # ``blocked/provider_settings.json`` cannot exist: a file is in the way.
        assert (
            save_provider_settings(
                blocker / "provider_settings.json", default_provider_settings()
            )
            is False
        )

    def test_no_temporary_file_is_left_behind(self, tmp_path) -> None:
        path = tmp_path / "provider_settings.json"

        assert save_provider_settings(path, default_provider_settings()) is True
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "provider_settings.json"
        ]

    def test_a_settings_object_is_required_to_save(self, tmp_path) -> None:
        with pytest.raises(ValueError):
            save_provider_settings(tmp_path / "x.json", {"advisor_1": {}})


class TestMalformed:
    """A damaged file is refused as a whole - the defaults plus "invalid"."""

    @pytest.mark.parametrize(
        "data",
        [
            "not json at all",
            json.dumps([1, 2, 3]),
            json.dumps({"advisor_1": {"provider": "nope"}}),
            json.dumps({"advisor_4": {"provider": "openai"}}),
            json.dumps({"advisor_1": "not an object"}),
            json.dumps({"advisor_1": {"provider": ""}}),
            json.dumps({"advisor_1": {"provider": 7}}),
            json.dumps({"advisor_1": {"provider": "openai", "model": 3}}),
            json.dumps({"advisor_1": {"provider": "openai", "api_key": 3}}),
        ],
    )
    def test_a_malformed_file_fails_safely(self, tmp_path, data: str) -> None:
        path = tmp_path / "provider_settings.json"
        path.write_text(data, encoding="utf-8")

        loaded = load_provider_settings(path)

        assert loaded.status == SETTINGS_STATUS_INVALID
        assert loaded.valid is False
        assert loaded.text == "provider settings invalid"
        # the panel keeps working: the defaults are what it falls back to
        assert loaded.settings == default_provider_settings()

    def test_an_unreadable_path_fails_safely(self, tmp_path) -> None:
        path = tmp_path / "a_directory"
        path.mkdir()

        loaded = load_provider_settings(path)

        assert loaded.status == SETTINGS_STATUS_DEFAULTS

    def test_a_missing_slot_falls_back_to_that_slots_default(self, tmp_path) -> None:
        path = write_settings(
            tmp_path / "provider_settings.json",
            {"advisor_2": {"provider": "deepseek", "model": "deepseek-chat"}},
        )

        loaded = load_provider_settings(path)

        assert loaded.status == SETTINGS_STATUS_LOADED
        assert loaded.settings.selection("advisor_1").provider == "openai"
        assert loaded.settings.selection("advisor_2").provider == "deepseek"
        assert loaded.settings.selection("advisor_3").provider == "grok"


class TestSecretSafety:
    """The key is a field of one file - and of no repr, view or message."""

    def selection(self) -> ProviderSelection:
        return ProviderSelection(provider="openai", model="m", api_key=KEY)

    def test_repr_and_str_never_render_the_key(self) -> None:
        selection = self.selection()

        for rendered in (repr(selection), str(selection), f"{selection}"):
            assert KEY not in rendered
            assert "sk-" not in rendered
        assert "api_key=set" in repr(selection)

    def test_the_settings_repr_never_renders_the_key(self) -> None:
        settings = ProviderSettings(
            advisor_1=self.selection(),
            advisor_2=self.selection(),
            advisor_3=self.selection(),
        )

        for rendered in (repr(settings), str(settings)):
            assert KEY not in rendered

    def test_the_view_is_key_free_and_reports_only_a_mask(self) -> None:
        view = self.selection().to_view()

        assert KEY not in json.dumps(view)
        assert view["key_set"] is True
        assert view["key_masked"] == MASKED_KEY
        assert "api_key" not in view
        assert ProviderSelection(provider="disabled").to_view()["key_masked"] == ""

    def test_to_file_dict_is_the_one_method_that_carries_the_key(self) -> None:
        """The documented exception: only the settings file ever sees it."""
        assert self.selection().to_file_dict()["api_key"] == KEY

    def test_a_rejected_key_value_is_never_echoed(self) -> None:
        with pytest.raises(ValueError) as raised:
            settings_from_mapping(
                {"advisor_1": {"provider": "openai", "api_key": 123456789}}
            )

        assert "123456789" not in str(raised.value)
        assert "api_key" in str(raised.value)

    def test_a_provider_is_normalized_but_a_model_is_kept_as_typed(self) -> None:
        selection = ProviderSelection(
            provider="  DeepSeek ", model=" DeepSeek-Chat ", api_key=" k "
        )

        assert selection.provider == "deepseek"
        assert selection.model == "DeepSeek-Chat"
        assert selection.api_key == "k"

    def test_an_unknown_provider_is_refused(self) -> None:
        with pytest.raises(ValueError):
            ProviderSelection(provider="gemini")

    def test_settings_are_immutable_value_objects(self) -> None:
        settings = default_provider_settings()

        replaced = settings.with_selection(
            "advisor_1", ProviderSelection(provider="deepseek")
        )

        assert settings.selection("advisor_1").provider == "openai"
        assert replaced.selection("advisor_1").provider == "deepseek"
        assert replaced.selection("advisor_2").provider == "claude"
        with pytest.raises(KeyError):
            settings.selection("advisor_9")
        with pytest.raises(KeyError):
            settings.with_selection("advisor_9", ProviderSelection(provider="grok"))
