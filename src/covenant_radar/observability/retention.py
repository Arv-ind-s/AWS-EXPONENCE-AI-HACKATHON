"""Durable log rotation, integrity sidecars and retention housekeeping."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from covenant_radar.observability.redaction import PromptLoggingError

DEFAULT_RETENTION_DAYS = 180
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_ROTATION_INTERVAL_SECONDS = 24 * 60 * 60
_ROTATED_FILE_RE = re.compile(r"^.+\.20\d{6}T\d{6}(?:\.\d{6})?Z(?:\.\d+)?$")


class LogIntegrityError(RuntimeError):
    """Raised when an integrity sidecar cannot be written or verified."""


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """Result of one retention pass."""

    deleted_files: int = 0
    deleted_bytes: int = 0
    errors: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.errors


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of ``path`` without loading it into memory."""

    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
    except OSError as error:
        raise LogIntegrityError(f"Unable to hash log file {path}: {error}") from error
    return digest.hexdigest()


def integrity_sidecar(path: Path) -> Path:
    """Return the sidecar path used for a log file's integrity digest."""

    return path.with_name(f"{path.name}.sha256")


def write_integrity_hash(path: Path, *, digest: str | None = None) -> Path:
    """Atomically write a standard ``sha256sum``-compatible sidecar.

    The temporary file is created beside the destination so ``os.replace`` is
    atomic on the filesystem that contains the log directory.  The sidecar is
    intentionally retained next to the rotated file for simple offline audit
    and tamper checks.
    """

    resolved_path = Path(path)
    if not resolved_path.is_file():
        raise LogIntegrityError(f"Cannot hash missing log file: {resolved_path}")
    value = digest or sha256_file(resolved_path)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise LogIntegrityError("A SHA-256 integrity digest must contain 64 lowercase hex digits.")

    sidecar = integrity_sidecar(resolved_path)
    temporary = sidecar.with_name(f".{sidecar.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{value}  {resolved_path.name}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, sidecar)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise LogIntegrityError(f"Unable to write integrity sidecar {sidecar}: {error}") from error
    return sidecar


def verify_integrity_hash(path: Path) -> bool:
    """Verify a log file against its sidecar, returning ``False`` on mismatch."""

    log_path = Path(path)
    sidecar = integrity_sidecar(log_path)
    try:
        line = sidecar.read_text(encoding="ascii").strip()
    except OSError as error:
        raise LogIntegrityError(f"Unable to read integrity sidecar {sidecar}: {error}") from error
    parts = line.split()
    if len(parts) != 2 or parts[1] != log_path.name or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
        raise LogIntegrityError(f"Malformed integrity sidecar: {sidecar}")
    return hmac.compare_digest(parts[0], sha256_file(log_path))


def purge_expired_logs(
    directory: Path,
    *,
    active_filenames: tuple[str, ...] = ("application.log", "model-call.log"),
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> RetentionReport:
    """Delete rotated logs older than the configured retention period.

    Only files matching the handler's timestamped archive naming scheme are
    eligible.  The active file, its sidecar, unrelated files and malformed
    names are never removed by this function.
    """

    if retention_days < 0:
        raise ValueError("retention_days cannot be negative")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cutoff = current.astimezone(UTC) - timedelta(days=retention_days)
    root = Path(directory)
    deleted_files = 0
    deleted_bytes = 0
    errors: list[str] = []
    try:
        entries = tuple(root.iterdir())
    except OSError as error:
        return RetentionReport(errors=(f"Unable to inspect log directory {root}: {error}",))

    active = frozenset(active_filenames)
    for candidate in entries:
        if not candidate.is_file() or candidate.name.endswith(".sha256"):
            continue
        if not any(candidate.name.startswith(f"{name}.") for name in active):
            continue
        if not _ROTATED_FILE_RE.match(candidate.name):
            continue
        try:
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            if modified > cutoff:
                continue
            size = candidate.stat().st_size
            candidate.unlink()
            deleted_files += 1
            deleted_bytes += size
            sidecar = integrity_sidecar(candidate)
            if sidecar.exists():
                sidecar_size = sidecar.stat().st_size
                sidecar.unlink()
                deleted_files += 1
                deleted_bytes += sidecar_size
        except OSError as error:
            errors.append(f"Unable to purge {candidate}: {error}")
    return RetentionReport(deleted_files, deleted_bytes, tuple(errors))


class IntegrityRotatingFileHandler(logging.Handler):
    """A size-or-time rotating handler that hashes every archive.

    ``logging.handlers`` provides either size or time rotation, while this
    handler needs both triggers and a sidecar written as part of rotation.  It
    also treats sink failures as an operational health signal: a logging
    outage must not turn a successful customer request into a 500 response.
    """

    def __init__(
        self,
        filename: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        interval_seconds: int = DEFAULT_ROTATION_INTERVAL_SECONDS,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        on_error: Callable[[Exception], None] | None = None,
        on_rotate: Callable[[], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        if retention_days < 0:
            raise ValueError("retention_days cannot be negative")
        self.filename = Path(filename)
        self.max_bytes = max_bytes
        self.interval_seconds = interval_seconds
        self.retention_days = retention_days
        self._on_error = on_error
        self._on_rotate = on_rotate
        self._now = now or (lambda: datetime.now(UTC))
        self._stream: TextIO | None = None
        self._opened_at: datetime | None = None
        self._next_rollover: datetime | None = None
        self._lock = threading.RLock()
        self.write_failures = 0
        self.rotated_files = 0
        self.last_error: str | None = None
        self._ensure_stream()

    @property
    def healthy(self) -> bool:
        return self._stream is not None and self.last_error is None

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            try:
                rendered = self.format(record)
                payload = rendered.encode("utf-8") + b"\n"
                if not self._ensure_stream():
                    return
                if self._rollover_required(len(payload)) and not self._rotate():
                    return
                if not self._ensure_stream():
                    return
                stream = self._stream
                if stream is None:
                    return
                stream.write(payload.decode("utf-8"))
                stream.flush()
                self._clear_error()
            except PromptLoggingError:
                raise
            except Exception as error:
                self._record_failure(error)

    def close(self) -> None:
        with self._lock:
            self._close_stream()
        super().close()

    def _ensure_stream(self) -> bool:
        if self._stream is not None:
            return True
        try:
            self.filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                self.filename,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                self._stream = os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
            except Exception:
                os.close(descriptor)
                raise
            opened = self._now().astimezone(UTC)
            self._opened_at = opened
            self._next_rollover = opened + timedelta(seconds=self.interval_seconds)
            self._clear_error()
            return True
        except (OSError, ValueError) as error:
            self._record_failure(error)
            return False

    def _rollover_required(self, payload_size: int) -> bool:
        try:
            current_size = self.filename.stat().st_size if self.filename.exists() else 0
        except OSError as error:
            self._record_failure(error)
            return False
        current = self._now().astimezone(UTC)
        time_due = self._next_rollover is not None and current >= self._next_rollover
        size_due = current_size > 0 and current_size + payload_size > self.max_bytes
        return bool(time_due or size_due)

    def _rotate(self) -> bool:
        self._close_stream()
        if not self.filename.is_file():
            return self._ensure_stream()
        timestamp = self._now().astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        archive = self.filename.with_name(f"{self.filename.name}.{timestamp}")
        suffix = 1
        while archive.exists() or integrity_sidecar(archive).exists():
            archive = self.filename.with_name(f"{self.filename.name}.{timestamp}.{suffix}")
            suffix += 1
        try:
            digest = sha256_file(self.filename)
            os.replace(self.filename, archive)
            write_integrity_hash(archive, digest=digest)
            self.rotated_files += 1
            report = purge_expired_logs(
                self.filename.parent,
                active_filenames=(self.filename.name,),
                retention_days=self.retention_days,
                now=self._now(),
            )
            if not report.healthy:
                raise LogIntegrityError("; ".join(report.errors))
            if self._on_rotate is not None:
                self._on_rotate()
            return self._ensure_stream()
        except Exception as error:
            self._record_failure(error)
            return False

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        self._opened_at = None
        self._next_rollover = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def _record_failure(self, error: Exception) -> None:
        self.write_failures += 1
        self.last_error = f"{type(error).__name__}: {error}"
        if self._on_error is not None:
            try:
                self._on_error(error)
            except Exception:
                # Health reporting is deliberately best effort and can never
                # recurse into the logging pipeline.
                pass

    def _clear_error(self) -> None:
        self.last_error = None


# Stable aliases for callers that use the conventional word order.
RotatingIntegrityFileHandler = IntegrityRotatingFileHandler
hash_file = sha256_file
write_hash = write_integrity_hash
verify_hash = verify_integrity_hash
purge_expired = purge_expired_logs


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_ROTATION_INTERVAL_SECONDS",
    "IntegrityRotatingFileHandler",
    "LogIntegrityError",
    "RetentionReport",
    "RotatingIntegrityFileHandler",
    "hash_file",
    "integrity_sidecar",
    "purge_expired",
    "purge_expired_logs",
    "sha256_file",
    "verify_hash",
    "verify_integrity_hash",
    "write_hash",
    "write_integrity_hash",
]
