"""The `studentassistant` command line.

`serve` runs the backend, `version` prints the version, `pair` shows a pairing QR minted by the
running backend, `devices` lists (or `devices revoke <id>` removes) the paired capture clients,
`cost` prints what the Claude calls recorded in the vault's ledgers cost, and `setup` creates or
clones the vault on this PC and records it in the configuration file.

Typer builds the command tree and `[project.scripts]` in `pyproject.toml` exposes it as the
`studentassistant` console script. Nothing here takes a flag the configuration cannot already set:
where the server listens comes from `studentassistant.config` (the TOML file plus the `SA_*`
environment variables), so there is one way to configure the backend and not two. `setup`'s
options are the answers it writes into that configuration, not a second way to set it.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import click
import segno
import typer
import uvicorn

from studentassistant import __version__
from studentassistant.config import ServerSettings, Settings, check_repo_name, write_vault_config
from studentassistant.llm.cost import day_usd, utc_now
from studentassistant.server.app import create_app
from studentassistant.server.devices import DeviceStore
from studentassistant.vault import (
    LedgerEntry,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    VaultError,
    list_subjects,
    list_topics,
    read_ledger,
)
from studentassistant.vault.github import GitHubHost, GitHubHostError, select_host
from studentassistant.vault.setup import SetupError, SetupResult, clone_vault, create_vault

cli = typer.Typer(
    name="studentassistant",
    help="Student Assistant backend: study sessions, master notes and generated study material.",
    add_completion=False,
)


@cli.command()
def serve() -> None:
    """Serve the FastAPI app on the configured host and port until interrupted."""
    server = Settings().server
    # No proxy sits in front: never let `X-Forwarded-For` rewrite the client address the LAN
    # guard and the loopback trust see (uvicorn trusts it from loopback by default).
    uvicorn.run(create_app(server=server), host=server.host, port=server.port, proxy_headers=False)


@cli.command()
def version() -> None:
    """Print the version of the installed backend."""
    typer.echo(__version__)


_WILDCARD_HOSTS = ("0.0.0.0", "::", "", "localhost")


def local_backend_url(server: ServerSettings) -> str:
    """Where this PC reaches its own backend: loopback unless `server.host` names one address."""
    host = "127.0.0.1" if server.host in _WILDCARD_HOSTS else server.host
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{server.port}"


def _post_json(url: str) -> dict[str, Any]:
    """POST an empty JSON body to `url` and return the decoded answer (tests replace this)."""
    request = urllib.request.Request(
        url, data=b"{}", method="POST", headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


@cli.command()
def pair() -> None:
    """Show a one-time pairing QR (valid 5 minutes) for a capture client to scan."""
    server = Settings().server
    url = local_backend_url(server) + "/api/pair/codes"
    try:
        minted = _post_json(url)
    except (urllib.error.URLError, OSError) as error:
        typer.echo(f"Could not reach the backend at {url}: is `studentassistant serve` running?")
        raise typer.Exit(code=1) from error
    expires_at = datetime.fromisoformat(minted["expires_at"]).astimezone()
    qr = io.StringIO()
    segno.make(json.dumps({"url": minted["url"], "code": minted["code"]})).terminal(
        out=qr, compact=True
    )
    typer.echo(qr.getvalue())
    typer.echo(f"URL:     {minted['url']}")
    typer.echo(f"Code:    {minted['code']}")
    typer.echo(f"Expires: {expires_at:%Y-%m-%d %H:%M:%S %Z} (single use)")


devices_cli = typer.Typer(help="The paired capture clients.", invoke_without_command=True)
cli.add_typer(devices_cli, name="devices")


def _device_store() -> DeviceStore:
    return DeviceStore(Settings().server.devices_path)


@devices_cli.callback()
def devices(ctx: typer.Context) -> None:
    """List the paired devices (the default) or revoke one."""
    if ctx.invoked_subcommand is None:
        list_devices()


@devices_cli.command("list")
def list_devices() -> None:
    """List the paired devices: id, name and when it was paired (never a token)."""
    paired = _device_store().list_devices()
    if not paired:
        typer.echo("No paired devices.")
        return
    for device in paired:
        paired_at = device.created_at.astimezone()
        typer.echo(f"{device.id}\t{device.name}\t{paired_at:%Y-%m-%d %H:%M:%S %Z}")


@devices_cli.command("revoke")
def revoke(device_id: str) -> None:
    """Remove a paired device: its token stops being accepted at once."""
    if not _device_store().revoke(device_id):
        typer.echo(f"No paired device {device_id}.")
        raise typer.Exit(code=1)
    typer.echo(f"Revoked {device_id}.")


def _topic_line(name: str, entries: list[LedgerEntry]) -> str:
    """One topic's totals: USD, tokens by kind, calls, and how many calls had no known price."""
    usd = sum(entry.estimated_usd or 0.0 for entry in entries)
    unpriced = sum(1 for entry in entries if entry.estimated_usd is None)
    line = (
        f"{name}: ${usd:.4f} ("
        f"entrada {sum(entry.input_tokens for entry in entries)}, "
        f"salida {sum(entry.output_tokens for entry in entries)}, "
        f"caché leída {sum(entry.cache_read_tokens for entry in entries)}, "
        f"caché escrita {sum(entry.cache_write_tokens for entry in entries)} tokens; "
        f"{len(entries)} llamadas"
    )
    if unpriced:
        line += f", {unpriced} sin precio conocido"
    return line + ")"


@cli.command()
def cost(
    topic: str | None = typer.Option(
        None, "--topic", help="Only this topic, as <subject-slug>/<topic-slug>."
    ),
) -> None:
    """Print the USD and tokens the ledger records per topic, plus today's (UTC) total."""
    try:
        vault = Vault.open(Settings().vault.path)
    except VaultError as error:
        typer.echo(f"No se puede abrir la bóveda: {error}")
        raise typer.Exit(code=1) from error
    if topic is not None:
        subject_slug, _, topic_slug = topic.partition("/")
        if not subject_slug or not topic_slug:
            typer.echo(f"«{topic}» no es un tema: usa --topic <asignatura>/<tema>.")
            raise typer.Exit(code=1)
        try:
            entries = read_ledger(vault, subject_slug, topic_slug)
        except (SubjectNotFoundError, TopicNotFoundError) as error:
            typer.echo(f"No existe el tema «{topic}» en la bóveda.")
            raise typer.Exit(code=1) from error
        typer.echo(_topic_line(topic, entries))
    else:
        printed = False
        for subject in list_subjects(vault):
            for stored in list_topics(vault, subject.slug):
                entries = read_ledger(vault, subject.slug, stored.slug)
                if entries:
                    typer.echo(_topic_line(f"{subject.slug}/{stored.slug}", entries))
                    printed = True
        if not printed:
            typer.echo("Sin gasto registrado.")
    typer.echo(f"Hoy (UTC): ${day_usd(vault, utc_now()):.4f}")


class SetupMode(StrEnum):
    """What `setup` does with the GitHub repository (the values are the Spanish prompt answers)."""

    create = "crear"
    clone = "clonar"


def _github_host() -> GitHubHost:
    """The way this PC reaches GitHub (tests replace this with a local one)."""
    return select_host()


def _ask_repo() -> str:
    while True:
        repo = typer.prompt("Repositorio de GitHub (propietario/nombre)").strip()
        try:
            return check_repo_name(repo)
        except ValueError:
            typer.echo(f"«{repo}» no es válido: escríbelo como propietario/nombre.")


_SETUP_DONE = {
    "created": "Vault creado en {path} y subido a {repo}.",
    "cloned": "Vault clonado de {repo} en {path}.",
    "already-set-up": "Este PC ya tenía el vault de {repo} en {path}: no hay nada que hacer.",
}


@cli.command()
def setup(
    vault_repo: Annotated[
        str | None, typer.Option(help="Repositorio de GitHub del vault, propietario/nombre.")
    ] = None,
    path: Annotated[
        Path | None, typer.Option(help="Carpeta local del vault (por defecto, vault.path).")
    ] = None,
    create: Annotated[
        bool, typer.Option("--create", help="Crear un vault nuevo y un repositorio privado.")
    ] = False,
    clone: Annotated[
        bool, typer.Option("--clone", help="Clonar un vault que ya está en GitHub.")
    ] = False,
    student: Annotated[
        str | None, typer.Option(help="Tu nombre, tal como lo guardará un vault nuevo.")
    ] = None,
) -> None:
    """Create or clone the vault from GitHub and record it in the configuration file.

    Every option left out is asked for (in Spanish); with `--vault-repo`, `--path` and one of
    `--create`/`--clone` nothing is asked. Re-running with the same answers changes nothing.
    """
    if create and clone:
        typer.echo("Elige solo una opción: --create o --clone.")
        raise typer.Exit(code=2)
    settings = Settings()
    # With the repository, the path and the mode given nothing is asked, not even the name.
    unattended = (create or clone) and vault_repo is not None and path is not None
    if create or clone:
        mode = SetupMode.create if create else SetupMode.clone
    else:
        mode = typer.prompt(
            "¿Quieres crear un vault nuevo o clonar uno que ya está en GitHub?",
            type=click.Choice([m.value for m in SetupMode]),
            default=SetupMode.clone.value,
        )
        mode = SetupMode(mode)
    if vault_repo is None:
        vault_repo = _ask_repo()
    else:
        try:
            check_repo_name(vault_repo)
        except ValueError:
            typer.echo(f"«{vault_repo}» no es válido: escríbelo como propietario/nombre.")
            raise typer.Exit(code=2) from None
    if path is None:
        path = Path(
            typer.prompt("Carpeta local del vault", default=str(settings.vault.path))
        ).expanduser()
    if mode is SetupMode.create and student is None:
        owner = vault_repo.split("/")[0]
        student = (
            owner
            if unattended
            else typer.prompt("Tu nombre (así te llamará la aplicación)", default=owner)
        )

    git = settings.vault.git
    try:
        host = _github_host()
        result: SetupResult
        if mode is SetupMode.create:
            assert student is not None
            result = create_vault(
                path,
                vault_repo,
                student,
                host,
                git.author_email,
                git.timeout_seconds,
                warn=typer.echo,
            )
        else:
            result = clone_vault(
                path, vault_repo, host, author_email=git.author_email, timeout=git.timeout_seconds
            )
    except (SetupError, GitHubHostError) as error:
        typer.echo(f"No se pudo preparar el vault: {error}")
        raise typer.Exit(code=1) from error
    write_vault_config(result.vault.path, result.repo)
    typer.echo(_SETUP_DONE[result.action].format(path=result.vault.path, repo=result.repo))
