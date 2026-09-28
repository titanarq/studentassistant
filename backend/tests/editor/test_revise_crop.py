"""A crop asked in the workspace chat (#493): `crop_image` in the revise turn, applied as edit."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable
from typing import Any

import cv2
import numpy as np
import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor import revise as revise_module
from studentassistant.editor.crop import (
    BLURRY_CROP_MESSAGE,
    CROP_FAILED_PREFIX,
    CROP_TOOL,
    REGION_NOT_FOUND_MESSAGE,
    TOOL_NAME,
    UNSUPPORTED_IMAGE_MESSAGE,
    CroppedImage,
)
from studentassistant.editor.notes_format import topic_source_resolver, validate
from studentassistant.editor.notes_lock import notes_write_lock
from studentassistant.editor.revise import (
    EDIT_TOOL,
    REPLY_DELTA,
    REPLY_RESTART,
    RevisionResult,
    revise_notes,
    undo_last_revision,
)
from studentassistant.llm import FakeClaude, load_prompt
from studentassistant.vault import (
    GitSync,
    Vault,
    list_sources,
    put_source,
    read_notes,
    read_source,
    write_notes,
)

BOX = {"x0": 0.25, "y0": 0.25, "x1": 0.75, "y1": 0.75}
REGION = "el diagrama de la página"
CONFIRMATION = "He añadido el recorte del diagrama de la página 1 del libro."
LINK = "![Imagen recortada 1](../sources/images/img-001.jpg)[^img001]"
FOOTNOTE = "[^img001]: [Imagen recortada 1](../sources/images/img-001.jpg)"
BOOK_PAGE = "sources/book/page-002.jpg"


@pytest.fixture
def topic(tmp_vault: Vault) -> ReviseTopic:
    return make_revise_topic(tmp_vault)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _run[T](coroutine: Awaitable[T]) -> T:
    async def main() -> T:
        return await asyncio.wait_for(coroutine, 30)

    return asyncio.run(main())


class Sink:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.items.append((kind, payload))


def _diagram(*, blurred: bool = False) -> bytes:
    """A line drawing on white paper, as a JPEG; `blurred` smears it past the sharpness floor."""
    image = np.full((600, 800, 3), 255, np.uint8)
    for i in range(12):
        cv2.line(image, (50 + i * 50, 80), (90 + i * 50, 500), (0, 0, 0), 2)
    cv2.circle(image, (400, 300), 120, (30, 30, 30), 3)
    if blurred:
        image = cv2.GaussianBlur(image, (0, 0), 25)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return encoded.tobytes()


def _book_page(topic: ReviseTopic, content: bytes, name: str = "foto.jpg") -> str:
    """A second textbook page, stored and committed: the student's Recursos selection."""
    path = put_source(topic.vault, topic.subject, topic.topic, "book", name, content, {"page": 2})
    GitSync(topic.vault).checkpoint("fixture: page 2")
    return path.relative_to(topic.vault.path).as_posix().split(f"{topic.topic}/", 1)[1]


def _crop_call(**change: Any) -> dict[str, Any]:
    return {
        "source": BOOK_PAGE,
        "region": REGION,
        "op": "insert_after",
        "section": "definicion",
        "block": 2,
        "summary": "Añado el recorte del diagrama",
        **change,
    }


def _revise(
    topic: ReviseTopic,
    sync: GitSync,
    fake: FakeClaude,
    message: str = "pon solo el diagrama de esta página",
    *,
    selected: list[str] | None = None,
    replies: Sink | None = None,
    events: Sink | None = None,
) -> RevisionResult:
    return _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            message,
            client=fake.client("editor"),
            crop_client=fake.client("observer"),
            sync=sync,
            on_reply=replies,
            on_event=events,
            selected_sources=[BOOK_PAGE] if selected is None else selected,
            expects_change=True,
        )
    )


def _notes(topic: ReviseTopic) -> str:
    notes = read_notes(topic.vault, topic.subject, topic.topic)
    assert notes is not None
    return notes


def _images(topic: ReviseTopic) -> list[str]:
    return [
        source.path
        for source in list_sources(topic.vault, topic.subject, topic.topic)
        if source.kind == "images"
    ]


def _git(vault: Vault, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=vault.path, check=True, capture_output=True, text=True
    ).stdout


def test_a_crop_is_stored_linked_and_cited_in_one_turn(topic: ReviseTopic, sync: GitSync) -> None:
    _book_page(topic, _diagram())
    fake = (
        FakeClaude()
        .reply_tool(CROP_TOOL, _crop_call(), text=CONFIRMATION)
        .reply_tool(TOOL_NAME, BOX)
    )
    events = Sink()

    result = _revise(topic, sync, fake, events=events)

    assert result.applied and result.notes_changed and result.errors == []
    assert result.reply == CONFIRMATION and result.warning is None
    assert result.summary == "Añado el recorte del diagrama"
    # The ordinary edit op: the image link with its footnote, after block 2 of #definicion.
    [op] = result.ops
    assert (op.op, op.section, op.block, op.text) == ("insert_after", "definicion", 2, LINK)
    notes = _notes(topic)
    assert f"Se escribe $f'(x)$.[^t2]\n\n{LINK}\n\n## 2. Próximo día" in notes
    assert FOOTNOTE in notes.splitlines()
    resolver = topic_source_resolver(topic.vault, topic.subject, topic.topic)
    assert validate(notes, "estricto", resolver) == []
    # The crop is a new source of the topic, traced back to the page, in the turn's commit.
    assert result.crop is not None and result.crop.error is None
    assert result.crop.source_id == "sources/images/img-001.jpg"
    assert result.crop.source == BOOK_PAGE and result.crop.region == REGION
    assert result.crop.path is not None and _images(topic) == [result.crop.path]
    meta = read_source(topic.vault, result.crop.path).meta
    assert meta is not None and meta["origin"] == "cropped"
    assert meta["cropped_from"].endswith(BOOK_PAGE)
    assert meta["requested_region"] == REGION
    committed = _git(topic.vault, "show", "--name-only", "--format=", result.commit or "")
    assert result.crop.path in committed.split() and set(result.paths) <= set(committed.split())
    assert result.crop.path in result.paths
    images = result.crop.path.rsplit("/", 1)[0]
    assert _git(topic.vault, "status", "--porcelain", "--", images).strip() == ""
    assert [kind for kind, _ in events.items] == ["notes.edited"]
    assert events.items[0][1]["crop"]["source_id"] == "sources/images/img-001.jpg"
    # Sonnet saw the selected page, not the editor's input.
    [_, located] = fake.requests
    assert located.role == "observer" and located.tools[0]["name"] == TOOL_NAME

    # The crop is undone with the turn: the notes and the new image go back.
    undone = _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))
    assert undone.undone_commit == result.commit
    assert LINK not in _notes(topic) and _images(topic) == []


def test_a_cited_pdf_page_is_cropped_from_its_rendered_page(
    topic: ReviseTopic, sync: GitSync
) -> None:
    put_source(
        topic.vault,
        topic.subject,
        topic.topic,
        "pdf",
        "doc.pdf",
        b"%PDF-1.4 fake",
        {"page_count": 3},
        derived={"p003.jpg": _diagram()},
    )
    notes = (
        _notes(topic).replace("Se escribe $f'(x)$.[^t2]", "Se escribe $f'(x)$.[^t2][^d3]")
        + "[^d3]: [PDF doc, página 3](../sources/pdf/page-001.pdf#page=3)\n"
    )
    write_notes(topic.vault, topic.subject, topic.topic, notes)
    sync.checkpoint("fixture: cite the pdf")
    fake = (
        FakeClaude()
        .reply_tool(
            CROP_TOOL,
            _crop_call(source="sources/pdf/page-001.pdf#page=3", op="replace_block", block=1),
            text="He añadido el recorte.",
        )
        .reply_tool(TOOL_NAME, BOX)
    )

    result = _revise(topic, sync, fake, "recorta el diagrama de la página 3 del PDF", selected=[])

    assert result.applied and result.crop is not None and result.crop.error is None
    meta = read_source(topic.vault, result.crop.path or "").meta
    assert meta is not None and meta["cropped_from"].endswith("sources/pdf/page-001.p003.jpg")
    assert _notes(topic).split("## 1. Definición {#definicion}\n\n", 1)[1].startswith(LINK)


@pytest.mark.parametrize(
    ("content", "name", "box_reply", "message", "sonnet_calls"),
    [
        (_diagram(blurred=True), "foto.jpg", "box", BLURRY_CROP_MESSAGE, 1),
        (b"GIF89a not really a gif", "foto.gif", None, UNSUPPORTED_IMAGE_MESSAGE, 0),
        (b"%PDF-1.4", "foto.pdf", None, UNSUPPORTED_IMAGE_MESSAGE, 0),
        (_diagram(), "foto.jpg", "refusal", REGION_NOT_FOUND_MESSAGE, 1),
    ],
    ids=["blurry", "undecodable-gif", "not-an-image", "box-refused"],
)
def test_a_failed_crop_changes_nothing_and_says_why(
    topic: ReviseTopic,
    sync: GitSync,
    content: bytes,
    name: str,
    box_reply: str | None,
    message: str,
    sonnet_calls: int,
) -> None:
    source = _book_page(topic, content, name)
    before = _notes(topic)
    head = _git(topic.vault, "rev-parse", "HEAD")
    fake = FakeClaude().reply_tool(CROP_TOOL, _crop_call(source=source), text=CONFIRMATION)
    if box_reply == "box":
        fake.reply_tool(TOOL_NAME, BOX)
    elif box_reply == "refusal":
        fake.reply_text("No puedo.", stop_reason="refusal")
    replies = Sink()
    events = Sink()

    result = _revise(topic, sync, fake, selected=[source], replies=replies, events=events)

    assert not result.applied and not result.notes_changed and result.ops == []
    assert result.reply == f"{CROP_FAILED_PREFIX}{message}"
    assert result.crop is not None and result.crop.error == message
    assert result.crop.source_id is None
    # The streamed confirmation is dropped and the reason streamed in its place.
    assert replies.items[-2][0] == REPLY_RESTART
    assert replies.items[-1] == (REPLY_DELTA, {"text": result.reply, "attempt": 2})
    assert _notes(topic) == before and _images(topic) == []
    assert _git(topic.vault, "rev-parse", "HEAD") == head and events.items == []
    assert len(fake.requests) == 1 + sonnet_calls and fake.pending == 0


def test_a_source_neither_cited_nor_selected_is_sent_back_before_any_crop(
    topic: ReviseTopic, sync: GitSync
) -> None:
    _book_page(topic, _diagram())
    fake = (
        FakeClaude()
        .reply_tool(CROP_TOOL, _crop_call(), text=CONFIRMATION)
        .reply_tool(CROP_TOOL, _crop_call(section="nope"), text=CONFIRMATION)
        .reply_text("No puedo recortar esa página: selecciónala en Recursos.")
    )

    result = _revise(topic, sync, fake, selected=[])

    assert not result.applied and _images(topic) == []
    # Nothing reached Sonnet: every call was the editor's.
    assert [request.role for request in fake.requests] == ["editor"] * 3
    first_error = fake.requests[1].messages[-1]["content"][0]
    assert first_error["type"] == "tool_result" and first_error["is_error"] is True
    assert "seleccionado en Recursos" in first_error["content"]
    assert f"`{CROP_TOOL}`" in fake.requests[1].messages[-1]["content"][-1]["text"]
    second_error = fake.requests[2].messages[-1]["content"][0]["content"]
    assert "nope" in second_error


def test_crop_and_apply_edits_together_are_sent_back(topic: ReviseTopic, sync: GitSync) -> None:
    _book_page(topic, _diagram())
    fake = FakeClaude()
    response = fake._response(
        [
            {"type": "text", "text": CONFIRMATION},
            {"type": "tool_use", "id": "a", "name": CROP_TOOL, "input": _crop_call()},
            {"type": "tool_use", "id": "b", "name": EDIT_TOOL, "input": {"summary": "x"}},
        ],
        "tool_use",
        None,
    )
    fake.reply(response).reply_tool(CROP_TOOL, _crop_call(), text=CONFIRMATION).reply_tool(
        TOOL_NAME, BOX
    )

    result = _revise(topic, sync, fake)

    assert result.applied and result.attempts == 2 and _images(topic) != []
    error = fake.requests[1].messages[-1]["content"][0]["content"]
    assert "no a las dos" in error


def test_the_crop_runs_outside_the_notes_lock_and_a_student_save_is_redone_on(
    topic: ReviseTopic, sync: GitSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    _book_page(topic, _diagram())
    student = _notes(topic).replace("Se escribe $f'(x)$.", "Se escribe $f'(x)$ o $y'$.")
    original = revise_module.crop_source_image
    held: list[bool] = []

    async def cropping_while_the_student_saves(*args: Any, **kwargs: Any) -> CroppedImage:
        held.append(notes_write_lock(topic.vault, topic.subject, topic.topic).locked())
        image = await original(*args, **kwargs)
        write_notes(topic.vault, topic.subject, topic.topic, student)
        return image

    monkeypatch.setattr(revise_module, "crop_source_image", cropping_while_the_student_saves)
    fake = (
        FakeClaude()
        .reply_tool(CROP_TOOL, _crop_call(), text=CONFIRMATION)
        .reply_tool(TOOL_NAME, BOX)
        .reply_tool(CROP_TOOL, _crop_call(), text=CONFIRMATION)
    )
    replies = Sink()

    result = _revise(topic, sync, fake, replies=replies)

    # The crop ran unlocked, once; the change was redone on the student's notes and kept them.
    assert held == [False]
    assert result.applied and result.attempts == 2
    assert [request.role for request in fake.requests] == ["editor", "observer", "editor"]
    assert "cambiado los apuntes" in fake.requests[2].messages[-1]["content"][-1]["text"]
    notes = _notes(topic)
    assert "Se escribe $f'(x)$ o $y'$.[^t2]" in notes and LINK in notes
    assert _images(topic) == [result.crop.path] if result.crop else False
    assert (REPLY_RESTART, {"attempt": 2}) in replies.items


def test_a_crop_whose_change_is_never_applied_is_retired(
    topic: ReviseTopic, sync: GitSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    _book_page(topic, _diagram())
    student = _notes(topic).replace("Se escribe $f'(x)$.", "Se escribe $f'(x)$ o $y'$.")
    original = revise_module.crop_source_image

    async def cropping_while_the_student_saves(*args: Any, **kwargs: Any) -> CroppedImage:
        image = await original(*args, **kwargs)
        write_notes(topic.vault, topic.subject, topic.topic, student)
        return image

    monkeypatch.setattr(revise_module, "crop_source_image", cropping_while_the_student_saves)
    fake = (
        FakeClaude()
        .reply_tool(CROP_TOOL, _crop_call(), text=CONFIRMATION)
        .reply_tool(TOOL_NAME, BOX)
        .reply_text("Vale, lo dejo así.")
        .reply_text("Los apuntes se quedan como los has dejado.")
    )

    result = _revise(topic, sync, fake)

    assert not result.applied and result.crop is None
    assert _notes(topic) == student and _images(topic) == []


def test_the_request_classifier_routes_a_crop_to_the_editor_as_an_edit() -> None:
    text = load_prompt("observer_requests").content
    edit_line = " ".join(text.split("-> `edit`;", 1)[0].rsplit("\n- ", 1)[1].split())
    assert "pon solo el diagrama de la página 3" in edit_line
    assert "recorta la tabla de esta foto" in edit_line
    revise = load_prompt("editor_revise").content
    assert f"`{CROP_TOOL}`" in revise and "Imagen recortada N" in revise
