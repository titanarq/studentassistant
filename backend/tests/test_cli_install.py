"""`setup` beyond the vault (API key, STT, service), `doctor` and `serve` exporting the key."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

import studentassistant.cli as cli_module
from github_fakes import LocalHost
from marp_fakes import fake_marp_path
from studentassistant.cli import cli
from studentassistant.config import ClaudeCodeSettings
from studentassistant.install import service
from studentassistant.install.apikey import API_KEY_ENV_VAR, read_api_key, store_api_key
from studentassistant.install.doctor import DoctorProbes
from studentassistant.install.service import SystemctlResult
from studentassistant.llm import ClaudeCodeStatus, LLMAPIError
from whisper_fakes import hide_faster_whisper, install_fakes

KEY = "sk-" + "ant-" + "api03-" + "z" * 40


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "conf" / "config.toml"))
    return tmp_path


@pytest.fixture
def host(env: Path, monkeypatch: pytest.MonkeyPatch) -> LocalHost:
    local = LocalHost(env / "github")
    monkeypatch.setattr(cli_module, "_github_host", lambda: local)
    return local


def key_path(env: Path) -> Path:
    return env / "conf" / "secrets.env"


def unattended(env: Path, *extra: str) -> list[str]:
    return ["setup", "--vault-repo", "ana/vault", "--path", str(env / "vault"), "--create", *extra]


def test_unattended_setup_reads_the_key_from_stdin_and_installs_the_service(
    runner: CliRunner, env: Path, host: LocalHost, systemctl
) -> None:
    result = runner.invoke(cli, unattended(env, "--api-key-stdin"), input=KEY + "\n")

    assert result.exit_code == 0, result.output
    assert KEY not in result.output
    assert read_api_key(key_path(env)) == KEY
    assert stat.S_IMODE(key_path(env).stat().st_mode) == 0o600
    assert KEY not in (env / "conf" / "config.toml").read_text(encoding="utf-8")
    assert "Voz: modo client, web-speech" in result.output
    unit = service.unit_path().read_text(encoding="utf-8")
    assert f"Environment=SA_CONFIG={env / 'conf' / 'config.toml'}\n" in unit
    assert " serve\n" in unit
    assert ("enable", "--now", service.UNIT_NAME) in systemctl.calls
    assert "Listo. Comprueba la instalación con `studentassistant doctor`." in result.output


def test_nothing_in_the_vault_ever_holds_the_key(
    runner: CliRunner, env: Path, host: LocalHost
) -> None:
    runner.invoke(cli, unattended(env, "--api-key-stdin"), input=KEY + "\n")

    for path in (env / "vault").rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path


def test_unattended_without_a_key_says_how_to_add_it(
    runner: CliRunner, env: Path, host: LocalHost
) -> None:
    result = runner.invoke(cli, unattended(env, "--no-service"))

    assert result.exit_code == 0, result.output
    assert "setup --api-key-stdin" in result.output
    assert "Servicio no instalado" in result.output
    assert not key_path(env).exists()
    assert not service.unit_path().exists()


def test_a_stored_or_exported_key_is_not_asked_again(
    runner: CliRunner, env: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_api_key(key_path(env), KEY)
    result = runner.invoke(cli, unattended(env, "--no-service"))
    assert "ya está guardada en" in result.output

    key_path(env).unlink()
    monkeypatch.setenv(API_KEY_ENV_VAR, KEY)
    result = runner.invoke(cli, unattended(env, "--no-service"))
    assert "viene de ANTHROPIC_API_KEY" in result.output
    assert not key_path(env).exists()


def test_interactive_setup_asks_for_the_key_hidden_and_the_service(
    runner: CliRunner, env: Path, host: LocalHost, systemctl
) -> None:
    answers = f"crear\nana/vault\n{env / 'vault'}\nAna\n{KEY}\nn\n"

    result = runner.invoke(cli, ["setup"], input=answers)

    assert result.exit_code == 0, result.output
    assert "Clave de la API de Anthropic" in result.output
    assert KEY not in result.output
    assert read_api_key(key_path(env)) == KEY
    assert "¿Instalar el servicio" in result.output
    assert systemctl.calls == []


def test_a_key_with_spaces_fails_setup(runner: CliRunner, env: Path, host: LocalHost) -> None:
    result = runner.invoke(cli, unattended(env, "--api-key-stdin", "--no-service"), input="a b\n")

    assert result.exit_code == 1
    assert "no es válida" in result.output


def test_a_systemd_failure_fails_setup(
    runner: CliRunner, env: Path, host: LocalHost, systemctl
) -> None:
    systemctl.answers["daemon-reload"] = SystemctlResult(ok=False, output="no bus")

    result = runner.invoke(cli, unattended(env))

    assert result.exit_code == 1
    assert "No se pudo activar el servicio: systemctl --user daemon-reload falló: no bus" in (
        result.output
    )


def test_setup_asks_for_no_key_with_the_claude_code_backend(
    runner: CliRunner, env: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_LLM__BACKEND", "claude-code")

    result = runner.invoke(cli, unattended(env, "--no-service"))

    assert result.exit_code == 0, result.output
    assert "Claude se usa con Claude Code (tu suscripción)" in result.output
    assert read_api_key(key_path(env)) is None


def test_setup_without_a_key_under_auto_says_claude_code_is_used(
    runner: CliRunner, env: Path, host: LocalHost
) -> None:
    result = runner.invoke(cli, unattended(env, "--no-service"))

    assert result.exit_code == 0, result.output
    assert "se usará Claude Code con tu suscripción" in result.output


def test_setup_downloads_the_whisper_model_when_selected(
    runner: CliRunner, env: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install_fakes(monkeypatch, env / "hf")
    monkeypatch.setenv("SA_STT__MODE", "server")
    monkeypatch.setenv("SA_STT__PROVIDER", "faster-whisper")

    result = runner.invoke(cli, unattended(env, "--no-service"))

    assert result.exit_code == 0, result.output
    assert "Modelo de Whisper large-v3-turbo listo en" in result.output
    assert fake.calls == [{"model": "large-v3-turbo", "cache_dir": None, "local_files_only": False}]

    hide_faster_whisper(monkeypatch)
    result = runner.invoke(cli, unattended(env, "--no-service"))
    assert result.exit_code == 1
    assert "No se pudo preparar la voz: faster-whisper no está instalado" in result.output


def fake_probes(host: LocalHost, **overrides: Any) -> DoctorProbes:
    probes = DoctorProbes(
        github_host=lambda: host,
        api_key_check=lambda key: None,
        port_free=lambda h, p: True,
        backend_answers=lambda: False,
        claude_code=lambda settings: ClaudeCodeStatus(executable="claude", logged_in=True),
        environ={},
    )
    for name, value in overrides.items():
        setattr(probes, name, value)
    return probes


def test_doctor_reports_one_line_per_check_and_exits_0(
    runner: CliRunner, env: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(cli, unattended(env, "--api-key-stdin"), input=KEY + "\n")
    marp_path = fake_marp_path(env / "bin")
    monkeypatch.setattr(
        cli_module, "_doctor_probes", lambda server: fake_probes(host, environ={"PATH": marp_path})
    )

    result = runner.invoke(cli, ["doctor", "--api-call"])

    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0].startswith("[ok] Configuración: ")
    assert len(lines) == 11
    assert all(line.startswith("[ok] ") for line in lines), result.output
    assert KEY not in result.output


def test_doctor_exits_1_when_a_check_fails(
    runner: CliRunner, env: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_LLM__BACKEND", "api")
    monkeypatch.setattr(cli_module, "_doctor_probes", lambda server: fake_probes(host))

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "[FALLO] Vault: " in result.output
    assert "[FALLO] Clave de la API de Anthropic: no hay clave" in result.output


def test_doctor_without_a_key_checks_claude_code_under_auto(
    runner: CliRunner, env: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(settings: ClaudeCodeSettings) -> ClaudeCodeStatus:
        raise LLMAPIError("'claude' is not on PATH")

    monkeypatch.setattr(
        cli_module, "_doctor_probes", lambda server: fake_probes(host, claude_code=missing)
    )

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "[FALLO] Claude Code: no se puede usar `claude`" in result.output
    assert "Clave de la API de Anthropic" not in result.output


def test_doctor_reports_an_invalid_configuration(runner: CliRunner, env: Path) -> None:
    config = env / "conf" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[server]\nport = "no"\n', encoding="utf-8")

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert result.output.startswith("[FALLO] Configuración: ")


def test_serve_exports_the_stored_key_unless_the_environment_has_one(
    runner: CliRunner, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: None)
    # Recorded as absent, so the key `serve` exports is removed again after the test.
    monkeypatch.setenv(API_KEY_ENV_VAR, "")
    monkeypatch.delenv(API_KEY_ENV_VAR)
    store_api_key(key_path(env), KEY)

    assert runner.invoke(cli, ["serve"]).exit_code == 0
    assert os.environ[API_KEY_ENV_VAR] == KEY

    monkeypatch.setenv(API_KEY_ENV_VAR, "from-env")
    assert runner.invoke(cli, ["serve"]).exit_code == 0
    assert os.environ[API_KEY_ENV_VAR] == "from-env"
