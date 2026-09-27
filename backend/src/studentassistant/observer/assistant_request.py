"""The `assistant.request` event: a request to the assistant, spoken or typed (#314, #327).

Pure models (no I/O, no LLM), so consumers (the server's editor turns, #315) read the event through
`studentassistant.observer` without importing the llm module. The detector that publishes it is
`observer/requests.py` (origin `observer`, `detector: "observer"`); the wake word (#318) publishes
the same shape with origin `stt` and `detector: "wake_word"`; a message typed in the workspace chat
is classified by the same prompt and written with origin `user` and `detector: "typed"` (#327).
The fold ignores the kind.

Also here: the context the detector gives Sonnet about the topic (`RequestContext`: its sources
with their states and the doubt asked in the chat now), which the server builds through an
injected `SourcesLookup` so the observer never imports the editor.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ASSISTANT_REQUEST_KIND = "assistant.request"
"""The persisted session event of one request to the assistant."""

RequestKind = Literal[
    "edit",
    "question",
    "prepare_notes",
    "incorporate",
    "set_aside",
    "restore",
    "doubt_answer",
    "study",
]
"""`edit`: change the notes; `question`: answer something; `prepare_notes`: "prepárame el tema";
`incorporate`: incorporate the `targets` into the notes; `set_aside` / `restore`: set the
`targets` (captured pages) aside or restore them; `doubt_answer`: the student's `answer` to the
doubt `pending_id` asked in the chat (#327); `study`: "ya está, quiero estudiar", end the capture
and switch the topic to Estudiar (#335)."""
REQUEST_KINDS: tuple[RequestKind, ...] = (
    "edit",
    "question",
    "prepare_notes",
    "incorporate",
    "set_aside",
    "restore",
    "doubt_answer",
    "study",
)
TARGET_KINDS: frozenset[str] = frozenset({"incorporate", "set_aside", "restore"})
"""The kinds whose request names its sources in `targets`."""

RequestDetectorName = Literal["observer", "wake_word", "typed"]

SUMMARY_MAX_CHARS = 140
"""The longest `summary` of a request (a short Spanish line the chat shows)."""
ANSWER_MAX_CHARS = 2000
"""The longest `answer` of a `doubt_answer` (as `editor.doubts.DoubtAnswer` takes it)."""

SegmentId = Annotated[str, Field(min_length=1, max_length=200)]
SourceId = Annotated[str, Field(min_length=1, max_length=300)]

TYPED_REQUEST_PREFIX = "req-t"
"""A typed request's id is `req-t<n>` (the session's n-th typed one), apart from the detector's
`req-<n>`, so both can write in one live session without a shared counter."""


class AssistantRequest(BaseModel):
    """The payload of an `assistant.request` event.

    `request_id` is `req-<n>` (the session's n-th spoken request) or `req-t<n>` (its n-th typed
    one); `summary` is a short Spanish line of what was asked; `text` the raw transcript of the
    span, its finals joined with a space (a typed request: the whole message); `segment_ids` the
    span's finals in order (empty for a typed request); `t_start_ms`/`t_end_ms` its session times.
    `targets` are the topic-relative source ids an `incorporate`/`set_aside`/`restore` acts on;
    `pending_id` and `answer` the doubt a `doubt_answer` answers and what the student said (the
    text, or a suggestion's number). `message_id` ties a typed request to its message.
    `selected_source_ids` are the sources the student had selected in Recursos when typing the
    message (#433; topic-relative, a PDF page as `<pdf>#page=K`), in their order: the referent of
    «esto» / «estas páginas». Events written before #327 or #433 read unchanged.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(pattern=r"^req-t?[1-9][0-9]*$")
    kind: RequestKind
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_CHARS)
    text: str
    segment_ids: list[SegmentId] = Field(default_factory=list)
    t_start_ms: int = Field(ge=0)
    t_end_ms: int = Field(ge=0)
    detector: RequestDetectorName
    targets: list[SourceId] = Field(default_factory=list)
    pending_id: str | None = Field(default=None, min_length=1, max_length=200)
    answer: str | None = Field(default=None, min_length=1, max_length=ANSWER_MAX_CHARS)
    message_id: str | None = Field(default=None, pattern=r"^msg-[0-9a-f]{8,32}$")
    selected_source_ids: list[SourceId] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_span(self) -> AssistantRequest:
        if self.t_end_ms < self.t_start_ms:
            raise ValueError("t_end_ms is before t_start_ms")
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("segment_ids repeats a segment")
        if self.detector != "typed" and not self.segment_ids:
            raise ValueError("a spoken request needs its segment_ids")
        if self.kind in TARGET_KINDS and not self.targets:
            raise ValueError(f"a {self.kind} request needs its targets")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets repeats a source")
        if self.kind == "doubt_answer" and (self.pending_id is None or self.answer is None):
            raise ValueError("a doubt_answer needs its pending_id and answer")
        return self

    @property
    def typed(self) -> bool:
        return self.detector == "typed"

    def payload(self) -> dict[str, Any]:
        """The event payload: the fields a request of its kind uses (defaults left out)."""
        return self.model_dump(mode="json", exclude_defaults=True)


# -- what the detector is told about the topic ---------------------------------------------------

SourceState = Literal["pendiente", "incorporada", "apartada"]

KIND_NAMES: dict[str, str] = {
    "notes": "apuntes",
    "book": "libro",
    "pdf": "PDF",
    "web": "web",
    "images": "imagen pegada",
}
CAPTURE_KINDS: frozenset[str] = frozenset({"notes", "book"})
"""The source kinds a student can set aside or restore (captured pages, #324)."""


class RequestSource(BaseModel):
    """One stored source of the topic, as the detector sees it (from `editor.source_status`)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    source_id: str = Field(description="Topic-relative: `sources/notes/page-003.jpg`.")
    kind: str
    number: int | None = None
    label: str = Field(description="How the student names it: «la página 3».")
    state: SourceState
    reason: str | None = None

    def line(self) -> str:
        """`<id>: página 3 (apuntes) -- apartada: borrosa`."""
        kind = KIND_NAMES.get(self.kind, self.kind)
        if self.kind in CAPTURE_KINDS and self.number is not None:
            name = f"página {self.number} ({kind})"
        else:
            name = f"{self.label.removeprefix('la ').removeprefix('el ')} ({kind})"
        state = (
            self.state
            if self.state != "apartada" or not self.reason
            else (f"apartada: {self.reason}")
        )
        return f"{self.source_id}: {name} -- {state}"


class AskedDoubtRef(BaseModel):
    """The doubt the workspace chat is asking now (one at a time, #325)."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    pending_id: str
    question: str
    suggestions: list[str] = Field(default_factory=list)


PAGE_FRAGMENT = "#page="
"""A PDF page's id is `<pdf source id>#page=K`."""


def base_source_id(source_id: str) -> str:
    """The stored source an id names: `sources/pdf/doc.pdf#page=3` -> `sources/pdf/doc.pdf`."""
    return source_id.partition("#")[0]


class RequestContext(BaseModel):
    """The topic's sources and the doubt asked now: the uncached context of a detector call.

    `selected` is the student's Recursos selection of a typed message (#433), in order; `None`
    for a spoken request, which has no selection (no block is rendered then).
    """

    model_config = ConfigDict(frozen=True)

    sources: list[RequestSource] = Field(default_factory=list)
    doubt: AskedDoubtRef | None = None
    selected: list[str] | None = None

    def source(self, source_id: str) -> RequestSource | None:
        """The stored source `source_id` names (a PDF page's id names its PDF)."""
        base = base_source_id(source_id)
        return next((s for s in self.sources if s.source_id == base), None)

    def is_selected(self, source_id: str) -> bool:
        """Whether the selection holds `source_id`'s stored source (or one of its pages)."""
        base = base_source_id(source_id)
        return any(base_source_id(selected) == base for selected in self.selected or ())

    def _selected_line(self, source_id: str) -> str:
        source = self.source(source_id)
        if source is None:  # the route checked it; a source removed meanwhile
            return f"{source_id}: (no longer a source of the topic)"
        _, _, page = source_id.partition(PAGE_FRAGMENT)
        if page:
            return f"{source_id}: page {page} of {source.line()}"
        return source.line()

    def render(self) -> str:
        """The context block of a call (English frame, the student's Spanish names)."""
        lines = ["Sources of the topic (id: name -- state), in order:"]
        if self.sources:
            lines += [f"- {source.line()}" for source in self.sources]
        else:
            lines.append("(none yet)")
        lines.append("")
        doubt = self.doubt
        if doubt is None:
            lines.append("Doubt asked in the chat now: none.")
        else:
            lines.append(f"Doubt asked in the chat now: {doubt.pending_id} «{doubt.question}»")
            for number, suggestion in enumerate(doubt.suggestions, start=1):
                lines.append(f"  suggestion {number}: {suggestion}")
        if self.selected is not None:
            lines.append("")
            if self.selected:
                lines.append("Selected by the student now (in Recursos), in order:")
                lines += [f"- {self._selected_line(source_id)}" for source_id in self.selected]
            else:
                lines.append("Selected by the student now (in Recursos): nothing.")
        return "\n".join(lines)


SourcesLookup = Callable[[str, str], Awaitable[RequestContext]]
"""`(subject_slug, topic_slug) -> RequestContext`, injected by the server (the editor's
`source_status` and the doubt asked in the chat), so the observer never imports the editor."""


__all__ = [
    "ANSWER_MAX_CHARS",
    "ASSISTANT_REQUEST_KIND",
    "CAPTURE_KINDS",
    "PAGE_FRAGMENT",
    "REQUEST_KINDS",
    "SUMMARY_MAX_CHARS",
    "TARGET_KINDS",
    "TYPED_REQUEST_PREFIX",
    "AskedDoubtRef",
    "AssistantRequest",
    "RequestContext",
    "RequestDetectorName",
    "RequestKind",
    "RequestSource",
    "SourceState",
    "SourcesLookup",
    "base_source_id",
]
