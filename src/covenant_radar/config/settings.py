"""Immutable, validated application settings loaded at process startup."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

if TYPE_CHECKING:
    from covenant_radar.config.capabilities import Capabilities

ENV_PREFIX = "COVENANT_RADAR_"
CONFIG_FILE_ENV = f"{ENV_PREFIX}CONFIG"
KEYRING_SERVICE = "covenant-radar"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "default.toml"
DOTENV_PATH = DEFAULT_CONFIG_PATH.parents[1] / ".env"
# Set to a false value to ignore `.env` entirely. A test run sets this, so a
# suite reads the same configuration on every machine instead of whatever the
# developer happens to have configured for the live gateway.
DOTENV_ENABLED_ENV = f"{ENV_PREFIX}DOTENV"
# Read by `load_deployment_environment` to gate the model-registry guard.
# Deliberately outside the validated `Settings` schema, so it must be excluded
# from the unknown-key scan below the same way `CONFIG_FILE_ENV` and
# `DOTENV_ENABLED_ENV` are. The helper applies the same process-environment /
# `.env` precedence as `load_settings`; otherwise a development workspace can
# load provider credentials from `.env` while this one guard behaves as prod.
DEPLOYMENT_ENVIRONMENT_ENV = f"{ENV_PREFIX}ENVIRONMENT"
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_DEVELOPMENT_ENVIRONMENT = "development"
_PRODUCTION_ENVIRONMENT = "production"


class SettingsError(RuntimeError):
    """Raised when startup configuration cannot be trusted."""


class _KeyringBackend(Protocol):
    """The small OS-keyring surface used without making it a runtime dependency."""

    def get_password(self, service_name: str, username: str) -> str | None:
        """Return the password associated with an application secret name."""


class DatabaseSettings(BaseModel):
    """Connection and pool settings for the configured database engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    pool_size: int = Field(ge=1)
    max_overflow: int = Field(ge=0)
    connect_timeout_seconds: int = Field(ge=1)


class SecuritySettings(BaseModel):
    """Authentication and browser-security configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_cookie_name: str
    session_secret: SecretStr | None = None
    sso_provider: Literal["none", "oidc", "saml"]
    sso_issuer: str | None = None
    sso_client_id: str | None = None
    sso_client_secret: SecretStr | None = None


class DocumentsSettings(BaseModel):
    """Document-store and OCR settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store: Literal["none", "local", "s3"]
    local_path: Path | None = None
    s3_bucket: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    ocr_enabled: bool
    ocr_command: str | None = None


class AiSettings(BaseModel):
    """Model-provider settings, including the offline recorded adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["none", "recorded", "tcs", "azure_openai", "anthropic"]
    endpoint: str | None = None
    model: str | None = None
    recorded_responses_path: Path | None = None
    api_key: SecretStr | None = None
    # Extra trust anchors, not a replacement set: a deployment behind a
    # TLS-inspecting proxy presents a chain signed by the organisation's own
    # CA, which is in the operating-system store but never in the bundle
    # httpx ships with. Naming that bundle here is the supported way to trust
    # it; certificate verification itself stays on and cannot be turned off.
    ca_bundle: Path | None = None


class NotificationsSettings(BaseModel):
    """SMTP and webhook-delivery settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    smtp_host: str | None = None
    smtp_port: int = Field(ge=1, le=65535)
    smtp_sender: str | None = None
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    webhooks_enabled: bool
    webhook_signing_secret: SecretStr | None = None


class IngestionSettings(BaseModel):
    """Inbound-file and connector settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_drop_path: Path
    poll_interval_seconds: int = Field(ge=1)


class ObservabilitySettings(BaseModel):
    """Logging, metrics and tracing settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    metrics_enabled: bool
    tracing_enabled: bool
    tracing_endpoint: str | None = None


class WebSettings(BaseModel):
    """ASGI listener configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    port: int = Field(ge=1, le=65535)
    workers: int = Field(ge=1)
    # The live workspace is progressive enhancement and remains opt-in until
    # a deployment has verified its polling budget and operational workflow.
    live_workspace_enabled: bool = False


class ForecastSettings(BaseModel):
    """Deterministic forecast configuration shared by jobs and screens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    horizons: tuple[int, ...] = (30, 60, 90)
    distance_weight: float = Field(gt=0)
    velocity_weight: float = Field(ge=0)
    pressure_weight: float = Field(ge=0)
    max_probability: float = Field(gt=0, lt=1)
    ml_enabled: bool = False
    ml_artifact_path: Path | None = None
    # `shadow` runs the ML challenger beside the deterministic model and
    # records its prediction without changing what the queue shows.
    # `champion` lets it replace the deterministic probability, and is honoured
    # only when the model register also carries an approved registration for
    # the challenger component.
    ml_mode: Literal["shadow", "champion"] = "shadow"


class Settings(BaseModel):
    """The complete, immutable configuration required by the application."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database: DatabaseSettings
    security: SecuritySettings
    documents: DocumentsSettings
    ai: AiSettings
    notifications: NotificationsSettings
    ingestion: IngestionSettings
    observability: ObservabilitySettings
    web: WebSettings
    forecast: ForecastSettings

    @property
    def capabilities(self) -> Capabilities:
        """Return the configured capabilities without attempting to use them."""
        from covenant_radar.config.capabilities import Capabilities

        return Capabilities.from_settings(self)


@dataclass(frozen=True)
class _Origin:
    """Source metadata used to make startup failures actionable."""

    description: str
    file_path: Path | None = None
    line: int | None = None


_SECRET_ENVIRONMENT_VARIABLES: dict[tuple[str, ...], str] = {
    ("security", "session_secret"): "COVENANT_RADAR_SECURITY_SESSION_SECRET",
    ("security", "sso_client_secret"): "COVENANT_RADAR_SECURITY_SSO_CLIENT_SECRET",
    ("documents", "s3_access_key_id"): "COVENANT_RADAR_DOCUMENTS_S3_ACCESS_KEY_ID",
    ("documents", "s3_secret_access_key"): "COVENANT_RADAR_DOCUMENTS_S3_SECRET_ACCESS_KEY",
    ("ai", "api_key"): "COVENANT_RADAR_AI_API_KEY",
    ("notifications", "smtp_password"): "COVENANT_RADAR_NOTIFICATIONS_SMTP_PASSWORD",
    (
        "notifications",
        "webhook_signing_secret",
    ): "COVENANT_RADAR_NOTIFICATIONS_WEBHOOK_SIGNING_SECRET",
}
# These credentials are loaded by ``security.secrets`` rather than mapped into
# the Pydantic settings model. They still need to be excluded from generic
# ``COVENANT_RADAR_*`` parsing or a valid crypto configuration is rejected as
# an unknown settings key during application startup.
_EXTERNAL_SECRET_ENVIRONMENT_VARIABLES = frozenset(
    {
        "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_KEY",
        "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_KEYS",
        "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_ACTIVE_KEY_ID",
        "COVENANT_RADAR_SECURITY_CIN_FINGERPRINT_KEY",
    }
)
_SECRET_KEY_TOKENS = (
    "access_key",
    "api_key",
    "password",
    "private_key",
    "secret",
    "token",
)
_DATABASE_URL_CREDENTIALS = re.compile(r"://[^/@:]+:[^/@]+@")


def get_settings() -> Settings:
    """Return the settings validated exactly once during module import."""
    return _SETTINGS


def load_settings(
    config_file: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings with defaults, file overrides, then environment overrides.

    When the process environment is used, a repository-root ``.env`` is read
    underneath it: a value already exported by the caller always wins, so CI
    and explicitly configured deployments stay predictable. Passing
    ``environ`` explicitly skips the file entirely, and so does setting
    ``COVENANT_RADAR_DOTENV`` to a false value — both keep a caller hermetic
    on a developer machine that has a ``.env``.
    """
    if environ is None:
        environment = dict(os.environ)
        if environment.get(DOTENV_ENABLED_ENV, "").strip().lower() not in _FALSE_VALUES:
            _apply_dotenv(environment, DOTENV_PATH)
    else:
        environment = dict(environ)
    defaults, default_lines = _load_toml(DEFAULT_CONFIG_PATH)
    _reject_file_secrets(defaults, DEFAULT_CONFIG_PATH, default_lines)
    _reject_unknown_keys(defaults, Settings, _file_origins(DEFAULT_CONFIG_PATH, default_lines))

    merged: dict[str, Any] = {}
    origins: dict[tuple[str, ...], _Origin] = {}
    _merge(merged, defaults, _file_origins(DEFAULT_CONFIG_PATH, default_lines), origins)

    selected_file = _configuration_file(config_file, environment)
    if selected_file is not None and selected_file.resolve() != DEFAULT_CONFIG_PATH.resolve():
        configured, configured_lines = _load_toml(selected_file)
        _reject_file_secrets(configured, selected_file, configured_lines)
        configured_origins = _file_origins(selected_file, configured_lines)
        _reject_unknown_keys(configured, Settings, configured_origins)
        _merge(merged, configured, configured_origins, origins)

    environment_values, environment_origins = _environment_overrides(environment)
    _reject_unknown_keys(environment_values, Settings, environment_origins)
    _merge(merged, environment_values, environment_origins, origins)
    _inject_secret_sources(merged, origins, environment)

    try:
        settings = Settings.model_validate(merged)
    except ValidationError as error:
        raise _validation_error(error, origins) from error

    _validate_dependent_settings(settings)
    return settings


def load_deployment_environment(environ: Mapping[str, str] | None = None) -> str:
    """Resolve the model-governance environment, defaulting safely to production.

    Explicit mappings are hermetic, just like :func:`load_settings`. When the
    process environment is used, `.env` may fill an unset value but can never
    override an exported one. Only the literal ``development`` relaxes the
    model-register guard; missing and unknown values collapse to production.
    """

    if environ is None:
        environment = dict(os.environ)
        if environment.get(DOTENV_ENABLED_ENV, "").strip().lower() not in _FALSE_VALUES:
            _apply_dotenv(environment, DOTENV_PATH)
    else:
        environment = dict(environ)
    value = environment.get(DEPLOYMENT_ENVIRONMENT_ENV, "").strip().lower()
    if value == _DEVELOPMENT_ENVIRONMENT:
        return _DEVELOPMENT_ENVIRONMENT
    return _PRODUCTION_ENVIRONMENT


_DOTENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_DOTENV_TRAILING_COMMENT = re.compile(r"\s+#.*$")


def _apply_dotenv(environment: dict[str, str], path: Path) -> None:
    """Fill blank or missing variables from ``path``, never overriding a set one.

    A blank entry in ``.env`` is a documentation placeholder — every variable
    the application understands is listed in ``.env.example`` — so it must not
    override the safe defaults in ``config/default.toml``. An unreadable or
    absent file is not an error: ``.env`` is a developer convenience, and a
    deployment configures itself through the environment or a TOML file.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOTENV_ASSIGNMENT.match(stripped)
        if match is None:
            continue
        name, raw_value = match.group(1), match.group(2).strip()
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {'"', "'"}:
            value = raw_value[1:-1]
        else:
            value = _DOTENV_TRAILING_COMMENT.sub("", raw_value)
        if not value.strip():
            continue
        if not environment.get(name, "").strip():
            environment[name] = value


def _configuration_file(config_file: Path | str | None, environ: Mapping[str, str]) -> Path | None:
    if config_file is not None:
        path = Path(config_file)
    elif configured_path := environ.get(CONFIG_FILE_ENV):
        path = Path(configured_path)
    else:
        return None

    if not path.is_file():
        raise SettingsError(f"Configuration file not found: {path}")
    return path


def _load_toml(path: Path) -> tuple[dict[str, Any], dict[tuple[str, ...], int]]:
    if not path.is_file():
        raise SettingsError(f"Configuration file not found: {path}")

    try:
        content = path.read_text(encoding="utf-8")
        parsed = tomllib.loads(content)
    except OSError as error:
        raise SettingsError(f"Configuration file cannot be read: {path}") from error
    except tomllib.TOMLDecodeError as error:
        line_match = re.search(r"line (\d+)", str(error))
        line = line_match.group(1) if line_match else "unknown"
        raise SettingsError(
            f"Invalid TOML in configuration file {path}, line {line}: {error}"
        ) from error

    return parsed, _toml_line_numbers(content)


def _toml_line_numbers(content: str) -> dict[tuple[str, ...], int]:
    line_numbers: dict[tuple[str, ...], int] = {}
    section: tuple[str, ...] = ()

    for number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = tuple(part.strip().strip('"') for part in stripped[1:-1].split("."))
            continue
        match = re.match(r"\s*([A-Za-z0-9_-]+)\s*=", line)
        if match:
            line_numbers[section + (match.group(1),)] = number

    return line_numbers


def _file_origins(
    path: Path, line_numbers: Mapping[tuple[str, ...], int]
) -> dict[tuple[str, ...], _Origin]:
    return {
        location: _Origin(description=f"file {path}", file_path=path, line=line)
        for location, line in line_numbers.items()
    }


def _reject_file_secrets(
    values: Mapping[str, Any], path: Path, line_numbers: Mapping[tuple[str, ...], int]
) -> None:
    location = _find_secret_location(values)
    if location is None:
        return

    line = line_numbers.get(location, "unknown")
    key = ".".join(location)
    raise SettingsError(
        f"Secret configuration key '{key}' is forbidden in file {path}, line {line}."
    )


def _find_secret_location(
    values: Mapping[str, Any], prefix: tuple[str, ...] = ()
) -> tuple[str, ...] | None:
    for key, value in values.items():
        location = prefix + (key,)
        normalized_key = key.lower()
        if any(token in normalized_key for token in _SECRET_KEY_TOKENS):
            return location
        if (
            normalized_key == "url"
            and isinstance(value, str)
            and _DATABASE_URL_CREDENTIALS.search(value)
        ):
            return location
        if (
            isinstance(value, Mapping)
            and (found := _find_secret_location(value, location)) is not None
        ):
            return found
    return None


def _environment_overrides(
    environ: Mapping[str, str],
) -> tuple[dict[str, Any], dict[tuple[str, ...], _Origin]]:
    values: dict[str, Any] = {}
    origins: dict[tuple[str, ...], _Origin] = {}
    secret_names = set(_SECRET_ENVIRONMENT_VARIABLES.values())
    secret_names.update(_EXTERNAL_SECRET_ENVIRONMENT_VARIABLES)

    for name, value in environ.items():
        if (
            not name.startswith(ENV_PREFIX)
            or name in {CONFIG_FILE_ENV, DOTENV_ENABLED_ENV, DEPLOYMENT_ENVIRONMENT_ENV}
            or name in secret_names
        ):
            continue
        path = tuple(part.lower() for part in name.removeprefix(ENV_PREFIX).split("__"))
        if not path or any(not part for part in path):
            continue
        _set_path(values, path, value)
        origins[path] = _Origin(description=f"environment variable {name}")

    return values, origins


def _inject_secret_sources(
    values: dict[str, Any], origins: dict[tuple[str, ...], _Origin], environ: Mapping[str, str]
) -> None:
    for path, environment_variable in _SECRET_ENVIRONMENT_VARIABLES.items():
        if _path_exists(values, path):
            continue
        value = _secret_from_environment_or_keyring(environment_variable, environ)
        if value is None:
            continue
        _set_path(values, path, value)
        origins[path] = _Origin(description=f"environment variable {environment_variable}")


def _secret_from_environment_or_keyring(
    environment_variable: str, environ: Mapping[str, str]
) -> str | None:
    if environment_variable in environ:
        return environ[environment_variable] or None

    try:
        keyring = cast(_KeyringBackend, import_module("keyring"))
    except ModuleNotFoundError:
        return None

    return keyring.get_password(KEYRING_SERVICE, environment_variable)


def _path_exists(values: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = values
    for component in path:
        if not isinstance(current, Mapping) or component not in current:
            return False
        current = current[component]
    return True


def _set_path(values: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = values
    for component in path[:-1]:
        child = current.setdefault(component, {})
        if not isinstance(child, dict):
            raise SettingsError(
                f"Configuration key '{'.'.join(path)}' conflicts with a scalar value."
            )
        current = child
    current[path[-1]] = value


def _merge(
    destination: dict[str, Any],
    source: Mapping[str, Any],
    source_origins: Mapping[tuple[str, ...], _Origin],
    destination_origins: dict[tuple[str, ...], _Origin],
    prefix: tuple[str, ...] = (),
) -> None:
    for key, value in source.items():
        location = prefix + (key,)
        if isinstance(value, Mapping):
            existing = destination.get(key)
            if existing is None:
                existing = {}
                destination[key] = existing
            if not isinstance(existing, dict):
                raise SettingsError(
                    f"Configuration key '{'.'.join(location)}' conflicts with a scalar value."
                )
            _merge(existing, value, source_origins, destination_origins, location)
            continue
        destination[key] = value
        if origin := source_origins.get(location):
            destination_origins[location] = origin


def _reject_unknown_keys(
    values: Mapping[str, Any],
    model: type[BaseModel],
    origins: Mapping[tuple[str, ...], _Origin],
    prefix: tuple[str, ...] = (),
) -> None:
    for key, value in values.items():
        location = prefix + (key,)
        field = model.model_fields.get(key)
        if field is None:
            origin = origins.get(location)
            raise _unknown_key_error(location, origin)
        nested_model = _nested_model(field.annotation)
        if nested_model is not None and isinstance(value, Mapping):
            _reject_unknown_keys(value, nested_model, origins, location)


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for candidate in get_args(annotation):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _unknown_key_error(location: tuple[str, ...], origin: _Origin | None) -> SettingsError:
    key = ".".join(location)
    if origin is None:
        return SettingsError(f"Unknown configuration key '{key}'.")
    if origin.file_path is not None:
        return SettingsError(
            f"Unknown configuration key '{key}' in file {origin.file_path}, line {origin.line}."
        )
    return SettingsError(f"Unknown configuration key '{key}' from {origin.description}.")


def _validation_error(
    error: ValidationError, origins: Mapping[tuple[str, ...], _Origin]
) -> SettingsError:
    detail = error.errors()[0]
    location = tuple(str(part) for part in detail["loc"])
    key = ".".join(location)
    origin = origins.get(location)
    allowed_values = _allowed_values(location)
    message = detail["msg"]

    if origin is not None and origin.file_path is not None:
        return SettingsError(
            f"Invalid configuration key '{key}' in file {origin.file_path}, line {origin.line}: "
            f"{message}. Allowed values: {allowed_values}."
        )
    if origin is not None:
        return SettingsError(
            f"Invalid configuration key '{key}' from {origin.description}: {message}. "
            f"Allowed values: {allowed_values}."
        )
    return SettingsError(
        f"Invalid configuration key '{key}': {message}. Allowed values: {allowed_values}."
    )


def _allowed_values(location: tuple[str, ...]) -> str:
    model: type[BaseModel] = Settings
    field: Any = None
    for component in location:
        field = model.model_fields.get(component)
        if field is None:
            return "a value accepted by the settings schema"
        nested_model = _nested_model(field.annotation)
        if nested_model is not None:
            model = nested_model

    if field is not None and get_origin(field.annotation) is Literal:
        return ", ".join(str(value) for value in get_args(field.annotation))
    if field is not None and field.annotation is bool:
        return "true, false"
    return "a value accepted by the settings schema"


def _validate_dependent_settings(settings: Settings) -> None:
    if settings.security.sso_provider != "none":
        _require_value(settings.security.sso_issuer, "security.sso_issuer")
        _require_value(settings.security.sso_client_id, "security.sso_client_id")
        _require_secret(
            settings.security.sso_client_secret, "COVENANT_RADAR_SECURITY_SSO_CLIENT_SECRET"
        )

    if settings.documents.store == "local":
        _require_value(settings.documents.local_path, "documents.local_path")
    if settings.documents.store == "s3":
        _require_value(settings.documents.s3_bucket, "documents.s3_bucket")
        _require_secret(
            settings.documents.s3_access_key_id, "COVENANT_RADAR_DOCUMENTS_S3_ACCESS_KEY_ID"
        )
        _require_secret(
            settings.documents.s3_secret_access_key,
            "COVENANT_RADAR_DOCUMENTS_S3_SECRET_ACCESS_KEY",
        )
    if settings.documents.ocr_enabled:
        _require_value(settings.documents.ocr_command, "documents.ocr_command")

    if settings.ai.provider == "recorded":
        _require_value(settings.ai.recorded_responses_path, "ai.recorded_responses_path")
    if settings.ai.provider not in {"none", "recorded"}:
        _require_value(settings.ai.endpoint, "ai.endpoint")
        _require_value(settings.ai.model, "ai.model")
        _require_secret(settings.ai.api_key, "COVENANT_RADAR_AI_API_KEY")
    if settings.ai.ca_bundle is not None and not settings.ai.ca_bundle.is_file():
        raise SettingsError(
            f"Configuration key 'ai.ca_bundle' names a file that does not exist: "
            f"{settings.ai.ca_bundle}."
        )

    if settings.notifications.smtp_host is not None:
        _require_value(settings.notifications.smtp_sender, "notifications.smtp_sender")
        if settings.notifications.smtp_username is not None:
            _require_secret(
                settings.notifications.smtp_password, "COVENANT_RADAR_NOTIFICATIONS_SMTP_PASSWORD"
            )
    if settings.notifications.webhooks_enabled:
        _require_secret(
            settings.notifications.webhook_signing_secret,
            "COVENANT_RADAR_NOTIFICATIONS_WEBHOOK_SIGNING_SECRET",
        )
    if settings.observability.tracing_enabled:
        _require_value(settings.observability.tracing_endpoint, "observability.tracing_endpoint")


def _require_value(value: object, key: str) -> None:
    if value is None or value == "":
        raise SettingsError(f"Configuration key '{key}' is required by the selected capability.")


def _require_secret(value: SecretStr | None, environment_variable: str) -> None:
    if value is None or not value.get_secret_value():
        raise SettingsError(f"Missing required secret: {environment_variable}.")


_SETTINGS = load_settings()
