"""The one composition-level factory that turns plain configuration into advisors.

Nothing in this module decides anything, calls a provider or reaches a database.
It answers exactly two questions, and it is the **single** place that knows how a
provider name maps onto an adapter class:

* *which adapter implements this provider?* - :meth:`AdvisorFactory.create` and
  :func:`probe_connection`;
* *what may the operator choose?* - :func:`provider_catalog` and
  :func:`default_model_for`.

Why it exists
-------------
The GUI must be able to let the operator pick a provider and a model **without
knowing a single adapter class**. It supplies plain configuration - a provider
token, a model string and a credential - and receives an
:class:`~architecture_assistant.ports.capabilities.AdvisorPort`. That keeps every
adapter in ``infrastructure`` and keeps the GUI an external host, exactly as the
architecture baseline requires.

Honesty about availability
--------------------------
A provider that this build cannot actually serve is reported by
:func:`provider_catalog` with ``available=False`` instead of being offered and
then failing at the first request. Today every listed provider has a working
adapter, so the flag is always true - but the mechanism exists so a future entry
is never *pretended* to work.

The disabled advisor
--------------------
``disabled`` is a first-class choice: it resolves to
:class:`~architecture_assistant.infrastructure.DisabledAdvisorAdapter`, an
:class:`AdvisorPort` that makes no call at all and abstains on every question.
It is deliberately **available**, because "run this slot without a provider" is a
valid configuration, not a missing feature.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from ..application import AdvisorReviewer
from ..domain.models import utc_now
from ..infrastructure import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_GROK_MODEL,
    DEFAULT_OPENAI_MODEL,
    ClaudeAdvisorAdapter,
    DeepSeekAdvisorAdapter,
    DisabledAdvisorAdapter,
    GrokAdvisorAdapter,
    OpenAIAdvisorAdapter,
)
from ..ports.capabilities import AdvisorPort, ConnectionStatus, CostPort
from .provider_settings import (
    ADVISOR_KEYS,
    PROVIDER_LABELS,
    SUPPORTED_PROVIDERS,
    ProviderSettings,
    ProviderSelection,
)

__all__ = [
    "AdvisorFactory",
    "PROVIDER_DEFAULT_MODELS",
    "assemble_reviewers",
    "default_model_for",
    "probe_connection",
    "provider_catalog",
]

#: Each provider's documented default model - the same constants the adapters
#: ship, referenced (never re-typed) so the panel and the adapter cannot drift.
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "openai": DEFAULT_OPENAI_MODEL,
    "claude": DEFAULT_CLAUDE_MODEL,
    "grok": DEFAULT_GROK_MODEL,
    "deepseek": DEFAULT_DEEPSEEK_MODEL,
    "disabled": "",
}


def default_model_for(provider: str) -> str:
    """The documented default model of one provider (``""`` for disabled)."""
    return PROVIDER_DEFAULT_MODELS.get(str(provider), "")


def _provider_for(adapter: AdvisorPort) -> str:
    """The provider id an adapter reports (never a class name)."""
    return str(getattr(adapter, "provider", ""))


class AdvisorFactory:
    """Maps ``(provider, model, credential)`` onto an ``AdvisorPort``."""

    @staticmethod
    def create(
        provider: str,
        model: str = "",
        credential: Optional[str] = None,
        *,
        project: Optional[str] = None,
        cost_sink: Optional[CostPort] = None,
        clock: Callable[[], datetime] = utc_now,
        transport: Optional[Callable[[Any], Any]] = None,
    ) -> AdvisorPort:
        """Build the adapter for one configuration - plain data in, a port out.

        The credential is handed to the adapter and never returned, stored or
        rendered. A blank credential means "no key supplied", exactly as the
        adapters already define it, so a project with no key still composes and a
        provider without a key is simply unusable rather than fatal.

        ``transport`` is the adapters' existing seam: production leaves it
        ``None`` (the ``urllib`` transport) and a probe or a test may inject one
        so an offline check exercises the same classification code path.
        """
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string")
        token = provider.strip().lower()
        model = "" if model is None else str(model).strip()
        if credential is not None and not isinstance(credential, str):
            raise ValueError("credential must be a string or None")
        key = credential if credential else None
        shared: dict[str, Any] = {
            "project": project,
            "cost_sink": cost_sink,
            "clock": clock,
            "transport": transport,
        }
        if token == "openai":
            return OpenAIAdvisorAdapter(
                key, model=model or DEFAULT_OPENAI_MODEL, **shared
            )
        if token == "claude":
            return ClaudeAdvisorAdapter(key, model=model or None, **shared)
        if token == "grok":
            return GrokAdvisorAdapter(key, model=model or None, **shared)
        if token == "deepseek":
            return DeepSeekAdvisorAdapter(key, model=model or None, **shared)
        if token == "disabled":
            return DisabledAdvisorAdapter()
        raise ValueError(
            f"provider must be one of {', '.join(SUPPORTED_PROVIDERS)}; "
            f"got {provider!r}"
        )

    @staticmethod
    def available(provider: str) -> bool:
        """Whether this build can actually serve a provider token."""
        token = str(provider).strip().lower()
        return token in PROVIDER_DEFAULT_MODELS


def provider_catalog() -> tuple[dict[str, Any], ...]:
    """Every provider the operator may choose - plain, key-free data.

    One entry per supported provider, in :data:`SUPPORTED_PROVIDERS` order, with
    the label the panel shows, whether the build can serve it and its documented
    default model. No entry carries a credential, and none is derived from an
    adapter instance.
    """
    return tuple(
        {
            "provider": token,
            "label": PROVIDER_LABELS.get(token, token),
            "available": AdvisorFactory.available(token),
            "default_model": default_model_for(token),
        }
        for token in SUPPORTED_PROVIDERS
    )


def assemble_reviewers(
    settings: ProviderSettings,
    *,
    project: Optional[str] = None,
    cost_sink: Optional[CostPort] = None,
    clock: Callable[[], datetime] = utc_now,
    shared: Optional[Mapping[str, AdvisorPort]] = None,
) -> tuple[AdvisorReviewer, ...]:
    """One ``AdvisorReviewer`` per advisor slot, built from the configuration.

    ``shared`` lets the composition root hand in the canonical adapter of a
    provider; an untouched default slot (no model, no key) then reuses exactly
    that instance instead of building a second one. That keeps the composed
    review and the composition's own advisor attributes the *same* objects - one
    capability, not two - while a slot that pins a model or supplies a key still
    gets its own adapter.
    """
    if not isinstance(settings, ProviderSettings):
        raise ValueError("settings must be a ProviderSettings")
    canonical: Mapping[str, AdvisorPort] = shared or {}
    reviewers: list[AdvisorReviewer] = []
    for key in ADVISOR_KEYS:
        selection: ProviderSelection = settings.selection(key)
        adapter = _adapter_for(
            selection,
            canonical=canonical,
            project=project,
            cost_sink=cost_sink,
            clock=clock,
        )
        reviewers.append(
            AdvisorReviewer(source=_provider_for(adapter), advisor=adapter)
        )
    return tuple(reviewers)


def _adapter_for(
    selection: ProviderSelection,
    *,
    canonical: Mapping[str, AdvisorPort],
    project: Optional[str],
    cost_sink: Optional[CostPort],
    clock: Callable[[], datetime],
) -> AdvisorPort:
    """The adapter for one slot, reusing the canonical instance when untouched."""
    if not selection.model and not selection.key_set:
        reusable = canonical.get(selection.provider)
        if reusable is not None:
            return reusable
    return AdvisorFactory.create(
        selection.provider,
        selection.model,
        selection.api_key,
        project=project,
        cost_sink=cost_sink,
        clock=clock,
    )


def probe_connection(
    provider: str,
    model: str = "",
    credential: Optional[str] = None,
    *,
    transport: Optional[Callable[[Any], Any]] = None,
) -> str:
    """One Test Connection verdict for a configuration - never an exception.

    The adapter's own private probe performs the smallest safe authenticated call,
    so the endpoint, the auth scheme and the model reference all stay in the
    provider adapter. This function only builds that adapter and reads the
    verdict, and it can answer with exactly one of the
    :class:`~architecture_assistant.ports.capabilities.ConnectionStatus` values:
    ``CONNECTED``, ``AUTH ERROR``, ``PROVIDER ERROR``, ``NETWORK ERROR``,
    ``MODEL ERROR`` - or ``DISABLED`` for the disabled advisor, which makes no
    call at all. Nothing here ever renders a key, a header or a response body.
    """
    try:
        adapter = AdvisorFactory.create(
            provider, model, credential, transport=transport
        )
        probe = getattr(adapter, "_test_connection", None)
        if not callable(probe):
            return ConnectionStatus.PROVIDER_ERROR.value
        verdict = probe()
    except Exception:  # noqa: BLE001 - a probe reports a verdict, it never raises
        return ConnectionStatus.PROVIDER_ERROR.value
    try:
        return ConnectionStatus(verdict).value
    except ValueError:
        return ConnectionStatus.PROVIDER_ERROR.value
