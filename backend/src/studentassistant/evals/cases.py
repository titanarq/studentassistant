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
      runs/                      what `eval run` writes; never a case

Every reference file is checked against the recording up front (`read_case`): a page for a
capture the recording does not have, or a segment that is not one of its finals, is an
`EvalSetError`, so a run never starts on a case it would score wrongly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studentassistant.protocol import TranscriptClientFinal
from studentassistant.server.recording import Recording, RecordingError, read_recording

CASE_RECORDING_DIR = "recording"
CASE_REFERENCE_DIR = "reference"
REFERENCE_NOTES_FILE = "notes.md"
REFERENCE_PAGES_DIR = "pages"
REFERENCE_SECTIONS_FILE = "sections.yaml"
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
            two sections.
    """
    name = directory.name
    try:
        recording = read_recording(directory / CASE_RECORDING_DIR)
    except RecordingError as error:
        raise EvalSetError(f"case {name}: {error}") from error
    reference = directory / CASE_REFERENCE_DIR
    notes = _read_text(reference / REFERENCE_NOTES_FILE, name)
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
            pages[page.stem] = _read_text(page, name) or ""
    sections = _read_sections(reference / REFERENCE_SECTIONS_FILE, name, recording)
    return EvalCase(
        name=name,
        directory=directory,
        recording=recording,
        reference_notes=notes,
        reference_pages=pages,
        reference_sections=sections,
    )


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
    # A server-mode recording has no transcript of its own: its segment ids are the backend's
    # (`server-<n>`), known only once replayed, so they are not checked here.
    check = recording.manifest.stt_mode == "client"
    finals = {m.segment_id for m in recording.transcript if isinstance(m, TranscriptClientFinal)}
    seen: set[str] = set()
    for section in parsed.sections:
        for segment in section.segments:
            if check and segment not in finals:
                raise EvalSetError(
                    f"case {case}: section «{section.title}» names segment {segment},"
                    " which is not a final of the recording"
                )
            if segment in seen:
                raise EvalSetError(f"case {case}: segment {segment} is in two sections")
            seen.add(segment)
    return tuple(parsed.sections)
