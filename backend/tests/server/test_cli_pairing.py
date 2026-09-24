"""`studentassistant pair` and `studentassistant devices`, driven against the app in-process."""

from __future__ import annotations

import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import studentassistant.cli as cli_module
from studentassistant.cli import cli, local_backend_url
from studentassistant.config import ServerSettings

PairDevice = Callable[[], dict[str, Any]]
QR_BLOCKS = set("█▀▄")


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, devices_path: Path) -> CliRunner:
    """The CLI sees the same devices file as the test app."""
    monkeypatch.setenv("SA_SERVER__DEVICES_PATH", str(devices_path))
    return CliRunner()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, local: TestClient) -> list[str]:
    """Route the CLI's HTTP call to the running backend through the test app, from loopback."""
    urls: list[str] = []

    def post_json(url: str) -> dict[str, Any]:
        urls.append(url)
        response = local.post(url.removeprefix("http://127.0.0.1:8765"))
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(cli_module, "_post_json", post_json)
    return urls


def test_pair_prints_the_url_the_code_and_a_qr(
    runner: CliRunner, backend: list[str], app: Any, lan: TestClient
) -> None:
    result = runner.invoke(cli, ["pair"])

    assert result.exit_code == 0, result.output
    assert backend == ["http://127.0.0.1:8765/api/pair/codes"]
    assert "http://192.168.1.20:8765" in result.output
    code = next(line.split()[-1] for line in result.output.splitlines() if line.startswith("Code:"))
    assert "Expires:" in result.output
    assert sum(1 for line in result.output.splitlines() if QR_BLOCKS & set(line)) >= 10
    # The printed code is a live one.
    assert app.state.codes.redeem(code) is True


def test_pair_says_so_when_the_backend_is_not_running(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(url: str) -> dict[str, Any]:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli_module, "_post_json", refuse)

    result = runner.invoke(cli, ["pair"])

    assert result.exit_code == 1
    assert "studentassistant serve" in result.output


def test_devices_lists_the_paired_devices_without_tokens(
    runner: CliRunner, pair_device: PairDevice, devices_path: Path
) -> None:
    first, second = pair_device(), pair_device()

    for args in (["devices"], ["devices", "list"]):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert first["device_id"] in result.output
        assert second["device_id"] in result.output
        assert "Móvil de Lucía" in result.output
        assert first["token"] not in result.output
        assert second["token"] not in result.output
        assert "hash" not in result.output


def test_devices_with_none_paired(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["devices"])

    assert result.exit_code == 0
    assert "No paired devices" in result.output


def test_devices_revoke_makes_that_token_stop_working(
    runner: CliRunner, pair_device: PairDevice, lan: TestClient
) -> None:
    revoked, kept = pair_device(), pair_device()
    headers = {"Authorization": f"Bearer {revoked['token']}"}
    assert lan.get("/", headers=headers).status_code == 200

    result = runner.invoke(cli, ["devices", "revoke", revoked["device_id"]])

    assert result.exit_code == 0, result.output
    assert lan.get("/", headers=headers).status_code == 401
    kept_headers = {"Authorization": f"Bearer {kept['token']}"}
    assert lan.get("/", headers=kept_headers).status_code == 200
    assert revoked["device_id"] not in runner.invoke(cli, ["devices"]).output


def test_devices_revoke_of_an_unknown_id_fails(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["devices", "revoke", "dev-nope"])

    assert result.exit_code == 1
    assert "dev-nope" in result.output


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("0.0.0.0", "http://127.0.0.1:8765"),
        ("::", "http://127.0.0.1:8765"),
        ("127.0.0.1", "http://127.0.0.1:8765"),
        ("192.168.1.20", "http://192.168.1.20:8765"),
    ],
)
def test_the_cli_reaches_its_own_backend(host: str, url: str) -> None:
    assert local_backend_url(ServerSettings(host=host)) == url
