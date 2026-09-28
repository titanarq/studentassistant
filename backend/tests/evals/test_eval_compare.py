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
from studentassistant.evals.scoring import (
    NotesFidelity,
    PageScore,
    RequestItem,
    SectionScore,
    score_requests,
)

RUN_TIMEOUT_S = 30


def _case(
    name: str,
    *,
    page: float = 0.9,
    agreement: float = 0.8,
    kept: float = 0.7,
    unique: float | None = 1.0,
) -> CaseResult:
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
            unique=unique,
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


def test_unique_is_compared_but_not_in_the_global_score() -> None:
    comparison = compare_reports(
        _report(_case("celula")),
        _report(_case("celula", unique=0.5)),
        margin=0.05,
        previous_run="a",
    )
    scores = _scores(comparison, "celula")
    assert scores["unique"] == (1.0, 0.5, -0.5, True)
    assert scores["score"] == (0.85, 0.85, 0.0, False)
    assert "| sin repetir | 100.0 % | 50.0 % | -50.0 pp | **regresión** |" in (
        render_comparison(comparison)
    )


def test_a_report_without_unique_compares_as_unknown() -> None:
    comparison = compare_reports(
        _report(_case("celula", unique=None)),
        _report(_case("celula")),
        margin=0.05,
        previous_run="a",
    )
    assert _scores(comparison, "celula")["unique"] == (None, 1.0, None, False)


def test_the_report_shows_unique_and_the_repeated_units() -> None:
    case = _case("celula", unique=0.5)
    assert case.notes is not None
    case.notes.repeated = ["Emisor: el que envía el mensaje."]
    text = render_report(_report(case))
    assert "| con fuente | sin repetir | global |" in text
    assert "| 100.0 % | 50.0 % | 85.0 % |" in text
    assert "sin repetir 50.0 %" in text
    assert (
        "- Ideas generadas que repiten otra anterior:\n  - Emisor: el que envía el mensaje." in text
    )


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


def test_request_scores_and_the_detector_are_compared(tmp_path: Path) -> None:
    edit = RequestItem(kind="edit", segments=["seg-2"], summary="edit", text="")
    question = RequestItem(kind="question", segments=["seg-4"], summary="question", text="")
    before = _case("celula").model_copy(update={"requests": score_requests([edit, question], [])})
    after = _case("celula").model_copy(
        update={"requests": score_requests([edit, question], [edit])}
    )
    previous = _report(before).model_copy(update={"request_detection": "wake_word"})
    current = _report(after).model_copy(
        update={"request_detection": "observer", "notes_path": "chat"}
    )
    comparison = compare_reports(previous, current, margin=0.05, previous_run="a")
    scores = _scores(comparison, "celula")
    assert scores["request_recall"] == (0.0, 0.5, 0.5, False)
    assert scores["request_f1"][2] == pytest.approx(0.6667, abs=1e-3)
    assert (comparison.previous_request_detection, comparison.request_detection) == (
        "wake_word",
        "observer",
    )
    text = render_comparison(comparison)
    assert "Detector de peticiones: `wake_word` antes, `observer` ahora" in text
    assert "Apuntes: `—` antes, `chat` ahora" in text
    assert "| peticiones (F1) | 0.0 % | 66.7 % |" in text


def test_an_older_report_without_the_request_fields_still_compares(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    old = runs / "20260901-000000"
    write_report(_report(_case("celula")), old)
    text = (old / "report.json").read_text(encoding="utf-8")
    for field in ('  "request_detection": null,\n', '  "notes_path": null,\n'):
        assert field in text
        text = text.replace(field, "")
    assert '"requests": null' in text
    (old / "report.json").write_text(text.replace('      "requests": null,\n', ""), "utf-8")
    found = previous_report(runs / "20260902-000000", warn=pytest.fail)
    assert found is not None
    _, earlier = found
    assert earlier.request_detection is None and earlier.cases[0].requests is None
    current = _report(_case("celula")).model_copy(update={"request_detection": "observer"})
    comparison = compare_reports(earlier, current, margin=0.05, previous_run="old")
    assert _scores(comparison, "celula")["request_f1"] == (None, None, None, False)
    assert "Detector de peticiones: `—` antes, `observer` ahora" in render_comparison(comparison)
