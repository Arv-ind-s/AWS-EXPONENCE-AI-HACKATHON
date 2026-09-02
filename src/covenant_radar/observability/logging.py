"""Structured logging with redaction, sampling and durable safe sinks.

``configure()`` is called once at process startup. Application code only uses
``structlog.get_logger(__name__)`` after that point. The application and
model-call streams share the sanitized JSON formatter but use separate files;
the audit stream remains owned by the audit subsystem and is never routed
through these handlers.
"""

from __future__ import annotations

import logging
import random
import re
import sys
import threading
import tomllib
import traceback
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, TextIO

import structlog
from structlog.stdlib import ProcessorFormatter

from covenant_radar.core.context import get_job_run_id, get_request_id
from covenant_radar.observability.redaction import (
    DEFAULT_PERSONAL_FIELDS,
    DEFAULT_SECRET_KEY_TOKENS,
    DEFAULT_SECRET_PATTERNS,
    PromptLoggingError,
    RedactionPolicy,
    RedactionProcessor,
)
from covenant_radar.observability.retention import (
    DEFAULT_MAX_BYTES,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_ROTATION_INTERVAL_SECONDS,
    IntegrityRotatingFileHandler,
    LogIntegrityError,
    purge_expired_logs,
)

Processor = Callable[..., Any]

DEFAULT_LOGGING_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "logging.toml"
_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_MODEL_LOGGER_PREFIXES = (
    "covenant_radar.ai",
    "covenant_radar.model_call",
    "model_call",
)


class LoggingConfigError(RuntimeError):
    """Raised when ``config/logging.toml`` cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SamplingSettings:
    """Sampling rates, resolved by exact name or longest logger prefix."""

    default_rate: float = 1.0
    logger_rates: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoggingConfiguration:
    """Validated logging configuration independent of the logging runtime."""

    level_name: str
    redaction_policy: RedactionPolicy
    log_directory: Path | None
    application_filename: str
    model_call_filename: str
    max_bytes: int
    rotation_interval_seconds: int
    retention_days: int
    sampling: SamplingSettings


_HEALTH_LOCK = threading.RLock()
_HEALTH: dict[str, object] = {
    "status": "unconfigured",
    "healthy": True,
    "durable": False,
    "directory": None,
    "application_file": None,
    "model_call_file": None,
    "retention_days": DEFAULT_RETENTION_DAYS,
    "write_failures": 0,
    "rotated_files": 0,
    "last_error": None,
}


def configure(
    config_file: Path | str | None = None,
    *,
    log_directory: Path | str | None = None,
    stdout: bool = True,
) -> None:
    """Configure structlog and the process root logger.

    Configuration errors are fatal because an untrusted redaction policy must
    never be silently accepted. Runtime sink errors are different: stdout
    remains available and the error is exposed through :func:`logging_health`
    so a customer request can still complete.
    """

    path = Path(config_file) if config_file is not None else DEFAULT_LOGGING_CONFIG_PATH
    configuration = _read_config(path)
    if log_directory is not None:
        configuration = LoggingConfiguration(
            level_name=configuration.level_name,
            redaction_policy=configuration.redaction_policy,
            log_directory=Path(log_directory),
            application_filename=configuration.application_filename,
            model_call_filename=configuration.model_call_filename,
            max_bytes=configuration.max_bytes,
            rotation_interval_seconds=configuration.rotation_interval_seconds,
            retention_days=configuration.retention_days,
            sampling=configuration.sampling,
        )

    level = _LEVELS[configuration.level_name]
    redaction = RedactionProcessor(configuration.redaction_policy)
    sampling = SamplingProcessor(configuration.sampling)
    formatter = _make_formatter(redaction)
    handlers: list[logging.Handler] = []
    _reset_health(configuration)

    if stdout:
        stream_handler = _ResilientStreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    if configuration.log_directory is not None:
        directory = configuration.log_directory
        application_handler = IntegrityRotatingFileHandler(
            directory / configuration.application_filename,
            max_bytes=configuration.max_bytes,
            interval_seconds=configuration.rotation_interval_seconds,
            retention_days=configuration.retention_days,
            on_error=_record_sink_error,
            on_rotate=_record_rotation,
        )
        application_handler.addFilter(_ApplicationLogFilter())
        application_handler.setFormatter(formatter)
        handlers.append(application_handler)

        model_handler = IntegrityRotatingFileHandler(
            directory / configuration.model_call_filename,
            max_bytes=configuration.max_bytes,
            interval_seconds=configuration.rotation_interval_seconds,
            retention_days=configuration.retention_days,
            on_error=_record_sink_error,
            on_rotate=_record_rotation,
        )
        model_handler.addFilter(_ModelCallLogFilter())
        model_handler.setFormatter(formatter)
        handlers.append(model_handler)

        with _HEALTH_LOCK:
            _HEALTH["application_file"] = str(directory / configuration.application_filename)
            _HEALTH["model_call_file"] = str(directory / configuration.model_call_filename)
            _HEALTH["durable"] = True
        retention_report = purge_expired_logs(
            directory,
            active_filenames=(
                configuration.application_filename,
                configuration.model_call_filename,
            ),
            retention_days=configuration.retention_days,
        )
        if not retention_report.healthy:
            _record_sink_error(LogIntegrityError("; ".join(retention_report.errors)))

    if not handlers:
        raise LoggingConfigError("Logging requires stdout or a durable log directory.")

    logging.basicConfig(level=level, handlers=handlers, force=True)
    structlog.configure(
        processors=[
            _bind_causal_ids,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redaction,
            structlog.processors.StackInfoRenderer(),
            _format_exc_info_safely,
            # Exception rendering happens after the first pass and can carry
            # sensitive text from an exception message, so sanitize again.
            redaction,
            sampling,
            ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def _make_formatter(redaction: RedactionProcessor) -> ProcessorFormatter:
    pre_chain: list[Processor] = [
        _bind_causal_ids,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redaction,
        structlog.processors.StackInfoRenderer(),
        _format_exc_info_safely,
        redaction,
    ]
    return ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redaction,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=pre_chain,
    )


def _bind_causal_ids(
    logger: object,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Attach both causal identifiers, null outside either context."""

    del logger, method_name
    event_dict.setdefault("request_id", get_request_id())
    event_dict.setdefault("job_run_id", get_job_run_id())
    return event_dict


def _redact(key_tokens: tuple[str, ...]) -> Processor:
    """Compatibility factory retained for callers of the T-005 processor."""

    return RedactionProcessor(
        RedactionPolicy(
            personal_fields=DEFAULT_PERSONAL_FIELDS,
            secret_key_tokens=key_tokens,
            secret_patterns=DEFAULT_SECRET_PATTERNS,
        )
    )


#: How many frames of the failing call stack are recorded, innermost last.
_EXCEPTION_FRAME_LIMIT = 20
#: How deep a `raise ... from ...` / `during handling` chain is followed.
_EXCEPTION_CHAIN_LIMIT = 5


def _format_exc_info_safely(
    logger: object,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Record where an exception came from, without its untrusted message.

    Exception messages routinely contain provider responses, filenames or
    customer input, and no regex pass can guarantee an arbitrary message is
    safe — so the message is still never written to this stream.

    Recording *only* the class, though, left an operator with
    ``{"error_class": "UndefinedError"}`` and nothing else: no file, no line,
    no cause.  A code location carries no user data, so this also records the
    frame trail (``file:line:function``) and the chained exception classes.
    That is enough to find the defect from the log alone, which is the whole
    point of writing it.
    """

    del logger, method_name
    value = event_dict.pop("exc_info", None)
    if value is True:
        value = sys.exc_info()
    if isinstance(value, tuple) and len(value) == 3:
        exception = value[1]
        if isinstance(exception, BaseException):
            event_dict["exception_class"] = type(exception).__name__
            event_dict["exception_location"] = _exception_frames(exception)
            chain = _exception_chain(exception)
            if chain:
                event_dict["exception_chain"] = chain
    return event_dict


def _exception_frames(exception: BaseException) -> list[str]:
    """The failing call stack as `file:line:function`, innermost last.

    Only code positions are emitted — never a source line, an argument or a
    local — so nothing here can carry customer data.
    """

    try:
        frames = traceback.extract_tb(exception.__traceback__)
    except Exception:  # pragma: no cover - a broken traceback must not mask the error
        return []
    return [
        f"{PurePath(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames[-_EXCEPTION_FRAME_LIMIT:]
    ]


def _exception_chain(exception: BaseException) -> list[str]:
    """Class names of the causes behind `exception`, nearest cause first."""

    chain: list[str] = []
    current = exception.__cause__ or exception.__context__
    seen: set[int] = {id(exception)}
    while current is not None and len(chain) < _EXCEPTION_CHAIN_LIMIT:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return chain


class SamplingProcessor:
    """Drop sampled debug/info events while retaining warnings and errors."""

    def __init__(self, settings: SamplingSettings) -> None:
        self.settings = settings

    def __call__(
        self,
        logger: object,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        if method_name in {"warning", "error", "exception", "critical"}:
            return event_dict
        rate = self._rate_for(getattr(logger, "name", ""))
        if rate >= 1.0:
            return event_dict
        if rate <= 0.0 or random.random() >= rate:
            raise structlog.DropEvent
        return event_dict

    def _rate_for(self, logger_name: object) -> float:
        name = str(logger_name)
        matches = [
            (prefix, rate)
            for prefix, rate in self.settings.logger_rates.items()
            if name == prefix or name.startswith(f"{prefix}.")
        ]
        if not matches:
            return self.settings.default_rate
        return max(matches, key=lambda item: len(item[0]))[1]


class _ModelCallLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return any(
            record.name == prefix or record.name.startswith(f"{prefix}.")
            for prefix in _MODEL_LOGGER_PREFIXES
        )


class _ApplicationLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _ModelCallLogFilter().filter(record)


class _ResilientStreamHandler(logging.StreamHandler[TextIO]):
    """Keep logging problems from changing application request outcomes.

    The stream is resolved at emit time rather than captured at
    configuration time.  `logging.StreamHandler` normally holds the exact
    object it was constructed with, so a process that replaces `sys.stdout`
    after `configure()` — a supervisor reopening it, a test harness capturing
    it — keeps writing to the old, possibly closed one.  Following the current
    `sys.stdout` is what a stdout sink is supposed to mean.
    """

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, value: TextIO) -> None:
        # `StreamHandler.__init__` and `setStream` assign here; the current
        # `sys.stdout` is always the answer, so the assignment is accepted
        # and ignored rather than making those callers fail.
        del value

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except PromptLoggingError:
            raise
        except Exception as error:
            _record_sink_error(error)


def logging_health() -> dict[str, object]:
    """Return a JSON-safe snapshot for the public health view."""

    with _HEALTH_LOCK:
        return dict(_HEALTH)


def get_logging_health() -> dict[str, object]:
    """Compatibility alias for health integrations."""

    return logging_health()


def _reset_health(configuration: LoggingConfiguration) -> None:
    with _HEALTH_LOCK:
        _HEALTH.update(
            {
                "status": "healthy",
                "healthy": True,
                "durable": configuration.log_directory is not None,
                "directory": str(configuration.log_directory)
                if configuration.log_directory is not None
                else None,
                "application_file": None,
                "model_call_file": None,
                "retention_days": configuration.retention_days,
                "write_failures": 0,
                "rotated_files": 0,
                "last_error": None,
            }
        )


def _record_sink_error(error: Exception) -> None:
    with _HEALTH_LOCK:
        _HEALTH["status"] = "degraded"
        _HEALTH["healthy"] = False
        failures = _HEALTH["write_failures"]
        _HEALTH["write_failures"] = (failures if isinstance(failures, int) else 0) + 1
        _HEALTH["last_error"] = f"{type(error).__name__}: {error}"


def _record_rotation() -> None:
    with _HEALTH_LOCK:
        rotated = _HEALTH["rotated_files"]
        _HEALTH["rotated_files"] = (rotated if isinstance(rotated, int) else 0) + 1


def _read_config(path: Path) -> LoggingConfiguration:
    content = _read_toml(path)
    level_name = content.get("level", "INFO")
    if not isinstance(level_name, str) or level_name not in _LEVELS:
        allowed = ", ".join(_LEVELS)
        raise LoggingConfigError(
            f"Invalid log level '{level_name}' in {path}. Allowed values: {allowed}."
        )

    redaction_table = _table(content, "redaction", path)
    key_tokens = _string_tuple(
        redaction_table.get("key_tokens", DEFAULT_SECRET_KEY_TOKENS),
        field="redaction.key_tokens",
        path=path,
    )
    personal_fields = _string_tuple(
        redaction_table.get(
            "personal_fields",
            redaction_table.get(
                "personal_field_names",
                redaction_table.get("field_names", tuple(sorted(DEFAULT_PERSONAL_FIELDS))),
            ),
        ),
        field="redaction.personal_fields",
        path=path,
    )
    secret_patterns = _string_tuple(
        redaction_table.get(
            "value_patterns",
            redaction_table.get(
                "secret_patterns",
                redaction_table.get("patterns", DEFAULT_SECRET_PATTERNS),
            ),
        ),
        field="redaction.value_patterns",
        path=path,
    )
    reject_prompt_bodies = redaction_table.get("reject_prompt_bodies", True)
    if not isinstance(reject_prompt_bodies, bool):
        raise LoggingConfigError(f"redaction.reject_prompt_bodies must be boolean in {path}.")
    try:
        policy = RedactionPolicy(
            personal_fields=frozenset(personal_fields),
            secret_key_tokens=key_tokens,
            secret_patterns=secret_patterns,
            reject_prompt_bodies=reject_prompt_bodies,
        )
    except (TypeError, ValueError, re.error) as error:
        raise LoggingConfigError(f"Invalid redaction policy in {path}: {error}") from error

    rotation = _table(content, "rotation", path)
    directory_value = rotation.get("directory", content.get("log_directory"))
    directory = _optional_path(directory_value, field="rotation.directory", path=path)
    application_filename = _safe_filename(
        rotation.get("application_filename", "application.log"),
        field="rotation.application_filename",
        path=path,
    )
    model_call_filename = _safe_filename(
        rotation.get("model_call_filename", "model-call.log"),
        field="rotation.model_call_filename",
        path=path,
    )
    max_bytes = _positive_int(
        rotation.get("max_bytes", rotation.get("max_size_bytes", DEFAULT_MAX_BYTES)),
        field="rotation.max_bytes",
        path=path,
    )
    interval_seconds = _positive_int(
        rotation.get(
            "interval_seconds",
            rotation.get("rotation_interval_seconds", DEFAULT_ROTATION_INTERVAL_SECONDS),
        ),
        field="rotation.interval_seconds",
        path=path,
    )
    retention_days = _non_negative_int(
        rotation.get("retention_days", DEFAULT_RETENTION_DAYS),
        field="rotation.retention_days",
        path=path,
    )
    if retention_days < DEFAULT_RETENTION_DAYS:
        raise LoggingConfigError(
            f"rotation.retention_days must be at least {DEFAULT_RETENTION_DAYS} in {path}."
        )

    sampling_table = _table(content, "sampling", path)
    default_rate = _rate(sampling_table.get("default_rate", 1.0), "sampling.default_rate", path)
    configured_rates: dict[str, float] = {}
    logger_table = sampling_table.get("loggers", sampling_table.get("logger_rates", {}))
    if logger_table is not None:
        if not isinstance(logger_table, Mapping):
            raise LoggingConfigError(f"sampling.loggers must be a table in {path}.")
        for logger_name, value in logger_table.items():
            if not isinstance(logger_name, str) or not logger_name.strip():
                raise LoggingConfigError(f"sampling logger names must be non-empty in {path}.")
            configured_rates[logger_name] = _rate(value, f"sampling.loggers.{logger_name}", path)
    # Also accept direct logger keys in [sampling] for a compact TOML form.
    for logger_name, value in sampling_table.items():
        if logger_name not in {"default_rate", "loggers"}:
            configured_rates[logger_name] = _rate(value, f"sampling.{logger_name}", path)

    return LoggingConfiguration(
        level_name=level_name,
        redaction_policy=policy,
        log_directory=directory,
        application_filename=application_filename,
        model_call_filename=model_call_filename,
        max_bytes=max_bytes,
        rotation_interval_seconds=interval_seconds,
        retention_days=retention_days,
        sampling=SamplingSettings(default_rate, configured_rates),
    )


def _load_config(path: Path) -> tuple[str, tuple[str, ...]]:
    """Return the original T-005 config pair for backwards compatibility."""

    configuration = _read_config(path)
    return configuration.level_name, tuple(
        token.lower() for token in configuration.redaction_policy.secret_key_tokens
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise LoggingConfigError(f"Logging configuration file not found: {path}")
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise LoggingConfigError(f"Unable to read logging configuration {path}: {error}") from error
    if not isinstance(value, dict):
        raise LoggingConfigError(f"Logging configuration must be a TOML table: {path}")
    return value


def _table(content: Mapping[str, Any], name: str, path: Path) -> Mapping[str, Any]:
    value = content.get(name, {})
    if not isinstance(value, Mapping):
        raise LoggingConfigError(f"{name} must be a TOML table in {path}.")
    return value


def _string_tuple(value: object, *, field: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise LoggingConfigError(f"{field} must be an array of strings in {path}.")
    result = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    if len(result) != len(value):
        raise LoggingConfigError(f"{field} must contain only non-empty strings in {path}.")
    return result


def _optional_path(value: object, *, field: str, path: Path) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise LoggingConfigError(f"{field} must be a path string in {path}.")
    return Path(value)


def _safe_filename(value: object, *, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoggingConfigError(f"{field} must be a non-empty filename in {path}.")
    candidate = Path(value)
    if candidate.name != value or value in {".", ".."}:
        raise LoggingConfigError(f"{field} must not contain a directory component in {path}.")
    return value


def _positive_int(value: object, *, field: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LoggingConfigError(f"{field} must be a positive integer in {path}.")
    return value


def _non_negative_int(value: object, *, field: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LoggingConfigError(f"{field} must be a non-negative integer in {path}.")
    return value


def _rate(value: object, field: str, path: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise LoggingConfigError(f"{field} must be a number between 0 and 1 in {path}.")
    return float(value)


__all__ = [
    "DEFAULT_LOGGING_CONFIG_PATH",
    "LoggingConfigError",
    "LoggingConfiguration",
    "SamplingProcessor",
    "SamplingSettings",
    "configure",
    "get_logging_health",
    "logging_health",
]
