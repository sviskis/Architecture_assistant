"""Infrastructure layer: the concrete adapters of the assistant.

Three families live here: the SQLite persistence adapters (which are also the only
code that imports :mod:`sqlite3`), the Cline file-channel worker adapter and the
OpenAI advisor adapter. Nothing in ``domain``, ``ports`` or ``application``
imports this package except the composition root and tests.
"""

from __future__ import annotations

from .cline import (
    DEFAULT_EXCHANGE_DIR,
    DEFAULT_PROTOCOL,
    ClineWorkerAdapter,
    ClineWorkerError,
    ReportMismatchError,
    ReportNotAvailableError,
    ReportParseError,
    TaskDispatchError,
)
from .migrations import (
    AUDIT_TABLE_NAME,
    BOOTSTRAP_SCHEMA_SQL,
    CHANGE_REQUEST_TABLE_NAME,
    MIGRATIONS,
    TABLE_NAMES,
    Migration,
)
from .openai import (
    ABSTAIN_REASON_FALLBACK,
    DEFAULT_BACKOFF_SCHEDULE,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    IMPLEMENTATION_ANALYST_ROLE,
    OPENAI_API_KEY_ENV_VAR,
    OPENAI_CHAT_COMPLETIONS_URL,
    OPENAI_PROVIDER,
    UNKNOWN_PRICE,
    HttpRequest,
    HttpResponse,
    HttpTransportError,
    MissingApiKeyError,
    ModelPrice,
    OpenAIAdvisorAbstainError,
    OpenAIAdvisorAdapter,
    OpenAIAdvisorError,
    OpenAIAdvisorHttpError,
    OpenAIAdvisorInvalidResponseError,
    OpenAIAdvisorRateLimitError,
    OpenAIAdvisorTimeoutError,
    OpenAIAdvisorTransportError,
    urllib_transport,
)
from .repositories import (
    SqliteADRRepository,
    SqliteArchitectureChangeRequestRepository,
    SqliteArchitectureVersionRepository,
    SqliteAuditRepository,
    SqliteDecisionRepository,
    SqliteFindingRepository,
    SqliteProjectRepository,
    SqliteRiskRepository,
    SqliteStepRepository,
    SqliteTaskRepository,
)
from .storage import SqliteStorage
from .sqlite import (
    DEFAULT_DATABASE_PATH,
    DatabasePath,
    SqliteTransactionPort,
    applied_versions,
    apply_migrations,
    close_database,
    open_database,
)

__all__ = [
    # connection + migrations
    "DEFAULT_DATABASE_PATH",
    "DatabasePath",
    "open_database",
    "close_database",
    "apply_migrations",
    "applied_versions",
    "SqliteTransactionPort",
    "Migration",
    "MIGRATIONS",
    "BOOTSTRAP_SCHEMA_SQL",
    "TABLE_NAMES",
    "AUDIT_TABLE_NAME",
    "CHANGE_REQUEST_TABLE_NAME",
    # adapters
    "SqliteStorage",
    "SqliteProjectRepository",
    "SqliteStepRepository",
    "SqliteTaskRepository",
    "SqliteArchitectureVersionRepository",
    "SqliteArchitectureChangeRequestRepository",
    "SqliteADRRepository",
    "SqliteRiskRepository",
    "SqliteFindingRepository",
    "SqliteDecisionRepository",
    "SqliteAuditRepository",
    # Cline file-channel worker adapter
    "ClineWorkerAdapter",
    "DEFAULT_EXCHANGE_DIR",
    "DEFAULT_PROTOCOL",
    "ClineWorkerError",
    "TaskDispatchError",
    "ReportNotAvailableError",
    "ReportParseError",
    "ReportMismatchError",
    # OpenAI advisor adapter (Step 12)
    "OpenAIAdvisorAdapter",
    "OpenAIAdvisorError",
    "MissingApiKeyError",
    "OpenAIAdvisorTransportError",
    "OpenAIAdvisorTimeoutError",
    "OpenAIAdvisorRateLimitError",
    "OpenAIAdvisorHttpError",
    "OpenAIAdvisorInvalidResponseError",
    "OpenAIAdvisorAbstainError",
    "HttpTransportError",
    "HttpRequest",
    "HttpResponse",
    "urllib_transport",
    "ModelPrice",
    "UNKNOWN_PRICE",
    "OPENAI_PROVIDER",
    "IMPLEMENTATION_ANALYST_ROLE",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_BACKOFF_SCHEDULE",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "ABSTAIN_REASON_FALLBACK",
    "OPENAI_CHAT_COMPLETIONS_URL",
    "OPENAI_API_KEY_ENV_VAR",
]
