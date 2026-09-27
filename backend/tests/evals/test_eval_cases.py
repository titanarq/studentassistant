"""The eval set on disk: cases read and checked against their recordings; the cost estimate."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from eval_fixtures import CAPTURE_ID, REFERENCE_NOTES, make_case, write_requests
from studentassistant.config import (
    EditorRoleSettings,
    EvalSettings,
    LlmRolesSettings,
    LlmSettings,
    ObserverSettings,
    Settings,
)
from studentassistant.evals import (
    EvalSetError,
    estimate_case,
    read_case,
    read_eval_set,
    run_settings,
)


def test_a_case_holds_its_recording_and_every_reference(tmp_path: Path) -> None:
    case = read_case(make_case(tmp_path))
    assert case.name == "celula"
    assert case.reference_notes == REFERENCE_NOTES
    assert list(case.reference_pages) == [CAPTURE_ID]
    assert case.reference_sections is not None
    assert [s.title for s in case.reference_sections] == ["Definición", "Partes"]
    assert [m.segment_id for m in case.finals] == ["seg-1", "seg-2", "seg-3"]


def test_pages_and_sections_are_optional(tmp_path: Path) -> None:
    directory = make_case(tmp_path)
    shutil.rmtree(directory / "reference" / "pages")
    (directory / "reference" / "sections.yaml").unlink()
    case = read_case(directory)
    assert case.reference_pages == {} and case.reference_sections is None
    assert case.reference_requests is None


def test_the_reference_requests_are_read(tmp_path: Path) -> None:
    directory = make_case(tmp_path)
    write_requests(directory)
    case = read_case(directory)
    assert case.reference_requests is not None
    [request] = case.reference_requests
    assert (request.kind, request.segments) == ("prepare_notes", ("seg-3",))
    assert request.note == "prepárame el tema"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("requests:\n  - kind: dance\n    segments: [seg-1]\n", "not a requests file"),
        ("requests:\n  - kind: edit\n    segments: []\n", "not a requests file"),
        ("requests:\n  - kind: edit\n    segments: [seg-9]\n", "segment seg-9"),
        ("requests:\n  - kind: edit\n    segment: [seg-1]\n", "not a requests file"),
        ("requests: [\n", "not a requests file"),
    ],
)
def test_a_wrong_requests_file_is_refused(tmp_path: Path, text: str, error: str) -> None:
    directory = make_case(tmp_path)
    (directory / "reference" / "requests.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(EvalSetError, match=error):
        read_case(directory)


def test_the_eval_set_lists_cases_in_order_and_skips_runs(tmp_path: Path) -> None:
    make_case(tmp_path, "b")
    make_case(tmp_path, "a")
    (tmp_path / "runs" / "20260925-100000").mkdir(parents=True)
    assert [c.name for c in read_eval_set(tmp_path)] == ["a", "b"]
    assert [c.name for c in read_eval_set(tmp_path, ["b"])] == ["b"]
    with pytest.raises(EvalSetError, match="no case named c"):
        read_eval_set(tmp_path, ["c"])
    with pytest.raises(EvalSetError, match="not a directory"):
        read_eval_set(tmp_path / "absent")


@pytest.mark.parametrize(
    ("path", "content", "message"),
    [
        ("notes.md", "  \n", "missing or empty"),
        ("pages/otra-captura.md", "texto", "names no capture"),
        ("sections.yaml", "sections: [{title: A, segments: [seg-9]}]", "not a final"),
        (
            "sections.yaml",
            "sections: [{title: A, segments: [seg-1]}, {title: B, segments: [seg-1]}]",
            "two sections",
        ),
        ("sections.yaml", "secciones: []", "not a sections file"),
    ],
)
def test_a_reference_that_does_not_match_the_recording_is_refused(
    tmp_path: Path, path: str, content: str, message: str
) -> None:
    directory = make_case(tmp_path)
    (directory / "reference" / path).write_text(content, encoding="utf-8")
    with pytest.raises(EvalSetError, match=message):
        read_case(directory)


def test_an_unreadable_recording_is_refused(tmp_path: Path) -> None:
    directory = make_case(tmp_path)
    (directory / "recording" / "manifest.yaml").unlink()
    with pytest.raises(EvalSetError, match="celula"):
        read_case(directory)


def test_the_estimate_prices_every_role_from_the_configured_prices(tmp_path: Path) -> None:
    case = read_case(make_case(tmp_path))
    estimate = estimate_case(case, Settings())
    roles = {role.role: role for role in estimate.roles}
    assert set(roles) == {"observer", "observer-requests", "transcriber", "editor"}
    assert roles["transcriber"].calls == 1 and roles["editor"].calls == 2
    assert roles["observer"].calls == 2  # three finals in one batch, plus the flush
    assert roles["observer-requests"].calls == 3  # one per final, at most
    assert all(role.usd is not None and role.usd > 0 for role in roles.values())
    assert estimate.usd == pytest.approx(sum(role.usd or 0 for role in roles.values()))
    assert estimate.unpriced == []


def test_a_model_without_a_price_is_reported_and_left_out(tmp_path: Path) -> None:
    case = read_case(make_case(tmp_path))
    roles = LlmRolesSettings(editor=EditorRoleSettings(model="claude-sin-precio"))
    estimate = estimate_case(case, Settings(llm=LlmSettings(roles=roles)))
    [editor] = [role for role in estimate.roles if role.role == "editor"]
    assert editor.usd is None and estimate.unpriced == ["claude-sin-precio"]
    assert estimate.usd == pytest.approx(sum(r.usd or 0 for r in estimate.roles if r is not editor))


def test_the_wake_word_under_test_makes_no_request_detection_call(tmp_path: Path) -> None:
    case = read_case(make_case(tmp_path))
    settings = Settings(eval=EvalSettings(request_detection="wake_word"))
    assert run_settings(settings).observer.request_detection == "wake_word"
    roles = {role.role for role in estimate_case(case, settings).roles}
    assert "observer-requests" not in roles and "observer" in roles


def test_the_eval_override_leaves_the_app_config_alone_when_unset(tmp_path: Path) -> None:
    settings = Settings(observer=ObserverSettings(request_detection="wake_word"))
    assert run_settings(settings).observer.request_detection == "wake_word"
    overridden = Settings(
        observer=ObserverSettings(request_detection="wake_word"),
        eval=EvalSettings(request_detection="observer"),
    )
    assert run_settings(overridden).observer.request_detection == "observer"
    assert overridden.observer.request_detection == "wake_word"  # a copy, not changed


def test_the_chat_path_estimates_one_turn_per_editor_request(tmp_path: Path) -> None:
    directory = make_case(tmp_path)
    settings = Settings(eval=EvalSettings(notes_path="chat"))
    # Without requests.yaml: one turn per batch of pages (one page here).
    [editor] = [
        r for r in estimate_case(read_case(directory), settings).roles if r.role == "editor"
    ]
    assert editor.calls == 1
    write_requests(directory, extra="  - kind: edit\n    segments: [seg-1]\n")
    [editor] = [
        r for r in estimate_case(read_case(directory), settings).roles if r.role == "editor"
    ]
    assert editor.calls == 2 and editor.usd is not None and editor.usd > 0
