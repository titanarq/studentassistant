"""The `studentassistant` command line.

`serve` runs the backend, `version` prints the version, `pair` shows a pairing QR minted by the
running backend, and `devices` lists (or `devices revoke <id>` removes) the paired capture clients.

Typer builds the command tree and `[project.scripts]` in `pyproject.toml` exposes it as the
`studentassistant` console script. Nothing here takes a flag the configuration cannot already set:
where the server listens comes from `studentassistant.config` (the TOML file plus the `SA_*`
environment variables), so there is one way to configure the backend and not two.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import segno
import typer
import uvicorn

from studentassistant import __version__
from studentassistant.config import ServerSettings, Settings
from studentassistant.server.app import create_app
from studentassistant.server.devices import DeviceStore

cli = typer.Typer(
    name="studentassistant",
    help="Student Assistant backend: study sessions, master notes and generated study material.",
    add_completion=False,
)


@cli.command()
def serve() -> None:
    """Serve the FastAPI app on the configured host and port until interrupted."""
    server = Settings().server
    uvicorn.run(create_app(server=server), host=server.host, port=server.port)


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
