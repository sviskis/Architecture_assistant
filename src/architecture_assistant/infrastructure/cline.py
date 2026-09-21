"""Cline worker adapter - a file-channel implementation of :class:`WorkerPort`.

Every Cline-specific detail lives here and nowhere else: the task/report JSON
shapes, the ``protocol``/``project``/``plan_version`` header, the directory
layout and the ``step_NNN_attempt_MMM_*`` file naming. The rest of the system
only ever sees :class:`WorkerRequest` / :class:`WorkerResult`.

Lifecycle (deliberately split so that Step 8 owns the policy)::

    dispatch()            write context, then write the task as commit marker
    read_report()         read + validate only - never moves or deletes
    acknowledge_report()  move the report to the archive, after the caller has
                          durably accepted the result

Delivery is **at-least-once**: ``read_report`` keeps returning the same
validated result until the caller acknowledges it. Idempotent de-duplication of
processing belongs to the Step 8 orchestrator.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from ..domain.enums import ReportStatus
from ..domain.models import Task, utc_now
from ..ports.capabilities import WorkerRequest, WorkerResult

__all__ = [
    "DEFAULT_EXCHANGE_DIR",
    "DEFAULT_PROTOCOL",
    "DIRECTIVE_KIND",
    "DIRECTIVE_VERSION",
    "ClineWorkerError",
    "TaskDispatchError",
    "ReportNotAvailableError",
    "ReportParseError",
    "ReportMismatchError",
    "ClineWorkerAdapter",
]

#: Root of the assistant's own Cline file channel (never ``.mini_build``, which
#: belongs to the build controller).
DEFAULT_EXCHANGE_DIR: Path = Path("data") / "cline"

#: Protocol identifier written into every dispatched task.
DEFAULT_PROTOCOL = "architecture-assistant/v1"

#: Kind marker written into a supervision directive artifact, so a human reading
#: the exchange directory can tell a directive from a task without guessing.
DIRECTIVE_KIND = "supervisor-directive"

#: Schema version of the directive artifact itself. A later Phase may extend the
#: artifact; the version is what lets the Cline side tell two shapes apart.
DIRECTIVE_VERSION = "1"

_TO_CLINE = "to_cline"
_CONTEXT = "context"
_FROM_CLINE = "from_cline"
_ARCHIVE = "archive"


class ClineWorkerError(Exception):
    """Base class for Cline worker adapter errors."""


class TaskDispatchError(ClineWorkerError):
    """Raised when the task or its context could not be published."""


class ReportNotAvailableError(ClineWorkerError):
    """Raised when the expected report file does not exist yet."""


class ReportParseError(ClineWorkerError):
    """Raised when a report file exists but is not valid JSON of the expected shape."""


class ReportMismatchError(ClineWorkerError):
    """Raised when a report belongs to a different step or attempt."""


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` atomically via a temp file and ``os.replace``.

    The temp file lives in the destination directory so the rename is atomic,
    and it carries a ``.tmp`` suffix which readers deliberately ignore.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class ClineWorkerAdapter:
    """File-channel Cline worker adapter.

    Implements :class:`~architecture_assistant.ports.capabilities.WorkerPort` for
    plugin-registry conformance and additionally exposes the finer-grained
    file-channel lifecycle (``dispatch`` / ``read_report`` /
    ``acknowledge_report``) that the orchestrator drives.
    """

    def __init__(
        self,
        exchange_dir: Path = DEFAULT_EXCHANGE_DIR,
        *,
        protocol: str = DEFAULT_PROTOCOL,
        project: str = "",
        plan_version: str = "",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be a callable returning a datetime")
        self._exchange_dir = Path(exchange_dir)
        self._protocol = protocol
        self._project = project
        self._plan_version = plan_version
        self._clock = clock

    # -- paths -----------------------------------------------------------
    @property
    def exchange_dir(self) -> Path:
        """Root of the Cline file channel."""
        return self._exchange_dir

    @staticmethod
    def _slug(step_no: int, attempt: int) -> str:
        """Deterministic file identity - the attempt is part of the name."""
        return f"step_{step_no:03d}_attempt_{attempt:03d}"

    def task_path(self, step_no: int, attempt: int) -> Path:
        """Path of the dispatched task file (the commit marker)."""
        return (
            self._exchange_dir
            / _TO_CLINE
            / f"{self._slug(step_no, attempt)}_task.json"
        )

    def context_path(self, step_no: int, attempt: int) -> Path:
        """Path of the context file referenced by the task."""
        return (
            self._exchange_dir
            / _CONTEXT
            / f"{self._slug(step_no, attempt)}_context.json"
        )

    def report_path(self, step_no: int, attempt: int) -> Path:
        """Path of the report file written by the worker."""
        return (
            self._exchange_dir
            / _FROM_CLINE
            / f"{self._slug(step_no, attempt)}_report.json"
        )

    def archive_dir(self) -> Path:
        """Directory holding acknowledged reports."""
        return self._exchange_dir / _FROM_CLINE / _ARCHIVE

    def directive_path(self, step_no: int, attempt: int) -> Path:
        """Path of the supervision directive artifact for one dispatch.

        Same deterministic identity as the task and the report
        (``step_NNN_attempt_MMM``), same exchange channel: a directive is not a
        second channel, it is another artifact of the one Cline talks through.
        """
        return (
            self._exchange_dir
            / _TO_CLINE
            / f"{self._slug(step_no, attempt)}_directive.json"
        )

    # -- dispatch --------------------------------------------------------
    def dispatch(self, request: WorkerRequest) -> Path:
        """Publish the context, then publish the task as the commit marker.

        The order is deliberate: the worker may only start once the task file
        exists, and by then the context is guaranteed to be present. If the
        context write fails the task is **never** published, so the worker can
        never observe a task without its context.
        """
        task = request.task
        context_path = self.context_path(task.step_no, task.attempt)
        task_path = self.task_path(task.step_no, task.attempt)

        context_text = _dump_json(dict(request.context))
        try:
            _atomic_write_text(context_path, context_text)
        except Exception as error:  # noqa: BLE001 - re-raised as adapter error
            raise TaskDispatchError(
                f"failed to write context {context_path.name}: {error}"
            ) from error

        task_text = _dump_json(self._task_payload(task, context_path))
        try:
            _atomic_write_text(task_path, task_text)
        except Exception as error:  # noqa: BLE001 - re-raised as adapter error
            raise TaskDispatchError(
                f"failed to write task {task_path.name}: {error}"
            ) from error
        return task_path

    def _task_payload(self, task: Task, context_path: Path) -> dict[str, Any]:
        """Build the Cline task JSON - Cline-specific fields live only here."""
        return {
            "protocol": self._protocol,
            "project": self._project,
            "plan_version": self._plan_version,
            "step_no": task.step_no,
            "attempt": task.attempt,
            "max_attempts": task.max_attempts,
            "phase": task.phase.value,
            "title": task.title,
            "description": task.description,
            "risk": task.risk.value,
            "context_file": str(context_path.resolve()),
            "instructions": dict(task.instructions),
            "report_schema": dict(task.report_schema),
            "created_at": task.created_at.isoformat(),
        }


    # -- report reading (read-only and repeatable) -----------------------
    def read_report(self, step_no: int, attempt: int) -> Optional[WorkerResult]:
        """Read and validate the report for ``(step_no, attempt)``.

        Returns ``None`` while the report has not been published. This method is
        strictly read-only - it never moves, deletes or rewrites the file - so it
        can be called repeatedly and always yields the same result until the
        caller acknowledges it (at-least-once delivery).
        """
        path = self.report_path(step_no, attempt)
        if not path.is_file():
            return None
        data = self._load_report_object(path)
        self._validate_report(data, path, step_no, attempt)
        return self._to_worker_result(data)

    # -- acknowledgement (explicit, caller-driven) -----------------------
    def read_report_bytes(self, step_no: int, attempt: int) -> Optional[bytes]:
        """The **exact** report bytes, or ``None`` while none exists.

        Supervision identity is a SHA-256 over these bytes, so this method hands
        over what the worker actually wrote: not parsed, not sanitized, not
        re-encoded. The atomic-write protocol above means a partially written
        file can never appear as a report - an in-flight write lives in a
        ``.tmp`` sibling and is only renamed into place when it is complete - so
        there is nothing to filter out here beyond "the file does not exist yet".
        """
        path = self.report_path(step_no, attempt)
        if not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError as error:
            raise ReportParseError(
                f"cannot read {path.name}: {error}"
            ) from error

    def publish_directive(
        self, directive: Mapping[str, Any], *, step_no: int, attempt: int
    ) -> Path:
        """Publish one directive artifact to the Cline channel.

        Written atomically, exactly like a task, so a worker can never read half
        a directive. Publication is deliberately **at-least-once**: republishing
        the identical directive is safe because the artifact is deterministic and
        the name is derived from ``(step_no, attempt)`` only - which is also why
        the caller re-checks whose directive it is holding before treating a
        publication as its own.

        A directive without ``supervision_id`` and ``source_report_hash`` is
        refused: an artifact that cannot be identified must never reach the
        worker, because nothing afterwards could tell which report it answers.
        """
        payload = dict(directive)
        for required in ("supervision_id", "source_report_hash"):
            value = payload.get(required)
            if not isinstance(value, str) or not value.strip():
                raise TaskDispatchError(
                    f"a directive artifact requires a non-empty {required!r}; "
                    f"got {value!r}"
                )
        path = self.directive_path(step_no, attempt)
        try:
            _atomic_write_text(path, _dump_json(payload))
        except Exception as error:  # noqa: BLE001 - re-raised as adapter error
            raise TaskDispatchError(
                f"failed to write directive {path.name}: {error}"
            ) from error
        return path

    def read_directive(
        self, step_no: int, attempt: int
    ) -> Optional[Mapping[str, Any]]:
        """Read back the published directive artifact, or ``None``.

        Strictly read-only. The caller uses it to reconcile a durable send
        intent, so a partially written file must never be mistaken for a
        published one: an unparsable artifact raises instead of returning an
        empty mapping, and the caller treats "present but unreadable" as *not*
        its own directive rather than as success.
        """
        path = self.directive_path(step_no, attempt)
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ReportParseError(
                f"cannot read {path.name}: {error}"
            ) from error
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ReportParseError(
                f"{path.name} is not valid JSON: {error}"
            ) from error
        if not isinstance(data, dict):
            raise ReportParseError(
                f"{path.name} must contain a JSON object; "
                f"got {type(data).__name__}"
            )
        return data

    def acknowledge_report(self, step_no: int, attempt: int) -> Path:
        """Move an already-read report into the archive and return its new path.

        Only the caller knows when a result has been durably processed, so this
        is never invoked from :meth:`read_report`. An invalid, mismatching or
        missing report is refused and left exactly where it is.
        """
        path = self.report_path(step_no, attempt)
        if not path.is_file():
            raise ReportNotAvailableError(
                f"no report to acknowledge at {path.name}"
            )
        data = self._load_report_object(path)
        self._validate_report(data, path, step_no, attempt)

        archive = self.archive_dir()
        archive.mkdir(parents=True, exist_ok=True)
        stamp = self._clock().strftime("%Y%m%d_%H%M%S")
        target = archive / f"{path.stem}_{stamp}.json"
        os.replace(path, target)
        return target

    # -- WorkerPort conformance ------------------------------------------
    def run(self, request: WorkerRequest) -> WorkerResult:
        """Dispatch, then perform exactly one non-blocking read.

        Deliberately not a polling loop: no sleep, no timeout scheduler and no
        acknowledgement. When the report is not ready this raises
        :class:`ReportNotAvailableError` and leaves the lifecycle decision to the
        caller.
        """
        task = request.task
        self.dispatch(request)
        result = self.read_report(task.step_no, task.attempt)
        if result is None:
            raise ReportNotAvailableError(
                "report for "
                f"{self._slug(task.step_no, task.attempt)} is not available yet"
            )
        return result


    # -- internals -------------------------------------------------------
    def _load_report_object(self, path: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ReportParseError(f"cannot read {path.name}: {error}") from error
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ReportParseError(
                f"{path.name} is not valid JSON: {error}"
            ) from error
        if not isinstance(data, dict):
            raise ReportParseError(
                f"{path.name} must contain a JSON object; "
                f"got {type(data).__name__}"
            )
        return data

    def _validate_report(
        self,
        data: Mapping[str, Any],
        path: Path,
        step_no: int,
        attempt: int,
    ) -> None:
        reported_step = data.get("step_no")
        if isinstance(reported_step, bool) or not isinstance(reported_step, int):
            raise ReportParseError(
                f"{path.name} step_no must be an int; got {reported_step!r}"
            )
        if reported_step != step_no:
            raise ReportMismatchError(
                f"{path.name} reports step_no={reported_step}; expected {step_no}"
            )

        reported_attempt = data.get("attempt")
        if isinstance(reported_attempt, bool) or not isinstance(
            reported_attempt, int
        ):
            raise ReportParseError(
                f"{path.name} attempt must be an int; got {reported_attempt!r}"
            )
        if reported_attempt != attempt:
            raise ReportMismatchError(
                f"{path.name} reports attempt={reported_attempt}; "
                f"expected {attempt}"
            )

        status = data.get("status")
        if not isinstance(status, str) or status not in _REPORT_STATUSES:
            raise ReportParseError(
                f"{path.name} has invalid status {status!r}; expected one of "
                + ", ".join(sorted(_REPORT_STATUSES))
            )

        summary = data.get("summary", "")
        if not isinstance(summary, str):
            raise ReportParseError(
                f"{path.name} summary must be a string; "
                f"got {type(summary).__name__}"
            )

        for field in _STRING_LIST_FIELDS:
            if field in data:
                _require_string_list(data[field], field, path)

    def _to_worker_result(self, data: Mapping[str, Any]) -> WorkerResult:
        """Map the report onto the provider-neutral result.

        Only the documented core fields are lifted out; every Cline-specific
        field stays inside ``raw``.
        """
        return WorkerResult(
            status=ReportStatus(data["status"]),
            summary=data.get("summary", ""),
            artifacts=tuple(data.get("files_created", ())),
            issues=tuple(data.get("issues", ())),
            architecture_questions=tuple(data.get("architecture_questions", ())),
            raw=dict(data),
        )


#: Accepted report statuses (the assistant's Cline contract).
_REPORT_STATUSES = frozenset(member.value for member in ReportStatus)

#: Report fields that must be lists of strings when present.
_STRING_LIST_FIELDS = (
    "files_created",
    "files_changed",
    "files_deleted",
    "issues",
    "architecture_questions",
    "dependencies_added",
)


def _dump_json(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON text: sorted keys, non-ASCII preserved."""
    return json.dumps(dict(payload), sort_keys=True, ensure_ascii=False, indent=2)


def _require_string_list(value: Any, field: str, path: Path) -> None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ReportParseError(
            f"{path.name} field {field!r} must be a list of strings; got {value!r}"
        )
