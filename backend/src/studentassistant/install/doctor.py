"""`studentassistant doctor`: is this PC ready to run the backend? One line per check.

The checks, in order: the Python dependencies, the configured STT mode and provider (and, only
when faster-whisper is selected, faster-whisper itself, CUDA and the downloaded model), the
Anthropic API key (present; with `api_call` also accepted by the API, through one free call) or,
when `[llm] backend` resolves to `claude-code`, the Claude Code CLI (on PATH and signed in,
through one `claude auth status`, never a model call),
the vault (opens, has an `origin`, may be pushed to, stays under `vault.size_warning_mb`), the
server port, the systemd service, and
Marp CLI (the slides generator's PDF/PPTX export; missing is only an `aviso`).
A check is `ok`, `aviso` (works, but worse than it could) or `fallo`; any `fallo` makes the
command exit 1. Everything the student reads is Spanish, and no check ever prints a secret.

Everything that reaches outside the process -- GitHub, the Anthropic API, the port, the running
backend, systemd -- comes in through `DoctorProbes` or `install.service.run_systemctl`, so the
tests replace each one and never touch the network, the machine's systemd or a real vault.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from typing import Literal

from studentassistant.config import (
    ClaudeCodeSettings,
    GeneratorsSettings,
    ServerSettings,
    Settings,
    SttSettings,
)
from studentassistant.install import service, whisper
from studentassistant.install.apikey import API_KEY_ENV_VAR, file_is_private, read_api_key
from studentassistant.llm import (
    ClaudeCodeStatus,
    LLMAPIError,
    LLMError,
    check_api_key,
    check_claude_code,
    find_ant_profile,
    resolve_backend,
)
from studentassistant.stt.registry import UnknownProviderError, provider_class
from studentassistant.vault import Vault, VaultError
from studentassistant.vault.github import GitHubHost, GitHubHostError, select_host
from studentassistant.vault.purge import format_size
from studentassistant.vault.setup import SetupError, check_remote_access
from studentassistant.vault.stats import vault_stats

Status = Literal["ok", "aviso", "fallo"]
_LABELS: dict[Status, str] = {"ok": "ok", "aviso": "aviso", "fallo": "FALLO"}
DISTRIBUTION = "studentassistant"
# How long `marp --version` may take (an `npx` command may first download the package).
MARP_VERSION_TIMEOUT_SECONDS = 30.0
MARP_INSTALL_HINT = (
    "instálalo con `npm install -g @marp-team/marp-cli` (necesita Node.js y Chrome o Chromium)"
    " o configura `generators.marp_command`"
)


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
    # `claude auth status` of the Claude Code CLI (the `claude-code` backend); raises `LLMError`
    # when the executable is missing or cannot be run.
    claude_code: Callable[[ClaudeCodeSettings], ClaudeCodeStatus] = check_claude_code
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
        hint = "lanza `uv sync` en backend/, con `--extra whisper` si usas Whisper en el PC"
        return Check(name, "fallo", f"faltan {', '.join(missing)} ({hint})")
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
        problem = whisper.cuda_libraries_error() if devices else None
        if devices and problem is None:
            detail = f"{devices} GPU disponible(s), cuBLAS y cuDNN cargan"
            checks.append(Check("CUDA", "ok", detail))
        elif devices:
            status = "fallo" if options.device == "cuda" else "aviso"
            fallback = "" if options.device == "cuda" else ": se usará la CPU (lento)"
            checks.append(
                Check("CUDA", status, f"{problem}{fallback}; {whisper.CUDA_LIBRARIES_HINT}")
            )
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


def check_claude_code_setting(settings: Settings, probes: DoctorProbes) -> Check:
    """The `claude-code` backend: the CLI is on PATH and signed in (no model call, no tokens)."""
    name = "Claude Code"
    executable = settings.llm.claude_code.executable
    try:
        status = probes.claude_code(settings.llm.claude_code)
    except LLMError as error:
        return Check(
            name,
            "fallo",
            f"no se puede usar `{executable}` ({error}): instala Claude Code o configura"
            " `llm.claude_code.executable`",
        )
    if not status.logged_in:
        return Check(
            name,
            "fallo",
            f"`{executable}` no tiene la sesión iniciada: ejecuta `{executable} auth login`",
        )
    plan = f", plan {status.subscription_type}" if status.subscription_type else ""
    how = status.auth_method or "sesión iniciada"
    return Check(name, "ok", f"{status.executable} ({how}{plan}); Claude va por tu suscripción")


def check_llm_access(settings: Settings, api_call: bool, probes: DoctorProbes) -> Check:
    """How Claude is reached on this PC: the API key check, or the Claude Code one."""
    backend = resolve_backend(settings, environ=probes.environ, ant_profile=probes.ant_profile)
    if backend == "claude-code":
        return check_claude_code_setting(settings, probes)
    return check_api_key_setting(settings, api_call, probes)


def check_vault(settings: Settings, probes: DoctorProbes) -> list[Check]:
    path = settings.vault.path
    try:
        vault = Vault.open(path)
    except VaultError as error:
        return [Check("Vault", "fallo", f"no se puede abrir {path}: {error}")]
    checks = [Check("Vault", "ok", f"{vault.path} (de {vault.meta.student})")]
    checks.extend(_check_vault_remote(vault, settings, probes))
    checks.append(check_vault_size(vault, settings.vault.size_warning_mb))
    return checks


def check_vault_size(vault: Vault, warning_mb: int) -> Check:
    """`aviso` once the working tree plus git's object store passes `warning_mb` (#284)."""
    name = "Tamaño del vault"
    try:
        stats = vault_stats(vault, top=0)
    except OSError as error:
        return Check(name, "aviso", f"no se pudo medir: {error}")
    size = (
        f"{format_size(stats.total_bytes)} (archivos {format_size(stats.working_tree_bytes)},"
        f" historial de git {format_size(stats.git_bytes)})"
    )
    if stats.total_bytes <= warning_mb * 1024 * 1024:
        return Check(name, "ok", f"{size}, por debajo del aviso de {warning_mb} MB")
    largest = ", ".join(
        f"{category.label} {format_size(category.bytes)}" for category in stats.largest_categories()
    )
    detail = (
        f"{size} supera vault.size_warning_mb ({warning_mb} MB); lo que más ocupa: {largest}."
        " Libera espacio con `studentassistant purge` (detalle con `studentassistant vault"
        " stats`) o valora pasar las imágenes a Git LFS (pregunta abierta de docs/VISION.md §10)"
    )
    return Check(name, "aviso", detail)


def _check_vault_remote(vault: Vault, settings: Settings, probes: DoctorProbes) -> list[Check]:
    checks: list[Check] = []
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
        return [Check("Remoto del vault", "fallo", str(error))]
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


def check_marp(generators: GeneratorsSettings, probes: DoctorProbes) -> Check:
    """Whether `generators.marp_command` is on the probes' `PATH`, and its `--version`."""
    name = "Marp CLI (diapositivas)"
    command = generators.marp_command
    path = probes.environ.get("PATH", "")
    executable = shutil.which(command[0], path=path)
    if executable is None:
        detail = (
            f"no se encuentra `{command[0]}`: no se exportarán PDF ni PPTX; {MARP_INSTALL_HINT}"
        )
        return Check(name, "aviso", detail)
    try:
        completed = subprocess.run(
            [executable, *command[1:], "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=MARP_VERSION_TIMEOUT_SECONDS,
            env={**probes.environ, "PATH": path},
            check=False,
        )
    except subprocess.TimeoutExpired:
        detail = f"`{command[0]} --version` no terminó en {MARP_VERSION_TIMEOUT_SECONDS:g} s"
        return Check(name, "aviso", detail)
    except OSError as error:
        return Check(name, "aviso", f"no se puede ejecutar {executable}: {error}")
    output = completed.stdout.decode("utf-8", "replace").strip()
    if completed.returncode != 0 or not output:
        detail = (
            f"`{command[0]} --version` falló (código {completed.returncode}); {MARP_INSTALL_HINT}"
        )
        return Check(name, "aviso", detail)
    return Check(name, "ok", f"{output.splitlines()[0]} ({executable})")


def run_doctor(
    settings: Settings, *, api_call: bool = False, probes: DoctorProbes | None = None
) -> list[Check]:
    """Every check, in the order the report prints them."""
    probes = probes or DoctorProbes()
    return [
        check_python_dependencies(),
        *check_stt(settings.stt),
        check_llm_access(settings, api_call, probes),
        *check_vault(settings, probes),
        check_port(settings.server, probes),
        check_service(),
        check_marp(settings.generators, probes),
    ]
