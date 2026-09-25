"""The `studentassistant` command line.

`serve` runs the backend (`serve --record` also records each session for `replay`), `version`
prints the version, `pair` shows a pairing QR minted by the running backend, `devices` lists (or
`devices revoke <id>` removes) the paired capture clients, `cost` prints what the Claude calls
recorded in the vault's ledgers cost, `import-pdf` adds a PDF (or a page range of it) to a topic
as a source, `replay` feeds a recorded session through the gateway as a capture client would,
`setup` gets a PC from clone to running (the vault, the Anthropic API key, the STT model, the
systemd service), `doctor` checks that it is, `index rebuild` recreates the derived search
index from the vault, `purge` applies the vault's retention policy, and `generate <kind> --topic`
builds one kind of study material from a topic's notes (`studentassistant.generators`).

Typer builds the command tree and `[project.scripts]` in `pyproject.toml` exposes it as the
`studentassistant` console script. Nothing here takes a flag the configuration cannot already set:
where the server listens comes from `studentassistant.config` (the TOML file plus the `SA_*`
environment variables), so there is one way to configure the backend and not two. `setup`'s
options are the answers it writes into that configuration, not a second way to set it, and
`serve --record` only switches recording on: where recordings go is `[server].recordings_dir`.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
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
from pydantic import ValidationError

from studentassistant import __version__
from studentassistant.config import (
    ServerSettings,
    Settings,
    check_repo_name,
    config_toml_path,
    write_vault_config,
)
from studentassistant.evals.cli import eval_cli
from studentassistant.generators import (
    GenerationError,
    UnknownGeneratorError,
    default_registry,
    run_generator,
)
from studentassistant.install import service as install_service
from studentassistant.install import whisper
from studentassistant.install.apikey import (
    API_KEY_ENV_VAR,
    export_api_key,
    read_api_key,
    store_api_key,
)
from studentassistant.install.doctor import DoctorProbes, run_doctor
from studentassistant.llm import (
    AnthropicTransport,
    CostConfirmationRequiredError,
    LedgerBinding,
    LLMError,
    RefusalError,
    Transport,
    get_client,
)
from studentassistant.llm.cost import day_usd, utc_now
from studentassistant.observer import (
    COMPACTED_EVENT_KIND,
    ObserverStateError,
    compaction_payload,
    load_observer_snapshot,
)
from studentassistant.observer.catchup import compactable_snapshot
from studentassistant.server.app import create_app
from studentassistant.server.devices import DeviceStore
from studentassistant.server.recorder import SessionRecorder
from studentassistant.server.recording import Recording, RecordingError, read_recording
from studentassistant.server.replay import (
    AsgiTransport,
    HttpTransport,
    ReplayError,
    ReplayResult,
    replay,
)
from studentassistant.sources import (
    PdfImportError,
    PdfTooLargeError,
    import_pdf,
    parse_page_range,
)
from studentassistant.vault import (
    GitSync,
    LedgerEntry,
    SecretRefused,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    VaultError,
    list_subjects,
    list_topics,
    read_ledger,
    read_topic_events,
    require_topic,
    topic_directory,
)
from studentassistant.vault.github import GitHubHost, GitHubHostError, select_host
from studentassistant.vault.index import IndexReport, VaultIndexError, rebuild_index
from studentassistant.vault.purge import (
    REASON_TEXT,
    Compaction,
    PurgeError,
    PurgeItem,
    TopicPurgePlan,
    apply_purge,
    format_size,
    plan_topic_purge,
    purged_history_paths,
)
from studentassistant.vault.setup import SetupError, SetupResult, clone_vault, create_vault

cli = typer.Typer(
    name="studentassistant",
    help="Student Assistant backend: study sessions, master notes and generated study material.",
    add_completion=False,
)


@cli.command()
def serve(
    record: Annotated[
        bool,
        typer.Option(
            "--record",
            help="Record each session's raw inputs under [server].recordings_dir, for `replay`.",
        ),
    ] = False,
) -> None:
    """Serve the FastAPI app on the configured host and port until interrupted."""
    settings = Settings()
    server = settings.server
    # The key `setup` stored on this PC, unless the environment already carries one.
    export_api_key(settings.llm.api_key_path())
    recorder = SessionRecorder(server.recordings_dir) if record else None
    try:
        # The live observer calls Claude through the real transport ([observer] enabled = false
        # turns it off).
        app = create_app(
            server=server,
            recorder=recorder,
            llm_transport=AnthropicTransport(),
            llm_settings=settings,
        )
    except ValueError as error:
        typer.echo(f"Cannot record: {error}", err=True)
        raise typer.Exit(code=1) from error
    if recorder is not None:
        typer.echo(f"Recording every session under {recorder.root}")
    # No proxy sits in front: never let `X-Forwarded-For` rewrite the client address the LAN
    # guard and the loopback trust see (uvicorn trusts it from loopback by default).
    uvicorn.run(app, host=server.host, port=server.port, proxy_headers=False)


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


@cli.command("import-pdf")
def import_pdf_command(
    topic: Annotated[str, typer.Argument(help="El tema, como <asignatura>/<tema>.")],
    file: Annotated[Path, typer.Argument(help="El PDF que se importa.")],
    pages: Annotated[
        str | None, typer.Option("--pages", help="Solo estas páginas del PDF, p. ej. 82-94.")
    ] = None,
) -> None:
    """Add a PDF, or only a page range of it, to a topic as a source and commit it to the vault.

    The kept pages are stored with their text and a thumbnail each (`studentassistant.sources`);
    the commit is local, and the server's sync pushes it.
    """
    settings = Settings()
    subject_slug, _, topic_slug = topic.partition("/")
    if not subject_slug or not topic_slug or "/" in topic_slug:
        typer.echo(f"«{topic}» no es un tema: escríbelo como <asignatura>/<tema>.")
        raise typer.Exit(code=2)
    try:
        page_range = parse_page_range(pages) if pages is not None else None
        if not file.is_file():
            typer.echo(f"No existe el archivo «{file}».")
            raise typer.Exit(code=1)
        if file.stat().st_size > settings.sources.max_pdf_bytes:
            # Refused before reading it into memory; `import_pdf` words the same limit.
            raise PdfTooLargeError(
                f"El PDF «{file.name}» supera el máximo que se importa"
                f" ({settings.sources.max_pdf_bytes / (1024 * 1024):.1f} MB)."
            )
        vault = Vault.open(settings.vault.path)
        imported = import_pdf(
            vault,
            subject_slug,
            topic_slug,
            file.name,
            file.read_bytes(),
            pages=page_range,
            settings=settings.sources,
        )
    except PdfImportError as error:
        typer.echo(f"No se ha importado el PDF: {error}")
        raise typer.Exit(code=1) from error
    except (SubjectNotFoundError, TopicNotFoundError) as error:
        typer.echo(f"No existe el tema «{topic}» en la bóveda.")
        raise typer.Exit(code=1) from error
    except SecretRefused as error:
        typer.echo("No se ha importado el PDF: parece contener una clave o un token.")
        raise typer.Exit(code=1) from error
    except VaultError as error:
        typer.echo(f"No se puede abrir la bóveda: {error}")
        raise typer.Exit(code=1) from error
    GitSync(vault, settings.vault.git).checkpoint(f"Importar PDF {file.name} en {topic}")
    meta = imported.meta
    typer.echo(
        f"PDF importado como {imported.source_id}: páginas {meta['first_page']}-"
        f"{meta['last_page']} del original ({meta['page_count']} páginas)."
    )
    empty = [page.original_page for page in imported.pages if not page.has_text]
    if empty:
        typer.echo(
            "Sin texto extraíble (¿escaneadas?): páginas "
            + ", ".join(str(number) for number in empty)
            + "."
        )


async def _replay_in_process(recording: Recording, **options: Any) -> ReplayResult:
    """Replay into an app built from the configuration, its lifespan run as `serve` runs it."""
    async with AsgiTransport(create_app()) as transport:
        return await replay(recording, transport, **options)


async def _replay_to(url: str, recording: Recording, **options: Any) -> ReplayResult:
    async with HttpTransport(url) as transport:
        return await replay(recording, transport, **options)


@cli.command("replay")
def replay_command(
    directory: Annotated[Path, typer.Argument(help="La carpeta de la sesión grabada.")],
    speed: Annotated[
        float, typer.Option("--speed", help="Cuántas veces más rápido que la grabación.")
    ] = 1.0,
    topic: Annotated[
        str | None,
        typer.Option("--topic", help="Otro tema en vez del grabado, como <asignatura>/<tema>."),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Un backend en marcha, p. ej. http://localhost:8765."),
    ] = None,
) -> None:
    """Replay a recorded session as a capture client: start, hello, the timed inputs, the end.

    With `--url` it talks to that running backend; without it, it builds the app in-process
    against the configured vault (and `[stt]`, which must match the recording's STT mode).
    """
    subject_id = topic_id = None
    if topic is not None:
        subject_id, _, topic_id = topic.partition("/")
        if not subject_id or not topic_id or "/" in topic_id:
            typer.echo(f"«{topic}» no es un tema: escríbelo como <asignatura>/<tema>.")
            raise typer.Exit(code=2)
    if speed <= 0:
        typer.echo("--speed tiene que ser mayor que 0.")
        raise typer.Exit(code=2)
    try:
        recording = read_recording(directory)
    except RecordingError as error:
        typer.echo(f"No se puede leer la grabación «{directory}»: {error}")
        raise typer.Exit(code=1) from error
    options: dict[str, Any] = {"speed": speed, "subject": subject_id, "topic": topic_id}
    try:
        if url is None:
            result = asyncio.run(_replay_in_process(recording, **options))
        else:
            result = asyncio.run(_replay_to(url, recording, **options))
    except ReplayError as error:
        typer.echo(f"La reproducción ha fallado: {error}")
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Sesión {result.session_id} reproducida en {result.subject_id}/{result.topic_id}: "
        f"{result.finals_sent} frases finales, {result.partials_sent} parciales, "
        f"{result.events_sent} eventos, {result.audio_frames_sent} tramas de audio, "
        f"{result.captures_stored} capturas guardadas"
        + (f" ({result.captures_duplicate} repetidas)" if result.captures_duplicate else "")
        + "."
    )


index_cli = typer.Typer(help="The derived search index of the vault (a rebuildable cache).")
cli.add_typer(index_cli, name="index")
cli.add_typer(eval_cli, name="eval")


def _index_summary(report: IndexReport) -> str:
    line = f"Índice reconstruido: {report.documents} documentos buscables."
    if report.skipped:
        line += f" {len(report.skipped)} partes de la bóveda no se pudieron leer:"
        line += "".join(f"\n  {unit}: {reason}" for unit, reason in report.skipped)
    return line


@index_cli.command("rebuild")
def index_rebuild() -> None:
    """Recreate the index from scratch, reading only the vault."""
    settings = Settings().vault
    try:
        vault = Vault.open(settings.path)
        report = rebuild_index(vault, settings.index_path)
    except VaultIndexError as error:
        typer.echo(f"No se pudo reconstruir el índice: {error}")
        raise typer.Exit(code=1) from error
    except VaultError as error:
        typer.echo(f"No se puede abrir la bóveda: {error}")
        raise typer.Exit(code=1) from error
    typer.echo(_index_summary(report))


stt_cli = typer.Typer(help="Speech-to-text on this PC (server mode, ADR-0008).")
cli.add_typer(stt_cli, name="stt")


@stt_cli.command("download")
def stt_download() -> None:
    """Download the faster-whisper model of `[stt.options.faster-whisper]` into its cache."""
    stt = Settings().stt
    if not whisper.uses_whisper(stt):
        typer.echo(
            f"Aviso: la voz usa ahora el modo {stt.mode} con {stt.provider}; el modelo solo se usa"
            " con stt.mode = server y stt.provider = faster-whisper."
        )
    options = whisper.whisper_options(stt)
    typer.echo(f"Descargando el modelo de Whisper {options.model} (puede tardar un rato)...")
    try:
        path = whisper.download(options)
    except whisper.WhisperError as error:
        typer.echo(f"No se pudo descargar el modelo: {error}")
        raise typer.Exit(code=1) from error
    typer.echo(f"Modelo de Whisper {options.model} listo en {path}.")


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
    api_key_stdin: Annotated[
        bool,
        typer.Option(
            "--api-key-stdin", help="Leer la clave de la API de Anthropic de la entrada estándar."
        ),
    ] = False,
    install_unit: Annotated[
        bool | None,
        typer.Option(
            "--service/--no-service",
            help="Instalar (o no) el servicio systemd que mantiene el backend en marcha.",
        ),
    ] = None,
) -> None:
    """Get this PC from clone to running: vault, API key, STT model and systemd service.

    The vault is created or cloned from GitHub and recorded in the configuration file; the
    Anthropic API key is stored in a file only you can read (never in the vault); the configured
    STT provider is prepared (the Whisper model downloaded, when faster-whisper is selected); and
    a systemd `--user` unit for `serve` is installed and started. Every option left out is asked
    for (in Spanish); with `--vault-repo`, `--path` and one of `--create`/`--clone` nothing is
    asked. Re-running with the same answers changes nothing.
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
            index_path = settings.vault.index_path

            def rebuild_after_clone(vault: Vault) -> None:
                # ADR-0002: install -> setup -> clone -> index rebuild.
                try:
                    typer.echo(_index_summary(rebuild_index(vault, index_path)))
                except VaultIndexError as error:
                    typer.echo(f"Aviso: no se pudo reconstruir el índice: {error}")

            result = clone_vault(
                path,
                vault_repo,
                host,
                post_clone=rebuild_after_clone,
                author_email=git.author_email,
                timeout=git.timeout_seconds,
            )
    except (SetupError, GitHubHostError) as error:
        typer.echo(f"No se pudo preparar el vault: {error}")
        raise typer.Exit(code=1) from error
    write_vault_config(result.vault.path, result.repo)
    typer.echo(_SETUP_DONE[result.action].format(path=result.vault.path, repo=result.repo))

    failed = [
        not _setup_api_key(settings, unattended=unattended, from_stdin=api_key_stdin),
        not _setup_stt(settings),
        not _setup_service(unattended=unattended, install=install_unit),
    ]
    if any(failed):
        raise typer.Exit(code=1)
    typer.echo("Listo. Comprueba la instalación con `studentassistant doctor`.")


def _setup_api_key(settings: Settings, *, unattended: bool, from_stdin: bool) -> bool:
    """Store the Anthropic API key on this PC; never echo it. Returns False on failure."""
    path = settings.llm.api_key_path()
    if from_stdin:
        key = sys.stdin.readline().strip()
    elif read_api_key(path) is not None:
        typer.echo(f"La clave de la API de Anthropic ya está guardada en {path}.")
        return True
    elif os.environ.get(API_KEY_ENV_VAR):
        typer.echo(f"La clave de la API de Anthropic viene de {API_KEY_ENV_VAR}: no se guarda.")
        return True
    elif unattended:
        key = ""
    else:
        key = typer.prompt(
            "Clave de la API de Anthropic (no se verá; Enter para dejarlo para luego)",
            default="",
            hide_input=True,
            show_default=False,
        ).strip()
    if not key:
        typer.echo(
            "Sin clave de la API de Anthropic: guárdala luego con"
            " `studentassistant setup --api-key-stdin`."
        )
        return True
    try:
        store_api_key(path, key)
    except ValueError:
        typer.echo("Esa clave no es válida (no puede tener espacios): no se ha guardado.")
        return False
    typer.echo(f"Clave de la API de Anthropic guardada en {path} (solo la puedes leer tú).")
    return True


def _setup_stt(settings: Settings) -> bool:
    """Prepare the configured STT provider: download the Whisper model when it is selected."""
    stt = settings.stt
    if not whisper.uses_whisper(stt):
        where = "el cliente" if stt.mode == "client" else "el proveedor"
        typer.echo(f"Voz: modo {stt.mode}, {stt.provider}; transcribe {where}, nada que descargar.")
        return True
    options = whisper.whisper_options(stt)
    typer.echo(f"Descargando el modelo de Whisper {options.model} (puede tardar un rato)...")
    try:
        path = whisper.download(options)
    except whisper.WhisperError as error:
        typer.echo(f"No se pudo preparar la voz: {error}")
        return False
    typer.echo(f"Modelo de Whisper {options.model} listo en {path}.")
    return True


def _setup_service(*, unattended: bool, install: bool | None) -> bool:
    """Install and start the systemd `--user` unit for `serve` (asked for unless given)."""
    if install is None:
        install = unattended or typer.confirm(
            "¿Instalar el servicio para que el backend arranque solo al iniciar sesión?",
            default=True,
        )
    if not install:
        typer.echo("Servicio no instalado: arranca el backend con `studentassistant serve`.")
        return True
    try:
        executable = install_service.serve_executable()
    except FileNotFoundError:
        typer.echo("No se encuentra el comando studentassistant: no se instala el servicio.")
        return False
    outcome = install_service.install_unit(executable, config_toml_path().absolute())
    if outcome.error is not None:
        typer.echo(f"No se pudo activar el servicio: {outcome.error}")
        return False
    typer.echo(f"Servicio {install_service.UNIT_NAME} instalado en {outcome.path} y en marcha.")
    return True


def _backend_answers(server: ServerSettings) -> bool:
    """Whether a Student Assistant backend answers `GET /api/health` on this PC."""
    url = local_backend_url(server) + "/api/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(body, dict) and "protocol_version" in body


def _doctor_probes(server: ServerSettings) -> DoctorProbes:
    """What `doctor` reaches the outside world through (tests replace this)."""
    return DoctorProbes(github_host=_github_host, backend_answers=lambda: _backend_answers(server))


@cli.command()
def doctor(
    api_call: Annotated[
        bool,
        typer.Option(
            "--api-call",
            help="Probar la clave con una llamada gratuita a la API de Anthropic.",
        ),
    ] = False,
) -> None:
    """Check that this PC is ready: one line per check, exit status 1 when any fails."""
    try:
        settings = Settings()
    except ValidationError as error:
        typer.echo(f"[FALLO] Configuración: {config_toml_path()} no es válida: {error}")
        raise typer.Exit(code=1) from error
    source = config_toml_path()
    shown = str(source) if source.exists() else f"{source} no existe: valores por defecto"
    typer.echo(f"[ok] Configuración: {shown}")
    checks = run_doctor(settings, api_call=api_call, probes=_doctor_probes(settings.server))
    for check in checks:
        typer.echo(check.line())
    if any(check.failed for check in checks):
        raise typer.Exit(code=1)


HARD_PURGE_WARNING = (
    "--hard reescribe el historial de la bóveda: lo purgado desaparece de todas sus versiones y"
    " la rama y las etiquetas se suben a GitHub a la fuerza. Después, cualquier otro PC que tenga"
    " la bóveda debe clonarla de nuevo (studentassistant setup --clone en una carpeta vacía) o, si"
    " no tiene nada sin subir, ejecutar `git fetch origin && git reset --hard origin/main` en"
    " ella; si no, volvería a subir lo purgado. Detén el servidor antes de continuar."
)
HARD_PURGE_WORD = "reescribir"


def _purge_targets(vault: Vault, topic: str | None) -> list[tuple[str, str]]:
    if topic is None:
        return [
            (subject.slug, stored.slug)
            for subject in list_subjects(vault)
            for stored in list_topics(vault, subject.slug)
        ]
    subject_slug, _, topic_slug = topic.partition("/")
    if not subject_slug or not topic_slug or "/" in topic_slug:
        typer.echo(f"«{topic}» no es un tema: escríbelo como <asignatura>/<tema>.")
        raise typer.Exit(code=2)
    try:
        require_topic(vault, subject_slug, topic_slug)
    except (SubjectNotFoundError, TopicNotFoundError) as error:
        typer.echo(f"No existe el tema «{topic}» en la bóveda.")
        raise typer.Exit(code=1) from error
    return [(subject_slug, topic_slug)]


def _compaction(vault: Vault, subject_slug: str, topic_slug: str) -> Compaction | None:
    """Where the topic's folded events end: the fold up to the observer's newest acknowledgement,
    so a batch it still owes is never compacted away (`observer.catchup`)."""
    try:
        snapshot = compactable_snapshot(read_topic_events(vault, subject_slug, topic_slug))
    except (ObserverStateError, VaultError) as error:
        typer.echo(f"{subject_slug}/{topic_slug}: sus eventos no se compactan ({error}).")
        return None
    if snapshot is None or snapshot.cursor is None:
        return None
    return Compaction(
        session_id=snapshot.cursor.session_id,
        seq=snapshot.cursor.seq,
        kind=COMPACTED_EVENT_KIND,
        payload=compaction_payload(snapshot),
    )


def _item_line(item: PurgeItem) -> str:
    size = (
        format_size(item.size_before)
        if item.removed
        else f"{format_size(item.size_before)} -> {format_size(item.size_after or 0)}"
    )
    verb = "se borra" if item.removed else "se compacta"
    return f"  - {item.path}: {verb} ({REASON_TEXT[item.reason]}, {size})"


def _print_plan(plan: TopicPurgePlan) -> None:
    name = f"{plan.subject_slug}/{plan.topic_slug}"
    if plan.skipped is not None:
        typer.echo(f"{name}: se omite, {plan.skipped}.")
        return
    typer.echo(f"{name}:" if plan.items else f"{name}: nada que purgar.")
    for item in plan.items:
        typer.echo(_item_line(item))
    for path in plan.protected:
        typer.echo(f"  = {path}: se conserva (lo citan los apuntes)")


@cli.command()
def purge(
    topic: Annotated[
        str | None, typer.Option("--topic", help="Solo este tema, como <asignatura>/<tema>.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Solo mostrar lo que se purgaría y cuánto ocupa.")
    ] = False,
    hard: Annotated[
        bool,
        typer.Option("--hard", help="Además, reescribir el historial de git para liberar espacio."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación para --hard.")] = False,
) -> None:
    """Apply the retention policy ([vault.purge]) to every topic, or to one, and commit it.

    Never removes a transcript, the notes or what they cite. The purge is a normal commit, so it
    can be undone from git history; `--hard` rewrites that history (asking first) and force-pushes.
    """
    settings = Settings()
    try:
        vault = Vault.open(settings.vault.path)
    except VaultError as error:
        typer.echo(f"No se puede abrir la bóveda: {error}")
        raise typer.Exit(code=1) from error
    targets = _purge_targets(vault, topic)
    sync = GitSync(vault, settings.vault.git)
    has_remote = sync.git.run("remote", "get-url", settings.vault.git.remote).ok
    if not dry_run:
        sync.checkpoint("cambios pendientes antes de la purga")
        if hard and has_remote:
            pulled = sync.sync()
            if not pulled.ok:
                typer.echo(
                    f"No se puede purgar con --hard sin estar al día con GitHub: {pulled.message}"
                )
                raise typer.Exit(code=1)
    try:
        plans = [
            plan_topic_purge(
                sync,
                subject_slug,
                topic_slug,
                settings.vault.purge,
                _compaction(vault, subject_slug, topic_slug),
            )
            for subject_slug, topic_slug in targets
        ]
    except (PurgeError, VaultError) as error:
        typer.echo(f"No se puede preparar la purga: {error}")
        raise typer.Exit(code=1) from error
    for plan in plans:
        _print_plan(plan)
    items = [item for plan in plans for item in plan.items]
    saved = format_size(sum(item.saved_bytes for item in items))

    if dry_run:
        typer.echo(
            f"Simulación: se liberarían {saved} en {len(items)} archivos; no se ha cambiado nada."
        )
        if hard:
            roots = [
                topic_directory(vault, plan.subject_slug, plan.topic_slug)
                for plan in plans
                if plan.skipped is None
            ]
            earlier = purged_history_paths(sync, roots)
            removed = {item.path for item in items if item.removed}
            typer.echo(
                f"Con --hard se borrarían del historial {len(removed | set(earlier))} archivos."
            )
        return
    if hard and not yes:
        typer.echo(HARD_PURGE_WARNING)
        answer = typer.prompt(f"Escribe «{HARD_PURGE_WORD}» para continuar", default="")
        if answer.strip() != HARD_PURGE_WORD:
            typer.echo("Cancelado: no se ha cambiado nada.")
            raise typer.Exit(code=1)

    def refresh_snapshot(plan: TopicPurgePlan) -> None:
        if plan.compacts_events:
            load_observer_snapshot(vault, plan.subject_slug, plan.topic_slug)

    try:
        result = apply_purge(sync, plans, hard=hard, before_commit=refresh_snapshot)
    except (PurgeError, VaultError) as error:
        typer.echo(f"La purga no se ha completado: {error}")
        raise typer.Exit(code=1) from error
    if result.commit is None:
        typer.echo("Nada que purgar.")
    else:
        typer.echo(
            f"Purga guardada en {result.commit[:10]}: {saved} menos en la bóveda; sigue"
            " recuperable en el historial de git."
        )
        if not hard and has_remote:
            if sync.push_now():
                typer.echo("Subida a GitHub.")
            else:
                typer.echo("No se ha podido subir ahora; se subirá en la próxima sincronización.")
    if result.history is not None:
        history = result.history
        typer.echo(
            f"Historial reescrito: {len(history.paths)} archivos fuera de todas las versiones;"
            f" el repositorio ocupa {format_size(history.size_after)}"
            f" (antes {format_size(history.size_before)})."
        )
        if history.pushed:
            typer.echo(
                "Subido a GitHub a la fuerza: clona de nuevo la bóveda en los demás PCs antes de"
                " usarlos."
            )
    elif hard:
        typer.echo("No había nada purgado en el historial que reescribir.")


def _option_value(text: str) -> Any:
    """A `--option` value: JSON when it parses as JSON (`10`, `true`, `["a"]`), else the text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _generator_transport() -> Transport:
    """The transport `generate` calls Claude through; tests replace this function."""
    return AnthropicTransport()


@cli.command("generate")
def generate_command(
    kind: Annotated[
        str, typer.Argument(help="Qué material: el tipo de generador (p. ej. esquema).")
    ],
    topic: Annotated[str, typer.Option("--topic", help="El tema, como <asignatura>/<tema>.")],
    option: Annotated[
        list[str] | None,
        typer.Option("--option", "-o", help="Una opción del generador, como clave=valor."),
    ] = None,
    confirm_over_cap: Annotated[
        bool,
        typer.Option("--confirm-over-cap", help="Generar aunque se haya alcanzado el límite."),
    ] = False,
) -> None:
    """Generate one kind of study material from a topic's notes into its `generated/` and commit.

    The artifact records the notes version it was built from, and is reported stale once the notes
    change. The commit is local; the server's sync pushes it.
    """
    settings = Settings()
    subject_slug, _, topic_slug = topic.partition("/")
    if not subject_slug or not topic_slug or "/" in topic_slug:
        typer.echo(f"«{topic}» no es un tema: escríbelo como <asignatura>/<tema>.")
        raise typer.Exit(code=2)
    if kind not in default_registry:
        typer.echo(str(UnknownGeneratorError(kind, default_registry.kinds())))
        raise typer.Exit(code=2)
    options: dict[str, Any] = {}
    for item in option or []:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            typer.echo(f"«{item}» no es una opción: escríbela como clave=valor.")
            raise typer.Exit(code=2)
        options[key.strip()] = _option_value(value)
    try:
        vault = Vault.open(settings.vault.path)
        require_topic(vault, subject_slug, topic_slug)
    except (SubjectNotFoundError, TopicNotFoundError) as error:
        typer.echo(f"No existe el tema «{topic}» en la bóveda.")
        raise typer.Exit(code=1) from error
    except VaultError as error:
        typer.echo(f"No se puede abrir la bóveda: {error}")
        raise typer.Exit(code=1) from error
    sync = GitSync(vault, settings.vault.git)
    client = get_client(
        "generator",
        settings=settings,
        transport=_generator_transport(),
        ledger=LedgerBinding(vault, subject_slug, topic_slug),
    )
    try:
        result = asyncio.run(
            run_generator(
                vault,
                subject_slug,
                topic_slug,
                kind,
                client=client,
                sync=sync,
                options=options,
                confirm_over_cap=confirm_over_cap,
                grounding_min_support=settings.generators.grounding_min_support,
            )
        )
    except GenerationError as error:
        typer.echo(f"No se ha generado: {error}")
        raise typer.Exit(code=1) from error
    except CostConfirmationRequiredError as error:
        scope = "de la sesión" if error.cap == "session" else "del día"
        typer.echo(
            f"Se ha alcanzado el límite de gasto {scope} ({error.total_usd:.2f} de"
            f" {error.limit_usd:.2f} USD). Repite con --confirm-over-cap para generar igualmente."
        )
        raise typer.Exit(code=1) from error
    except RefusalError as error:
        typer.echo("Claude se ha negado a generar este material.")
        raise typer.Exit(code=1) from error
    except LLMError as error:
        typer.echo(f"No se ha podido generar: Claude no ha respondido ({error}).")
        raise typer.Exit(code=1) from error
    except VaultError as error:
        typer.echo(f"No se ha podido guardar en la bóveda: {error}")
        raise typer.Exit(code=1) from error
    version = f"apuntes v{result.notes.version}" if result.notes.version else "apuntes sin versión"
    if result.notes.version and result.notes.changed_since_version:
        version += " con cambios"
    typer.echo(f"Generado «{kind}» de {topic} a partir de {version}:")
    for path in result.files:
        typer.echo(f"  {path}")
    for path in result.removed:
        typer.echo(f"  (borrado) {path}")
    for warning in result.warnings:
        typer.echo(f"Aviso: {warning}")
