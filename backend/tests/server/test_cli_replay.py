"""`studentassistant replay <dir> [--speed] [--topic] [--url]`: a recording through the gateway.

Without `--url` the command builds the app in-process from the configuration, which here points
at `tmp_vault` and a temporary devices file through `SA_*` variables (the autouse
`isolated_config` already cleared the machine's). `--speed 1000` keeps the real pacing to a few
milliseconds. `--url` is checked by standing an in-process app in for the running server.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from studentassistant import cli as cli_module
from studentassistant.cli import cli
from studentassistant.protocol import TranscriptClientFinal
from studentassistant.server.app import create_app
from studentassistant.server.recording import read_recording
from studentassistant.server.replay import AsgiTransport
from studentassistant.vault import Event, Vault, list_sessions, read_jsonl, sources_directory

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sessions" / "sample"


@pytest.fixture
def configured(tmp_vault: Vault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """`tmp_vault` as the configured vault, client-mode STT, devices in a temporary file."""
    monkeypatch.setenv("SA_VAULT__PATH", str(tmp_vault.path))
    monkeypatch.setenv("SA_SERVER__DEVICES_PATH", str(tmp_path / "state" / "devices.json"))
    monkeypatch.setenv("SA_STT__MODE", "client")
    return tmp_vault


def finals_in(vault: Vault, subject: str, topic: str) -> list[str]:
    [meta] = list_sessions(vault, subject, topic)
    assert meta.ended_at is not None
    path = vault.path / "subjects" / subject / "topics" / topic / "sessions" / meta.id
    events = read_jsonl(path / "events.jsonl", Event)
    return [event.payload["text"] for event in events if event.kind == "transcript.final"]


def recorded_finals() -> list[str]:
    transcript = read_recording(FIXTURE).transcript
    return [message.text for message in transcript if isinstance(message, TranscriptClientFinal)]


def test_replay_in_process_fills_the_configured_vault(configured: Vault) -> None:
    result = CliRunner().invoke(cli, ["replay", str(FIXTURE), "--speed", "1000"])

    assert result.exit_code == 0, result.output
    assert "reproducida en biologia/la-celula: 3 frases finales" in result.output
    assert "1 eventos, 0 tramas de audio, 1 capturas guardadas." in result.output
    assert finals_in(configured, "biologia", "la-celula") == recorded_finals()
    book = sources_directory(configured, "biologia", "la-celula", "book")
    assert (book / "page-001.jpg").is_file()


def test_the_topic_option_overrides_the_recorded_topic(configured: Vault) -> None:
    result = CliRunner().invoke(
        cli, ["replay", str(FIXTURE), "--speed", "1000", "--topic", "fisica/cinematica"]
    )

    assert result.exit_code == 0, result.output
    assert finals_in(configured, "fisica", "cinematica") == recorded_finals()
    assert not (configured.path / "subjects" / "biologia").exists()


def test_the_url_option_talks_to_that_backend(
    configured: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []

    def running_server(url: str, **_: Any) -> AsgiTransport:
        urls.append(url)
        return AsgiTransport(create_app(vault=configured))

    monkeypatch.setattr(cli_module, "HttpTransport", running_server)

    result = CliRunner().invoke(
        cli, ["replay", str(FIXTURE), "--speed", "1000", "--url", "http://localhost:8765"]
    )

    assert result.exit_code == 0, result.output
    assert urls == ["http://localhost:8765"]
    assert finals_in(configured, "biologia", "la-celula") == recorded_finals()


def test_a_backend_in_the_other_stt_mode_is_refused(
    configured: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_STT__MODE", "server")
    monkeypatch.setenv("SA_STT__PROVIDER", "fake")

    result = CliRunner().invoke(cli, ["replay", str(FIXTURE), "--speed", "1000"])

    assert result.exit_code == 1
    assert "La reproducción ha fallado" in result.output
    assert "client mode" in result.output


@pytest.mark.parametrize("topic", ["biologia", "biologia/", "/la-celula", "a/b/c"])
def test_a_malformed_topic_is_refused(configured: Vault, topic: str) -> None:
    result = CliRunner().invoke(cli, ["replay", str(FIXTURE), "--topic", topic])

    assert result.exit_code == 2
    assert "no es un tema" in result.output
    assert not (configured.path / "subjects" / "biologia").exists()


def test_a_non_positive_speed_is_refused(configured: Vault) -> None:
    result = CliRunner().invoke(cli, ["replay", str(FIXTURE), "--speed", "0"])

    assert result.exit_code == 2
    assert "--speed" in result.output


def test_a_directory_that_is_no_recording_is_refused(configured: Vault, tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["replay", str(tmp_path / "nothing")])

    assert result.exit_code == 1
    assert "No se puede leer la grabación" in result.output
