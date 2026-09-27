"""What replaying a case will cost, estimated before any Claude call is made.

A deliberately rough, generous estimate from the recording alone: how many calls each role makes
and how many tokens go in and out, priced with `[llm.prices]` at the uncached input price (prompt
caching only makes the real run cheaper). The constants below are the whole model of it; the real
cost of a run is read from its ledger afterwards and reported next to this.

- observer: one call per `[observer] batch_segments` finals, plus the end-of-session flush; each
  sends its prompt and the transcript so far (capped at `context_max_tokens`).
- transcriber: one call per stored capture, `IMAGES_PER_PAGE` images each; out, the page's text.
- editor: one "prepárame el tema", sending the whole transcript and every page; out, notes about
  the length of the reference notes, plus its thinking; then the search for contradictions
  between the sources (#65), the same input again with its own prompt and a short answer.
  With `[eval] notes_path = "chat"` there is no such generation: instead one incorporation or
  edit turn per editor request the session holds (`requests.yaml`'s requests of `EDITOR_KINDS`;
  without it, one per `[editor] incorporate_batch_size` pages, at least one), each sending the
  `editor_incorporate` prompt, the notes so far and its share of the sources.
- request detector (role observer, only when the run's `request_detection` is `observer`; the
  wake word makes no call): one call per final (generous: the debounce often coalesces them),
  each sending the `observer_requests` prompt, `REQUEST_CONTEXT_TOKENS` of topic context and the
  window of the newest `request_window_segments` finals.

The run's settings are `run_settings`: `[eval] request_detection` applied over `[observer]`.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, computed_field

from studentassistant.config import ObserverSettings, Settings
from studentassistant.evals.cases import EvalCase
from studentassistant.llm import load_prompt
from studentassistant.protocol import TranscriptClientFinal

# Spanish text runs at about 3.5 characters per token.
CHARS_PER_TOKEN = 3.5
# A server-mode recording has no text yet: about four tokens per second of speech.
AUDIO_TOKENS_PER_SECOND = 4.0
# A page image as the API bills it (about 1.15 megapixels), and the images sent per page.
IMAGE_TOKENS = 1_600
IMAGES_PER_PAGE = 2
# What each role writes per call beyond its visible answer (thinking, tool-call overhead).
OBSERVER_OUTPUT_TOKENS = 1_000
TRANSCRIBER_THINKING_TOKENS = 1_000
EDITOR_THINKING_TOKENS = 8_000
CONTRADICTIONS_OUTPUT_TOKENS = 3_000
# A page with no reference text: about a full handwritten page.
PAGE_TOKENS_DEFAULT = 600
# The editor's context beyond sources: outline, pending queue, style guide, digest.
EDITOR_CONTEXT_TOKENS = 3_000

# The request detector: its answer (the tool call, usually an empty list) and the topic's
# sources with their states it is told about.
REQUEST_OUTPUT_TOKENS = 300
REQUEST_CONTEXT_TOKENS = 500
# The request kinds that make an editor call on the chat path.
EDITOR_KINDS = frozenset({"edit", "question", "prepare_notes", "incorporate", "doubt_answer"})

OBSERVER_PROMPTS = ("observer", "structured-output")
REQUESTS_PROMPT = "observer_requests"
INCORPORATE_PROMPT = "editor_incorporate"
TRANSCRIBER_PROMPT = "page_transcription"
EDITOR_PROMPT = "editor_generate"
CONTRADICTIONS_PROMPT = "editor_contradictions"


class RoleEstimate(BaseModel):
    role: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    # `None` when `[llm.prices]` has no price for the model.
    usd: float | None


class CaseEstimate(BaseModel):
    case: str
    roles: list[RoleEstimate]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def usd(self) -> float:
        """The priced roles' total; see `unpriced` for the models left out."""
        return sum(role.usd or 0.0 for role in self.roles)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unpriced(self) -> list[str]:
        return sorted({role.model for role in self.roles if role.usd is None})


def tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _prompt_tokens(*names: str) -> int:
    return sum(tokens(load_prompt(name).content) for name in names)


def _price(
    settings: Settings, role: str, model: str, calls: int, tin: int, tout: int
) -> RoleEstimate:
    price = settings.llm.prices.get(model)
    usd = None
    if price is not None:
        usd = round((tin * price.input_per_mtok + tout * price.output_per_mtok) / 1_000_000, 4)
    return RoleEstimate(
        role=role, model=model, calls=calls, input_tokens=tin, output_tokens=tout, usd=usd
    )


def run_settings(settings: Settings) -> Settings:
    """`settings` as an eval run uses them: `[eval] request_detection`, when set, replaces
    `[observer] request_detection`."""
    detection = settings.eval.request_detection
    if detection is None or detection == settings.observer.request_detection:
        return settings
    observer: ObserverSettings = settings.observer.model_copy(
        update={"request_detection": detection}
    )
    return settings.model_copy(update={"observer": observer})


def estimate_case(case: EvalCase, settings: Settings) -> CaseEstimate:
    """The estimated calls, tokens and USD of replaying `case` with `settings`."""
    settings = run_settings(settings)
    recording = case.recording
    finals = [m for m in recording.transcript if isinstance(m, TranscriptClientFinal)]
    if recording.manifest.stt_mode == "client":
        transcript = sum(tokens(m.text) for m in finals)
        segments = len(finals)
    else:
        audio = recording.audio_path.stat().st_size if recording.audio_path else 0
        seconds = audio / (16_000 * 2)
        transcript = math.ceil(seconds * AUDIO_TOKENS_PER_SECOND)
        segments = math.ceil(seconds / 5)  # one final every five seconds or so
    roles = settings.llm.roles
    estimates: list[RoleEstimate] = []

    if settings.observer.enabled:
        calls = math.ceil(segments / settings.observer.batch_segments) + 1
        prompt = _prompt_tokens(*OBSERVER_PROMPTS)
        cap = settings.observer.context_max_tokens
        tin = sum(prompt + min(cap, math.ceil(transcript * i / calls)) for i in range(1, calls + 1))
        tout = calls * OBSERVER_OUTPUT_TOKENS
        estimates.append(_price(settings, "observer", roles.observer.model, calls, tin, tout))
        if settings.observer.request_detection == "observer" and segments:
            window = settings.observer.request_window_segments
            per_segment = transcript / segments
            prompt = _prompt_tokens(REQUESTS_PROMPT) + REQUEST_CONTEXT_TOKENS
            tin = sum(
                prompt + math.ceil(per_segment * min(window, i)) for i in range(1, segments + 1)
            )
            tout = segments * REQUEST_OUTPUT_TOKENS
            estimates.append(
                _price(settings, "observer-requests", roles.observer.model, segments, tin, tout)
            )

    pages = len(recording.captures)
    page_tokens = [
        tokens(case.reference_pages[c.metadata.capture_id])
        if c.metadata.capture_id in case.reference_pages
        else PAGE_TOKENS_DEFAULT
        for c in recording.captures
    ]
    if pages and settings.sources.transcription_enabled:
        prompt = _prompt_tokens(TRANSCRIBER_PROMPT)
        tin = pages * (prompt + IMAGES_PER_PAGE * IMAGE_TOKENS)
        tout = sum(page_tokens) + pages * TRANSCRIBER_THINKING_TOKENS
        estimates.append(_price(settings, "transcriber", roles.transcriber.model, pages, tin, tout))

    topic = transcript + sum(page_tokens) + EDITOR_CONTEXT_TOKENS
    notes_out = math.ceil(tokens(case.reference_notes) * 1.5)
    if settings.eval.notes_path == "chat":
        if case.reference_requests is not None:
            turns = sum(r.kind in EDITOR_KINDS for r in case.reference_requests)
        else:
            turns = max(1, math.ceil(pages / settings.editor.incorporate_batch_size))
        if turns:
            # Each turn: its share of the sources, the whole notes in, and the notes out.
            share = math.ceil((transcript + sum(page_tokens)) / turns)
            prompt = _prompt_tokens(INCORPORATE_PROMPT) + EDITOR_CONTEXT_TOKENS
            tin = turns * (prompt + share + notes_out)
            tout = turns * (notes_out + EDITOR_THINKING_TOKENS)
            estimates.append(_price(settings, "editor", roles.editor.model, turns, tin, tout))
        return CaseEstimate(case=case.name, roles=estimates)
    tin = _prompt_tokens(EDITOR_PROMPT) + topic
    tin += _prompt_tokens(CONTRADICTIONS_PROMPT) + topic + notes_out  # the notes are sent too
    tout = notes_out + EDITOR_THINKING_TOKENS + CONTRADICTIONS_OUTPUT_TOKENS
    estimates.append(_price(settings, "editor", roles.editor.model, 2, tin, tout))
    return CaseEstimate(case=case.name, roles=estimates)
