"""SQLite cost plugin - the concrete, idempotent :class:`CostPort`.

This is the **accounting telemetry** capability, and it is deliberately the only
thing in the project that writes cost rows:

* provider adapters (OpenAI/Claude/Grok advisors, the OpenAI judge) only call
  ``cost_sink.record(CostRecord)`` - they never import ``sqlite3`` and never
  write here;
* the plugin holds the *same* connection and therefore the *same* transaction
  ownership rule as every other SQLite adapter (see
  :class:`~architecture_assistant.infrastructure.repositories._SqliteRepository`),
  so a ``record()`` inside ``storage.transaction()`` joins that transaction and
  is rolled back with it instead of committing early;
* the plugin never imports a provider adapter - it knows only the
  provider-neutral ``CostPort`` DTOs.

Idempotence
-----------
``event_id`` is the stable identity of one logical billable provider event, so
``record()`` is idempotent by construction:

* same ``event_id`` + same accounting content -> **no-op** (a replay, a
  recovery, an at-least-once redelivery or an accidental double call adds
  nothing);
* same ``event_id`` + different content -> :class:`CostIdentityConflictError`
  (fail closed - never overwrite, never double count);
* different ``event_id`` -> **two rows**, even if every other field is
  identical, because two genuine calls that happen to cost the same are two
  billable events.

``INSERT OR REPLACE`` is deliberately never used: it would silently destroy the
stored event. The table's ``UNIQUE(event_id)`` is the database-level backstop.

What is stored, and what it is not
----------------------------------
Actual token usage, provider, model, project, step, event id, timestamp, the
calculated cost when a price is known and the explicit ``pricing_known`` state.
An entry with ``pricing_known=False`` is *not* a free call - its ``cost_usd`` of
``0.0`` is an unknown, and :class:`CostSummary` reports priced and unpriced
record counts separately so nothing is presented as a known zero cost by
accident.

These are **estimates from a caller-supplied price table, not billing truth**:
prices change, differ per contract and are not the assistant's knowledge. There
is no billing-API integration and no hardcoded price table anywhere.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Sequence

from ..ports.capabilities import (
    CostIdentityConflictError,
    CostQuery,
    CostRecord,
    CostSummary,
)
from .repositories import _SqliteRepository, _parse_dt

__all__ = ["SqliteCostPlugin"]


class SqliteCostPlugin(_SqliteRepository):
    """Idempotent SQLite implementation of the ``CostPort`` capability."""

    _INSERT = """
        INSERT INTO cost_records (
            event_id, provider, model, input_tokens, output_tokens, cost_usd,
            pricing_known, project, step_no, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    _SELECT_BY_EVENT_ID = "SELECT * FROM cost_records WHERE event_id = ?"

    # -- CostPort ----------------------------------------------------------
    def record(self, cost: CostRecord) -> None:
        """Persist one cost event - idempotently, and never destructively.

        Look-first rather than insert-and-repair, because the two outcomes are
        genuinely different: a replay is a quiet no-op while a re-used identity
        with different content is an accounting defect that must be visible.
        Nothing is caught or swallowed here - a conflict or a database failure
        propagates, so the caller's telemetry error handling can surface it.
        """
        if not isinstance(cost, CostRecord):
            raise ValueError(f"cost must be a CostRecord; got {cost!r}")
        stored = self._fetch_one(
            self._SELECT_BY_EVENT_ID, (cost.event_id,), _row_to_cost
        )
        if stored is not None:
            if _same_accounting_event(stored, cost):
                return
            raise CostIdentityConflictError(
                f"cost event {cost.event_id!r} is already recorded with "
                "different accounting content"
            )
        self._execute_write(
            self._INSERT,
            (
                cost.event_id,
                cost.provider,
                cost.model,
                cost.input_tokens,
                cost.output_tokens,
                cost.cost_usd,
                1 if cost.pricing_known else 0,
                cost.project,
                cost.step_no,
                _utc_iso(cost.created_at),
            ),
        )

    def query(self, cost_filter: CostQuery) -> CostSummary:
        """Aggregate the stored events that match ``cost_filter``.

        The filter is narrowed in SQL on exactly the fields ``CostQuery``
        defines, and every fetched row is then re-checked with the canonical
        :meth:`CostQuery.matches` predicate, so the database and the contract
        can never disagree about what "matches" means.
        """
        if not isinstance(cost_filter, CostQuery):
            raise ValueError(
                f"cost_filter must be a CostQuery; got {cost_filter!r}"
            )
        where, params = _where_clause(cost_filter)
        stored = self._fetch_all(
            f"SELECT * FROM cost_records{where} ORDER BY id ASC",
            params,
            _row_to_cost,
        )
        return _summarize(
            [record for record in stored if cost_filter.matches(record)]
        )


# ---------------------------------------------------------------------------
# row <-> CostRecord
# ---------------------------------------------------------------------------

def _row_to_cost(row: sqlite3.Row) -> CostRecord:
    return CostRecord(
        provider=row["provider"],
        event_id=row["event_id"],
        model=row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"],
        pricing_known=bool(row["pricing_known"]),
        project=row["project"],
        step_no=row["step_no"],
        created_at=_parse_dt(row["created_at"]),
    )


def _utc_iso(value: datetime) -> str:
    """Serialize an instant in UTC so textual range comparisons stay ordered.

    ``created_at`` is stored as ISO-8601 ``TEXT`` like every other timestamp in
    the schema. Storing the UTC instant (and translating query bounds the same
    way) is what makes a plain string comparison in SQL chronological, which a
    stored local offset such as ``+03:00`` would silently break. A naive
    timestamp is interpreted as UTC - a documented, deterministic choice instead
    of guessing the machine's local zone.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _accounting_content(record: CostRecord) -> tuple:
    """The billable content of one event.

    ``created_at`` is deliberately excluded: a replayed event may carry a fresh
    clock and must still be recognised as the same event, while any real
    accounting difference (tokens, cost, pricing state, project, step, provider,
    model) still fails closed.
    """
    return (
        record.provider,
        record.model,
        record.input_tokens,
        record.output_tokens,
        record.cost_usd,
        record.pricing_known,
        record.project,
        record.step_no,
    )


def _same_accounting_event(stored: CostRecord, incoming: CostRecord) -> bool:
    """Whether two records describe the same billable event."""
    return _accounting_content(stored) == _accounting_content(incoming)


def _where_clause(cost_filter: CostQuery) -> tuple[str, tuple[Any, ...]]:
    """Translate the exact ``CostQuery`` fields into a SQL ``WHERE`` clause.

    Only the fields the contract defines are translated - no speculative
    analytics column is introduced - and every clause mirrors
    :meth:`CostQuery.matches` field for field.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if cost_filter.project is not None:
        clauses.append("project = ?")
        params.append(cost_filter.project)
    if cost_filter.step_no is not None:
        clauses.append("step_no = ?")
        params.append(cost_filter.step_no)
    if cost_filter.provider is not None:
        clauses.append("provider = ?")
        params.append(cost_filter.provider)
    if cost_filter.from_time is not None:
        clauses.append("created_at >= ?")
        params.append(_utc_iso(cost_filter.from_time))
    if cost_filter.to_time is not None:
        clauses.append("created_at <= ?")
        params.append(_utc_iso(cost_filter.to_time))
    if not clauses:
        return "", ()
    return " WHERE " + " AND ".join(clauses), tuple(params)


def _summarize(records: Sequence[CostRecord]) -> CostSummary:
    """Sum the records and keep priced and unpriced events apart.

    A record with ``pricing_known=False`` contributes its documented ``0.0`` to
    ``total_usd`` and is counted in ``unpriced_record_count``, so an unknown
    price is never reported as if it were a known zero-dollar cost. The total is
    rounded to 6 decimals - the same precision the price model uses - so summing
    floats stays deterministic.
    """
    total_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    priced = 0
    unpriced = 0
    for record in records:
        total_usd += record.cost_usd
        input_tokens += record.input_tokens
        output_tokens += record.output_tokens
        if record.pricing_known:
            priced += 1
        else:
            unpriced += 1
    return CostSummary(
        total_usd=round(total_usd, 6),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        record_count=len(records),
        priced_record_count=priced,
        unpriced_record_count=unpriced,
    )
