"""An SVG diagram drawn in the workspace chat (#511): `draw_diagram` in the revise turn."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable
from typing import Any

import pytest

from revise_topic import ReviseTopic, make_revise_topic
from studentassistant.editor import revise as revise_module
from studentassistant.editor.crop import CROP_TOOL
from studentassistant.editor.diagram import (
    DIAGRAM_TOOL,
    DRAWN_ORIGIN,
    DrawnDiagram,
    store_diagram,
)
from studentassistant.editor.direct_edit import normalise_student_text
from studentassistant.editor.notes_format import (
    parse,
    parse_provenance,
    topic_source_resolver,
    validate,
)
from studentassistant.editor.revise import (
    EDIT_TOOL,
    RevisionResult,
    chat_history,
    revise_notes,
    undo_last_revision,
)
from studentassistant.llm import FakeClaude, load_prompt
from studentassistant.vault import (
    GitSync,
    Vault,
    list_sources,
    read_notes,
    read_source,
    write_notes,
)

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 100" onload="alert(1)">'
    "<script>alert(1)</script>"
    '<polygon points="10,90 110,90 60,10" fill="none" stroke="black"/>'
    '<line x1="60" y1="10" x2="60" y2="90" stroke="red" onclick="steal()"/>'
    '<a href="https://evil.example/"><text x="5" y="98">enlace</text></a>'
    '<text x="62" y="50">h</text></svg>'
)
TITLE = "Triángulo con su altura"
CONFIRMATION = "He añadido un diagrama del triángulo con su altura."
LINK = "![Diagrama 1](../sources/images/img-001.svg)[^img001]"
FOOTNOTE = "[^img001]: [Diagrama 1](../sources/images/img-001.svg)"


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


def _call(**change: Any) -> dict[str, Any]:
    return {
        "svg": SVG,
        "title": TITLE,
        "op": "insert_after",
        "section": "definicion",
        "block": 2,
        "summary": "Añado un diagrama del triángulo",
        **change,
    }


def _revise(topic: ReviseTopic, sync: GitSync, fake: FakeClaude) -> RevisionResult:
    return _run(
        revise_notes(
            topic.vault,
            topic.subject,
            topic.topic,
            "hazme un dibujo del triángulo con su altura",
            client=fake.client("editor"),
            sync=sync,
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


def test_a_diagram_is_sanitized_stored_linked_and_cited_in_one_turn(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = FakeClaude().reply_tool(DIAGRAM_TOOL, _call(), text=CONFIRMATION)

    result = _revise(topic, sync, fake)

    assert result.applied and result.notes_changed and result.errors == []
    assert result.reply == CONFIRMATION and result.warning is None
    [op] = result.ops
    assert (op.op, op.section, op.block, op.text) == ("insert_after", "definicion", 2, LINK)
    notes = _notes(topic)
    assert f"Se escribe $f'(x)$.[^t2]\n\n{LINK}\n\n## 2. Próximo día" in notes
    assert FOOTNOTE in notes.splitlines()
    resolver = topic_source_resolver(topic.vault, topic.subject, topic.topic)
    assert validate(notes, "estricto", resolver) == []
    # The footnote parses as an `images` provenance «Diagrama 1».
    [definition] = [d for d in parse(notes).footnotes if d.label == "img001"]
    provenance = parse_provenance(definition)
    assert provenance.kind == "images" and provenance.text == "Diagrama 1"
    # The turn's crop record names the diagram.
    crop = result.crop
    assert crop is not None and crop.kind == "diagram" and crop.error is None
    assert crop.source_id == "sources/images/img-001.svg" and crop.region == TITLE
    assert crop.path is not None and _images(topic) == [crop.path]
    stored = read_source(topic.vault, crop.path)
    assert stored.media_type == "image/svg+xml"
    assert stored.meta is not None and stored.meta["origin"] == DRAWN_ORIGIN
    assert stored.meta["title"] == TITLE and stored.meta["content_type"] == "image/svg+xml"
    assert crop.sha256 == stored.meta["sha256"]
    # Only the sanitized drawing reached the vault.
    svg = stored.content.decode()
    assert "<polygon" in svg and ">h</text>" in svg
    for bad in ("script", "onload", "onclick", "evil.example", "<a ", "enlace"):
        assert bad not in svg
    committed = _git(topic.vault, "show", "--name-only", "--format=", result.commit or "").split()
    assert crop.path in committed
    # The editor was offered the tool and told when to use it instead of Mermaid.
    [request] = fake.requests
    assert DIAGRAM_TOOL in [tool["name"] for tool in request.tools]
    history = chat_history(topic.vault, topic.subject, topic.topic)
    last = history.turns[-1]
    assert last.crop is not None and last.crop.kind == "diagram"

    # Undone with the turn: the notes and the drawing go back.
    undone = _run(undo_last_revision(topic.vault, topic.subject, topic.topic, sync=sync))
    assert undone.undone_commit == result.commit
    assert LINK not in _notes(topic) and _images(topic) == []


def test_an_svg_that_cannot_be_kept_is_sent_back_and_redrawn(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = (
        FakeClaude()
        .reply_tool(DIAGRAM_TOOL, _call(svg="<div>no es svg</div>"), text=CONFIRMATION)
        .reply_tool(DIAGRAM_TOOL, _call(), text=CONFIRMATION)
    )

    result = _revise(topic, sync, fake)

    assert result.applied and result.attempts == 2
    reask = fake.requests[1].messages[-1]["content"]
    assert any("<svg>" in str(block) and DIAGRAM_TOOL in str(block) for block in reask)
    # One drawing stored: nothing of the refused attempt.
    assert len(_images(topic)) == 1


def test_a_diagram_with_a_bad_anchor_is_sent_back_before_anything_is_stored(
    topic: ReviseTopic, sync: GitSync
) -> None:
    fake = (
        FakeClaude()
        .reply_tool(DIAGRAM_TOOL, _call(section="no-existe"), text=CONFIRMATION)
        .reply_tool(DIAGRAM_TOOL, _call(section="no-existe"), text=CONFIRMATION)
        .reply_tool(DIAGRAM_TOOL, _call(section="no-existe"), text=CONFIRMATION)
    )
    before = _notes(topic)

    result = _revise(topic, sync, fake)

    assert not result.applied and result.errors and result.crop is None
    assert _notes(topic) == before and _images(topic) == []


@pytest.mark.parametrize("other", [EDIT_TOOL, CROP_TOOL])
def test_a_diagram_together_with_another_change_tool_is_sent_back(
    topic: ReviseTopic, sync: GitSync, other: str
) -> None:
    fake = FakeClaude()
    both = fake._response(
        [
            {"type": "tool_use", "id": "toolu_a", "name": DIAGRAM_TOOL, "input": _call()},
            {"type": "tool_use", "id": "toolu_b", "name": other, "input": {}},
        ],
        "tool_use",
        None,
    )
    fake.reply(both).reply_tool(DIAGRAM_TOOL, _call(), text=CONFIRMATION)

    result = _revise(topic, sync, fake)

    assert result.applied and result.attempts == 2 and len(_images(topic)) == 1


def test_a_diagram_whose_change_is_never_applied_is_retired(
    topic: ReviseTopic, sync: GitSync, monkeypatch: pytest.MonkeyPatch
) -> None:
    student = _notes(topic).replace("Se escribe $f'(x)$.", "Se escribe $f'(x)$ o $y'$.")
    original = revise_module.store_diagram

    async def drawing_while_the_student_saves(*args: Any, **kwargs: Any) -> DrawnDiagram:
        diagram = await original(*args, **kwargs)
        write_notes(topic.vault, topic.subject, topic.topic, student)
        return diagram

    monkeypatch.setattr(revise_module, "store_diagram", drawing_while_the_student_saves)
    fake = (
        FakeClaude()
        .reply_tool(DIAGRAM_TOOL, _call(), text=CONFIRMATION)
        .reply_text("Vale, lo dejo así.")
        .reply_text("Los apuntes se quedan como los has dejado.")
    )

    result = _revise(topic, sync, fake)

    assert not result.applied and result.crop is None
    assert _notes(topic) == student and _images(topic) == []


def test_a_student_save_citing_a_diagram_link_footnotes_it_as_a_diagram(
    topic: ReviseTopic,
) -> None:
    diagram = _run(store_diagram(topic.vault, topic.subject, topic.topic, SVG, TITLE))
    notes = _notes(topic).replace(
        "## 2. Próximo día", f"{diagram.markdown}\n\n## 2. Próximo día", 1
    )

    normalised = normalise_student_text(notes)

    assert FOOTNOTE in normalised.splitlines()


def test_the_revise_prompt_keeps_mermaid_and_offers_svg_for_the_rest() -> None:
    revise = load_prompt("editor_revise").content
    assert f"`{DIAGRAM_TOOL}`" in revise and "Diagrama N" in revise
    assert "mermaid" in revise
    requests = load_prompt("observer_requests").content
    assert "hazme un dibujo del triángulo con sus alturas" in requests
