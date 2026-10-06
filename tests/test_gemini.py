"""Offline Gemini wire, review, deliberation and configuration coverage."""
import json
import pytest
from architecture_assistant.composition.advisor_factory import (
    AdvisorFactory, create_deliberation_agent, create_deliberation_lead,
    probe_connection, probe_deliberation, provider_catalog,
)
from architecture_assistant.composition.provider_settings import ProviderSelection
from architecture_assistant.infrastructure.gemini import (
    GEMINI_CHAT_COMPLETIONS_URL, DEFAULT_GEMINI_MODEL,
    GeminiAdvisorInvalidResponseError, GeminiMissingApiKeyError,
)
from architecture_assistant.infrastructure._http import HttpResponse
from architecture_assistant.ports.capabilities import AgentAnalysisQuery, LeadReviewQuery, SLOT_AGENT_A
from tests.test_deepseek_advisor import answer, make_query

KEY = "gemini-offline-test-key"


def transport(content, calls):
    def send(request):
        calls.append(request)
        return HttpResponse(200, json.dumps({"choices": [{"message": {"content": json.dumps(content)}}]}).encode())
    return send


def test_review_identity_endpoint_auth_and_model():
    calls = []
    def send(request):
        calls.append(request)
        return answer()
    adapter = AdvisorFactory.create("gemini", "", KEY, transport=send)
    finding = adapter.advise(make_query())
    assert finding.source == "gemini"
    assert KEY not in repr(adapter) and KEY not in repr(finding)
    request = calls[0]
    assert request.url == GEMINI_CHAT_COMPLETIONS_URL
    assert request.headers["Authorization"] == "Bearer " + KEY
    assert json.loads(request.body)["model"] == DEFAULT_GEMINI_MODEL
    assert KEY.encode() not in request.body


@pytest.mark.parametrize("kind", ["agent", "lead"])
def test_deliberation_seats_use_gemini(kind):
    calls = []
    send = transport({"proposal_summary": "architecture", "agreements": ["shared design"]}, calls)
    if kind == "agent":
        seat = create_deliberation_agent("gemini", "custom-gemini-model", KEY, transport=send)
        result = seat.analyse(AgentAnalysisQuery(project="p", requirement="r", deliberation_id="d", slot=SLOT_AGENT_A))
    else:
        seat = create_deliberation_lead("gemini", "custom-gemini-model", KEY, transport=send)
        result = seat.review(LeadReviewQuery(project="p", requirement="r", deliberation_id="d"))
    assert result.source == "gemini"
    assert calls[0].url == GEMINI_CHAT_COMPLETIONS_URL
    assert json.loads(calls[0].body)["model"] == "custom-gemini-model"
    assert KEY.encode() not in calls[0].body


@pytest.mark.parametrize("status,expected", [(200,"CONNECTED"),(401,"AUTH ERROR"),(404,"MODEL ERROR"),(503,"PROVIDER ERROR")])
def test_connection_verdicts(status, expected):
    assert probe_connection("gemini", "", KEY, transport=lambda r: HttpResponse(status,b"{}")) == expected


def test_missing_key_never_calls_network(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    adapter = AdvisorFactory.create("gemini", "", None, transport=lambda r: pytest.fail("Network called"))
    with pytest.raises(GeminiMissingApiKeyError):
        adapter.advise(make_query())


def test_malformed_review_fails_closed():
    adapter = AdvisorFactory.create("gemini", "", KEY, transport=lambda r: HttpResponse(200,b"{}"))
    with pytest.raises(GeminiAdvisorInvalidResponseError):
        adapter.advise(make_query())


def test_catalog_settings_and_deliberation_connection():
    assert ProviderSelection("gemini", "custom-model", KEY).provider == "gemini"
    assert any(p["provider"] == "gemini" and p["available"] for p in provider_catalog())
    assert probe_deliberation("gemini", "", KEY, transport=transport({"ok": True}, [])) == "CONNECTED"


@pytest.mark.parametrize("status,expected", [(200,"CONNECTED"),(400,"MODEL ERROR"),(401,"AUTH ERROR"),(403,"AUTH ERROR"),(404,"MODEL ERROR"),(429,"PROVIDER ERROR"),(503,"PROVIDER ERROR")])
def test_deliberation_probe_classifies_http_without_generated_json(status, expected):
    calls = []
    def send(request):
        calls.append(request)
        return HttpResponse(status,b"{}")
    assert probe_deliberation("gemini", "gemini-3.1-pro-preview", KEY, transport=send) == expected
    assert len(calls) == 1


def test_probe_details_do_not_expose_error_body():
    from architecture_assistant.composition.advisor_factory import probe_gemini_details
    result = probe_gemini_details("model", KEY, transport=lambda r: HttpResponse(429, KEY.encode()))
    assert result == {"status": "PROVIDER ERROR", "http_status": 429}
    assert KEY not in repr(result)


@pytest.mark.parametrize("provider", ["gemini", "deepseek"])
def test_reasoning_architects_have_room_for_complete_json(provider):
    seat = create_deliberation_agent(provider, "model", KEY)
    body = json.loads(seat._body("Return JSON"))
    assert body["max_tokens"] == 16384
    assert body["response_format"] == {"type": "json_object"}
    assert seat._timeout == 120


def test_claude_chair_budget_and_multiple_text_blocks():
    seat = create_deliberation_lead("claude", "model", KEY)
    assert json.loads(seat._body("Return JSON"))["max_tokens"] == 16384
    assert seat._timeout == 120
    assert seat._text({"content": [{"type": "text", "text": "{\"ok\":"}, {"type": "text", "text": "true}"}]}) == '{"ok":true}'


def test_json_extractor_reads_one_complete_object_with_trailing_prose():
    from architecture_assistant.infrastructure.deliberation_lead import _extract_object
    assert _extract_object('Result: {"agreements": []} Extra note: {not JSON}') == {"agreements": []}


def test_json_extractor_still_rejects_truncated_object():
    from architecture_assistant.infrastructure.deliberation_lead import _extract_object, DeliberationInvalidResponseError
    with pytest.raises(DeliberationInvalidResponseError):
        _extract_object('{"agreements": [{"text":"unfinished"}')
