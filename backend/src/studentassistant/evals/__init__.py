"""Eval set from real sessions: page-transcription, observer and editor fidelity, scored.

`studentassistant eval run` replays each recorded session of the eval set (`[eval] path`, outside
the code repository) through the whole pipeline with real Claude calls into a fresh vault of its
own, then scores what came out against the student's reference (`scoring.py`, rubric in
`docs/modules/infra.md`). The estimated cost is shown, and confirmed, before anything is sent.
"""

from studentassistant.evals.cases import (
    CASE_RECORDING_DIR,
    CASE_REFERENCE_DIR,
    RUNS_DIR_NAME,
    EvalCase,
    EvalSetError,
    ReferenceSection,
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
from studentassistant.evals.estimate import CaseEstimate, RoleEstimate, estimate_case
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
    NotesFidelity,
    PageScore,
    SectionScore,
    score_notes,
    score_page,
    score_sections,
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
    "EvalSetError",
    "NotesFidelity",
    "PageScore",
    "ReferenceSection",
    "RoleEstimate",
    "RunComparison",
    "SectionScore",
    "compare_reports",
    "estimate_case",
    "previous_report",
    "read_case",
    "read_eval_set",
    "read_report",
    "render_comparison",
    "render_report",
    "run_case",
    "run_eval",
    "score_notes",
    "score_page",
    "score_sections",
    "write_report",
]
