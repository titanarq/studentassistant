"""The eval set on disk: one directory per case, a recorded session and the student's reference.

    <[eval] path>/
      <case>/
        recording/               a session recording as `serve --record` writes it
                                 (`server/recording.py`), read with `read_recording`
        reference/
          notes.md               required: the notes the student means (Markdown, free form)
          pages/<capture_id>.md  optional: what each captured page really says
          sections.yaml          optional: `sections: [{title, segments: [<segment_id>...]}]`,
                                 the section each transcript segment belongs to (the
                                 `segment_id` of the recording's finals; `server-<n>` for a
                                 server-mode recording)
          requests.yaml          optional: `requests: [{kind, segments: [<segment_id>...],
                                 note?}]`, the requests to the assistant the student spoke
                                 (`kind` one of `observer.REQUEST_KINDS`)
          triage.yaml            optional: `captures: [{capture_id, status, reasons?, note?}]`,
                                 the triage each capture should get (`status` kept, flagged or
                                 set_aside; `reasons` among `sources.triage.TRIAGE_REASONS`)
      runs/                      what `eval run` writes; never a case

Every reference file is checked against the recording up front (`read_case`): a page for a
capture the recording does not have, or a segment that is not one of its finals, is an
`EvalSetError`, so a run never starts on a case it would score wrongly; so is a request of an
unknown kind; so is a triage entry for a capture the recording lacks, or listed twice.

A reference `eval import-session` wrote and nobody corrected yet starts with `DRAFT_MARKER` (an
HTML comment in Markdown, a YAML comment in `triage.yaml`). HTML comments are never part of a
reference text (they are removed on read), and the files still marked are listed in
`EvalCase.drafts`, which the report shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.observer import RequestKind
from studentassistant.protocol import TranscriptClientFinal
from studentassistant.server.recording import Recording, RecordingError, read_recording
from studentassistant.sources.triage import TriageReason, TriageStatus

CASE_RECORDING_DIR = "recording"
CASE_REFERENCE_DIR = "reference"
REFERENCE_NOTES_FILE = "notes.md"
REFERENCE_PAGES_DIR = "pages"
REFERENCE_SECTIONS_FILE = "sections.yaml"
REFERENCE_REQUESTS_FILE = "requests.yaml"
REFERENCE_TRIAGE_FILE = "triage.yaml"
DRAFT_MARKER = "borrador sin corregir"
"""Written by `eval import-session` at the top of each reference stub; the student removes it."""

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
RUNS_DIR_NAME = "runs"


class EvalSetError(ValueError):
    """An eval set or one of its cases that cannot be read or does not match its recording."""


class ReferenceSection(BaseModel):
    """One section of the reference outline and the transcript segments that belong to it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1)
    segments: tuple[str, ...] = ()


class _SectionsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: list[ReferenceSection]


class ReferenceRequest(BaseModel):
    """One request to the assistant the student spoke: its kind and the finals that say it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RequestKind
    segments: tuple[str, ...] = Field(min_length=1)
    # What the student meant, for the report (free Spanish text).
    note: str | None = None


class _RequestsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: list[ReferenceRequest]


class ReferenceTriage(BaseModel):
    """The triage one capture should get: kept, flagged or set aside, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_id: str = Field(min_length=1)
    status: TriageStatus
    reasons: tuple[TriageReason, ...] = ()
    # Free Spanish text for the report (what the page is, why it goes).
    note: str | None = None


class _TriageFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captures: list[ReferenceTriage]


@dataclass(frozen=True)
class EvalCase:
    """One validated case: its recording and every reference the student wrote for it."""

    name: str
    directory: Path
    recording: Recording
    reference_notes: str
    # capture id -> the page's reference text.
    reference_pages: dict[str, str]
    # `None` when the case has no `sections.yaml`: its observer score is then not computed.
    reference_sections: tuple[ReferenceSection, ...] | None
    # `None` when the case has no `requests.yaml`: its request detection is then not scored.
    reference_requests: tuple[ReferenceRequest, ...] | None = None
    # `None` when the case has no `triage.yaml`: its capture triage is then not scored.
    reference_triage: tuple[ReferenceTriage, ...] | None = None
    # The reference files (relative to `reference/`) still marked as an uncorrected draft.
    drafts: tuple[str, ...] = ()

    @property
    def finals(self) -> list[TranscriptClientFinal]:
        """The recording's final transcript segments, in the order they were sent."""
        return [m for m in self.recording.transcript if isinstance(m, TranscriptClientFinal)]


def read_eval_set(path: Path, names: list[str] | None = None) -> list[EvalCase]:
    """Every case under `path` (or only those in `names`), in name order.

    Raises:
        EvalSetError: when `path` is not a directory, a named case does not exist, or any case
            cannot be read (see `read_case`).
    """
    if not path.is_dir():
        raise EvalSetError(f"{path} is not a directory")
    available = sorted(
        entry.name
        for entry in path.iterdir()
        if entry.is_dir() and entry.name != RUNS_DIR_NAME and not entry.name.startswith(".")
    )
    if names:
        missing = [name for name in names if name not in available]
        if missing:
            raise EvalSetError(f"no case named {', '.join(missing)} in {path}")
        available = [name for name in available if name in names]
    return [read_case(path / name) for name in available]


def read_case(directory: Path) -> EvalCase:
    """Read and validate the case in `directory` against its recording.

    Raises:
        EvalSetError: when the recording is not readable, `reference/notes.md` is missing or
            empty, a reference page names a capture the recording lacks, or `sections.yaml` is
            malformed, names a segment that is not a final of the recording, or puts a segment in
            two sections, or `requests.yaml` is malformed, names an unknown kind or a segment that
            is not a final of the recording.
    """
    name = directory.name
    try:
        recording = read_recording(directory / CASE_RECORDING_DIR)
    except RecordingError as error:
        raise EvalSetError(f"case {name}: {error}") from error
    reference = directory / CASE_REFERENCE_DIR
    drafts: list[str] = []
    notes = _read_reference(reference, REFERENCE_NOTES_FILE, name, drafts)
    if notes is None or not notes.strip():
        raise EvalSetError(f"case {name}: {reference / REFERENCE_NOTES_FILE} is missing or empty")
    captures = {capture.metadata.capture_id for capture in recording.captures}
    pages: dict[str, str] = {}
    pages_dir = reference / REFERENCE_PAGES_DIR
    if pages_dir.is_dir():
        for page in sorted(pages_dir.glob("*.md")):
            if page.stem not in captures:
                raise EvalSetError(
                    f"case {name}: reference page {page.name} names no capture of the recording"
                )
            relative = f"{REFERENCE_PAGES_DIR}/{page.name}"
            pages[page.stem] = _read_reference(reference, relative, name, drafts) or ""
    sections = _read_sections(reference / REFERENCE_SECTIONS_FILE, name, recording)
    requests = _read_requests(reference / REFERENCE_REQUESTS_FILE, name, recording)
    triage = _read_triage(reference / REFERENCE_TRIAGE_FILE, name, captures)
    if triage is not None and _is_draft(_read_text(reference / REFERENCE_TRIAGE_FILE, name)):
        drafts.append(REFERENCE_TRIAGE_FILE)
    return EvalCase(
        name=name,
        directory=directory,
        recording=recording,
        reference_notes=notes,
        reference_pages=pages,
        reference_sections=sections,
        reference_requests=requests,
        reference_triage=triage,
        drafts=tuple(drafts),
    )


def _is_draft(text: str | None) -> bool:
    """Whether a reference file still starts with the import's draft marker."""
    if text is None:
        return False
    first = text.lstrip().split("\n", 1)[0]
    return DRAFT_MARKER in first


def _read_reference(reference: Path, relative: str, case: str, drafts: list[str]) -> str | None:
    """A Markdown reference without its HTML comments; noted in `drafts` when still a draft."""
    text = _read_text(reference / relative, case)
    if text is None:
        return None
    if _is_draft(text):
        drafts.append(relative)
    return _COMMENT.sub("", text)


def _read_text(path: Path, case: str) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise EvalSetError(f"case {case}: cannot read {path}: {error}") from error


def _read_sections(
    path: Path, case: str, recording: Recording
) -> tuple[ReferenceSection, ...] | None:
    text = _read_text(path, case)
    if text is None:
        return None
    try:
        parsed = _SectionsFile.model_validate(yaml.safe_load(text))
    except (yaml.YAMLError, ValidationError) as error:
        raise EvalSetError(f"case {case}: {path} is not a sections file: {error}") from error
    finals = _checked_finals(recording)
    seen: set[str] = set()
    for section in parsed.sections:
        for segment in section.segments:
            if finals is not None and segment not in finals:
                raise EvalSetError(
                    f"case {case}: section «{section.title}» names segment {segment},"
                    " which is not a final of the recording"
                )
            if segment in seen:
                raise EvalSetError(f"case {case}: segment {segment} is in two sections")
            seen.add(segment)
    return tuple(parsed.sections)


def _checked_finals(recording: Recording) -> set[str] | None:
    """The segment ids a reference may name, or `None` when they cannot be checked.

    A server-mode recording has no transcript of its own: its segment ids are the backend's
    (`server-<n>`), known only once replayed, so they are not checked.
    """
    if recording.manifest.stt_mode != "client":
        return None
    return {m.segment_id for m in recording.transcript if isinstance(m, TranscriptClientFinal)}


def _read_requests(
    path: Path, case: str, recording: Recording
) -> tuple[ReferenceRequest, ...] | None:
    text = _read_text(path, case)
    if text is None:
        return None
    try:
        parsed = _RequestsFile.model_validate(yaml.safe_load(text))
    except (yaml.YAMLError, ValidationError) as error:
        raise EvalSetError(f"case {case}: {path} is not a requests file: {error}") from error
    finals = _checked_finals(recording)
    for number, request in enumerate(parsed.requests, 1):
        for segment in request.segments:
            if finals is not None and segment not in finals:
                raise EvalSetError(
                    f"case {case}: request {number} ({request.kind}) names segment {segment},"
                    " which is not a final of the recording"
                )
    return tuple(parsed.requests)


def _read_triage(path: Path, case: str, captures: set[str]) -> tuple[ReferenceTriage, ...] | None:
    text = _read_text(path, case)
    if text is None:
        return None
    try:
        parsed = _TriageFile.model_validate(yaml.safe_load(text))
    except (yaml.YAMLError, ValidationError) as error:
        raise EvalSetError(f"case {case}: {path} is not a triage file: {error}") from error
    seen: set[str] = set()
    for entry in parsed.captures:
        if entry.capture_id not in captures:
            raise EvalSetError(
                f"case {case}: triage names capture {entry.capture_id},"
                " which the recording does not have"
            )
        if entry.capture_id in seen:
            raise EvalSetError(f"case {case}: capture {entry.capture_id} is in triage twice")
        seen.add(entry.capture_id)
    return tuple(parsed.captures)
