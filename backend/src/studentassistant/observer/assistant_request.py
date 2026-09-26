"""The `assistant.request` event: a request to the assistant detected in the transcript (#314).

Pure models (no I/O, no LLM), so consumers (the server's editor turns, #315) read the event through
`studentassistant.observer` without importing the llm module. The detector that publishes it is
`observer/requests.py` (origin `observer`, `detector: "observer"`); the wake word (#318) publishes
the same shape with origin `stt` and `detector: "wake_word"`. The fold ignores the kind.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ASSISTANT_REQUEST_KIND = "assistant.request"
"""The persisted session event of one detected request to the assistant."""

RequestKind = Literal["edit", "question", "prepare_notes"]
"""`edit`: change the notes; `question`: answer something; `prepare_notes`: "prepárame el tema"."""
REQUEST_KINDS: tuple[RequestKind, ...] = ("edit", "question", "prepare_notes")

RequestDetectorName = Literal["observer", "wake_word"]

SUMMARY_MAX_CHARS = 140
"""The longest `summary` of a request (a short Spanish line the chat shows)."""

SegmentId = Annotated[str, Field(min_length=1, max_length=200)]


class AssistantRequest(BaseModel):
    """The payload of an `assistant.request` event.

    `request_id` is `req-<n>` (the session's n-th request); `summary` is a short Spanish line of
    what was asked; `text` the raw transcript of the span, its finals joined with a space;
    `segment_ids` the span's finals in order; `t_start_ms`/`t_end_ms` its session times.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(pattern=r"^req-[1-9][0-9]*$")
    kind: RequestKind
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_CHARS)
    text: str
    segment_ids: list[SegmentId] = Field(min_length=1)
    t_start_ms: int = Field(ge=0)
    t_end_ms: int = Field(ge=0)
    detector: RequestDetectorName

    @model_validator(mode="after")
    def check_span(self) -> AssistantRequest:
        if self.t_end_ms < self.t_start_ms:
            raise ValueError("t_end_ms is before t_start_ms")
        if len(set(self.segment_ids)) != len(self.segment_ids):
            raise ValueError("segment_ids repeats a segment")
        return self


__all__ = [
    "ASSISTANT_REQUEST_KIND",
    "REQUEST_KINDS",
    "SUMMARY_MAX_CHARS",
    "AssistantRequest",
    "RequestDetectorName",
    "RequestKind",
]
