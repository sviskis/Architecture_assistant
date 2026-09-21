"""Offline, deterministic supervisor - the ``SupervisorPort`` for tests and pilots.

The assistant must be able to run the whole supervision lifecycle with **no
provider, no key, no network and no cost**, and a test must be able to describe
exactly what a supervisor answers without simulating a model. This adapter is
that: a scripted double.

It is deliberately dumb and completely predictable:

* it answers from a fixed, ordered script (``responses``) and falls back to a
  configured default (``NO_ACTION``) once the script is exhausted;
* it never sleeps, never retries, never randomizes and never touches the
  network or the filesystem;
* it records every :class:`SupervisorContext` it was handed, so a test can assert
  *what the supervisor was shown* - which is how the "bounded facts, no chat
  history, no secret" promise is actually checked.

It is an infrastructure adapter and therefore knows only ``domain`` and
``ports``: it holds no storage, no transaction boundary, no worker channel and no
FSM, so even a hostile script could not move a Step or set ``VERIFIED``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from ..domain.enums import SupervisorAction, SupervisorRisk
from ..ports.capabilities import SupervisorContext, SupervisorResult

__all__ = [
    "DEFAULT_SCRIPTED_PROVIDER",
    "ScriptedSupervisor",
    "scripted_result",
    "no_action",
]

#: The provider identity recorded on every answer this adapter produces.
DEFAULT_SCRIPTED_PROVIDER = "scripted"

#: One scripted answer: either a ready result or the fields to build one.
ScriptEntry = Union[SupervisorResult, Mapping[str, Any], str, None]


def scripted_result(
    action: Union[SupervisorAction, str],
    *,
    reason: str = "",
    risk: Union[SupervisorRisk, str] = SupervisorRisk.LOW,
    instruction_for_cline: str = "",
    requires_human: bool = False,
    evidence: Sequence[str] = (),
    provider: str = DEFAULT_SCRIPTED_PROVIDER,
) -> SupervisorResult:
    """Build one deterministic scripted answer."""
    resolved = (
        action if isinstance(action, SupervisorAction) else SupervisorAction(action)
    )
    return SupervisorResult(
        action=resolved,
        reason=reason or f"scripted {resolved.value}",
        risk=risk,
        instruction_for_cline=instruction_for_cline,
        requires_human=requires_human,
        evidence=tuple(evidence),
        provider=provider,
    )


def no_action(
    reason: str = "nothing to correct in this report",
    *,
    provider: str = DEFAULT_SCRIPTED_PROVIDER,
) -> SupervisorResult:
    """The answer that lets the authoritative review proceed."""
    return scripted_result(
        SupervisorAction.NO_ACTION, reason=reason, provider=provider
    )


class ScriptedSupervisor:
    """A supervisor whose answers are given to it, in order, up front.

    The script is consumed **in call order**: the first analysis takes the first
    entry, the second the second, and so on. That is exactly what a test needs to
    describe "the report was corrected and the second analysis was satisfied" -
    and because the assistant performs at most one analysis per supervision
    identity, a re-run after a crash is the only way the same identity can
    consume a second entry (which the tests exercise deliberately).
    """

    def __init__(
        self,
        responses: Optional[Iterable[ScriptEntry]] = None,
        *,
        default: ScriptEntry = None,
        provider: str = DEFAULT_SCRIPTED_PROVIDER,
        failure: Optional[BaseException] = None,
    ) -> None:
        if failure is not None and not isinstance(failure, BaseException):
            raise ValueError(
                f"failure must be an exception instance; got {failure!r}"
            )
        self._script: list[ScriptEntry] = list(responses or ())
        self._default = default
        self._provider = provider
        self._failure = failure
        #: Every context this supervisor was asked to analyse, in order.
        self.contexts: list[SupervisorContext] = []

    @property
    def provider(self) -> str:
        """The provider identity recorded on answers."""
        return self._provider

    @property
    def call_count(self) -> int:
        """How many times ``supervise`` was called."""
        return len(self.contexts)

    def reset(self) -> None:
        """Rewind the script and forget the recorded contexts."""
        self._script = []
        self.contexts = []

    def script(self, responses: Iterable[ScriptEntry]) -> None:
        """Replace the remaining script (the call log is kept)."""
        self._script = list(responses)

    def supervise(self, context: SupervisorContext) -> SupervisorResult:
        """Answer deterministically - and record what was asked."""
        if not isinstance(context, SupervisorContext):
            raise ValueError(
                f"context must be a SupervisorContext; got {context!r}"
            )
        self.contexts.append(context)
        if self._failure is not None:
            raise self._failure
        entry = self._script.pop(0) if self._script else self._default
        return self._build(entry)

    def _build(self, entry: ScriptEntry) -> SupervisorResult:
        """Turn one script entry into a result."""
        if entry is None:
            return no_action(provider=self._provider)
        if isinstance(entry, SupervisorResult):
            return entry
        if isinstance(entry, str):
            return scripted_result(entry, provider=self._provider)
        if isinstance(entry, Mapping):
            fields = dict(entry)
            fields.setdefault("provider", self._provider)
            return scripted_result(fields.pop("action"), **fields)
        raise ValueError(
            "a scripted supervisor answer must be a SupervisorResult, a mapping, "
            f"an action name or None; got {entry!r}"
        )
