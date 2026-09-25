"""'Prepárame el tema' (`editor.generate`): FakeClaude as Opus over the fixture topic."""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

import pytest

from generate_topic import (
    PAGE_1_TRANSCRIPTION,
    STYLE_GUIDE,
    GenerateTopic,
    invalid_notes,
    make_pdf,
    make_topic,
    valid_notes,
)
from studentassistant.config import Settings
from studentassistant.editor.generate import (
    MAX_REASKS,
    NOTES_GENERATED_KIND,
    GenerationResult,
    generate_notes,
    notes_text,
)
from studentassistant.editor.inputs import assemble_input, needs_image
from studentassistant.llm import (
    CostConfirmationRequiredError,
    FakeClaude,
    LedgerBinding,
    LLMRequest,
    LLMResponse,
    RefusalError,
    load_prompt,
)
from studentassistant.sources import PageRange, import_pdf
from studentassistant.vault import (
    GitSync,
    Vault,
    put_page_transcription,
    read_conversation,
    read_ledger,
    read_notes,
    read_notes_draft,
)


@pytest.fixture
def topic(tmp_vault: Vault) -> GenerateTopic:
    return make_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _run(
    topic: GenerateTopic,
    sync: GitSync,
    fake: FakeClaude,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    **options: Any,
) -> GenerationResult:
    async def on_event(kind: str, payload: dict[str, Any]) -> None:
        if events is not None:
            events.append((kind, payload))

    # Detecting contradictions is a second call, tested in test_contradictions.py.
    options.setdefault("detect_contradictions", False)

    async def main() -> GenerationResult:
        return await asyncio.wait_for(
            generate_notes(
                topic.vault,
                topic.subject,
                topic.topic,
                client=fake.client("editor", ledger=options.pop("ledger", None)),
                sync=sync,
                on_event=on_event,
                **options,
            ),
            30,
        )

    return asyncio.run(main())


def _texts(request: LLMRequest) -> list[str]:
    return [b["text"] for b in request.messages[0]["content"] if b["type"] == "text"]


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


# -- a valid first answer -------------------------------------------------------------------------


def test_valid_notes_are_written_committed_and_tagged_v1(
    topic: GenerateTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_text(valid_notes(topic.session))
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(topic, sync, fake, events)

    assert not result.draft and result.errors == [] and result.attempts == 1
    assert result.version == 1
    assert result.tag == f"{topic.subject}/{topic.topic}/apuntes-v1"
    assert result.path.endswith("/notes/apuntes.md")
    assert read_notes(topic.vault, topic.subject, topic.topic) == valid_notes(topic.session)
    tags = sync.list_notes_tags(topic.subject, topic.topic)
    assert [t.name for t in tags] == [result.tag] and tags[0].commit == result.commit
    tagged = _git(topic.vault, "show", f"{result.tag}:{result.path}")
    assert tagged == valid_notes(topic.session)
    assert "Apuntes v1" in _git(topic.vault, "log", "-1", "--format=%s", result.tag)
    assert events == [(NOTES_GENERATED_KIND, result.model_dump(mode="json"))]
    assert result.model == "claude-opus-5-5"


def test_the_request_uses_the_editor_role_and_the_generate_prompt(
    topic: GenerateTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_text(valid_notes(topic.session))
    _run(topic, sync, fake)

    [request] = fake.requests
    assert request.role == "editor" and request.model == "claude-opus-5-5"
    assert request.effort == "high" and request.max_tokens == 64000
    prompt = load_prompt("editor_generate")
    assert request.prompt_hash == prompt.hash
    assert request.system[0]["text"] == prompt.content
    topic_block = request.system[1]["text"]
    assert "Asignatura: Matemáticas" in topic_block and "Tema: Derivadas" in topic_block
    assert "Modo de fidelidad: estricto" in topic_block and STYLE_GUIDE in topic_block
    assert request.system[-1]["cache_control"] == {"type": "ephemeral"}
    assert request.tools == [] and request.tool_choice is None


def test_the_input_puts_the_sources_first_and_the_transcript_by_section(
    topic: GenerateTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_text(valid_notes(topic.session))
    _run(topic, sync, fake)

    content = fake.requests[0].messages[0]["content"]
    texts = _texts(fake.requests[0])
    joined = "\n".join(texts)
    # Catalogue first, with the exact footnote definitions to copy.
    assert texts[0].startswith("## Catálogo de fuentes citables")
    assert "[Apuntes, página 1](../sources/notes/page-001.jpg)" in texts[0]
    assert "[Apuntes, página 2](../sources/notes/page-002.jpg)" in texts[0]
    # Page 1 goes as its transcription (no image); page 2 has none, so its image goes.
    assert PAGE_1_TRANSCRIPTION.strip() in joined
    assert "(Sin transcripción: lee la página en la imagen.)" in joined
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 1 and images[0]["source"]["media_type"] == "image/jpeg"
    # The sources end with a cache breakpoint, before the volatile part.
    marked = [i for i, b in enumerate(content) if "cache_control" in b]
    assert len(marked) == 2 and marked[-1] == len(content) - 1
    assert content[marked[0]]["type"] == "image"  # page 2, the last source
    transcript = next(t for t in texts if t.startswith("## Transcripción"))
    assert content.index(next(b for b in content if b.get("text") == transcript)) > marked[0]
    # Segments are grouped under the observer's sections, each with session and span.
    definition = transcript.index("Definición (sección sec-def)")
    unassigned = transcript.index("### Sin sección asignada")
    s1 = transcript.index(f"[{topic.session} t=00:00:02-00:00:10] La derivada")
    s3 = transcript.index(f"[{topic.session} t=01:02:05-01:02:10] Mañana")
    assert definition < s1 < unassigned < s3
    assert "Páginas: sources/notes/page-001.jpg" in transcript
    assert "Conceptos: derivada" in transcript
    pending = next(t for t in texts if t.startswith("## Dudas pendientes"))
    assert "p-1 [unexplained_concept] Se menciona la regla de la cadena" in pending
    assert (
        "p-1 [unexplained_concept] Se menciona la regla de la cadena sin explicarla."
        f" ({topic.session} t=01:02:05-01:02:10)" in pending
    )
    assert (
        "p-2 [illegible, resuelta por el estudiante] Palabra ilegible en la página 1."
        " (sources/notes/page-001.jpg) -> Pone «incremental»." in pending
    )
    assert (
        "p-3 [possible_error, descartada] ¿Falta el signo en la fórmula?"
        " (sources/notes/page-002.jpg)" in pending
    )
    assert "p-3" in pending.split("Cerradas")[1]
    assert texts[-1].startswith("Escribe ahora `notes/apuntes.md` del tema «Derivadas»")


def test_the_conversation_is_persisted_without_the_attachments(
    topic: GenerateTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_text(valid_notes(topic.session))
    result = _run(topic, sync, fake)

    records = read_conversation(topic.vault, topic.subject, topic.topic, "editor")
    assert [r.kind for r in records] == [
        "context",
        "user",
        "assistant",
        "validation",
        NOTES_GENERATED_KIND,
    ]
    context = records[0]
    assert context.model == "claude-opus-5-5"
    assert context.prompt_hash == load_prompt("editor_generate").hash
    assert context.detail is not None and context.detail["reason"] == "generate"
    assert context.detail["images"] == ["sources/notes/page-002.jpg"]
    assert context.detail["sessions"] == [topic.session]
    user = records[1].message
    assert user is not None
    assert {"type": "image_ref", "source_id": "sources/notes/page-002.jpg"} in [
        {k: v for k, v in b.items() if k != "cache_control"} for b in user["content"]
    ]
    assert "base64" not in json.dumps(user)
    assert records[2].usage is not None
    assert records[3].detail == {"attempt": 1, "errors": []}
    assert records[4].detail == result.model_dump(mode="json")


def test_every_call_is_recorded_in_the_topics_ledger(topic: GenerateTopic, sync: GitSync) -> None:
    fake = FakeClaude().reply_text(invalid_notes(topic.session))
    fake.reply_text(valid_notes(topic.session))
    binding = LedgerBinding(topic.vault, topic.subject, topic.topic)
    _run(topic, sync, fake, ledger=binding)

    entries = read_ledger(topic.vault, topic.subject, topic.topic)
    assert [(e.role, e.model) for e in entries] == [("editor", "claude-opus-5-5")] * 2


def test_a_reached_cost_cap_needs_confirmation_and_writes_nothing(
    topic: GenerateTopic, sync: GitSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_LLM__MAX_USD_PER_DAY", "0")
    settings = Settings()
    fake = FakeClaude().reply_text(valid_notes(topic.session))
    binding = LedgerBinding(topic.vault, topic.subject, topic.topic)

    async def main() -> None:
        await generate_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            client=fake.client("editor", settings=settings, ledger=binding),
            sync=sync,
        )

    with pytest.raises(CostConfirmationRequiredError):
        asyncio.run(asyncio.wait_for(main(), 30))
    assert fake.requests == []
    assert read_notes(topic.vault, topic.subject, topic.topic) is None
    assert read_conversation(topic.vault, topic.subject, topic.topic, "editor") == []


# -- re-asks and drafts ---------------------------------------------------------------------------


def test_a_failing_document_is_re_asked_with_the_error_list(
    topic: GenerateTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_text(invalid_notes(topic.session))
    fake.reply_text(valid_notes(topic.session))

    result = _run(topic, sync, fake)

    assert not result.draft and result.attempts == 2 and result.version == 1
    first, second = fake.requests
    assert len(second.messages) == 3
    assert second.messages[:1] == first.messages
    assert second.messages[1]["role"] == "assistant"
    reask = second.messages[2]["content"][0]["text"]
    assert reask.startswith("El validador de procedencia ha rechazado el documento")
    assert "Sección #definicion, bloque 2" in reask and "no tiene nota al pie" in reask
    kinds = [r.kind for r in read_conversation(topic.vault, topic.subject, topic.topic, "editor")]
    assert kinds == [
        "context",
        "user",
        "assistant",
        "validation",
        "user",
        "assistant",
        "validation",
        NOTES_GENERATED_KIND,
    ]


def test_a_document_still_failing_after_the_re_asks_is_kept_as_a_draft(
    topic: GenerateTopic, sync: GitSync
) -> None:
    fake = FakeClaude()
    for _ in range(MAX_REASKS + 1):
        fake.reply_text(invalid_notes(topic.session))
    events: list[tuple[str, dict[str, Any]]] = []

    result = _run(topic, sync, fake, events)

    assert len(fake.requests) == 3 and result.attempts == 3
    assert result.draft and result.version is None and result.tag is None
    assert result.errors and "no tiene nota al pie" in result.errors[0]
    assert result.warning is not None and "borrador" in result.warning
    assert result.path.endswith("/notes/borrador.md")
    assert read_notes(topic.vault, topic.subject, topic.topic) is None
    assert read_notes_draft(topic.vault, topic.subject, topic.topic) == invalid_notes(topic.session)
    assert sync.list_notes_tags(topic.subject, topic.topic) == []
    assert result.commit is not None
    assert "Borrador" in _git(topic.vault, "log", "-1", "--format=%s")
    assert events[0][1]["draft"] is True


def test_a_draft_does_not_overwrite_the_last_valid_notes(
    topic: GenerateTopic, sync: GitSync
) -> None:
    _run(topic, sync, FakeClaude().reply_text(valid_notes(topic.session)))
    fake = FakeClaude()
    for _ in range(MAX_REASKS + 1):
        fake.reply_text(invalid_notes(topic.session))

    result = _run(topic, sync, fake)

    assert result.draft
    assert read_notes(topic.vault, topic.subject, topic.topic) == valid_notes(topic.session)
    # A later valid generation removes the draft.
    _run(topic, sync, FakeClaude().reply_text(valid_notes(topic.session)))
    assert read_notes_draft(topic.vault, topic.subject, topic.topic) is None


def test_a_truncated_answer_is_re_asked(topic: GenerateTopic, sync: GitSync) -> None:
    fake = FakeClaude().reply_text(valid_notes(topic.session), stop_reason="max_tokens")
    fake.reply_text(valid_notes(topic.session))

    result = _run(topic, sync, fake)

    assert result.attempts == 2 and not result.draft
    assert "quedó cortado" in fake.requests[1].messages[2]["content"][0]["text"]


def test_a_refusal_raises_and_writes_no_notes(topic: GenerateTopic, sync: GitSync) -> None:
    fake = FakeClaude().reply_text("", stop_reason="refusal")
    with pytest.raises(RefusalError):
        _run(topic, sync, fake)
    assert read_notes(topic.vault, topic.subject, topic.topic) is None
    assert read_notes_draft(topic.vault, topic.subject, topic.topic) is None


# -- regeneration ---------------------------------------------------------------------------------


def test_a_regeneration_is_the_next_version_and_sees_the_current_notes(
    topic: GenerateTopic, sync: GitSync
) -> None:
    _run(topic, sync, FakeClaude().reply_text(valid_notes(topic.session)))
    fake = FakeClaude().reply_text(valid_notes(topic.session).replace("mañana", "otro día"))

    result = _run(topic, sync, fake)

    assert result.version == 2
    assert result.tag == f"{topic.subject}/{topic.topic}/apuntes-v2"
    assert [t.version for t in sync.list_notes_tags(topic.subject, topic.topic)] == [1, 2]
    current = next(t for t in _texts(fake.requests[0]) if "Versión actual" in t)
    assert "{#definicion}" in current and "Conserva las anclas" in current


# -- inputs ---------------------------------------------------------------------------------------


def test_the_digest_goes_into_the_input(topic: GenerateTopic) -> None:
    assembled = assemble_input(
        topic.vault,
        topic.subject,
        topic.topic,
        prompt=load_prompt("editor_generate"),
        digest=lambda _v, _s, _t: "Sesión 1: definición de derivada.",
    )
    digest = next(b["text"] for b in assembled.content if "Resumen del tema" in b.get("text", ""))
    assert "Sesión 1: definición de derivada." in digest
    assert assembled.summary()["digest"] is True


def test_page_images_past_the_limit_are_left_out(topic: GenerateTopic) -> None:
    assembled = assemble_input(
        topic.vault,
        topic.subject,
        topic.topic,
        prompt=load_prompt("editor_generate"),
        max_page_images=0,
    )
    assert not any(b["type"] == "image" for b in assembled.content)
    assert assembled.omitted == ["sources/notes/page-002.jpg"]


def test_a_pdf_goes_as_a_document_cited_with_its_original_page_numbers(
    topic: GenerateTopic,
) -> None:
    import_pdf(
        topic.vault, topic.subject, topic.topic, "libro.pdf", make_pdf(90), pages=PageRange(84, 85)
    )
    assembled = assemble_input(
        topic.vault, topic.subject, topic.topic, prompt=load_prompt("editor_generate")
    )

    [document] = [b for b in assembled.content if b["type"] == "document"]
    assert document["source"]["media_type"] == "application/pdf"
    catalogue = assembled.content[0]["text"]
    assert "[PDF, página 84](../sources/pdf/page-001.pdf#page=1)" in catalogue
    assert "[PDF, página 85](../sources/pdf/page-001.pdf#page=2)" in catalogue
    assert assembled.documents == ["sources/pdf/page-001.pdf"]


def test_a_pdf_past_the_attachment_budget_goes_as_its_page_texts(topic: GenerateTopic) -> None:
    import_pdf(topic.vault, topic.subject, topic.topic, "libro.pdf", make_pdf(3))
    assembled = assemble_input(
        topic.vault,
        topic.subject,
        topic.topic,
        prompt=load_prompt("editor_generate"),
        max_attachment_bytes=10,
    )
    assert not any(b["type"] in ("document", "image") for b in assembled.content)
    joined = "\n".join(b.get("text", "") for b in assembled.content)
    assert "Página 2 del libro" in joined
    assert "sources/pdf/page-001.pdf" in assembled.omitted


def _scanned_pdf() -> bytes:
    """Page 1 has a text layer; page 2 is an image only (a scanned page)."""
    import pymupdf

    document = pymupdf.open()
    try:
        document.new_page().insert_text((72, 72), "Página 1 del libro", fontsize=14)
        picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        picture.clear_with(200)
        document.new_page().insert_image(pymupdf.Rect(72, 72, 272, 272), pixmap=picture)
        return document.tobytes()
    finally:
        document.close()


def test_a_scanned_pdf_page_past_the_budget_goes_as_its_transcription(
    topic: GenerateTopic,
) -> None:
    imported = import_pdf(topic.vault, topic.subject, topic.topic, "escaneado.pdf", _scanned_pdf())
    assert [page.has_text for page in imported.pages] == [True, False]
    pdf_path = imported.path.relative_to(topic.vault.path).as_posix()
    put_page_transcription(topic.vault, pdf_path, "# Tema 4\n\nLa derivada escaneada.\n", page=2)

    assembled = assemble_input(
        topic.vault,
        topic.subject,
        topic.topic,
        prompt=load_prompt("editor_generate"),
        max_attachment_bytes=10,
    )
    joined = "\n".join(b.get("text", "") for b in assembled.content)
    assert "Página 1 del libro" in joined
    assert (
        "#### sources/pdf/page-001.pdf#page=2 (página 2 del original, página escaneada,"
        " transcrita)\n# Tema 4\n\nLa derivada escaneada." in joined
    )


def test_a_scanned_pdf_page_without_transcription_is_left_out(topic: GenerateTopic) -> None:
    import_pdf(topic.vault, topic.subject, topic.topic, "escaneado.pdf", _scanned_pdf())
    assembled = assemble_input(
        topic.vault,
        topic.subject,
        topic.topic,
        prompt=load_prompt("editor_generate"),
        max_attachment_bytes=10,
    )
    joined = "\n".join(b.get("text", "") for b in assembled.content)
    assert "sources/pdf/page-001.pdf#page=1 (" in joined
    assert "sources/pdf/page-001.pdf#page=2 (" not in joined


def test_a_topic_without_anything_captured_still_assembles(tmp_vault: Vault) -> None:
    from studentassistant.vault import create_subject, create_topic

    subject = create_subject(tmp_vault, "Historia").slug
    topic = create_topic(tmp_vault, subject, "Vacío").slug
    assembled = assemble_input(tmp_vault, subject, topic, prompt=load_prompt("editor_generate"))
    joined = "\n".join(b.get("text", "") for b in assembled.content)
    assert "(No hay transcripción de este tema.)" in joined
    assert "(No hay dudas registradas.)" in joined
    assert "La asignatura no tiene guía de estilo todavía." in assembled.system[1]


@pytest.mark.parametrize(
    ("transcription", "meta", "expected"),
    [
        (None, None, True),
        ("   ", None, True),
        ("Texto claro.", None, False),
        ("Una [[?palabra]] dudosa.", None, True),
        ("Ilegible: [[?]].", None, True),
        ("```mermaid\ngraph TD; A-->B\n```", None, True),
        ("- causa\n  - efecto", None, True),
        ("calor → trabajo", None, True),
        ("Texto claro.", {"transcription_confidence": 0.4}, True),
        ("Texto claro.", {"transcription_confidence": 0.95}, False),
    ],
)
def test_needs_image(
    transcription: str | None, meta: dict[str, Any] | None, expected: bool
) -> None:
    assert needs_image(transcription, meta) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# T\n\nx.[^p1]\n", "# T\n\nx.[^p1]\n"),
        ("```markdown\n# T\n```", "# T\n"),
        ("```\n# T\n```\n", "# T\n"),
        ("  \n", ""),
    ],
)
def test_notes_text_strips_a_code_fence(text: str, expected: str) -> None:
    response = LLMResponse(model="m", content=[{"type": "text", "text": text}])
    assert notes_text(response) == expected
