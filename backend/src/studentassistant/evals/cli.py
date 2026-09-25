"""`studentassistant eval run`: score the pipeline on the eval set; the cost is confirmed first."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

import studentassistant
from studentassistant.config import Settings
from studentassistant.evals.cases import EvalSetError, read_eval_set
from studentassistant.evals.estimate import CaseEstimate, estimate_case
from studentassistant.evals.run import CaseResult, run_directory, run_eval, write_report
from studentassistant.install.apikey import export_api_key
from studentassistant.llm import AnthropicTransport, Transport

eval_cli = typer.Typer(help="The eval set from real sessions (`[eval] path`).")


def _eval_transport() -> Transport:
    """The transport the eval calls Claude through; tests replace this function."""
    return AnthropicTransport()


def _code_checkout() -> Path | None:
    """The source checkout this backend runs from, when it runs from one (an editable install)."""
    root = Path(studentassistant.__file__).resolve().parents[3]
    return root if (root / "backend" / "pyproject.toml").is_file() else None


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def _echo_estimate(estimates: list[CaseEstimate]) -> float:
    typer.echo("Coste estimado (aproximado, sin contar la caché):")
    for estimate in estimates:
        typer.echo(f"  {estimate.case}: {estimate.usd:.2f} USD")
        for role in estimate.roles:
            price = "sin precio" if role.usd is None else f"{role.usd:.2f} USD"
            typer.echo(
                f"    {role.role} ({role.model}): {role.calls} llamadas, ~{role.input_tokens}"
                f" tokens de entrada y ~{role.output_tokens} de salida, {price}"
            )
    total = sum(estimate.usd for estimate in estimates)
    typer.echo(f"Total estimado: {total:.2f} USD")
    unpriced = sorted({model for e in estimates for model in e.unpriced})
    if unpriced:
        typer.echo(
            "Aviso: no hay precio en [llm.prices] para "
            + ", ".join(unpriced)
            + "; su coste no está en el total."
        )
    return total


def _echo_case(result: CaseResult) -> None:
    if result.error is not None:
        typer.echo(f"  {result.case}: no se ha podido reproducir ({result.error})")
        return
    cost = result.actual_usd or 0.0
    typer.echo(f"  {result.case}: global {_pct(result.score)}, coste real {cost:.2f} USD")


@eval_cli.command("run")
def run_command(
    case: Annotated[
        list[str] | None,
        typer.Option("--case", help="Solo este caso (se puede repetir)."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="No preguntar antes de gastar el coste estimado.")
    ] = False,
) -> None:
    """Replay every case with real Claude calls, score it against its reference and report.

    The estimated cost is shown first and must be confirmed (or `--yes`). Each case runs in a vault
    of its own under `runs/<time>/`, never in the student's vault.
    """
    settings = Settings()
    path = settings.eval.path
    if _inside(path, settings.vault.path) or _inside(settings.vault.path, path):
        typer.echo(
            f"El conjunto de evaluación «{path}» no puede estar dentro de la bóveda ni al revés."
        )
        raise typer.Exit(code=1)
    checkout = _code_checkout()
    if checkout is not None and _inside(path, checkout):
        typer.echo(
            f"El conjunto de evaluación «{path}» está dentro del repositorio del código: las"
            " grabaciones son privadas, pon [eval] path fuera de él."
        )
        raise typer.Exit(code=1)
    try:
        cases = read_eval_set(path, case)
    except EvalSetError as error:
        typer.echo(f"No se puede leer el conjunto de evaluación: {error}")
        raise typer.Exit(code=1) from error
    if not cases:
        typer.echo(
            f"No hay casos en «{path}»: cada caso es una carpeta con recording/ y reference/."
        )
        raise typer.Exit(code=1)
    _echo_estimate([estimate_case(c, settings) for c in cases])
    if not yes and not typer.confirm("¿Continuar con las llamadas reales a Claude?", default=False):
        typer.echo("Cancelado: no se ha llamado a Claude.")
        raise typer.Exit(code=1)
    # The key `setup` stored on this PC, unless the environment already carries one.
    export_api_key(settings.llm.api_key_path())
    transport = _eval_transport()
    directory = run_directory(path, datetime.now(UTC))
    typer.echo(f"Evaluando {len(cases)} casos en {directory}:")
    report = asyncio.run(
        run_eval(cases, directory, settings=settings, transport=transport, on_case=_echo_case)
    )
    markdown = write_report(report, directory)
    typer.echo(f"Coste real {report.actual_usd:.2f} USD (estimado {report.estimated_usd:.2f} USD).")
    typer.echo(f"Informe: {markdown}")
    if any(result.error is not None for result in report.cases):
        raise typer.Exit(code=1)
