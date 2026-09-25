"""Comparing eval runs: synthetic reports, `run_eval`'s previous-run lookup and `eval compare`."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.config import EvalSettings, Settings
from studentassistant.evals import CaseEstimate, CaseResult, EvalReport, render_report, run_eval
from studentassistant.evals.compare import (
    compare_reports,
    previous_report,
    render_comparison,
)
from studentassistant.evals.run import write_report
from studentassistant.evals.scoring import NotesFidelity, PageScore, SectionScore

RUN_TIMEOUT_S = 30


def _case(name: str, *, page: float = 0.9, agreement: float = 0.8, kept: float = 0.7) -> CaseResult:
    return CaseResult(
        case=name,
        estimate=CaseEstimate(case=name, roles=[]),
        actual_usd=0.1,
        pages=[
            PageScore(capture_id="p1", transcribed=True, char_accuracy=page, word_accuracy=page)
        ],
        sections=SectionScore(
            segments=3,
            assigned=3,
            coverage=1.0,
            pairwise_agreement=agreement,
            reference_sections=2,
            observer_sections=2,
        ),
        notes=NotesFidelity(
            reference_units=3,
            generated_units=3,
            kept=kept,
            supported=1.0,
            dropped=[],
            unsupported=[],
        ),
    )


def _report(*cases: CaseResult) -> EvalReport:
    moment = datetime(2026, 9, 25, 10, tzinfo=UTC)
    return EvalReport(
        started_at=moment, finished_at=moment, models={"editor": "m"}, cases=list(cases)
    )


def _scores(comparison, case: str) -> dict[str, tuple]:  # type: ignore[no-untyped-def]
    [found] = [c for c in comparison.cases if c.case == case]
    return {s.score: (s.previous, s.current, s.delta, s.regression) for s in found.scores}


def test_an_improvement_is_no_regression() -> None:
    comparison = compare_reports(
        _report(_case("celula")),
        _report(_case("celula", page=0.95, kept=0.9)),
        margin=0.05,
        previous_run="a",
    )
    scores = _scores(comparison, "celula")
    assert scores["page_char_accuracy"] == (0.9, 0.95, 0.05, False)
    assert scores["kept"] == (0.7, 0.9, 0.2, False)
    assert scores["section_coverage"] == (1.0, 1.0, 0.0, False)
    assert comparison.regressions == 0 and not comparison.added and not comparison.removed
    text = render_comparison(comparison)
    assert "## Comparación con la ejecución anterior" in text
    assert "| conservado | 70.0 % | 90.0 % | +20.0 pp |  |" in text
    assert "regresión**" not in text


def test_a_drop_beyond_the_margin_is_a_regression() -> None:
    comparison = compare_reports(
        _report(_case("celula")),
        _report(_case("celula", agreement=0.76, kept=0.5)),
        margin=0.05,
        previous_run="20260101-000000",
    )
    scores = _scores(comparison, "celula")
    assert scores["section_agreement"][3] is False  # -4 points: within the margin
    assert scores["kept"] == (0.7, 0.5, -0.2, True)
    assert scores["score"] == (0.85, 0.79, -0.06, True)  # the mean of the four drops
    assert "kept" in comparison.cases[0].regressions
    text = render_comparison(comparison)
    assert "| conservado | 70.0 % | 50.0 % | -20.0 pp | **regresión** |" in text
    assert "`20260101-000000`" in text


def test_cases_added_and_removed_are_listed() -> None:
    comparison = compare_reports(
        _report(_case("celula"), _case("mitosis")),
        _report(_case("celula"), _case("adn")),
        margin=0.05,
        previous_run="a",
    )
    assert [c.case for c in comparison.cases] == ["celula"]
    assert comparison.added == ["adn"] and comparison.removed == ["mitosis"]
    text = render_comparison(comparison)
    assert "Casos nuevos (sin valor anterior): adn" in text
    assert "Casos que ya no están: mitosis" in text


def test_a_case_that_did_not_run_compares_as_unknown() -> None:
    broken = CaseResult(case="celula", estimate=CaseEstimate(case="celula", roles=[]), error="x")
    comparison = compare_reports(
        _report(_case("celula")), _report(broken), margin=0.05, previous_run="a"
    )
    assert all(s.delta is None and not s.regression for s in comparison.cases[0].scores)


def test_no_previous_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    assert previous_report(runs / "20260925-100000", warn=pytest.fail) is None
    runs.mkdir()
    (runs / "20260925-100000").mkdir()
    assert previous_report(runs / "20260925-100000", warn=pytest.fail) is None
    report = _report(_case("celula"))
    assert "No hay ninguna ejecución anterior" in render_report(report)


def test_the_most_recent_readable_earlier_run_is_used(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    write_report(_report(_case("celula", kept=0.1)), runs / "20260901-000000")
    write_report(_report(_case("celula", kept=0.2)), runs / "20260910-000000")
    write_report(_report(_case("celula", kept=0.9)), runs / "20260930-000000")  # later
    broken = runs / "20260920-000000"
    broken.mkdir()
    (broken / "report.json").write_text("{not json", encoding="utf-8")
    (runs / "20260921-000000").mkdir()  # a run that wrote nothing is not a report
    warnings: list[str] = []
    found = previous_report(runs / "20260925-000000", warn=warnings.append)
    assert found is not None
    name, report = found
    assert name == "20260910-000000" and report.cases[0].notes is not None
    assert report.cases[0].notes.kept == 0.2
    [warning] = warnings
    assert "20260920-000000" in warning and "se ignora" in warning


def test_an_older_report_without_a_comparison_still_loads(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    old = runs / "20260901-000000"
    write_report(_report(_case("celula")), old)
    text = (old / "report.json").read_text(encoding="utf-8")
    assert '"comparison": null' in text
    (old / "report.json").write_text(
        text.replace('  "comparison": null,\n', "").replace('  "comparison_warnings": [],\n', ""),
        encoding="utf-8",
    )
    assert previous_report(runs / "20260902-000000", warn=pytest.fail) is not None


def test_run_eval_compares_with_the_previous_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    write_report(_report(_case("celula")), runs / "20260901-000000")
    broken = runs / "20260902-000000"
    broken.mkdir()
    (broken / "report.json").write_text("[]", encoding="utf-8")
    seen: list[str] = []
    settings = Settings(eval=EvalSettings(path=tmp_path, regression_margin=0.1))
    report = asyncio.run(
        asyncio.wait_for(
            run_eval(
                [],
                runs / "20260925-000000",
                settings=settings,
                transport=None,  # type: ignore[arg-type]
                on_warning=seen.append,
            ),
            RUN_TIMEOUT_S,
        )
    )
    assert report.comparison is not None
    assert report.comparison.previous_run == "20260901-000000"
    assert report.comparison.margin == 0.1 and report.comparison.removed == ["celula"]
    assert len(seen) == 1 and report.comparison_warnings == seen
    markdown = write_report(report, runs / "20260925-000000").read_text(encoding="utf-8")
    assert "Frente a `20260901-000000`" in markdown and "Aviso:" in markdown


@pytest.fixture
def evals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    path = tmp_path / "evals"
    monkeypatch.setenv("SA_EVAL__PATH", str(path))
    return path


def test_eval_compare_prints_the_comparison(evals: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = evals / "runs"
    write_report(_report(_case("celula")), runs / "a")
    write_report(_report(_case("celula", kept=0.6)), runs / "b")
    result = CliRunner().invoke(cli, ["eval", "compare", "a", str(runs / "b")])
    assert result.exit_code == 0, result.output
    assert "Comparación con la ejecución anterior" in result.output
    assert "| conservado | 70.0 % | 60.0 % | -10.0 pp | **regresión** |" in result.output
    monkeypatch.setenv("SA_EVAL__REGRESSION_MARGIN", "0.2")
    result = CliRunner().invoke(cli, ["eval", "compare", "a", "b"])
    assert result.exit_code == 0 and "**regresión**" not in result.output


def test_eval_compare_names_an_unreadable_run(evals: Path) -> None:
    write_report(_report(_case("celula")), evals / "runs" / "a")
    result = CliRunner().invoke(cli, ["eval", "compare", "a", "missing"])
    assert result.exit_code == 1
    assert "No se puede leer el informe" in result.output and "missing" in result.output
