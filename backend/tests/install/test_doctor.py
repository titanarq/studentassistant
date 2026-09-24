"""`run_doctor`: one check per line, `fallo` on what is broken, never a secret in the report."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from whisper_fakes import hide_faster_whisper, install_fakes

from github_fakes import LocalHost
from studentassistant.config import Settings
from studentassistant.install.apikey import API_KEY_ENV_VAR, store_api_key
from studentassistant.install.doctor import Check, DoctorProbes, run_doctor
from studentassistant.install.service import SystemctlResult
from studentassistant.llm import LLMAPIError, LLMConnectionError
from studentassistant.vault.setup import create_vault

KEY = "sk-" + "ant-" + "api03-" + "y" * 40


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
def host(env: Path) -> LocalHost:
    return LocalHost(env / "github")


@pytest.fixture
def vault_ready(env: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch) -> Path:
    create_vault(env / "vault", "ana/vault", "Ana", host)
    monkeypatch.setenv("SA_VAULT__PATH", str(env / "vault"))
    monkeypatch.setenv("SA_VAULT__REPO", "ana/vault")
    return env / "vault"


def probes(host: LocalHost, **overrides) -> DoctorProbes:
    calls: list[str] = []

    def api_key_check(key: str) -> None:
        calls.append(key)

    base = DoctorProbes(
        github_host=lambda: host,
        api_key_check=api_key_check,
        port_free=lambda host_, port: True,
        backend_answers=lambda: False,
        environ={},
    )
    for name, value in overrides.items():
        setattr(base, name, value)
    return base


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


def test_a_ready_pc_passes_every_check(vault_ready: Path, host: LocalHost) -> None:
    settings = Settings()
    store_api_key(settings.llm.api_key_path(), KEY)
    seen: list[str] = []

    checks = run_doctor(settings, api_call=True, probes=probes(host, api_key_check=seen.append))

    assert [check.status for check in checks] == ["ok"] * len(checks), [c.line() for c in checks]
    assert list(by_name(checks)) == [
        "Dependencias de Python",
        "Voz (STT)",
        "Clave de la API de Anthropic",
        "Vault",
        "Remoto del vault",
        "Subida al vault",
        "Puerto",
        "Servicio",
    ]
    assert seen == [KEY]
    assert all(KEY not in check.line() for check in checks)
    assert checks[0].line().startswith("[ok] Dependencias de Python: ")


def test_without_api_call_the_key_is_only_looked_for(vault_ready: Path, host: LocalHost) -> None:
    seen: list[str] = []
    checks = by_name(
        run_doctor(
            Settings(),
            probes=probes(host, api_key_check=seen.append, environ={API_KEY_ENV_VAR: KEY}),
        )
    )

    assert checks["Clave de la API de Anthropic"].status == "ok"
    assert "ANTHROPIC_API_KEY" in checks["Clave de la API de Anthropic"].detail
    assert seen == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMAPIError("bad", status_code=401), "Anthropic rechaza la clave"),
        (LLMConnectionError("offline"), "no se pudo contactar"),
    ],
)
def test_a_key_that_does_not_work_fails(
    vault_ready: Path, host: LocalHost, error: Exception, expected: str
) -> None:
    def refuse(key: str) -> None:
        raise error

    check = by_name(
        run_doctor(
            Settings(),
            api_call=True,
            probes=probes(host, api_key_check=refuse, environ={API_KEY_ENV_VAR: KEY}),
        )
    )["Clave de la API de Anthropic"]

    assert check.failed
    assert expected in check.detail
    assert KEY not in check.line()


def test_no_key_and_an_open_key_file_fail(vault_ready: Path, host: LocalHost) -> None:
    settings = Settings()
    check = by_name(run_doctor(settings, probes=probes(host)))["Clave de la API de Anthropic"]
    assert check.failed and "no hay clave" in check.detail

    path = settings.llm.api_key_path()
    store_api_key(path, KEY)
    path.chmod(0o644)
    check = by_name(run_doctor(settings, probes=probes(host)))["Clave de la API de Anthropic"]
    assert check.failed and "chmod 600" in check.detail


def test_a_missing_vault_fails_and_skips_the_remote(env: Path, host: LocalHost) -> None:
    checks = by_name(run_doctor(Settings(), probes=probes(host)))

    assert checks["Vault"].failed
    assert "Remoto del vault" not in checks
    assert "Subida al vault" not in checks


def test_the_wrong_origin_and_no_push_access_fail(
    vault_ready: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    import shutil

    monkeypatch.setenv("SA_VAULT__REPO", "ana/other")
    shutil.rmtree(env / "github")

    checks = by_name(run_doctor(Settings(), probes=probes(host)))

    assert checks["Remoto del vault"].failed
    assert "ana/other" in checks["Remoto del vault"].detail
    assert checks["Subida al vault"].failed


def test_no_github_credentials_still_tries_git(vault_ready: Path, host: LocalHost) -> None:
    from studentassistant.vault.github import GitHubHostError

    def no_host() -> LocalHost:
        raise GitHubHostError("sin credenciales")

    checks = by_name(run_doctor(Settings(), probes=probes(host, github_host=no_host)))

    assert checks["Subida al vault"].status == "ok"


def test_the_port(vault_ready: Path, host: LocalHost) -> None:
    busy = probes(host, port_free=lambda h, p: False)
    assert by_name(run_doctor(Settings(), probes=busy))["Puerto"].failed

    ours = probes(host, port_free=lambda h, p: False, backend_answers=lambda: True)
    check = by_name(run_doctor(Settings(), probes=ours))["Puerto"]
    assert check.status == "ok" and "Student Assistant" in check.detail


def test_an_inactive_service_fails(vault_ready: Path, host: LocalHost, systemctl) -> None:
    systemctl.answers["is-active"] = SystemctlResult(ok=False, output="inactive")

    check = by_name(run_doctor(Settings(), probes=probes(host)))["Servicio"]

    assert check.failed and "inactive" in check.detail


def test_server_mode_with_an_unknown_provider_fails(
    vault_ready: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_STT__MODE", "server")
    monkeypatch.setenv("SA_STT__PROVIDER", "nope")

    check = by_name(run_doctor(Settings(), probes=probes(host)))["Voz (STT)"]

    assert check.failed and "nope" in check.detail


def test_server_mode_with_the_fake_provider_passes(
    vault_ready: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_STT__MODE", "server")
    monkeypatch.setenv("SA_STT__PROVIDER", "fake")

    checks = by_name(run_doctor(Settings(), probes=probes(host)))

    assert checks["Voz (STT)"].status == "ok"
    assert "CUDA" not in checks and "faster-whisper" not in checks


@pytest.fixture
def whisper_selected(vault_ready: Path, monkeypatch: pytest.MonkeyPatch, env: Path) -> Path:
    """Server mode with faster-whisper, registered in code as the fake provider class."""
    from studentassistant.stt.fakes import FakeProvider
    from studentassistant.stt.registry import register_provider, unregister_provider

    register_provider("faster-whisper", FakeProvider)
    monkeypatch.setenv("SA_STT__MODE", "server")
    monkeypatch.setenv("SA_STT__PROVIDER", "faster-whisper")
    models = env / "models"
    monkeypatch.setenv("SA_STT__OPTIONS", f'{{"faster-whisper": {{"download_root": "{models}"}}}}')
    yield models
    unregister_provider("faster-whisper")


def test_faster_whisper_ready(
    whisper_selected: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    install_fakes(monkeypatch, env / "hf", cuda_devices=1)
    settings = Settings()
    (whisper_selected / "large-v3-turbo").mkdir(parents=True)

    checks = by_name(run_doctor(settings, probes=probes(host)))

    assert checks["faster-whisper"].status == "ok"
    assert checks["CUDA"].status == "ok"
    assert checks["Modelo de Whisper"].status == "ok"


def test_faster_whisper_without_gpu_or_model(
    whisper_selected: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch, env: Path
) -> None:
    install_fakes(monkeypatch, env / "hf", cuda_devices=0)

    checks = by_name(run_doctor(Settings(), probes=probes(host)))

    assert checks["CUDA"].status == "aviso"
    assert checks["Modelo de Whisper"].failed

    monkeypatch.setenv("SA_STT__OPTIONS", '{"faster-whisper": {"device": "cuda"}}')
    assert by_name(run_doctor(Settings(), probes=probes(host)))["CUDA"].failed
    monkeypatch.setenv("SA_STT__OPTIONS", '{"faster-whisper": {"device": "cpu"}}')
    assert "CUDA" not in by_name(run_doctor(Settings(), probes=probes(host)))


def test_faster_whisper_not_installed(
    whisper_selected: Path, host: LocalHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    hide_faster_whisper(monkeypatch)

    checks = by_name(run_doctor(Settings(), probes=probes(host)))

    assert checks["faster-whisper"].failed
    assert "Modelo de Whisper" not in checks
