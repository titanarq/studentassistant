"""`run_eval`: the sample case replayed through the pipeline (Claude scripted), scored, reported."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

from eval_fixtures import CAPTURE_ID, PipelineClaude, make_case, write_requests
from studentassistant.config import EditorSettings, EvalSettings, ObserverSettings, Settings
from studentassistant.evals import read_case, run_eval, write_report
from studentassistant.evals.compare import case_scores, read_report
from studentassistant.evals.run import REPORT_JSON
from studentassistant.protocol import CaptureImage, CaptureUploadRequest
from studentassistant.server.recording import RecordingWriter, read_manifest

RUN_TIMEOUT_S = 60


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Git (run by `Vault.init`) must not read or write the real home directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


@pytest.mark.skip(reason="waits for the content routes to be scoped to the user (#551)")
def test_the_sample_case_is_replayed_scored_and_reported(home: Path) -> None:
    directory = make_case(home / "evals")
    write_requests(directory)
    case = read_case(directory)
    runs = home / "evals" / "runs" / "now"
    claude = PipelineClaude(runs)
    # The whole-topic call the scripted editor answers (batched mode is tested in the editor).
    settings = Settings(observer=ObserverSettings(), editor=EditorSettings(prepare_mode="single"))
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
    # The notes, then the search for contradictions between the sources (none here).
    assert len(claude.editor.requests) == 2

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

    # The detector heard no request: the reference's "prepárame el tema" was missed.
    assert claude.requests.requests
    requests = result.requests
    assert requests is not None
    assert (requests.reference, requests.detected, requests.matched) == (1, 0, 0)
    assert (requests.precision, requests.recall, requests.f1) == (1.0, 0.0, 0.0)
    [missed] = requests.missed
    assert missed.kind == "prepare_notes" and missed.segments == ["seg-3"]
    assert missed.text == "En el libro pone que el núcleo guarda el material genético."
    assert report.request_detection == "observer" and report.notes_path == "generate"

    # Everything happened in the run's own vault.
    assert result.vault == str(runs / "celula" / "vault")
    assert (runs / "celula" / "vault" / "vault.yaml").is_file()
    assert result.actual_usd is not None

    markdown = write_report(report, runs)
    text = markdown.read_text(encoding="utf-8")
    assert "| celula |" in text and "Las mitocondrias" in text
    assert "Peticiones no detectadas" in text and "prepárame el tema" in text
    saved = json.loads((runs / REPORT_JSON).read_text(encoding="utf-8"))
    assert saved["cases"][0]["notes"]["kept"] == notes.kept
    assert saved["cases"][0]["score"] == result.score
    assert saved["estimated_usd"] == report.estimated_usd


def _run(runs: Path, case_dir: Path, claude: PipelineClaude, settings: Settings):  # noqa: ANN202
    moment = datetime(2026, 9, 25, 10, tzinfo=UTC)
    return asyncio.run(
        asyncio.wait_for(
            run_eval(
                [read_case(case_dir)],
                runs,
                settings=settings,
                transport=claude,
                sleep=_no_wait,
                clock=lambda: moment,
            ),
            RUN_TIMEOUT_S,
        )
    )


def _chat_settings(**eval_options: object) -> Settings:
    return Settings(
        observer=ObserverSettings(),
        editor=EditorSettings(prepare_mode="single"),
        eval=EvalSettings(notes_path="chat", **eval_options),  # type: ignore[arg-type]
    )


def test_the_chat_path_scores_the_notes_the_spoken_requests_built(home: Path) -> None:
    directory = make_case(home / "evals")
    write_requests(directory)
    runs = home / "evals" / "runs" / "now"
    # The student says "prepárame el tema" in seg-3: the turn, not the eval, writes the notes.
    claude = PipelineClaude(runs, spoken=(("prepare_notes", "seg-3"),))

    report = _run(runs, directory, claude, _chat_settings())

    [result] = report.cases
    assert result.error is None and result.generate_error is None, result
    assert report.notes_path == "chat"
    assert len(claude.editor.requests) == 2  # the turn's generation and its contradictions
    requests = result.requests
    assert requests is not None and requests.missed == [] and requests.spurious == []
    assert (requests.precision, requests.recall, requests.f1) == (1.0, 1.0, 1.0)
    [kind] = requests.per_kind
    assert (kind.kind, kind.matched, kind.f1) == ("prepare_notes", 1, 1.0)
    assert result.notes is not None
    assert result.notes.kept == pytest.approx(2 / 3, abs=1e-3)
    assert result.score is not None


def test_the_chat_path_never_asks_for_the_generation(home: Path) -> None:
    directory = make_case(home / "evals")
    runs = home / "evals" / "runs" / "now"
    claude = PipelineClaude(runs)  # nothing spoken to the assistant

    report = _run(runs, directory, claude, _chat_settings())

    [result] = report.cases
    assert result.error is None and result.generate_error is None, result
    assert claude.editor.requests == []  # no "prepárame el tema" over REST
    assert result.notes is None and result.requests is None
    assert result.score is not None
    markdown = write_report(report, runs).read_text(encoding="utf-8")
    assert "ninguna petición los ha escrito" in markdown
    assert "los que construyeron las peticiones" in markdown


def test_the_request_detector_under_test_reaches_the_app(home: Path) -> None:
    directory = make_case(home / "evals")
    runs = home / "evals" / "runs" / "now"
    claude = PipelineClaude(runs)

    report = _run(runs, directory, claude, _chat_settings(request_detection="wake_word"))

    # The wake word makes no Claude call: the Sonnet detector never ran.
    assert claude.requests.requests == []
    assert claude.observer.requests
    assert report.request_detection == "wake_word"
    saved = json.loads(write_report(report, runs).with_name(REPORT_JSON).read_text("utf-8"))
    assert saved["request_detection"] == "wake_word" and saved["notes_path"] == "chat"


BLANK_ID = "0b1a2c3d-0000-4000-8000-000000000001"
AGAIN_ID = "0b1a2c3d-0000-4000-8000-000000000002"


def _add_capture(case_dir: Path, capture_id: str, image: bytes, client_time_ms: int) -> None:
    recording = case_dir / "recording"
    writer = RecordingWriter(recording, read_manifest(recording))
    metadata = CaptureUploadRequest(
        capture_id=capture_id,
        trigger="button",
        client_time_ms=client_time_ms,
        images=[
            CaptureImage(
                part="image_0",
                content_type="image/jpeg",
                width_px=240,
                height_px=320,
                client_time_ms=client_time_ms,
            )
        ],
    )
    writer.add_capture(metadata, {"image_0": image})


def test_the_captures_triage_is_scored_against_the_reference_triage(home: Path) -> None:
    directory = make_case(home / "evals")
    sample = next((directory / "recording" / "captures").iterdir()).read_bytes()
    ok, blank = cv2.imencode(".jpg", np.full((320, 240, 3), 250, np.uint8))
    assert ok
    # A blank sheet, then the sample page again, both before the recording ends.
    _add_capture(directory, BLANK_ID, blank.tobytes(), 1760000010000)
    _add_capture(directory, AGAIN_ID, sample, 1760000010500)
    # The student says the repeated page is the same content, not a duplicate: right call,
    # wrong reason.
    (directory / "reference" / "triage.yaml").write_text(
        "captures:\n"
        f"  - capture_id: {CAPTURE_ID}\n    status: kept\n"
        f"  - capture_id: {BLANK_ID}\n    status: set_aside\n    reasons: [blank]\n"
        f"  - capture_id: {AGAIN_ID}\n    status: set_aside\n    reasons: [same_content]\n",
        encoding="utf-8",
    )
    runs = home / "evals" / "runs" / "now"
    claude = PipelineClaude(runs)
    settings = Settings(observer=ObserverSettings(), editor=EditorSettings(prepare_mode="single"))

    report = _run(runs, directory, claude, settings)

    [result] = report.cases
    assert result.error is None, result
    assert len(claude.transcriber.requests) == 1  # the pages set aside are never transcribed
    triage = result.triage
    assert triage is not None
    assert (triage.captures, triage.reference_set_aside, triage.set_aside) == (3, 2, 2)
    assert (triage.precision, triage.recall, triage.f1) == (1.0, 1.0, 1.0)
    by_reason = {r.reason: r for r in triage.per_reason}
    assert by_reason["blank"].accuracy == 1.0
    assert by_reason["duplicate"].accuracy == pytest.approx(2 / 3, abs=1e-3)
    assert by_reason["same_content"].accuracy == pytest.approx(2 / 3, abs=1e-3)
    [(wanted, got)] = triage.mismatches
    assert wanted.capture_id == AGAIN_ID and got.reasons == ["duplicate"]

    markdown = write_report(report, runs).read_text(encoding="utf-8")
    assert "Triaje de capturas" in markdown and "mismo contenido" in markdown
    assert "| triaje (F1) |" in markdown
    saved = json.loads((runs / REPORT_JSON).read_text(encoding="utf-8"))
    assert saved["cases"][0]["triage"]["f1"] == 1.0
    scores = case_scores(read_report(runs).cases[0])
    assert scores["triage_f1"] == 1.0
    assert scores["triage_reasons"] == triage.reason_accuracy
