"""Control C9: secrets must not reach log output."""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from analyst_agent.observability import logging as obs

DSN = "postgresql://app_rw:sup3rsecret@db:5432/analyst"
API_KEY = "sk-ant-fake-key-for-tests-only"


@pytest.fixture(autouse=True)
def _clean_logging():
    obs.set_redaction_secrets(())
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    yield
    obs.set_redaction_secrets(())
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    logging.getLogger().handlers = []


def test_dsn_is_redacted_from_a_field() -> None:
    obs.set_redaction_secrets((DSN, "sup3rsecret"))
    event = obs.redact_secrets(None, "info", {"event": "connecting", "dsn": DSN})
    assert event["dsn"] == obs.REDACTED
    assert "sup3rsecret" not in json.dumps(event)


def test_password_is_redacted_even_when_it_appears_alone() -> None:
    obs.set_redaction_secrets((DSN, "sup3rsecret"))
    event = obs.redact_secrets(
        None, "error", {"event": "boom", "detail": "auth failed for password sup3rsecret"}
    )
    assert "sup3rsecret" not in event["detail"]
    assert obs.REDACTED in event["detail"]


def test_redaction_reaches_nested_structures() -> None:
    obs.set_redaction_secrets((API_KEY,))
    event = obs.redact_secrets(
        None,
        "info",
        {"event": "call", "meta": {"headers": {"x-api-key": API_KEY}, "list": [API_KEY]}},
    )
    assert API_KEY not in json.dumps(event)


def test_exception_text_containing_a_dsn_is_redacted() -> None:
    # The leak that actually happens in practice: a driver error echoing the DSN back.
    obs.set_redaction_secrets((DSN, "sup3rsecret"))
    event = obs.redact_secrets(
        None, "error", {"event": "db error", "exception": f'could not connect to "{DSN}"'}
    )
    assert "sup3rsecret" not in event["exception"]


def test_nothing_is_touched_when_no_secrets_are_registered() -> None:
    event = obs.redact_secrets(None, "info", {"event": "hello", "value": "sup3rsecret"})
    assert event["value"] == "sup3rsecret"


def test_long_sql_is_shortened_but_reports_its_true_length() -> None:
    sql = "SELECT " + ", ".join(f"col_{i}" for i in range(200)) + " FROM analytics.orders"
    event = obs.truncate_sql(None, "info", {"event": "q", "sql": sql})
    assert len(event["sql"]) < len(sql)
    assert "see sql_audit" in event["sql"]
    assert str(len(sql)) in event["sql"]


def test_short_sql_is_left_alone() -> None:
    event = obs.truncate_sql(None, "info", {"event": "q", "sql": "SELECT 1"})
    assert event["sql"] == "SELECT 1"


def test_bound_context_is_restored_after_the_block() -> None:
    obs.configure_logging("INFO", "json")
    log = obs.get_logger("test")
    with obs.bound(run_id="r-1"):
        assert structlog.contextvars.get_contextvars().get("run_id") == "r-1"
        log.info("inside")
    assert "run_id" not in structlog.contextvars.get_contextvars()


def test_settings_expose_every_secret_for_redaction() -> None:
    from analyst_agent.config import Settings

    settings = Settings(
        _env_file=None,
        db_rw_dsn=DSN,
        db_ro_dsn="postgresql://analyst_ro:otherpass@db:5432/analyst",
        anthropic_api_key=API_KEY,
    )
    secrets = settings.secret_values
    assert DSN in secrets
    assert API_KEY in secrets
    # The bare passwords are registered too, not just the whole DSN.
    assert "sup3rsecret" in secrets
    assert "otherpass" in secrets


def test_log_level_is_validated() -> None:
    from analyst_agent.config import Settings

    with pytest.raises(ValueError, match="log_level"):
        Settings(_env_file=None, db_rw_dsn=DSN, db_ro_dsn=DSN, log_level="chatty")


def test_allowed_schemas_accepts_a_comma_separated_env_value() -> None:
    from analyst_agent.config import Settings

    settings = Settings(_env_file=None, db_rw_dsn=DSN, db_ro_dsn=DSN, allowed_schemas="analytics, marts")
    assert settings.allowed_schemas == ("analytics", "marts")


def test_missing_api_key_fails_loudly_rather_than_at_the_first_model_call() -> None:
    from analyst_agent.config import Settings

    settings = Settings(_env_file=None, db_rw_dsn=DSN, db_ro_dsn=DSN, anthropic_api_key=None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        settings.require_api_key()


def test_blank_api_key_reads_as_absent() -> None:
    """`ANTHROPIC_API_KEY=` is what .env.example ships, and it must not look configured."""
    from analyst_agent.config import Settings

    settings = Settings(_env_file=None, db_rw_dsn=DSN, db_ro_dsn=DSN, anthropic_api_key="")
    assert settings.anthropic_api_key is None
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        settings.require_api_key()
