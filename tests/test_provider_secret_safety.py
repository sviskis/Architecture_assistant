"""The provider API key must never leave its one local file.

This is the security half of the per-advisor provider configuration, asserted
against the **real** composed object graph rather than against a summary of it:

* the composed review uses exactly the configured providers and models;
* an advisor the operator disabled abstains, and no provider is called for it;
* the stored key appears in **no** event log, **no** audit entry, **no**
  ``ArchitectureReviewResult`` and **no** exported report;
* a provider error that *echoes* the key cannot push it into a reason, because
  the observation seam reports only the exception *type name*.

``data/provider_settings.json`` is the only place the key is written, and every
run here uses a throwaway file under ``tmp_path``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from architecture_assistant.composition import (
    CompositionConfig,
    compose,
    default_provider_settings,
    observe,
    settings_from_mapping,
)
from architecture_assistant.domain.enums import Mode
from architecture_assistant.infrastructure import (
    HttpRequest,
    HttpResponse,
    OpenAIAdvisorAdapter,
)
from architecture_assistant.ports.capabilities import AdvisorQuery

FIXED = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

#: A literal that is not, and never was, a real credential.
KEY = "sk-provider-secret-safety-not-a-real-key"

QUESTION = "Review the current architecture state of this managed project."

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "architecture_assistant"


def fixed_clock() -> datetime:
    return FIXED


def make_config(tmp_path: Path, **overrides) -> CompositionConfig:
    """A composition whose provider settings live beside its database."""
    payload = {
        "database_path": tmp_path / "assistant.db",
        "exchange_dir": tmp_path / "cline",
        "report_dir": tmp_path / "reports",
        "source_root": SOURCE_ROOT,
        "mode": Mode.AUTO,
        "timeout": timedelta(hours=1),
        "clock": fixed_clock,
        "provider_settings_path": tmp_path / "provider_settings.json",
    }
    payload.update(overrides)
    return CompositionConfig(**payload)


def write_settings(config: CompositionConfig, mapping: dict) -> None:
    Path(config.provider_settings_path).write_text(
        json.dumps(mapping), encoding="utf-8"
    )


@dataclass
class EchoingTransport:
    """A transport whose rejection echoes the credential it was sent."""

    secret: str = KEY
    requests: list = field(default_factory=list)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        # a pathological provider: the key comes back in the failure body
        return HttpResponse(
            401, f"bad credential {request.headers.get('Authorization')}".encode()
        )


@pytest.fixture
def configured(tmp_path):
    """A composed assistant whose disabled slot carries a stored key.

    The key is deliberately stored on a **disabled** slot: a slot bound to a real
    provider would need a real endpoint, and a test must never reach one. The key
    is genuinely in play - it lives in the composition's configuration and flows
    through the factory - which is what makes the leak assertions meaningful.
    """
    config = make_config(tmp_path)
    write_settings(
        config,
        {
            "advisor_1": {"provider": "openai", "model": "gpt-4.1-mini"},
            "advisor_2": {"provider": "disabled", "api_key": KEY},
            "advisor_3": {"provider": "disabled"},
        },
    )
    composition = compose(config)
    yield composition
    composition.close()


class TestTheConfiguredGraph:
    """The composed review is wired to exactly what was configured."""

    def test_the_composed_reviewers_are_the_configured_providers(
        self, configured
    ) -> None:
        reviewers = configured.architecture_review.reviewers

        assert [reviewer.source for reviewer in reviewers] == [
            "openai",
            "disabled",
            "disabled",
        ]
        assert [reviewer.advisor.provider for reviewer in reviewers] == [
            "openai",
            "disabled",
            "disabled",
        ]

    def test_the_settings_are_reported_loaded_not_invalid(self, configured) -> None:
        assert configured.provider_settings_status == "loaded"
        assert configured.provider_settings.selection("advisor_2").key_set is True

    def test_a_malformed_file_is_reported_invalid_and_falls_back(
        self, tmp_path
    ) -> None:
        config = make_config(tmp_path)
        Path(config.provider_settings_path).write_text(
            "{ not json", encoding="utf-8"
        )

        composition = compose(config)
        try:
            assert composition.provider_settings_status == "invalid"
            assert composition.provider_settings == default_provider_settings()
            assert [r.source for r in composition.architecture_review.reviewers] == [
                "openai",
                "claude",
                "grok",
            ]
        finally:
            composition.close()

    def test_a_missing_file_uses_the_defaults(self, tmp_path) -> None:
        composition = compose(make_config(tmp_path))
        try:
            assert composition.provider_settings_status == "defaults"
            assert [r.source for r in composition.architecture_review.reviewers] == [
                "openai",
                "claude",
                "grok",
            ]
        finally:
            composition.close()

    def test_a_saved_change_rewires_only_the_review(self, tmp_path) -> None:
        """What the panel saves is what the next review uses - and nothing else."""
        from architecture_assistant.composition import apply_provider_settings

        composition = compose(make_config(tmp_path))
        try:
            before = composition.realization_control
            apply_provider_settings(
                composition,
                settings_from_mapping({"advisor_1": {"provider": "disabled"}}),
            )

            assert composition.provider_settings_status == "loaded"
            assert composition.architecture_review.reviewers[0].source == "disabled"
            # the deterministic graph did not move
            assert composition.realization_control is before
        finally:
            composition.close()


class TestTheKeyNeverLeaves:
    """One review, and the key is in none of its products."""

    def review(self, composition) -> tuple[dict, list]:
        events: list = []
        snapshot = composition.monitor.snapshot()
        result = composition.architecture_review.review(
            snapshot, question=QUESTION, on_event=events.append
        )
        return result.to_dict(), events

    def test_the_review_runs_without_any_provider_call(self, configured) -> None:
        """One slot has no key and two are disabled: nothing may be called."""
        result, events = self.review(configured)

        assert result["question"] == QUESTION
        assert [stage["source"] for stage in result["providers"]] == [
            "openai",
            "disabled",
            "disabled",
        ]
        assert [stage["status"] for stage in result["providers"]] == [
            "ERROR",
            "ABSTAIN",
            "ABSTAIN",
        ]
        assert events

    def test_the_key_is_absent_from_the_review_result(self, configured) -> None:
        result, _events = self.review(configured)

        rendered = json.dumps(result, default=str)
        assert KEY not in rendered
        assert "api_key" not in rendered
        assert "Authorization" not in rendered

    def test_the_key_is_absent_from_every_event(self, configured) -> None:
        _result, events = self.review(configured)

        for event in events:
            rendered = json.dumps(event, default=str)
            assert KEY not in rendered, event
            assert "api_key" not in rendered, event
            assert "Authorization" not in rendered, event

    def test_the_key_is_absent_from_the_audit_trail(self, configured) -> None:
        before = [entry.to_dict() for entry in configured.storage.audit.list()]

        self.review(configured)

        after = [entry.to_dict() for entry in configured.storage.audit.list()]
        assert after == before
        rendered = json.dumps(after, default=str)
        assert KEY not in rendered
        assert "api_key" not in rendered

    def test_the_key_is_absent_from_the_exported_reports(self, configured) -> None:
        self.review(configured)
        payload = configured.report_builder.build().to_dict()

        markdown = configured.markdown_reporting.render(payload)
        excel_path = configured.excel_reporting.render(payload)

        assert KEY not in markdown
        assert "api_key" not in markdown
        workbook = Path(excel_path)
        if workbook.exists():
            assert KEY.encode("utf-8") not in workbook.read_bytes()

    def test_the_key_is_written_to_exactly_one_file(self, configured) -> None:
        """It lives in the settings file and in nothing the assistant produces."""
        self.review(configured)
        configured.markdown_reporting.render(
            configured.report_builder.build().to_dict()
        )

        settings_path = Path(configured.config.provider_settings_path)
        offenders = [
            path
            for path in sorted(Path(configured.config.report_dir).rglob("*"))
            if path.is_file() and KEY.encode("utf-8") in path.read_bytes()
        ]
        assert settings_path.exists()
        assert offenders == []
        assert KEY in settings_path.read_text(encoding="utf-8")


class TestProviderErrorsStaySanitized:
    """A provider that echoes the key cannot push it into a reason."""

    def test_the_reason_is_the_exception_type_only(self) -> None:
        transport = EchoingTransport()
        advisor = OpenAIAdvisorAdapter(
            KEY, model="gpt-4.1-mini", transport=transport
        )

        observation = observe(
            advisor.provider,
            lambda: advisor.advise(AdvisorQuery(question=QUESTION, step_no=1)),
        )

        assert transport.requests  # the call really happened
        assert KEY not in observation.reason
        assert observation.reason == "advisor failed: OpenAIAdvisorHttpError"

    def test_the_adapter_error_itself_is_already_redacted(self) -> None:
        transport = EchoingTransport()
        advisor = OpenAIAdvisorAdapter(
            KEY, model="gpt-4.1-mini", transport=transport
        )

        with pytest.raises(Exception) as raised:
            advisor.advise(AdvisorQuery(question=QUESTION, step_no=1))

        assert KEY not in str(raised.value)
        assert "***" in str(raised.value)

    def test_the_observation_never_reports_a_vendor_body(self) -> None:
        transport = EchoingTransport()
        advisor = OpenAIAdvisorAdapter(
            KEY, model="gpt-4.1-mini", transport=transport
        )

        observation = observe(
            "openai",
            lambda: advisor.advise(AdvisorQuery(question=QUESTION, step_no=1)),
        )

        assert "credential" not in observation.reason
        assert "bad " not in observation.reason
