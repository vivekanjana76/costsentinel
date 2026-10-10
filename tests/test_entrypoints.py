"""Configuration, CLI, API and observability tests.

The configuration tests exist to pin one promise: a fresh clone with no ``.env``
runs the whole pipeline with no credentials of any kind.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from costsentinel import __version__
from costsentinel.api.app import create_app, settings_dependency
from costsentinel.cli import build_parser, main, render_json, summarise
from costsentinel.config import (
    LLMBackend,
    LogLevel,
    RunMode,
    Settings,
    get_settings,
)
from costsentinel.domain.report import ScanResult
from costsentinel.graph.runner import run_scan
from costsentinel.observability import tracing_status
from costsentinel.observability.logging import (
    LOGGER_NAME,
    JsonFormatter,
    configure_logging,
    get_logger,
)
from costsentinel.providers import get_provider
from costsentinel.providers.base import ProviderNotConfiguredError
from costsentinel.providers.mock import CLIENT_MINISTRY, CLIENT_NORTHWIND, MockAzureProvider

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_defaults_require_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh clone with no .env runs fully offline (golden rule 2)."""
    for name in ("MODE", "LLM_BACKEND", "COSTSENTINEL_MODE", "COSTSENTINEL_LLM_BACKEND"):
        monkeypatch.delenv(name, raising=False)

    fresh = Settings(_env_file=None)  # type: ignore[call-arg]
    assert fresh.mode is RunMode.MOCK
    assert fresh.llm_backend is LLMBackend.FAKE
    assert fresh.is_mock_mode
    assert fresh.uses_fake_llm
    assert fresh.requires_no_credentials
    assert not fresh.memory_enabled
    assert not fresh.tracing_enabled


def test_mode_is_readable_from_the_bare_environment_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODE", "real")
    monkeypatch.setenv("LLM_BACKEND", "gemini")
    configured = Settings(_env_file=None)  # type: ignore[call-arg]
    assert configured.mode is RunMode.REAL
    assert configured.llm_backend is LLMBackend.GEMINI
    assert not configured.requires_no_credentials


def test_mode_is_also_readable_from_the_prefixed_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prefixed form avoids collisions in a shared environment."""
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.setenv("COSTSENTINEL_MODE", "real")
    assert Settings(_env_file=None).mode is RunMode.REAL  # type: ignore[call-arg]


def test_checkpoint_path_lives_under_the_state_dir(tmp_path: Path) -> None:
    configured = Settings(state_dir=tmp_path / "state")
    assert configured.checkpoint_path == tmp_path / "state" / "checkpoints.sqlite"
    assert configured.ensure_state_dir().exists()


def test_memory_and_tracing_are_detected_when_configured(tmp_path: Path) -> None:
    from pydantic import SecretStr

    configured = Settings(
        state_dir=tmp_path,
        database_url=SecretStr("postgresql://localhost/db"),
        langfuse_host="https://example.invalid",
        langfuse_public_key="pk",
        langfuse_secret_key=SecretStr("sk"),
    )
    assert configured.memory_enabled
    assert configured.tracing_enabled


def test_secrets_are_not_exposed_by_repr(tmp_path: Path) -> None:
    """No secrets in logs, which includes an accidental settings dump."""
    from pydantic import SecretStr

    configured = Settings(state_dir=tmp_path, azure_client_secret=SecretStr("super-secret"))
    assert "super-secret" not in repr(configured)
    assert "super-secret" not in str(configured.model_dump())


def test_headroom_factor_must_exceed_one(tmp_path: Path) -> None:
    """A factor of 1.0 would leave no headroom at all."""
    from decimal import Decimal

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(state_dir=tmp_path, rightsize_headroom_factor=Decimal("1.0"))


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_log_level_values() -> None:
    assert {level.value for level in LogLevel} == {"debug", "info", "warning", "error"}


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def test_factory_returns_the_mock_by_default(settings: Settings) -> None:
    assert isinstance(get_provider(settings), MockAzureProvider)


def test_factory_passes_the_seed_through(tmp_path: Path) -> None:
    configured = Settings(state_dir=tmp_path, mock_seed=4242)
    built = get_provider(configured)
    assert isinstance(built, MockAzureProvider)
    assert built.seed == 4242


def test_real_mode_fails_fast_with_a_clear_message(settings: Settings) -> None:
    """Not implemented until Phase 2 -- and it says so rather than half-working."""
    real = settings.model_copy(update={"mode": RunMode.REAL})
    with pytest.raises(ProviderNotConfiguredError, match="Phase 3"):
        get_provider(real)


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def test_logging_emits_structured_json(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    logger = configure_logging(settings)
    logger.info("test event", extra={"run_id": "run-1", "client": "acme"})
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["message"] == "test event"
    assert payload["run_id"] == "run-1"
    assert payload["client"] == "acme"
    assert payload["level"] == "info"
    assert payload["logger"] == LOGGER_NAME


def test_logging_configuration_is_idempotent(settings: Settings) -> None:
    """Both the CLI and the API configure logging on start-up."""
    first = configure_logging(settings)
    before = len(first.handlers)
    configure_logging(settings)
    assert len(first.handlers) == before


def test_logger_namespacing() -> None:
    assert get_logger("agents.test").name == f"{LOGGER_NAME}.agents.test"


def test_json_formatter_includes_exception_detail() -> None:
    formatter = JsonFormatter()
    try:
        msg = "boom"
        raise ValueError(msg)
    except ValueError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, "f", 1, "failed", None, sys.exc_info())
    payload = json.loads(formatter.format(record))
    assert "boom" in payload["error"]


def test_tracing_is_a_no_op_when_unconfigured(settings: Settings) -> None:
    assert "disabled" in tracing_status(settings)


def test_tracing_reports_configured_when_langfuse_is_set(settings: Settings) -> None:
    from pydantic import SecretStr

    configured = settings.model_copy(
        update={
            "langfuse_host": "https://example.invalid",
            "langfuse_public_key": "pk",
            "langfuse_secret_key": SecretStr("sk"),
        }
    )
    assert "langfuse configured" in tracing_status(configured)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Point the CLI's ``get_settings`` at the temporary state directory."""
    monkeypatch.setattr("costsentinel.cli.get_settings", lambda: settings)
    yield settings


def test_parser_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_scan_emits_json_by_default(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["reports"]) == 2
    assert {r["client"] for r in payload["reports"]} == {CLIENT_NORTHWIND, CLIENT_MINISTRY}


def test_scan_can_be_narrowed_to_one_client(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan", "--client", CLIENT_NORTHWIND]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["reports"]) == 1
    assert payload["reports"][0]["client"] == CLIENT_NORTHWIND


def test_scan_json_is_single_line_at_zero_indent(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan", "--client", CLIENT_MINISTRY, "--indent", "0"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_scan_summary_names_the_wasteful_resources(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["scan", "--summary"]) == 0
    out = capsys.readouterr().out
    for name in (
        "disk-analytics-01-data",
        "vm-analytics-01",
        "pip-legacy-api",
        "disk-portal-legacy-snapshot",
        "snap-web-01-pre-upgrade",
        "sql-dev-sandbox",
        "nw-compute-dsv5-3y",
        "app-portal-plan",
    ):
        assert name in out
    assert "Awaiting approval:" in out
    assert "10 finding(s)" in out


def test_config_command_reports_the_active_backends(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "mode                 mock" in out
    assert "llm_backend          fake" in out
    assert "none required" in out


def test_cli_reports_a_backend_failure_as_exit_1(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = settings.model_copy(update={"mode": RunMode.REAL})
    monkeypatch.setattr("costsentinel.cli.get_settings", lambda: real)
    assert main(["scan"]) == 1
    assert "Phase 3" in capsys.readouterr().err


def test_render_json_round_trips(settings: Settings) -> None:
    result = run_scan(client=CLIENT_MINISTRY, settings=settings)
    reloaded = ScanResult.model_validate_json(render_json(result))
    assert reloaded.clients == result.clients


def test_money_serialises_as_a_decimal_string(settings: Settings) -> None:
    """Deliberate: binary floats cannot represent a cent exactly."""
    result = run_scan(client=CLIENT_MINISTRY, settings=settings)
    payload = json.loads(render_json(result))
    amount = payload["reports"][0]["totals"]["projected_monthly_savings"]["amount"]
    assert isinstance(amount, str)
    assert amount == "208.70"


def test_summarise_handles_an_empty_sweep() -> None:
    empty = ScanResult(reports=())
    assert "0 client(s)" in summarise(empty)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A test client whose settings point at the temporary state directory."""
    app = create_app()
    app.dependency_overrides[settings_dependency] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_health_reports_the_active_backends(client: TestClient) -> None:
    body: dict[str, Any] = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["phase"] == 1
    assert body["mode"] == "mock"
    assert body["llm_backend"] == "fake"
    assert body["requires_no_credentials"] is True
    assert "disabled" in body["tracing"]


def test_post_scan_returns_one_report_per_client(client: TestClient) -> None:
    response = client.post("/scan", json={})
    assert response.status_code == 200
    body = response.json()
    assert len(body["reports"]) == 2
    assert {r["client"] for r in body["reports"]} == {CLIENT_NORTHWIND, CLIENT_MINISTRY}


def test_post_scan_narrowed_to_one_client(client: TestClient) -> None:
    body = client.post("/scan", json={"client": CLIENT_NORTHWIND}).json()
    assert len(body["reports"]) == 1
    report = body["reports"][0]
    assert report["client"] == CLIENT_NORTHWIND
    assert report["totals"]["findings_count"] == 8
    assert report["totals"]["projected_monthly_savings"]["amount"] == "1350.41"
    assert {f["resource_name"] for f in report["findings"]} == {
        "vm-analytics-01",
        "vm-batch-02",
        "nw-compute-dsv5-3y",
        "sql-dev-sandbox",
        "disk-analytics-01-data",
        "snap-web-01-pre-upgrade",
        "snap-analytics-baseline",
        "pip-legacy-api",
    }


def test_post_scan_findings_are_all_gated(client: TestClient) -> None:
    body = client.post("/scan", json={"client": CLIENT_NORTHWIND}).json()
    for finding in body["reports"][0]["findings"]:
        assert finding["requires_approval"] is True
        assert finding["action_class"] in {"review", "block"}


def test_post_scan_rejects_an_unexpected_field(client: TestClient) -> None:
    assert client.post("/scan", json={"client": "acme", "surprise": 1}).status_code == 422


def test_post_scan_records_the_requester(client: TestClient) -> None:
    response = client.post("/scan", json={"client": CLIENT_MINISTRY, "requested_by": "pytest"})
    assert response.status_code == 200


def test_post_scan_returns_503_when_a_backend_is_unavailable(settings: Settings) -> None:
    """An unconfigured backend is an operator condition, not a bad request."""
    real = settings.model_copy(update={"mode": RunMode.REAL})
    app = create_app()
    app.dependency_overrides[settings_dependency] = lambda: real
    with TestClient(app) as test_client:
        response = test_client.post("/scan", json={})
    assert response.status_code == 503
    assert "Phase 3" in response.json()["detail"]


def test_openapi_schema_is_served(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/scan" in schema["paths"]
    assert "/health" in schema["paths"]


def test_scan_result_json_carries_run_metrics(settings: Settings) -> None:
    """Token, cost and latency travel with the sweep, not inside a client report."""
    result = run_scan(client=CLIENT_MINISTRY, settings=settings)
    payload = json.loads(render_json(result))
    assert len(payload["metrics"]) == 1
    metrics = payload["metrics"][0]
    assert metrics["run_id"] == payload["reports"][0]["run_id"]
    assert metrics["calls"]
    assert all(call["routing"]["tier"] in {"small", "large"} for call in metrics["calls"])
    # A client report is about their estate, not about what our model calls cost.
    assert "metrics" not in payload["reports"][0]


def test_health_reports_the_tracer(client: TestClient) -> None:
    body = client.get("/health").json()
    assert "disabled" in body["tracing"]
