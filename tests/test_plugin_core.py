"""Tests for the plugin core registry.

Covers the mandated properties: several named adapters per capability coexist,
default selection is deterministic, the default can be changed without
re-registering, and nonexistent names/capabilities raise explicit errors.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from architecture_assistant.application import (
    PluginAlreadyRegisteredError,
    PluginBinding,
    PluginDefaultNotSetError,
    PluginError,
    PluginNotFoundError,
    PluginRegistry,
)
from architecture_assistant.ports import PluginCapability


class FakeAdapter:
    """Stand-in for a real adapter; the registry never inspects it."""

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"FakeAdapter({self.label!r})"


class TestRegistration:
    def test_registers_a_named_adapter(self) -> None:
        registry = PluginRegistry()
        binding = registry.register("worker", "cline", "CLINE")

        assert binding.capability is PluginCapability.WORKER
        assert binding.name == "cline"
        assert binding.plugin == "CLINE"
        assert binding.is_default is True

    def test_accepts_a_string_capability(self) -> None:
        registry = PluginRegistry()
        registry.register("judge", "claude-opus", "OPUS")
        assert registry.get(PluginCapability.JUDGE, "claude-opus") == "OPUS"

    def test_multiple_names_of_one_capability_coexist(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "cline", "CLINE")
        registry.register("worker", "codex", "CODEX")
        registry.register("worker", "third", "THIRD")

        assert registry.names("worker") == ("cline", "codex", "third")
        assert registry.get("worker", "codex") == "CODEX"
        assert len(registry.list("worker")) == 3

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "cline", "CLINE")
        with pytest.raises(PluginAlreadyRegisteredError, match="already registered"):
            registry.register("worker", "cline", "OTHER")

    def test_same_name_under_different_capabilities_is_allowed(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "default", "W")
        registry.register("judge", "default", "J")
        assert registry.get("worker", "default") == "W"
        assert registry.get("judge", "default") == "J"

    def test_unknown_capability_is_rejected(self) -> None:
        with pytest.raises(PluginError, match="unknown capability"):
            PluginRegistry().register("nope", "x", object())

    def test_empty_name_is_rejected(self) -> None:
        with pytest.raises(PluginError, match="non-empty string"):
            PluginRegistry().register("worker", "   ", object())


class TestDefaultSelection:
    def test_first_registered_adapter_becomes_the_default(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "cline", "CLINE")
        registry.register("worker", "codex", "CODEX")

        assert registry.default_name("worker") == "cline"
        assert registry.get_default("worker") == "CLINE"

    def test_later_registrations_never_steal_the_default(self) -> None:
        registry = PluginRegistry()
        registry.register("notifier", "windows", "WIN")
        registry.register("notifier", "email", "MAIL")
        assert registry.get_default("notifier") == "WIN"

    def test_set_default_switches_without_re_registering(self) -> None:
        registry = PluginRegistry()
        registry.register("reporting", "markdown", "MD")
        registry.register("reporting", "excel", "XLSX")

        registry.set_default("reporting", "excel")

        assert registry.default_name("reporting") == "excel"
        assert registry.get_default("reporting") == "XLSX"
        assert registry.names("reporting") == ("excel", "markdown")

    def test_set_default_to_unregistered_name_is_rejected(self) -> None:
        registry = PluginRegistry()
        registry.register("cost", "sqlite", "COST")
        with pytest.raises(PluginNotFoundError, match="unregistered"):
            registry.set_default("cost", "does-not-exist")

    def test_default_without_plugins_raises_explicit_error(self) -> None:
        registry = PluginRegistry()
        with pytest.raises(PluginDefaultNotSetError, match="no registered plugin"):
            registry.get_default("worker")
        with pytest.raises(PluginDefaultNotSetError):
            registry.default_name("worker")

    def test_defaults_are_independent_per_capability(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "cline", "CLINE")
        registry.register("worker", "codex", "CODEX")
        registry.register("judge", "claude-opus", "OPUS")

        registry.set_default("worker", "codex")

        assert registry.default_name("worker") == "codex"
        assert registry.default_name("judge") == "claude-opus"

    def test_default_selection_is_deterministic_across_rebuilds(self) -> None:
        def build() -> str:
            registry = PluginRegistry()
            for name in ("b", "a", "c"):
                registry.register("advisor", name, name.upper())
            return registry.default_name("advisor")

        assert build() == build() == "b"


class TestLookup:
    def test_get_unknown_name_raises(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "cline", "CLINE")
        with pytest.raises(PluginNotFoundError, match="not registered"):
            registry.get("worker", "codex")

    def test_get_unknown_capability_raises(self) -> None:
        with pytest.raises(PluginError, match="unknown capability"):
            PluginRegistry().get("nope", "x")

    def test_has_reports_registration(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "cline", "CLINE")
        assert registry.has("worker", "cline") is True
        assert registry.has("worker", "codex") is False
        assert registry.has("nope", "cline") is False
        assert registry.has("worker", "   ") is False

    def test_binding_reports_the_default_flag(self) -> None:
        registry = PluginRegistry()
        registry.register("judge", "a", "A")
        registry.register("judge", "b", "B")

        assert registry.binding("judge", "a").is_default is True
        assert registry.binding("judge", "b").is_default is False

        registry.set_default("judge", "b")
        assert registry.binding("judge", "a").is_default is False
        assert registry.binding("judge", "b").is_default is True

    def test_names_of_an_empty_capability(self) -> None:
        assert PluginRegistry().names("advisor") == ()


class TestListing:
    def test_list_is_sorted_and_deterministic(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "codex", "X")
        registry.register("advisor", "grok", "G")
        registry.register("worker", "cline", "C")
        registry.register("advisor", "claude", "CL")

        assert [(b.capability.value, b.name) for b in registry.list()] == [
            ("advisor", "claude"),
            ("advisor", "grok"),
            ("worker", "cline"),
            ("worker", "codex"),
        ]
        assert registry.list() == registry.list()

    def test_list_can_be_scoped_to_one_capability(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "cline", "C")
        registry.register("advisor", "openai", "O")
        scoped = registry.list("worker")
        assert [(b.capability.value, b.name) for b in scoped] == [
            ("worker", "cline")
        ]

    def test_list_marks_exactly_one_default_per_capability(self) -> None:
        registry = PluginRegistry()
        registry.register("cost", "a", "A")
        registry.register("cost", "b", "B")
        registry.register("judge", "c", "C")

        defaults = [b.name for b in registry.list() if b.is_default]
        assert defaults == ["a", "c"]


class TestPluginBinding:
    def test_requires_a_capability_enum(self) -> None:
        with pytest.raises(
            ValueError, match="capability must be a PluginCapability"
        ):
            PluginBinding(capability="worker", name="x", plugin=None)

    def test_requires_a_non_empty_name(self) -> None:
        with pytest.raises(ValueError, match="name must be a non-empty string"):
            PluginBinding(
                capability=PluginCapability.WORKER, name="  ", plugin=None
            )

    def test_is_frozen(self) -> None:
        binding = PluginBinding(
            capability=PluginCapability.WORKER, name="cline", plugin="C"
        )
        with pytest.raises(FrozenInstanceError):
            binding.name = "other"


class TestRegistryIsPure:
    def test_holds_arbitrary_adapter_objects(self) -> None:
        registry = PluginRegistry()
        marker = object()
        registry.register("storage", "sqlite", marker)
        assert registry.get("storage", "sqlite") is marker

    def test_registry_does_not_inspect_or_clone_adapters(self) -> None:
        registry = PluginRegistry()
        adapter = FakeAdapter("cline")
        registry.register("worker", "cline", adapter)
        assert registry.get_default("worker") is adapter

    def test_module_has_no_infrastructure_or_provider_knowledge(self) -> None:
        import architecture_assistant.application.plugin_core as module

        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "sqlite3" not in source
        assert "infrastructure" not in source
        assert "openai" not in source.lower()
        assert "anthropic" not in source.lower()

    def test_realistic_multi_adapter_configuration(self) -> None:
        registry = PluginRegistry()
        registry.register("worker", "cline", "CLINE")
        registry.register("worker", "codex", "CODEX")
        registry.register("judge", "claude-opus", "OPUS")
        registry.register("judge", "other-judge", "OTHER")
        registry.register("notifier", "windows", "WIN")
        registry.register("notifier", "email", "MAIL")
        registry.register("reporting", "markdown", "MD")
        registry.register("reporting", "excel", "XLSX")
        for name in ("openai", "claude", "grok"):
            registry.register("advisor", name, name.upper())

        assert registry.names("worker") == ("cline", "codex")
        assert registry.names("advisor") == ("claude", "grok", "openai")
        assert registry.names("reporting") == ("excel", "markdown")
        assert len(registry.list()) == 11

        registry.set_default("advisor", "grok")
        assert registry.get_default("advisor") == "GROK"
        assert registry.get_default("worker") == "CLINE"

    def test_storage_capability_does_not_imply_a_single_adapter_rule(self) -> None:
        registry = PluginRegistry()
        registry.register("storage", "sqlite", "SQLITE")
        registry.register("storage", "in-memory", "MEMORY")
        assert registry.names("storage") == ("in-memory", "sqlite")
        assert registry.get_default("storage") == "SQLITE"
        registry.set_default("storage", "in-memory")
        assert registry.get_default("storage") == "MEMORY"

