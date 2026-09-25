"""`run_eval`: the sample case replayed through the pipeline (Claude scripted), scored, reported."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eval_fixtures import CAPTURE_ID, PipelineClaude, make_case
from studentassistant.config import ObserverSettings, Settings
from studentassistant.evals import read_case, run_eval, write_report
from studentassistant.evals.run import REPORT_JSON

RUN_TIMEOUT_S = 60


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Git (run by `Vault.init`) must not read or write the real home directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def test_the_sample_case_is_replayed_scored_and_reported(home: Path) -> None:
    case = read_case(make_case(home / "evals"))
    runs = home / "evals" / "runs" / "now"
    claude = PipelineClaude(runs)
    settings = Settings(observer=ObserverSettings())
    moments = iter([datetime(2026, 9, 25, 10, tzinfo=UTC), datetime(2026, 9, 25, 11, tzinfo=UTC)])

    report = asyncio.run(
        asyncio.wait_for(
            run_eval(
                [case],
                runs,
                settings=settings,
                transport=claude,
                sleep=_no_wait,
                clock=lambda: next(moments),
            ),
            RUN_TIMEOUT_S,
        )
    )

    [result] = report.cases
    assert result.error is None and result.generate_error is None, result
    assert claude.observer.requests and len(claude.transcriber.requests) == 1
    assert len(claude.editor.requests) == 1

    # The page: one wrong word of six, the unreadable mark counting as its word.
    [page] = result.pages
    assert page.capture_id == CAPTURE_ID and page.transcribed
    assert page.word_accuracy == pytest.approx(5 / 6, abs=1e-3)
    assert 0.9 < page.char_accuracy < 1.0

    # The observer put the reference's two sections in one: only the pair seg-2/seg-3 agrees.
    assert result.sections is not None
    assert result.sections.coverage == 1.0
    assert result.sections.pairwise_agreement == pytest.approx(1 / 3, abs=1e-3)
    assert (result.sections.reference_sections, result.sections.observer_sections) == (2, 1)

    # The notes: the genetic material was dropped and the mitochondria come from nowhere.
    notes = result.notes
    assert notes is not None and not result.notes_draft
    assert notes.dropped == ["El núcleo guarda el material genético."]
    assert notes.kept == pytest.approx(2 / 3, abs=1e-3)
    assert notes.unsupported == ["Las mitocondrias producen energía mediante respiración celular."]
    assert notes.supported == pytest.approx(2 / 3, abs=1e-3)

    # Everything happened in the run's own vault.
    assert result.vault == str(runs / "celula" / "vault")
    assert (runs / "celula" / "vault" / "vault.yaml").is_file()
    assert result.actual_usd is not None

    markdown = write_report(report, runs)
    text = markdown.read_text(encoding="utf-8")
    assert "| celula |" in text and "Las mitocondrias" in text
    saved = json.loads((runs / REPORT_JSON).read_text(encoding="utf-8"))
    assert saved["cases"][0]["notes"]["kept"] == notes.kept
    assert saved["cases"][0]["score"] == result.score
    assert saved["estimated_usd"] == report.estimated_usd
