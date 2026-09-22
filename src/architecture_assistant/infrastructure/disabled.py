"""The disabled advisor: a deliberate "no provider" ``AdvisorPort``.

One of the five entries the operator may pick for an advisor pane is *Disabled*.
It is not a provider, and it is deliberately not the same thing as a missing
key, a broken endpoint or a blank model: it is the operator **choosing** that an
advisor slot participates without any provider at all.

That choice must be honest in both directions:

* :meth:`DisabledAdvisorAdapter.advise` makes **no provider call, ever**. It
  raises :class:`DisabledAdvisorAbstainError`, which the observation seam
  classifies as an **abstention** - so the review reports a clear
  disabled/abstain status for that slot and the two enabled advisors are
  completely unaffected. Nothing is inferred, invented or guessed;
* :meth:`DisabledAdvisorAdapter._test_connection` answers
  :attr:`~architecture_assistant.ports.capabilities.ConnectionStatus.DISABLED`
  without touching a network, because "there is nothing to connect to" is the
  truthful answer.

It carries nothing else: no key, no URL, no transport, no cost sink and no
clock. There is no state to configure because there is nothing to call.
"""

from __future__ import annotations

from ..domain.models import Finding
from ..ports.capabilities import AdvisorQuery, ConnectionStatus

__all__ = [
    "DISABLED_PROVIDER",
    "DISABLED_ROLE",
    "DisabledAdvisorError",
    "DisabledAdvisorAbstainError",
    "DisabledAdvisorAdapter",
]

#: The provider id the disabled advisor reports as ``Finding.source``.
#: It is the id of *no provider*, which is exactly what it means.
DISABLED_PROVIDER = "disabled"

#: The persona label of a disabled slot - adapter metadata, not a domain field.
DISABLED_ROLE = "Disabled"

#: The one sanitized reason a disabled advisor ever reports.
DISABLED_REASON = "advisor is disabled; no provider call was made"


class DisabledAdvisorError(Exception):
    """Base class for the disabled advisor.

    Deliberately **not** a provider error family: a disabled advisor has no
    provider to fail.
    """


class DisabledAdvisorAbstainError(DisabledAdvisorError):
    """A disabled advisor abstains on every question, without any call.

    It is an *abstention* - a deliberate, legitimate refusal to answer - never a
    provider failure, and the observation seam must therefore classify it as
    ``ABSTAIN`` rather than ``ERROR``.
    """

    def __init__(self, reason: str = DISABLED_REASON) -> None:
        text = reason.strip() if isinstance(reason, str) and reason.strip() else DISABLED_REASON
        super().__init__(f"advisor disabled: {text}")
        self.reason = text


class DisabledAdvisorAdapter:
    """The disabled slot of the advisor port: structurally valid, inert.

    It satisfies ``AdvisorPort`` (it has ``advise``) so a disabled slot can be
    wired exactly like a real advisor, and that is the whole point: the operator's
    choice is expressed as a configured advisor, never as a hole in the graph, a
    missing pane or a ``None`` someone has to special-case.
    """

    #: Read-only identity, for parity with the provider adapters.
    @property
    def provider(self) -> str:
        """The provider id this adapter reports as ``Finding.source``."""
        return DISABLED_PROVIDER

    @property
    def model(self) -> str:
        """A disabled advisor names no model - there is nothing to ask."""
        return ""

    @property
    def role(self) -> str:
        """The persona label - adapter metadata, not a domain field."""
        return DISABLED_ROLE

    @property
    def is_configured(self) -> bool:
        """Always ``False``: a disabled advisor is never ready to answer."""
        return False

    def __repr__(self) -> str:
        """Key-free by construction - a disabled advisor holds no secret."""
        return f"{type(self).__name__}(provider={DISABLED_PROVIDER!r})"

    # -- AdvisorPort -------------------------------------------------------
    def advise(self, query: AdvisorQuery) -> Finding:
        """Abstain without calling anything - every time, deterministically.

        The query is still validated (a wrong type is a programming defect, not
        a disabled advisor), and then the abstention is raised. No transport, no
        cost sink and no clock is ever touched.
        """
        if not isinstance(query, AdvisorQuery):
            raise ValueError(f"query must be an AdvisorQuery; got {query!r}")
        raise DisabledAdvisorAbstainError()

    # -- connection probe (Test Connection) --------------------------------
    def _test_connection(self) -> str:
        """Answer ``DISABLED`` - truthfully, and without any provider call."""
        return ConnectionStatus.DISABLED.value
