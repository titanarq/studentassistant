"""Eval set from real sessions: page transcription, observer, request detection and editor
fidelity, scored.

`studentassistant eval run` replays each recorded session of the eval set (`[eval] path`, outside
the code repository) through the whole pipeline with real Claude calls into a fresh vault of its
own, then scores what came out against the student's reference (`scoring.py`, rubric in
`docs/modules/infra.md`). The estimated cost is shown, and confirmed, before anything is sent.
`studentassistant eval import-session` turns a session of the vault into a new case
(`import_session.py`).
"""

from studentassistant.evals.cases import (
    CASE_RECORDING_DIR,
    CASE_REFERENCE_DIR,
    RUNS_DIR_NAME,
    EvalCase,
    EvalSetError,
    ReferenceRequest,
    ReferenceSection,
    ReferenceTriage,
    read_case,
    read_eval_set,
)
from studentassistant.evals.compare import (
    RunComparison,
    compare_reports,
    previous_report,
    read_report,
    render_comparison,
)
from studentassistant.evals.estimate import (
    CaseEstimate,
    RoleEstimate,
    estimate_case,
    run_settings,
)
from studentassistant.evals.import_session import (
    ImportedCase,
    SessionImportError,
    default_case_name,
    import_session,
)
from studentassistant.evals.run import (
    CaseOutput,
    CaseResult,
    EvalReport,
    render_report,
    run_case,
    run_eval,
    write_report,
)
from studentassistant.evals.scoring import (
    KindScore,
    NotesFidelity,
    PageScore,
    RequestItem,
    RequestScore,
    SectionScore,
    TriageItem,
    TriageScore,
    score_notes,
    score_page,
    score_requests,
    score_sections,
    score_triage,
)

__all__ = [
    "CASE_RECORDING_DIR",
    "CASE_REFERENCE_DIR",
    "RUNS_DIR_NAME",
    "CaseEstimate",
    "CaseOutput",
    "CaseResult",
    "EvalCase",
    "EvalReport",
    "ImportedCase",
    "EvalSetError",
    "KindScore",
    "NotesFidelity",
    "PageScore",
    "ReferenceRequest",
    "ReferenceSection",
    "ReferenceTriage",
    "RequestItem",
    "RequestScore",
    "RoleEstimate",
    "RunComparison",
    "SectionScore",
    "SessionImportError",
    "TriageItem",
    "TriageScore",
    "compare_reports",
    "default_case_name",
    "estimate_case",
    "import_session",
    "previous_report",
    "read_case",
    "read_eval_set",
    "read_report",
    "render_comparison",
    "render_report",
    "run_case",
    "run_eval",
    "run_settings",
    "score_notes",
    "score_page",
    "score_requests",
    "score_sections",
    "score_triage",
    "write_report",
]
