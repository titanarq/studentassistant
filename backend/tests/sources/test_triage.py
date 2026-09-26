"""Capture triage: the checks on synthetic pages, the stored decision, swaps, content duplicates,
the student's decisions and the reader (`studentassistant.sources.triage`)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import pytest
import yaml

from studentassistant.config import SourcesSettings
from studentassistant.sources import (
    BurstStill,
    TriageRecord,
    check_same_content,
    process_burst,
    set_capture_triage,
    store_capture,
    triage_capture,
    triage_status,
)
from studentassistant.sources.triage import (
    CAPTURE_TRIAGED_KIND,
    TriageResult,
    ambiguous_checks,
    capture_metrics,
    decide,
    hamming,
    normalise_text,
    page_border_sides,
    text_similarity,
    with_verdict,
)
from studentassistant.vault import (
    GitSync,
    Vault,
    create_subject,
    create_topic,
    put_page_transcription,
    put_source,
    sources_directory,
    write_notes,
)
from triage_images import cut_by_frame, encode, framed, paper, written

SETTINGS = SourcesSettings()
NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Lengua").slug
    return subject, create_topic(tmp_vault, subject, "La comunicación").slug


def _triage(image, others=(), settings: SourcesSettings = SETTINGS) -> TriageResult:
    return triage_capture(process_burst([encode(image)], settings), others, settings, now=NOW)


def _record(image, source_id: str = "sources/notes/page-001.jpg", **kw) -> TriageRecord:
    metrics = capture_metrics(process_burst([encode(image)], SETTINGS))
    return TriageRecord(
        source_id=source_id,
        status=kw.get("status", "kept"),
        dhash=metrics["dhash"],
        dhash256=metrics["dhash256"],
        sharpness=metrics["sharpness"],
        protected=kw.get("protected", False),
    )


def _store(vault: Vault, topic: tuple[str, str], image, capture_id: str, kind: str = "notes"):
    return store_capture(
        vault,
        *topic,
        kind,
        [BurstStill(encode(image), "image/jpeg")],
        {"capture_id": capture_id, "session": "20260926-174302", "source_context": kind},
        30_000,
        SETTINGS,
    )


def _sidecar(vault: Vault, topic: tuple[str, str], number: int, kind: str = "notes") -> dict:
    path = sources_directory(vault, *topic, kind) / f"page-{number:03d}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# -- the checks ------------------------------------------------------------------------------------


def test_a_written_page_is_kept_with_its_metrics() -> None:
    result = _triage(framed(written(3)))
    assert result.status == "kept"
    assert result.reasons == []
    assert result.decided_by == "auto"
    assert result.decided_at == NOW
    metrics = result.metrics
    assert set(metrics) >= {"ink_ratio", "border_ink", "sharpness", "dhash", "page_detected"}
    assert len(metrics["dhash"]) == 16
    assert metrics["ink_ratio"] > SETTINGS.triage_blank_max_ink
    assert metrics["sharpness"] > SETTINGS.triage_min_sharpness


def test_a_blank_sheet_with_texture_and_shadow_is_blank_and_only_blank() -> None:
    result = _triage(paper(shadow=True))
    assert result.status == "set_aside"
    assert result.reasons == ["blank"]  # an empty sheet is never sharp: not also "blurry"
    assert _triage(paper(shadow=False)).reasons == ["blank"]


def test_a_sheet_with_a_few_handwritten_lines_is_not_blank() -> None:
    for lines in (1, 2, 3):
        assert "blank" not in _triage(framed(written(5, lines=lines))).reasons


def test_the_same_page_shifted_rescaled_or_re_exposed_is_a_duplicate() -> None:
    kept = _record(framed(written(3)))
    for variant in (
        framed(written(3), shift=(25, -15)),
        framed(written(3), scale=0.93),
        framed(written(3), exposure=0.8),
    ):
        result = _triage(variant, [kept])
        assert result.status == "set_aside"
        assert result.reasons == ["duplicate"]
        assert result.duplicate_of == kept.source_id
        assert result.metrics["duplicate_distance"] <= SETTINGS.triage_duplicate_max_distance


def test_different_pages_are_not_duplicates() -> None:
    kept = [_record(framed(written(seed)), f"sources/notes/page-{seed:03d}.jpg") for seed in (3, 4)]
    for seed in range(5, 11):
        result = _triage(framed(written(seed)), kept)
        assert "duplicate" not in result.reasons, seed
        assert result.metrics["duplicate_distance_256"] > 4 * SETTINGS.triage_duplicate_max_distance


def test_only_kept_captures_with_a_hash_are_compared() -> None:
    image = framed(written(3))
    aside = _record(image, status="set_aside")
    legacy = TriageRecord("sources/notes/page-002.jpg", "kept", None, None)
    result = _triage(image, [aside, legacy])
    assert result.status == "kept"
    assert "duplicate_distance" not in result.metrics


def test_a_blurred_capture_is_blurry() -> None:
    result = _triage(framed(written(3), blur=15))
    assert result.status == "set_aside"
    assert result.reasons == ["blurry"]
    assert result.metrics["sharpness"] < SETTINGS.triage_min_sharpness
    assert _triage(framed(written(3), blur=3)).status == "kept"


def test_a_page_cut_by_the_frame_is_flagged_or_set_aside_by_setting() -> None:
    result = _triage(cut_by_frame())
    assert result.status == "flagged"
    assert result.reasons == ["partial"]
    assert result.metrics["border_ink"] > SETTINGS.triage_partial_max_border_ink
    strict = SourcesSettings(triage_partial_sets_aside=True)
    assert _triage(cut_by_frame(), settings=strict).status == "set_aside"


def test_a_detected_page_on_two_sides_of_the_image_is_partial() -> None:
    assert page_border_sides([(0, 0), (500, 40), (480, 400), (30, 380)], 800, 600) == 2
    assert page_border_sides([(40, 30), (500, 40), (480, 400), (30, 380)], 800, 600) == 0
    assert page_border_sides(None, 800, 600) == 0
    metrics = {"ink_ratio": 0.05, "border_ink": 0.0, "sharpness": 300.0, "page_border_sides": 2}
    assert decide(metrics, [], SETTINGS).reasons == ["partial"]


def test_a_sharper_duplicate_replaces_the_older_capture_unless_it_is_protected() -> None:
    older = _record(framed(written(3), blur=5))
    result = _triage(framed(written(3)), [older])
    assert result.status == "kept"
    assert result.replaces == older.source_id
    assert "replaces" not in result.sidecar()

    protected = _record(framed(written(3), blur=5), protected=True)
    result = _triage(framed(written(3)), [protected])
    assert result.status == "set_aside"
    assert result.reasons == ["duplicate"]
    assert result.replaces is None


def test_a_duplicate_not_sharper_enough_is_set_aside() -> None:
    older = _record(framed(written(3)))
    result = _triage(framed(written(3), shift=(4, 2)), [older])
    assert result.reasons == ["duplicate"]
    assert result.replaces is None


# -- the optional Sonnet stage's helpers -----------------------------------------------------------


def test_ambiguous_checks_are_those_near_a_threshold_and_a_verdict_replaces_them() -> None:
    near = {"ink_ratio": 0.05, "border_ink": 0.0, "sharpness": 24.0, "dhash": "0" * 16}
    result = decide(near, [], SETTINGS)
    assert result.reasons == ["blurry"]
    assert ambiguous_checks(result, SETTINGS) == ["blurry"]
    refined = with_verdict(result, {"blurry": False}, SETTINGS, ["blurry"])
    assert refined.status == "kept"
    assert refined.reasons == []
    assert refined.metrics["llm_verdict"] == {"blurry": False}
    far = decide({**near, "sharpness": 300.0}, [], SETTINGS)
    assert ambiguous_checks(far, SETTINGS) == []


# -- stored with the capture -----------------------------------------------------------------------


def test_store_capture_writes_the_triage_block_in_the_sidecar(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    stored = _store(tmp_vault, topic, framed(written(3)), "cap-1")
    assert stored.triage is not None and stored.triage.status == "kept"
    block = _sidecar(tmp_vault, topic, 1)["triage"]
    assert set(block) == {
        "status",
        "reasons",
        "duplicate_of",
        "metrics",
        "decided_by",
        "decided_at",
    }
    assert block["status"] == "kept"
    assert block["decided_by"] == "auto"
    assert block["metrics"]["dhash"] == stored.triage.metrics["dhash"]


def test_a_repeat_of_a_stored_capture_is_set_aside_but_kept_in_the_vault(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    _store(tmp_vault, topic, framed(written(3)), "cap-1")
    again = _store(tmp_vault, topic, framed(written(3), shift=(6, 3)), "cap-2")
    assert again.triage.status == "set_aside"
    assert again.triage.duplicate_of == "sources/notes/page-001.jpg"
    assert again.path.is_file()  # never deleted
    assert _sidecar(tmp_vault, topic, 2)["triage"]["reasons"] == ["duplicate"]
    # Another kind is compared only with its own captures.
    book = _store(tmp_vault, topic, framed(written(3)), "cap-3", kind="book")
    assert book.triage.status == "kept"


def test_a_sharper_repeat_sets_the_older_capture_aside(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    _store(tmp_vault, topic, framed(written(3), blur=5), "cap-1")
    sharper = _store(tmp_vault, topic, framed(written(3)), "cap-2")
    assert sharper.triage.status == "kept"
    [change] = sharper.triage_changes
    assert change.source_id == "sources/notes/page-001.jpg"
    assert change.capture_id == "cap-1"
    assert change.result.status == "set_aside"
    assert change.result.duplicate_of == "sources/notes/page-002.jpg"
    older = _sidecar(tmp_vault, topic, 1)["triage"]
    assert older["status"] == "set_aside"
    assert older["reasons"] == ["duplicate"]
    assert older["history"][0]["status"] == "kept"


def test_a_page_the_notes_link_is_never_set_aside_automatically(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    _store(tmp_vault, topic, framed(written(3), blur=5), "cap-1")
    write_notes(
        tmp_vault,
        *topic,
        "# La comunicación\n\nTexto.[^p1]\n\n"
        "[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n",
    )
    sharper = _store(tmp_vault, topic, framed(written(3)), "cap-2")
    assert sharper.triage_changes == ()
    assert sharper.triage.status == "set_aside"
    assert sharper.triage.duplicate_of == "sources/notes/page-001.jpg"
    assert _sidecar(tmp_vault, topic, 1)["triage"]["status"] == "kept"


def test_triage_can_be_switched_off(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    stored = store_capture(
        tmp_vault,
        *topic,
        "notes",
        [BurstStill(encode(paper()), "image/jpeg")],
        {"capture_id": "cap-1"},
        0,
        SourcesSettings(triage_enabled=False),
    )
    assert stored.triage is None
    assert "triage" not in _sidecar(tmp_vault, topic, 1)


# -- the reader ------------------------------------------------------------------------------------


def test_triage_status_reads_legacy_sources_as_kept(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    put_source(tmp_vault, *topic, "notes", "old.jpg", encode(paper()), {"capture_id": "old"})
    _store(tmp_vault, topic, paper(), "cap-2")
    status = triage_status(tmp_vault, *topic)
    assert status["sources/notes/page-001.jpg"].status == "kept"
    assert status["sources/notes/page-001.jpg"].decided_by == "legacy"
    assert status["sources/notes/page-002.jpg"].status == "set_aside"
    assert status["sources/notes/page-002.jpg"].reasons == ["blank"]


# -- content duplicates ----------------------------------------------------------------------------

TEXT = (
    "# La comunicación\n\n- **Emisor**: quien envía el mensaje.\n- Receptor: quien lo recibe.\n"
    "- Canal: el medio físico por el que circula el mensaje entre ambos.\n"
)


def test_normalised_text_drops_case_accents_markdown_and_marks() -> None:
    assert normalise_text("# La **Comunicación** [[?emisor]] y [[?]]") == [
        "la",
        "comunicacion",
        "emisor",
        "y",
    ]
    assert text_similarity(TEXT, TEXT.replace("Emisor", "[[?emisor]]")) == 1.0
    assert text_similarity("[[?]]", "[[?]]") == 0.0  # illegible pages are not compared


def _transcribe(vault: Vault, source_path: str, text: str) -> None:
    put_page_transcription(vault, source_path, text)


def _vault_path(stored, vault: Vault) -> str:
    return stored.path.relative_to(vault.path).as_posix()


def test_the_page_with_more_doubt_marks_is_set_aside_as_same_content(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    first = _store(tmp_vault, topic, framed(written(3)), "cap-1")
    second = _store(tmp_vault, topic, framed(written(8)), "cap-2")  # another framing
    assert second.triage.status == "kept"
    _transcribe(tmp_vault, _vault_path(first, tmp_vault), TEXT)
    doubtful = TEXT.replace("Receptor", "[[?Receptor]]")
    _transcribe(tmp_vault, _vault_path(second, tmp_vault), doubtful)

    [change] = check_same_content(
        tmp_vault, *topic, _vault_path(second, tmp_vault), doubtful, SETTINGS, now=NOW
    )
    assert change.source_id == "sources/notes/page-002.jpg"
    assert change.result.reasons == ["same_content"]
    assert change.result.duplicate_of == "sources/notes/page-001.jpg"
    assert _sidecar(tmp_vault, topic, 2)["triage"]["status"] == "set_aside"


def test_a_tie_sets_aside_the_less_sharp_and_never_a_linked_page(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    soft = _store(tmp_vault, topic, framed(written(3), blur=5), "cap-1")
    sharp = _store(tmp_vault, topic, framed(written(8)), "cap-2")
    for stored in (soft, sharp):
        _transcribe(tmp_vault, _vault_path(stored, tmp_vault), TEXT)
    [change] = check_same_content(tmp_vault, *topic, _vault_path(sharp, tmp_vault), TEXT, SETTINGS)
    assert change.source_id == "sources/notes/page-001.jpg"  # the older, less sharp one

    # With the soft page linked by the notes, nothing is set aside automatically.
    set_capture_triage(tmp_vault, *topic, change.source_id, "restore", sync=None)
    third = _store(tmp_vault, topic, framed(written(9)), "cap-3")
    _transcribe(tmp_vault, _vault_path(third, tmp_vault), TEXT)
    write_notes(
        tmp_vault,
        *topic,
        "Texto.[^p1][^p2]\n\n[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)\n"
        "[^p2]: [Apuntes, página 3](../sources/notes/page-003.jpg)\n",
    )
    changes = check_same_content(tmp_vault, *topic, _vault_path(third, tmp_vault), TEXT, SETTINGS)
    assert [c.source_id for c in changes] == ["sources/notes/page-002.jpg"]


def test_different_transcriptions_set_nothing_aside(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    first = _store(tmp_vault, topic, framed(written(3)), "cap-1")
    second = _store(tmp_vault, topic, framed(written(8)), "cap-2")
    _transcribe(tmp_vault, _vault_path(first, tmp_vault), TEXT)
    other = (
        "# El texto\n\nUn texto es una unidad de comunicación con sentido completo y coherencia."
    )
    _transcribe(tmp_vault, _vault_path(second, tmp_vault), other)
    assert (
        check_same_content(tmp_vault, *topic, _vault_path(second, tmp_vault), other, SETTINGS) == []
    )


# -- the student's decisions -----------------------------------------------------------------------


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def test_the_student_sets_a_capture_aside_and_restores_it_with_commits(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    for seed, capture in ((3, "cap-1"), (8, "cap-2")):
        _store(tmp_vault, topic, framed(written(seed)), capture)
    sync = GitSync(tmp_vault)
    aside = set_capture_triage(
        tmp_vault, *topic, "sources/notes/page-002.jpg", "set_aside", reason="no sirve", sync=sync
    )
    assert aside.status == "set_aside"
    assert aside.decided_by == "student"
    assert aside.reasons == []
    assert aside.note == "no sirve"
    assert _git(tmp_vault, "log", "-1", "--format=%s").strip() == (
        f"Página 2 de {topic[0]}/{topic[1]} apartada"
    )
    block = _sidecar(tmp_vault, topic, 2)["triage"]
    assert block["status"] == "set_aside"
    assert block["history"][0]["decided_by"] == "auto"

    restored = set_capture_triage(
        tmp_vault, *topic, "sources/notes/page-002.jpg", "restore", sync=sync
    )
    assert restored.status == "kept"
    assert restored.reasons == []
    assert [entry["status"] for entry in restored.history] == ["kept", "set_aside"]
    assert _git(tmp_vault, "log", "-1", "--format=%s").strip() == (
        f"Página 2 de {topic[0]}/{topic[1]} recuperada"
    )
    # A student decision is protected: a sharper repeat does not set it aside.
    assert triage_status(tmp_vault, *topic)["sources/notes/page-002.jpg"].decided_by == "student"


def test_a_student_reason_that_is_a_triage_reason_is_kept_as_one(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    stored = _store(tmp_vault, topic, framed(written(3)), "cap-1")
    result = set_capture_triage(
        tmp_vault, *topic, _vault_path(stored, tmp_vault), "set_aside", reason="blurry", sync=None
    )
    assert result.reasons == ["blurry"]
    with pytest.raises(ValueError):
        set_capture_triage(tmp_vault, *topic, "sources/notes/page-001.jpg", "drop", sync=None)  # type: ignore[arg-type]


def test_the_event_kind_is_exported() -> None:
    assert CAPTURE_TRIAGED_KIND == "capture.triaged"
    assert hamming("ff", "0f") == 4
