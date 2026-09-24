"""The `studentassistant` command line: `serve` runs the backend, `version` prints the version.

Typer builds the command tree and `[project.scripts]` in `pyproject.toml` exposes it as the
`studentassistant` console script. Nothing here takes a flag the configuration cannot already set:
where the server listens comes from `studentassistant.config` (the TOML file plus the `SA_*`
environment variables), so there is one way to configure the backend and not two.
"""

from __future__ import annotations

import typer
import uvicorn

from studentassistant import __version__
from studentassistant.config import Settings
from studentassistant.server.app import create_app

cli = typer.Typer(
    name="studentassistant",
    help="Student Assistant backend: study sessions, master notes and generated study material.",
    add_completion=False,
)


@cli.command()
def serve() -> None:
    """Serve the FastAPI app on the configured host and port until interrupted."""
    server = Settings().server
    uvicorn.run(create_app(), host=server.host, port=server.port)


@cli.command()
def version() -> None:
    """Print the version of the installed backend."""
    typer.echo(__version__)
