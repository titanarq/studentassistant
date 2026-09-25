"""Compare two eval runs: per case and per score, the previous value, the new one and the delta.

`eval run` compares its report with the most recent earlier `runs/<time>/report.json` of the same
eval set (`previous_report`), and `eval compare` any two runs; both render the same Spanish
section (`render_comparison`). A score that drops by more than `[eval] regression_margin` is a
regression: reported, never fatal. Nothing here calls Claude.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError, computed_field

if TYPE_CHECKING:
    from studentassistant.evals.run import CaseResult, EvalReport

REPORT_JSON = "report.json"

# score key -> the Spanish name the report gives it, in report order.
SCORE_LABELS: dict[str, str] = {
    "page_char_accuracy": "páginas (caracteres)",
    "page_word_accuracy": "páginas (palabras)",
    "section_agreement": "secciones (acuerdo)",
    "section_coverage": "secciones (cobertura)",
    "kept": "conservado",
    "supported": "con fuente",
    "score": "global",
}


class ScoreDelta(BaseModel):
    """One score of one case in both runs; `None` where a run has no value for it."""

    score: str
    previous: float | None
    current: float | None
    delta: float | None
    regression: bool


class CaseComparison(BaseModel):
    case: str
    scores: list[ScoreDelta]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def regressions(self) -> list[str]:
        return [s.score for s in self.scores if s.regression]


class RunComparison(BaseModel):
    """The report of one run against an earlier one (`previous_run`, its directory name)."""

    previous_run: str
    margin: float
    cases: list[CaseComparison]
    # Cases only in the new run, and only in the previous one.
    added: list[str] = []
    removed: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def regressions(self) -> int:
        return sum(len(case.regressions) for case in self.cases)


def case_scores(result: CaseResult) -> dict[str, float | None]:
    """Every compared score of one case, keyed as `SCORE_LABELS`."""
    sections, notes = result.sections, result.notes
    return {
        "page_char_accuracy": result.page_char_accuracy,
        "page_word_accuracy": result.page_word_accuracy,
        "section_agreement": sections.pairwise_agreement if sections else None,
        "section_coverage": sections.coverage if sections else None,
        "kept": notes.kept if notes else None,
        "supported": notes.supported if notes else None,
        "score": result.score,
    }


def compare_reports(
    previous: EvalReport, current: EvalReport, *, margin: float, previous_run: str
) -> RunComparison:
    """Compare `current` with `previous`; a drop larger than `margin` is a regression."""
    before = {case.case: case for case in previous.cases}
    now = {case.case: case for case in current.cases}
    cases: list[CaseComparison] = []
    for name, result in now.items():
        if name not in before:
            continue
        old, new = case_scores(before[name]), case_scores(result)
        deltas = []
        for key in SCORE_LABELS:
            a, b = old[key], new[key]
            delta = None if a is None or b is None else round(b - a, 4)
            deltas.append(
                ScoreDelta(
                    score=key,
                    previous=a,
                    current=b,
                    delta=delta,
                    regression=delta is not None and -delta > margin,
                )
            )
        cases.append(CaseComparison(case=name, scores=deltas))
    return RunComparison(
        previous_run=previous_run,
        margin=margin,
        cases=cases,
        added=[name for name in now if name not in before],
        removed=[name for name in before if name not in now],
    )


class ReportUnreadableError(Exception):
    """A `report.json` that is missing or does not hold an `EvalReport`."""


def read_report(directory: Path) -> EvalReport:
    """The `EvalReport` a run wrote into `directory`.

    Raises:
        ReportUnreadableError: when there is no readable report there.
    """
    from studentassistant.evals.run import EvalReport

    path = directory / REPORT_JSON
    try:
        return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        raise ReportUnreadableError(f"{path}: {error}") from error


def previous_report(
    directory: Path, *, warn: Callable[[str], None]
) -> tuple[str, EvalReport] | None:
    """The most recent readable report of a run earlier than the one in `directory`.

    Runs are the sibling directories (`runs/<UTC time>/`, whose names sort chronologically) whose
    name sorts before `directory`'s. A report that cannot be read is named through `warn` and
    skipped. Returns `(run name, report)`, or `None` when there is no earlier readable run.
    """
    runs = directory.parent
    if not runs.is_dir():
        return None
    earlier = sorted(
        (p for p in runs.iterdir() if p.is_dir() and p.name < directory.name),
        key=lambda p: p.name,
        reverse=True,
    )
    for run in earlier:
        if not (run / REPORT_JSON).exists():
            continue
        try:
            return run.name, read_report(run)
        except ReportUnreadableError as error:
            warn(f"No se puede leer el informe anterior {error}; se ignora.")
    return None


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def _points(delta: float | None) -> str:
    return "—" if delta is None else f"{delta * 100:+.1f} pp"


def render_comparison(comparison: RunComparison | None, warnings: Sequence[str] = ()) -> str:
    """The Spanish Markdown section "Comparación con la ejecución anterior"."""
    lines = ["## Comparación con la ejecución anterior", ""]
    lines += [f"- Aviso: {warning}" for warning in warnings]
    if comparison is None:
        lines.append("No hay ninguna ejecución anterior con la que comparar.")
        return "\n".join(lines) + "\n"
    c = comparison
    lines.append(
        f"Frente a `{c.previous_run}`. Una bajada de más de {c.margin * 100:.1f} puntos es una"
        f" regresión; hay {c.regressions}."
    )
    if c.added:
        lines.append("- Casos nuevos (sin valor anterior): " + ", ".join(c.added))
    if c.removed:
        lines.append("- Casos que ya no están: " + ", ".join(c.removed))
    for case in c.cases:
        lines += [
            "",
            f"### {case.case}",
            "",
            "| puntuación | anterior | nueva | diferencia | |",
            "|---|---|---|---|---|",
        ]
        for s in case.scores:
            mark = "**regresión**" if s.regression else ""
            lines.append(
                f"| {SCORE_LABELS[s.score]} | {_pct(s.previous)} | {_pct(s.current)}"
                f" | {_points(s.delta)} | {mark} |"
            )
    return "\n".join(lines) + "\n"
