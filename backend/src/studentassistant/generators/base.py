"""The `Generator` interface and what a generator is given and gives back.

A generator turns a topic's master notes (`notes/apuntes.md`) into study material. It is a
subclass of `Generator` with a `kind` (the slug that names it in the registry, the CLI and the API,
and that every file it writes under `generated/` starts with), a Spanish `title`, a `version`
(bumped when its output changes shape, which makes older artifacts stale) and an `options_model`
(the Pydantic model of the options it takes, `NoOptions` by default).

`generate(context)` receives a `GeneratorContext` -- the notes as text and parsed, their section
anchors, the notes version they are, the validated options and an LLM helper bound to the
`generator` role and the topic's cost ledger -- and returns a `GeneratorOutput`: the files to
write (names relative to `generated/`), the provenance of every item (which note anchors it was
built from, ADR-0005), the text of every item to check against those sections and any Spanish
warnings. It never writes the vault: storing, stale
detection and committing are the framework's (`generators.run`).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.editor.notes_format import NotesDocument
from studentassistant.llm import LLMClient, StructuredResult, structured
from studentassistant.vault import ConversationRecord, Vault, append_conversation_record

logger = logging.getLogger(__name__)

CONVERSATION_NAME = "generator"
"""The topic's conversation file (`conversations/generator.jsonl`) every generator call goes to."""

Clock = Callable[[], datetime]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoOptions(_Strict):
    """The options of a generator that takes none."""


class NotesBasis(_Strict):
    """Which notes an artifact was built from."""

    sha256: str = Field(description="SHA-256 of the `apuntes.md` text the generator read.")
    version: int | None = Field(
        default=None, description="`N` of the newest `apuntes-vN` tag then; none before the first."
    )
    tag: str | None = None
    changed_since_version: bool = Field(
        default=False,
        description="`apuntes.md` differed from that version (a revision not tagged yet).",
    )


class ItemProvenance(_Strict):
    """One item of an artifact (a node, a question, a card) and the note anchors it came from."""

    item: str = Field(min_length=1, description="The generator's id of the item within the kind.")
    anchors: list[str] = Field(default_factory=list, description="Section anchors, no `#`.")


class NoteSection(_Strict):
    """A section of the notes a generator may cite: its anchor, title and heading level."""

    anchor: str
    title: str
    level: int


@dataclass
class GeneratorOutput:
    """What one generation produced.

    `files` maps names relative to `generated/` to their content (text is written as UTF-8); every
    name starts with the generator's kind followed by `.`, `-` or `/`. `items` is the provenance
    of every item; an item with no anchor, or one the notes do not have, is kept and reported.
    `item_texts` maps an item's id to the text of it that must come from the notes it cites (a
    quiz answer and explanation, a flashcard's back...); the framework checks it against those
    sections (`generators.grounding`) and reports, never drops, what they do not hold.
    """

    files: dict[str, str | bytes]
    items: list[ItemProvenance] = field(default_factory=list)
    item_texts: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_hash: str | None = None


@dataclass
class GeneratorContext:
    """Everything one generation reads; built by the framework, read-only for the generator."""

    vault: Vault
    subject: str
    topic: str
    topic_title: str
    notes_text: str
    notes: NotesDocument
    basis: NotesBasis
    options: BaseModel
    client: LLMClient
    kind: str
    confirm_over_cap: bool = False
    clock: Clock | None = None

    @property
    def sections(self) -> list[NoteSection]:
        """The notes' anchored sections, in document order."""
        return [
            NoteSection(anchor=s.anchor, title=s.heading.title, level=s.heading.level)
            for s in self.notes.sections
            if s.anchor is not None
        ]

    @property
    def anchors(self) -> list[str]:
        return [section.anchor for section in self.sections]

    def notes_block(self) -> dict[str, Any]:
        """A text content block with the notes, their version and the anchors to cite."""
        version = f"apuntes v{self.basis.version}" if self.basis.version else "sin versión"
        anchors = ", ".join(f"#{anchor}" for anchor in self.anchors) or "(ninguna)"
        text = (
            f"Apuntes del tema «{self.topic_title}» ({version}).\n"
            f"Anclas de sección que puedes citar: {anchors}\n\n"
            f"<apuntes>\n{self.notes_text}</apuntes>"
        )
        return {"type": "text", "text": text}

    async def record(self, kind: str, **fields: Any) -> None:
        """Append a record to `conversations/generator.jsonl`; a failure is only logged."""
        now = self.clock() if self.clock is not None else datetime.now(UTC)
        detail = {"kind": self.kind, **(fields.pop("detail", None) or {})}
        entry = ConversationRecord(time=now, kind=kind, detail=detail, **fields)
        try:
            await asyncio.to_thread(
                append_conversation_record,
                self.vault,
                self.subject,
                self.topic,
                CONVERSATION_NAME,
                entry,
            )
        except Exception:
            logger.exception(
                "could not record the generator conversation of %s/%s", self.subject, self.topic
            )

    async def structured[T: BaseModel](
        self,
        messages: list[dict[str, Any]],
        output: type[T],
        *,
        tool_name: str,
        tool_description: str,
        system: str | list[str] | list[dict[str, Any]] | None = None,
        prompt_hash: str | None = None,
        max_tokens: int | None = None,
    ) -> StructuredResult[T]:
        """`llm.structured` on this generation's client, recorded in the conversation file.

        Raises what `llm.structured` raises (`CostConfirmationRequiredError`, `RefusalError`,
        `StructuredOutputError`, `LLMError`).
        """
        result = await structured(
            self.client,
            messages,
            output,
            tool_name=tool_name,
            tool_description=tool_description,
            system=system,
            max_tokens=max_tokens,
            prompt_hash=prompt_hash,
            confirm_over_cap=self.confirm_over_cap,
        )
        for message in messages:
            await self.record("user", message=message, model=self.client.model)
        for response in result.responses:
            await self.record(
                "assistant",
                message=response.assistant_turn(),
                model=response.model or self.client.model,
                prompt_hash=prompt_hash,
                usage=response.usage.model_dump(),
            )
        return result


class Generator(ABC):
    """One kind of study material. Subclasses set the class attributes and `generate`."""

    kind: ClassVar[str]
    """Slug (`[a-z0-9-]`): the registry key, the CLI/API name and the stem of its files."""
    title: ClassVar[str]
    """Spanish, for the student (`Esquema`, `Quiz`...)."""
    description: ClassVar[str] = ""
    version: ClassVar[int] = 1
    """Bumped when the output changes shape: artifacts of an older version are stale."""
    options_model: ClassVar[type[BaseModel]] = NoOptions

    @abstractmethod
    async def generate(self, context: GeneratorContext) -> GeneratorOutput:
        """Build the artifact from `context`; never writes the vault."""
