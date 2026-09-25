"""An eval case built from the sample recording, and Claude scripted per role for replaying it."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from studentassistant.editor.notes_format import page_provenance, transcript_provenance
from studentassistant.llm import FakeClaude, LLMRequest, LLMResponse
from studentassistant.observer.live import TOOL_NAME

_SEGMENT = re.compile(r'"kind":"transcript.final".*?"segment_id":"([^"]+)"')
SAMPLE = Path(__file__).parent / "fixtures" / "sessions" / "sample"
CAPTURE_ID = "5f0c2a7e-8d4b-4e61-9b3a-2c7d1e0f4a58"
REFERENCE_PAGE = "# La célula\n\n- membrana, citoplasma y núcleo\n"
# The transcriber gets one word wrong and marks one as unreadable.
PAGE_TRANSCRIPTION = "# La célula\n\n- membrana, [[?citoplasma]] y nucleos\n"
REFERENCE_NOTES = (
    "# La célula\n"
    "\n"
    "## Definición\n"
    "\n"
    "La célula es la unidad básica de los seres vivos.\n"
    "\n"
    "## Partes\n"
    "\n"
    "- Tiene tres partes: membrana, citoplasma y núcleo.\n"
    "- El núcleo guarda el material genético.\n"
)
REFERENCE_SECTIONS = (
    "sections:\n"
    "  - title: Definición\n"
    "    segments: [seg-1]\n"
    "  - title: Partes\n"
    "    segments: [seg-2, seg-3]\n"
)


def make_case(root: Path, name: str = "celula") -> Path:
    """A case directory under `root`: the sample recording plus a full reference."""
    case = root / name
    shutil.copytree(SAMPLE, case / "recording")
    reference = case / "reference"
    (reference / "pages").mkdir(parents=True)
    (reference / "notes.md").write_text(REFERENCE_NOTES, encoding="utf-8")
    (reference / "pages" / f"{CAPTURE_ID}.md").write_text(REFERENCE_PAGE, encoding="utf-8")
    (reference / "sections.yaml").write_text(REFERENCE_SECTIONS, encoding="utf-8")
    return case


def generated_notes(session_id: str) -> str:
    """Notes that keep two of the three reference ideas and add one from nowhere."""
    page = page_provenance("book", 1)
    first = transcript_provenance(session_id, 0, 4)
    second = transcript_provenance(session_id, 4, 8)
    return (
        "# La célula\n"
        "\n"
        "## 1. Definición {#definicion}\n"
        "\n"
        "La célula es la unidad básica de los seres vivos.[^t1]\n"
        "\n"
        "## 2. Partes {#partes}\n"
        "\n"
        "- Membrana, citoplasma y núcleo.[^t2][^p1]\n"
        "- Las mitocondrias producen energía mediante respiración celular.[^t2]\n"
        "\n"
        f"{page.definition('p1')}\n"
        f"{first.definition('t1')}\n"
        f"{second.definition('t2')}\n"
    )


class PipelineClaude:
    """A transport scripting each role for one replay of the sample case.

    The observer puts every segment stored so far in one section; the transcriber answers the
    page; the editor answers `generated_notes` for the one session it finds under `runs`, which
    only exists once the replay started it.
    """

    def __init__(self, runs: Path) -> None:
        self.runs = runs
        self.observer = FakeClaude()
        self.assigned: set[str] = set()
        self.transcriber = FakeClaude()
        self.editor = FakeClaude()

    async def send(self, request: LLMRequest, **options: object) -> LLMResponse:
        if request.role == "observer":
            self.observer.reply_tool(TOOL_NAME, {"ops": self._observer_ops()})
            return await self.observer.send(request, **options)  # type: ignore[arg-type]
        if request.role == "transcriber":
            self.transcriber.reply_text(PAGE_TRANSCRIPTION)
            return await self.transcriber.send(request, **options)  # type: ignore[arg-type]
        sessions = sorted(self.runs.glob("**/sessions/*/events.jsonl"))
        self.editor.reply_text(generated_notes(sessions[-1].parent.name))
        return await self.editor.send(request, **options)  # type: ignore[arg-type]

    def _observer_ops(self) -> list[dict[str, object]]:
        """Add the section once, then assign it every segment stored since the last call."""
        stored: list[str] = []
        for log in self.runs.glob("**/sessions/*/events.jsonl"):
            stored += _SEGMENT.findall(log.read_text(encoding="utf-8"))
        new = [segment for segment in dict.fromkeys(stored) if segment not in self.assigned]
        ops: list[dict[str, object]] = []
        if not self.observer.requests:
            ops.append({"op": "add_section", "section_id": "sec-1", "title": "La célula"})
        if new:
            ops.append({"op": "assign_segments", "section_id": "sec-1", "segment_ids": new})
            self.assigned.update(new)
        return ops
