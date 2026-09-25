"""`studentassistant doctor`: is this PC ready to run the backend? One line per check.

The checks, in order: the Python dependencies, the configured STT mode and provider (and, only
when faster-whisper is selected, faster-whisper itself, CUDA and the downloaded model), the
Anthropic API key (present; with `api_call` also accepted by the API, through one free call),
the vault (opens, has an `origin`, may be pushed to), the server port, and the systemd service.
A check is `ok`, `aviso` (works, but worse than it could) or `fallo`; any `fallo` makes the
command exit 1. Everything the student reads is Spanish, and no check ever prints a secret.

Everything that reaches outside the process -- GitHub, the Anthropic API, the port, the running
backend, systemd -- comes in through `DoctorProbes` or `install.service.run_systemctl`, so the
tests replace each one and never touch the network, the machine's systemd or a real vault.
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from typing import Literal

from studentassistant.config import ServerSettings, Settings, SttSettings
from studentassistant.install import service, whisper
from studentassistant.install.apikey import API_KEY_ENV_VAR, file_is_private, read_api_key
from studentassistant.llm import LLMAPIError, LLMError, check_api_key, find_ant_profile
from studentassistant.stt.registry import UnknownProviderError, provider_class
from studentassistant.vault import Vault, VaultError
from studentassistant.vault.github import GitHubHost, GitHubHostError, select_host
from studentassistant.vault.setup import SetupError, check_remote_access

Status = Literal["ok", "aviso", "fallo"]
_LABELS: dict[Status, str] = {"ok": "ok", "aviso": "aviso", "fallo": "FALLO"}
DISTRIBUTION = "studentassistant"


@dataclass(frozen=True)
class Check:
    """One line of the report."""

    name: str
    status: Status
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == "fallo"

    def line(self) -> str:
        return f"[{_LABELS[self.status]}] {self.name}: {self.detail}"


def port_is_free(host: str, port: int) -> bool:
    """Whether a server could bind `host:port` right now."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _no_backend() -> bool:
    return False


@dataclass
class DoctorProbes:
    """What the checks reach outside the process through (the tests pass fakes)."""

    # How git authenticates to GitHub; raising `GitHubHostError` means "no credentials".
    github_host: Callable[[], GitHubHost] = select_host
    # One free call with the key (`None`: whatever the SDK resolves, e.g. an `ant auth`
    # profile); raises an `LLMError` when it does not work.
    api_key_check: Callable[[str | None], None] = check_api_key
    # The `ant auth` profile the SDK would use, or `None` (never reads a secret).
    ant_profile: Callable[[], str | None] = find_ant_profile
    port_free: Callable[[str, int], bool] = port_is_free
    # Whether a Student Assistant backend answers `GET /api/health` on this PC.
    backend_answers: Callable[[], bool] = _no_backend
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))


def check_python_dependencies() -> Check:
    name = "Dependencias de Python"
    try:
        requirements = metadata.requires(DISTRIBUTION) or []
    except metadata.PackageNotFoundError:
        return Check(name, "fallo", "el paquete studentassistant no está instalado (uv sync)")
    missing: list[str] = []
    required = 0
    for requirement in requirements:
        if "extra ==" in requirement:
            continue
        match = re.match(r"[A-Za-z0-9._-]+", requirement)
        if match is None:
            continue
        required += 1
        try:
            metadata.version(match.group(0))
        except metadata.PackageNotFoundError:
            missing.append(match.group(0))
    if missing:
        return Check(name, "fallo", f"faltan {', '.join(missing)} (lanza `uv sync` en backend/)")
    return Check(name, "ok", f"las {required} dependencias están instaladas")


def check_stt(stt: SttSettings) -> list[Check]:
    name = "Voz (STT)"
    if stt.mode == "client":
        return [Check(name, "ok", f"modo cliente: transcribe el cliente ({stt.provider})")]
    try:
        provider_class(stt.provider)
    except UnknownProviderError:
        return [
            Check(name, "fallo", f"modo servidor, pero no hay proveedor «{stt.provider}» instalado")
        ]
    except Exception as error:  # a broken entry point: ImportError, TypeError...
        return [Check(name, "fallo", f"el proveedor «{stt.provider}» no carga: {error}")]
    checks = [Check(name, "ok", f"modo servidor, proveedor {stt.provider}")]
    if whisper.uses_whisper(stt):
        checks.extend(check_whisper(stt))
    return checks


def check_whisper(stt: SttSettings) -> list[Check]:
    options = whisper.whisper_options(stt)
    try:
        whisper.faster_whisper_module()
    except whisper.WhisperError as error:
        return [Check("faster-whisper", "fallo", str(error))]
    checks = [Check("faster-whisper", "ok", "instalado")]
    if options.device != "cpu":
        devices = whisper.cuda_devices()
        if devices:
            checks.append(Check("CUDA", "ok", f"{devices} GPU disponible(s)"))
        elif options.device == "cuda":
            checks.append(Check("CUDA", "fallo", "device = cuda, pero no se ve ninguna GPU CUDA"))
        else:
            checks.append(Check("CUDA", "aviso", "no se ve ninguna GPU: se usará la CPU (lento)"))
    try:
        path = whisper.cached_model(options)
    except whisper.WhisperError as error:
        checks.append(Check("Modelo de Whisper", "fallo", str(error)))
    else:
        checks.append(Check("Modelo de Whisper", "ok", f"{options.model} en {path}"))
    return checks


def check_api_key_setting(settings: Settings, api_call: bool, probes: DoctorProbes) -> Check:
    """The key's source, in the order the SDK sees it once `serve` has exported the key file:
    the environment, the key file, then an `ant auth` profile."""
    name = "Clave de la API de Anthropic"
    path = settings.llm.api_key_path()
    key = probes.environ.get(API_KEY_ENV_VAR) or None
    where, of_where = f"la variable {API_KEY_ENV_VAR}", f"de la variable {API_KEY_ENV_VAR}"
    if key is None:
        key = read_api_key(path)
        where, of_where = f"el fichero {path}", f"del fichero {path}"
        if key is not None and not file_is_private(path):
            return Check(name, "fallo", f"{path} lo pueden leer otros usuarios: chmod 600 {path}")
    if key is None:
        profile = probes.ant_profile()
        if profile is None:
            return Check(name, "fallo", "no hay clave: guárdala con `studentassistant setup`")
        where = f"el perfil «{profile}» de `ant auth`"
        of_where = f"del perfil «{profile}» de `ant auth`"
    if not api_call:
        return Check(name, "ok", f"en {where} (prueba que funciona con --api-call)")
    try:
        probes.api_key_check(key)  # `None` for a profile: the SDK resolves it
    except LLMAPIError as error:
        if error.status_code in (401, 403):
            return Check(name, "fallo", f"Anthropic rechaza la clave {of_where}")
        return Check(name, "fallo", f"la API respondió con un error ({error.status_code})")
    except LLMError as error:
        return Check(name, "fallo", f"no se pudo contactar con la API de Anthropic: {error}")
    return Check(name, "ok", f"la clave {of_where} funciona")


def check_vault(settings: Settings, probes: DoctorProbes) -> list[Check]:
    path = settings.vault.path
    try:
        vault = Vault.open(path)
    except VaultError as error:
        return [Check("Vault", "fallo", f"no se puede abrir {path}: {error}")]
    checks = [Check("Vault", "ok", f"{vault.path} (de {vault.meta.student})")]
    try:
        host: GitHubHost | None = probes.github_host()
    except GitHubHostError:
        host = None  # git may still reach the remote on its own (SSH, a credential helper)
    git = settings.vault.git
    try:
        access = check_remote_access(
            vault.path, host, settings.vault.repo, git.author_email, git.timeout_seconds
        )
    except SetupError as error:
        return [*checks, Check("Remoto del vault", "fallo", str(error))]
    if access.repo_matches is False:
        checks.append(
            Check(
                "Remoto del vault",
                "fallo",
                f"origin es {access.url}, no el repositorio {settings.vault.repo} de la"
                " configuración",
            )
        )
    elif settings.vault.repo is None:
        detail = f"origin es {access.url}; falta vault.repo"
        checks.append(Check("Remoto del vault", "aviso", detail))
    else:
        checks.append(Check("Remoto del vault", "ok", f"{settings.vault.repo} ({access.url})"))
    if access.push_error is None:
        checks.append(Check("Subida al vault", "ok", "se puede subir (git push --dry-run)"))
    else:
        checks.append(Check("Subida al vault", "fallo", access.push_error))
    return checks


def check_port(server: ServerSettings, probes: DoctorProbes) -> Check:
    name = "Puerto"
    where = f"{server.host}:{server.port}"
    if probes.port_free(server.host, server.port):
        return Check(name, "ok", f"{where} libre")
    if probes.backend_answers():
        return Check(name, "ok", f"{where} en uso por Student Assistant")
    return Check(name, "fallo", f"{where} ocupado por otro programa (cambia server.port)")


def check_service() -> Check:
    name = "Servicio"
    state = service.service_state()
    if state == "active":
        return Check(name, "ok", f"{service.UNIT_NAME} activo")
    detail = f"{service.UNIT_NAME} no está activo ({state}): lanza `studentassistant setup`"
    return Check(name, "fallo", detail)


def run_doctor(
    settings: Settings, *, api_call: bool = False, probes: DoctorProbes | None = None
) -> list[Check]:
    """Every check, in the order the report prints them."""
    probes = probes or DoctorProbes()
    return [
        check_python_dependencies(),
        *check_stt(settings.stt),
        check_api_key_setting(settings, api_call, probes),
        *check_vault(settings, probes),
        check_port(settings.server, probes),
        check_service(),
    ]
