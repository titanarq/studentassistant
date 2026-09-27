"""Run the eval set: replay each case into a fresh vault, generate its notes, score, report.

One case runs exactly as `tests/server/test_pipeline_e2e.py` wires the pipeline, with the real
Claude transport instead of fakes: an in-process `create_app` (lifespan included) over a vault
of the case's own under the run directory, the recording fed through `replay` (the session
WebSocket, the capture upload, the page transcriber, the live observer and the request detector),
then "prepárame el tema" over REST -- or, with `[eval] notes_path = "chat"`, no generation: the
notes scored are the ones the session's own requests built, once the turn queue has drained. The
detector under test is `[eval] request_detection` (`estimate.run_settings`). What the pipeline
produced is then read back from that vault only through `studentassistant.vault` (and the
observer's loader) and scored with `scoring.py`.

    <[eval] path>/runs/<UTC time>/
      report.md        the Spanish report (`render_report`)
      report.json      the same, as `EvalReport` JSON: what a later run is compared with

The report carries the comparison with the most recent earlier run of the same eval set
(`compare.py`): per case and per score the previous value, the new one and the delta.
      <case>/vault/    the vault the case produced, kept to look into (it has no remote)

Nothing here touches the student's vault, index or paired devices: every path the app could
write to is under the run directory.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, computed_field

from studentassistant.config import ServerSettings, Settings, SttSettings, VaultSettings
from studentassistant.evals.cases import RUNS_DIR_NAME, EvalCase
from studentassistant.evals.compare import (
    REPORT_JSON,
    RunComparison,
    compare_reports,
    previous_report,
    render_comparison,
)
from studentassistant.evals.estimate import CaseEstimate, estimate_case, run_settings
from studentassistant.evals.scoring import (
    NotesFidelity,
    PageScore,
    RequestItem,
    RequestScore,
    SectionScore,
    score_notes,
    score_page,
    score_requests,
    score_sections,
)
from studentassistant.llm import Transport
from studentassistant.observer import ASSISTANT_REQUEST_KIND, load_observer_snapshot
from studentassistant.server.app import create_app
from studentassistant.server.assistant_requests import AssistantRequestConsumer
from studentassistant.server.replay import AsgiTransport, ReplayError, replay
from studentassistant.vault import (
    Event,
    GitSync,
    SourceNotFoundError,
    TranscriptSegment,
    Vault,
    VaultError,
    list_sources,
    read_all_ledgers,
    read_jsonl,
    read_notes,
    read_notes_draft,
    read_source,
    sessions_directory,
)

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

EVAL_STUDENT = "Evaluación"
REPORT_MARKDOWN = "report.md"
CASE_VAULT_DIR = "vault"
TRANSCRIPT_FILE = "transcript.jsonl"
EVENTS_FILE = "events.jsonl"
# Bounds that only keep a stuck run from hanging: the replay gets its paced duration on top.
REPLAY_MARGIN_S = 600.0
GENERATE_TIMEOUT_S = 1_200.0
# `notes_path = "chat"`: how long the session's queued turns may take after the replay, and how
# long the queue must stay empty to count as drained (a request still on the bus is not queued).
CHAT_TURNS_TIMEOUT_S = 1_800.0
CHAT_SETTLE_S = 0.2


@dataclass
class CaseOutput:
    """What the pipeline produced for one case, read back from the case's vault."""

    session_id: str
    subject: str
    topic: str
    transcript: list[str] = field(default_factory=list)
    # capture id -> its page transcription; `None` for a stored page never transcribed.
    pages: dict[str, str | None] = field(default_factory=dict)
    assignments: dict[str, str] = field(default_factory=dict)
    notes: str | None = None
    notes_draft: bool = False
    notes_errors: list[str] = field(default_factory=list)
    generate_error: str | None = None
    actual_usd: float | None = None
    # The spoken requests the detector published in the session (`assistant.request`).
    requests: list[RequestItem] = field(default_factory=list)


class CaseResult(BaseModel):
    """One case of a report: its estimate, what it really cost and every score."""

    case: str
    estimate: CaseEstimate
    actual_usd: float | None = None
    session_id: str | None = None
    vault: str | None = None
    # Why the case produced nothing to score (the replay failed); `None` when it ran.
    error: str | None = None
    pages: list[PageScore] = []
    sections: SectionScore | None = None
    notes: NotesFidelity | None = None
    notes_draft: bool = False
    notes_errors: list[str] = []
    generate_error: str | None = None
    # `None` when the case has no `requests.yaml` (and in reports written before #371).
    requests: RequestScore | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def page_char_accuracy(self) -> float | None:
        return _mean([p.char_accuracy for p in self.pages])

    @computed_field  # type: ignore[prop-decorator]
    @property
    def page_word_accuracy(self) -> float | None:
        return _mean([p.word_accuracy for p in self.pages])

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float | None:
        """The mean of the case's headline scores: pages, sections, requests, kept, supported."""
        parts = [self.page_char_accuracy]
        if self.sections is not None:
            parts.append(self.sections.pairwise_agreement)
        if self.requests is not None:
            parts.append(self.requests.f1)
        if self.notes is not None:
            parts += [self.notes.kept, self.notes.supported]
        elif self.error is None:
            parts += [0.0, 0.0]  # no notes at all: nothing kept, nothing to trust
        return _mean([p for p in parts if p is not None])


class EvalReport(BaseModel):
    started_at: datetime
    finished_at: datetime
    models: dict[str, str]
    cases: list[CaseResult]
    # The request detector under test and how the scored notes were made (`[eval]`); `None` in
    # reports written before they were recorded.
    request_detection: str | None = None
    notes_path: str | None = None
    # Against the most recent earlier run of the eval set; `None` when there was none (and in
    # reports written before comparisons existed).
    comparison: RunComparison | None = None
    # Earlier reports that could not be read, and were skipped.
    comparison_warnings: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def estimated_usd(self) -> float:
        return sum(case.estimate.usd for case in self.cases)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def actual_usd(self) -> float:
        return sum(case.actual_usd or 0.0 for case in self.cases)


def _mean(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known) / len(known), 4) if known else None


def _replay_seconds(case: EvalCase, speed: float) -> float:
    recording = case.recording
    start = recording.manifest.started_client_time_ms
    times = [m.client_end_ms for m in recording.transcript]
    times += [e.client_time_ms for e in recording.events]
    times += [c.metadata.client_time_ms for c in recording.captures]
    if recording.audio_path is not None:
        times.append(start + int(recording.audio_path.stat().st_size / 32))  # 16 kHz PCM16
    return max([0, *(t - start for t in times)]) / 1000 / speed


def _stt(case: EvalCase, settings: Settings) -> SttSettings:
    manifest = case.recording.manifest
    if manifest.stt_mode == "client":
        return SttSettings(
            mode="client", provider=manifest.stt_provider, language=settings.stt.language
        )
    return settings.stt.model_copy(update={"mode": "server"})


async def produce_case(
    case: EvalCase,
    directory: Path,
    *,
    settings: Settings,
    transport: Transport,
    sleep: Sleep = asyncio.sleep,
) -> CaseOutput:
    """Replay `case` into a new vault under `directory` and read back what it produced.

    Raises:
        ReplayError: when the replay fails or does not finish in time.
    """
    settings = run_settings(settings)
    vault = Vault.init(directory / CASE_VAULT_DIR, student=EVAL_STUDENT)
    vault_settings = VaultSettings(path=vault.path, index_path=directory / "index.sqlite3")
    app = create_app(
        static_dir=directory / "no-web-build",
        server=ServerSettings(
            devices_path=directory / "devices.json", recordings_dir=directory / "recordings"
        ),
        vault=vault,
        sync=GitSync(vault, vault_settings.git),
        vault_settings=vault_settings,
        stt=_stt(case, settings),
        sources=settings.sources,
        llm_transport=transport,
        # The doubts chat (#325) would review the observer's doubts with extra editor calls
        # after the generation; the eval measures the observer and the notes, not those.
        llm_settings=settings.model_copy(
            update={"editor": settings.editor.model_copy(update={"doubts_in_chat": False})}
        ),
    )
    speed = settings.eval.speed
    async with AsgiTransport(app) as client:
        try:
            result = await asyncio.wait_for(
                replay(case.recording, client, speed=speed, sleep=sleep),
                _replay_seconds(case, speed) + REPLAY_MARGIN_S,
            )
        except TimeoutError as error:
            raise ReplayError("the replay did not finish in time") from error
        output = CaseOutput(
            session_id=result.session_id, subject=result.subject_id, topic=result.topic_id
        )
        if settings.eval.notes_path == "chat":
            await _drain_turns(app.state.assistant_requests, output)
        else:
            await _generate(client, output)
    _read_back(vault, output)
    return output


async def _drain_turns(consumer: AssistantRequestConsumer | None, output: CaseOutput) -> None:
    """Wait (bounded) until the session's requests have all run as editor turns."""
    if consumer is None:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CHAT_TURNS_TIMEOUT_S
    while True:
        try:
            await consumer.wait_idle(max(0.0, deadline - loop.time()))
        except TimeoutError:
            output.generate_error = "los turnos del chat no terminaron a tiempo"
            return
        await asyncio.sleep(CHAT_SETTLE_S)
        try:
            await consumer.wait_idle(0.0)  # still idle: nothing arrived meanwhile
        except TimeoutError:
            continue
        return


async def _generate(client: AsgiTransport, output: CaseOutput) -> None:
    """ "Prepárame el tema" over REST, as the button asks for it."""
    # The eval already had its estimated cost confirmed: a reached cap does not stop it.
    try:
        generated = await asyncio.wait_for(
            client.request(
                "POST",
                f"/api/subjects/{output.subject}/topics/{output.topic}/notes/generate",
                b'{"confirm_over_cap": true}',
                "application/json",
            ),
            GENERATE_TIMEOUT_S,
        )
    except TimeoutError:
        output.generate_error = "la generación de los apuntes no terminó a tiempo"
        return
    if generated.status == 200:
        body = generated.json()
        output.notes_draft = bool(body.get("draft"))
        output.notes_errors = list(body.get("errors") or [])
    else:
        output.generate_error = f"{generated.status}: {generated.detail()}"


def _read_back(vault: Vault, output: CaseOutput) -> None:
    subject, topic = output.subject, output.topic
    transcript = sessions_directory(vault, subject, topic) / output.session_id / TRANSCRIPT_FILE
    if transcript.is_file():
        output.transcript = [s.text for s in read_jsonl(transcript, TranscriptSegment)]
    events = transcript.with_name(EVENTS_FILE)
    if events.is_file():
        output.requests = [
            RequestItem(
                kind=str(event.payload.get("kind")),
                segments=[str(s) for s in event.payload.get("segment_ids") or []],
                summary=str(event.payload.get("summary") or ""),
                text=str(event.payload.get("text") or ""),
            )
            for event in read_jsonl(events, Event)
            if event.kind == ASSISTANT_REQUEST_KIND and event.payload.get("detector") != "typed"
        ]
    for source in list_sources(vault, subject, topic):
        capture_id = (source.meta or {}).get("capture_id")
        if not capture_id:
            continue
        stem = source.path.rsplit("/", 1)[-1].split(".", 1)[0]
        page_path = f"{source.path.rsplit('/', 1)[0]}/{stem}.md"
        try:
            text = read_source(vault, page_path).content.decode("utf-8")
        except SourceNotFoundError:
            text = None
        output.pages[str(capture_id)] = text
    output.assignments = dict(
        load_observer_snapshot(vault, subject, topic, write_back=False).state.assignments
    )
    if output.generate_error is None:
        read = read_notes_draft if output.notes_draft else read_notes
        output.notes = read(vault, subject, topic)
    prices = [entry.estimated_usd for entry in read_all_ledgers(vault)]
    output.actual_usd = round(sum(p or 0.0 for p in prices), 4) if prices else 0.0


def score_case(case: EvalCase, output: CaseOutput) -> CaseResult:
    """Score what `output` holds against `case`'s reference (the estimate is filled in later)."""
    pages = [
        score_page(capture_id, reference, output.pages.get(capture_id))
        for capture_id, reference in sorted(case.reference_pages.items())
    ]
    sections = None
    if case.reference_sections is not None:
        sections = score_sections(
            [(s.title, s.segments) for s in case.reference_sections], output.assignments
        )
    notes = None
    if output.notes is not None:
        sources = [*output.transcript, *(t for t in output.pages.values() if t)]
        notes = score_notes(case.reference_notes, output.notes, sources)
    requests = None
    if case.reference_requests is not None:
        finals = {m.segment_id: m.text for m in case.finals}
        requests = score_requests(
            [
                RequestItem(
                    kind=r.kind,
                    segments=list(r.segments),
                    summary=r.note or r.kind,
                    text=" ".join(finals.get(s, "") for s in r.segments).strip(),
                )
                for r in case.reference_requests
            ],
            output.requests,
        )
    return CaseResult(
        case=case.name,
        estimate=CaseEstimate(case=case.name, roles=[]),
        actual_usd=output.actual_usd,
        session_id=output.session_id,
        pages=pages,
        sections=sections,
        notes=notes,
        notes_draft=output.notes_draft,
        notes_errors=output.notes_errors,
        generate_error=output.generate_error,
        requests=requests,
    )


async def run_case(
    case: EvalCase,
    directory: Path,
    *,
    settings: Settings,
    transport: Transport,
    sleep: Sleep = asyncio.sleep,
) -> CaseResult:
    """Replay, read back and score one case; a failed replay is a result with `error` set."""
    estimate = estimate_case(case, settings)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        output = await produce_case(
            case, directory, settings=settings, transport=transport, sleep=sleep
        )
    except (ReplayError, VaultError) as error:
        logger.warning("eval case %s did not run: %s", case.name, error)
        result = CaseResult(case=case.name, estimate=estimate, error=str(error))
    else:
        result = score_case(case, output).model_copy(update={"estimate": estimate})
    return result.model_copy(update={"vault": str(directory / CASE_VAULT_DIR)})


def run_directory(eval_path: Path, now: datetime) -> Path:
    """Where a run started at `now` writes: `runs/<UTC time>/` under the eval set."""
    return eval_path / RUNS_DIR_NAME / now.astimezone(UTC).strftime("%Y%m%d-%H%M%S")


async def run_eval(
    cases: list[EvalCase],
    directory: Path,
    *,
    settings: Settings,
    transport: Transport,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_case: Callable[[CaseResult], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> EvalReport:
    """Run every case, one after another, into `directory`, and return the report.

    The report is compared with the most recent earlier readable run next to `directory`; an
    unreadable earlier report is logged, passed to `on_warning` and skipped.
    """
    started = clock()
    results: list[CaseResult] = []
    for case in cases:
        result = await run_case(
            case, directory / case.name, settings=settings, transport=transport, sleep=sleep
        )
        results.append(result)
        if on_case is not None:
            on_case(result)
    settings = run_settings(settings)
    roles = settings.llm.roles
    warnings: list[str] = []

    def warn(message: str) -> None:
        logger.warning("%s", message)
        warnings.append(message)
        if on_warning is not None:
            on_warning(message)

    report = EvalReport(
        started_at=started,
        finished_at=clock(),
        models={
            "observer": roles.observer.model,
            "transcriber": roles.transcriber.model,
            "editor": roles.editor.model,
        },
        cases=results,
        request_detection=settings.observer.request_detection,
        notes_path=settings.eval.notes_path,
    )
    previous = previous_report(directory, warn=warn)
    comparison = None
    if previous is not None:
        name, earlier = previous
        comparison = compare_reports(
            earlier, report, margin=settings.eval.regression_margin, previous_run=name
        )
    return report.model_copy(update={"comparison": comparison, "comparison_warnings": warnings})


NOTES_PATH_LABELS = {
    "generate": "«prepárame el tema» tras la sesión",
    "chat": "los que construyeron las peticiones de la sesión",
}


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f} %"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f} USD"


def render_report(report: EvalReport) -> str:
    """The report in Spanish Markdown: a summary table, then every case in detail."""
    lines = [
        "# Evaluación de la canalización",
        "",
        f"- Inicio: {report.started_at:%Y-%m-%d %H:%M:%S %Z}",
        "- Modelos: " + ", ".join(f"{role} `{model}`" for role, model in report.models.items()),
        f"- Coste estimado: {_usd(report.estimated_usd)}; coste real: {_usd(report.actual_usd)}",
        f"- Detector de peticiones: `{report.request_detection or '—'}`; apuntes:"
        f" {NOTES_PATH_LABELS.get(report.notes_path or '', '—')}",
        "",
        "| caso | páginas (caracteres) | páginas (palabras) | secciones | peticiones (F1)"
        " | conservado | con fuente | global | coste |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for case in report.cases:
        sections = case.sections.pairwise_agreement if case.sections else None
        requests = case.requests.f1 if case.requests else None
        kept = case.notes.kept if case.notes else None
        supported = case.notes.supported if case.notes else None
        lines.append(
            f"| {case.case} | {_pct(case.page_char_accuracy)} | {_pct(case.page_word_accuracy)}"
            f" | {_pct(sections)} | {_pct(requests)} | {_pct(kept)} | {_pct(supported)}"
            f" | {_pct(case.score)} | {_usd(case.actual_usd)} |"
        )
    for case in report.cases:
        lines += ["", f"## {case.case}", ""]
        if case.error is not None:
            lines.append(f"No se ha podido reproducir: {case.error}")
            continue
        lines.append(f"- Sesión `{case.session_id}`; bóveda de la prueba: `{case.vault}`")
        lines.append(f"- Coste estimado {_usd(case.estimate.usd)}, real {_usd(case.actual_usd)}")
        if case.pages:
            lines.append("- Páginas transcritas:")
            for page in case.pages:
                state = "" if page.transcribed else " (sin transcribir)"
                lines.append(
                    f"  - `{page.capture_id}`: caracteres {_pct(page.char_accuracy)},"
                    f" palabras {_pct(page.word_accuracy)}{state}"
                )
        if case.sections is not None:
            s = case.sections
            lines.append(
                f"- Secciones del observador: acuerdo por pares {_pct(s.pairwise_agreement)},"
                f" {s.assigned} de {s.segments} segmentos asignados ({_pct(s.coverage)}),"
                f" {s.observer_sections} secciones frente a {s.reference_sections} de referencia"
            )
        if case.requests is not None:
            lines += _render_requests(case.requests)
        if case.generate_error is not None:
            lines.append(f"- Los apuntes no se han generado: {case.generate_error}")
        elif case.notes is None:
            lines.append("- No hay apuntes que puntuar: ninguna petición los ha escrito")
        if case.notes is not None:
            n = case.notes
            draft = " (borrador: no pasaron el validador)" if case.notes_draft else ""
            lines.append(
                f"- Apuntes{draft}: conservado {_pct(n.kept)} de {n.reference_units} ideas de"
                f" referencia, con fuente {_pct(n.supported)} de {n.generated_units} generadas"
            )
            for error in case.notes_errors:
                lines.append(f"  - Validador: {error}")
            if n.dropped:
                lines.append("- Ideas de referencia que faltan:")
                lines += [f"  - {unit}" for unit in n.dropped]
            if n.unsupported:
                lines.append("- Ideas generadas sin apoyo en las fuentes:")
                lines += [f"  - {unit}" for unit in n.unsupported]
    lines += ["", render_comparison(report.comparison, report.comparison_warnings).rstrip("\n")]
    return "\n".join(lines) + "\n"


def _request_line(request: RequestItem) -> str:
    segments = ", ".join(f"`{s}`" for s in request.segments)
    return f"  - {request.kind} ({segments}): {request.summary} — «{request.text}»"


def _render_requests(score: RequestScore) -> list[str]:
    lines = [
        f"- Peticiones al asistente: precisión {_pct(score.precision)}, exhaustividad"
        f" {_pct(score.recall)}, F1 {_pct(score.f1)} ({score.matched} acertadas de"
        f" {score.reference} de referencia y {score.detected} detectadas)"
    ]
    for kind in score.per_kind:
        lines.append(
            f"  - {kind.kind}: precisión {_pct(kind.precision)}, exhaustividad"
            f" {_pct(kind.recall)}, F1 {_pct(kind.f1)} ({kind.matched}/{kind.reference}"
            f" de referencia, {kind.detected} detectadas)"
        )
    if score.missed:
        lines.append("- Peticiones no detectadas:")
        lines += [_request_line(r) for r in score.missed]
    if score.spurious:
        lines.append("- Peticiones detectadas que no estaban en la referencia:")
        lines += [_request_line(r) for r in score.spurious]
    return lines


def write_report(report: EvalReport, directory: Path) -> Path:
    """Write `report.md` and `report.json` into `directory`; returns the Markdown's path."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / REPORT_JSON).write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    markdown = directory / REPORT_MARKDOWN
    markdown.write_text(render_report(report), encoding="utf-8")
    return markdown
