"""Tests for startup configuration, secret handling and capability reporting."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from covenant_radar.config import settings as settings_module
from covenant_radar.config.settings import (
    ENV_PREFIX,
    SettingsError,
    load_deployment_environment,
    load_settings,
)


def _write_config(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _isolated_process_environment(monkeypatch: pytest.MonkeyPatch, dotenv: Path) -> None:
    """Point the loader at *dotenv* with no inherited application variables.

    This clears ``COVENANT_RADAR_DOTENV`` along with the rest, so these tests
    exercise the file-reading path that ``tests/conftest.py`` switches off for
    every other test in the suite.
    """
    for name in [name for name in os.environ if name.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings_module, "DOTENV_PATH", dotenv)


def test_precedence_env_over_file(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path / "settings.toml", "[web]\nport = 8100\n")

    settings = load_settings(
        config_file,
        environ={"COVENANT_RADAR_WEB__PORT": "8200"},
    )

    assert settings.web.port == 8200


def test_invalid_value_names_key_file_and_line(tmp_path: Path) -> None:
    config_file = _write_config(
        tmp_path / "settings.toml", '[observability]\nlog_level = "TRACE"\n'
    )

    with pytest.raises(SettingsError) as raised:
        load_settings(config_file, environ={})

    message = str(raised.value)
    assert "observability.log_level" in message
    assert str(config_file) in message
    assert "line 2" in message
    assert "DEBUG, INFO, WARNING, ERROR, CRITICAL" in message


def test_missing_secret_refuses_start(tmp_path: Path) -> None:
    config_file = _write_config(
        tmp_path / "settings.toml",
        '[ai]\nprovider = "azure_openai"\nendpoint = "https://model.example"\nmodel = "credit"\n',
    )

    with pytest.raises(SettingsError, match="COVENANT_RADAR_AI_API_KEY"):
        load_settings(config_file, environ={})


def test_crypto_secret_environment_variables_are_not_settings_keys() -> None:
    settings = load_settings(
        environ={
            "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_KEY": "field-key",
            "COVENANT_RADAR_SECURITY_FIELD_ENCRYPTION_ACTIVE_KEY_ID": "v1",
            "COVENANT_RADAR_SECURITY_CIN_FINGERPRINT_KEY": "fingerprint-key",
        }
    )

    assert settings.security.sso_provider == "none"


def test_secret_in_file_refuses_start(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path / "settings.toml", '[ai]\napi_key = "forbidden"\n')

    with pytest.raises(SettingsError, match="Secret configuration key 'ai.api_key'"):
        load_settings(config_file, environ={})


def test_unknown_key_refuses(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path / "settings.toml", "[web]\nports = 8100\n")

    with pytest.raises(SettingsError, match="Unknown configuration key 'web.ports'"):
        load_settings(config_file, environ={})


def test_capabilities_reflect_configuration(tmp_path: Path) -> None:
    config_file = _write_config(
        tmp_path / "settings.toml",
        """[ai]
provider = "recorded"
recorded_responses_path = "evaluation/cassettes"

[documents]
store = "local"
local_path = "var/documents"
ocr_enabled = true
ocr_command = "tesseract"

[notifications]
smtp_host = "mail.internal"
smtp_sender = "radar@example.test"
webhooks_enabled = false
""",
    )

    capabilities = load_settings(config_file, environ={}).capabilities

    assert capabilities.model_provider.configured
    assert not capabilities.sso.configured
    assert capabilities.ocr.configured
    assert capabilities.smtp.configured
    assert not capabilities.webhooks.configured
    assert capabilities.document_store.configured


def test_settings_immutable_after_load() -> None:
    settings = load_settings(environ={})

    with pytest.raises(ValidationError):
        settings.web.port = 8100


def test_dotenv_fills_unset_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# a comment line\n"
        "export COVENANT_RADAR_WEB__PORT=8300\n"
        'COVENANT_RADAR_OBSERVABILITY__LOG_LEVEL="DEBUG"\n'
        "COVENANT_RADAR_WEB__HOST=\n",
        encoding="utf-8",
    )
    _isolated_process_environment(monkeypatch, dotenv)

    settings = load_settings()

    assert settings.web.port == 8300
    assert settings.observability.log_level == "DEBUG"
    # A blank entry documents the variable; it must not override the default.
    assert settings.web.host == "127.0.0.1"


def test_process_environment_wins_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("COVENANT_RADAR_WEB__PORT=8300\n", encoding="utf-8")
    _isolated_process_environment(monkeypatch, dotenv)
    monkeypatch.setenv("COVENANT_RADAR_WEB__PORT", "8400")

    assert load_settings().web.port == 8400


def test_dotenv_can_be_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("COVENANT_RADAR_WEB__PORT=8300\n", encoding="utf-8")
    _isolated_process_environment(monkeypatch, dotenv)
    monkeypatch.setenv("COVENANT_RADAR_DOTENV", "0")

    # The switch itself is not a settings key, so it must not be rejected.
    assert load_settings().web.port == 8000


def test_explicit_environment_ignores_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("COVENANT_RADAR_WEB__PORT=8300\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "DOTENV_PATH", dotenv)

    assert load_settings(environ={}).web.port == 8000


def test_deployment_environment_uses_dotenv_without_overriding_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("COVENANT_RADAR_ENVIRONMENT=development\n", encoding="utf-8")
    _isolated_process_environment(monkeypatch, dotenv)

    assert load_deployment_environment() == "development"

    monkeypatch.setenv("COVENANT_RADAR_ENVIRONMENT", "production")
    assert load_deployment_environment() == "production"


@pytest.mark.parametrize("value", [None, "", "prod", "DEVELOPMENTS"])
def test_deployment_environment_fails_closed(value: str | None) -> None:
    environ = {} if value is None else {"COVENANT_RADAR_ENVIRONMENT": value}

    assert load_deployment_environment(environ) == "production"


def test_missing_ca_bundle_refuses_start() -> None:
    with pytest.raises(SettingsError, match="ai.ca_bundle"):
        load_settings(environ={"COVENANT_RADAR_AI__CA_BUNDLE": "var/absent-bundle.pem"})


def test_ca_bundle_accepted_when_present(tmp_path: Path) -> None:
    bundle = tmp_path / "corporate-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")

    settings = load_settings(environ={"COVENANT_RADAR_AI__CA_BUNDLE": str(bundle)})

    assert settings.ai.ca_bundle == bundle
