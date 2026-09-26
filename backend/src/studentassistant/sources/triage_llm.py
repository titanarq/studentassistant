"""The optional Sonnet stage of capture triage (`[sources] triage_llm_check`, off by default).

When a capture's metrics are within `triage_llm_margin` of a threshold (`triage.ambiguous_checks`:
`blank`, `blurry`, `partial`), its page image is sent to Claude (role `transcriber`, a client bound
to the session's ledger) with the strict tool `triage_verdict` (`llm.structured`), and the verdict
replaces those checks (`triage.with_verdict`). The near-duplicate check stays deterministic. Any
failure (a reached cap, a refusal, a malformed answer) keeps the deterministic result.

Not re-exported by the package root (it imports `studentassistant.llm`).
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence

from pydantic import BaseModel, Field

from studentassistant.config import SourcesSettings
from studentassistant.llm import LLMClient, LLMError, load_prompt, structured
from studentassistant.sources.triage import TriageResult, ambiguous_checks, with_verdict

logger = logging.getLogger(__name__)

PROMPT_NAME = "capture_triage"
TOOL_NAME = "triage_verdict"
TOOL_DESCRIPTION = "Give the verdict on the page checks you were asked about."
MAX_TOKENS = 400


class TriageVerdict(BaseModel):
    """What Claude answers: each asked check true or false, and why (Spanish)."""

    blank: bool = Field(description="The page has no content worth transcribing.")
    blurry: bool = Field(description="The writing is too blurred to be read reliably.")
    partial: bool = Field(description="The frame cuts written content off at an edge.")
    reason: str = Field(description="A short reason, in Spanish.")


def verdict_request(page: bytes, checks: Sequence[str]) -> list[dict[str, object]]:
    """The user message: the page image, then which checks to decide."""
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(page).decode("ascii"),
                    },
                },
                {
                    "type": "text",
                    "text": "Decide these checks: "
                    + ", ".join(checks)
                    + ". Answer the others false.",
                },
            ],
        }
    ]


async def refine_triage(
    client: LLMClient, page: bytes, result: TriageResult, settings: SourcesSettings
) -> TriageResult:
    """`result` with its ambiguous checks decided by Claude; `result` unchanged when none is
    ambiguous or the call fails."""
    checks = ambiguous_checks(result, settings)
    if not checks:
        return result
    prompt = load_prompt(PROMPT_NAME)
    try:
        answer = await structured(
            client,
            verdict_request(page, checks),
            TriageVerdict,
            tool_name=TOOL_NAME,
            tool_description=TOOL_DESCRIPTION,
            system=prompt.content,
            max_tokens=MAX_TOKENS,
            prompt_hash=prompt.hash,
        )
    except LLMError as error:
        logger.warning("the triage check of a capture failed, the metrics decide: %s", error)
        return result
    verdict = answer.value
    refined = with_verdict(result, verdict.model_dump(), settings, checks)
    refined.metrics["llm_reason"] = verdict.reason
    return refined


__all__ = ["PROMPT_NAME", "TOOL_NAME", "TriageVerdict", "refine_triage", "verdict_request"]
