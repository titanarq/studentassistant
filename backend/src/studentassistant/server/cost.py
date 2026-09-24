"""`GET /api/cost`: the cost-cap status of the llm module, over REST.

The web and the capture clients read it to show that the observer is paused or that editor calls
need confirmation. The server computes nothing here: it opens the configured vault and returns
`studentassistant.llm.cost_status` as is. The configuration is read on every request, so a cap
changed in `config.toml` shows up without restarting the server.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from studentassistant.config import Settings
from studentassistant.llm import CostStatus, LedgerBinding, cost_status
from studentassistant.vault import (
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    VaultError,
    get_topic,
)

TOPIC_REQUIRED_DETAIL = "Indica a la vez «subject» y «topic» (y, si quieres, «session»)."
UNKNOWN_TOPIC_DETAIL = "No existe ese tema en la bóveda."
VAULT_UNAVAILABLE_DETAIL = "No se puede abrir la bóveda."


def cost_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/cost")
    def cost(
        subject: str | None = None, topic: str | None = None, session: str | None = None
    ) -> CostStatus:
        if (subject is None) != (topic is None) or (session is not None and subject is None):
            raise HTTPException(status_code=422, detail=TOPIC_REQUIRED_DETAIL)
        settings = Settings()
        try:
            vault = Vault.open(settings.vault.path)
        except VaultError as error:
            raise HTTPException(status_code=503, detail=VAULT_UNAVAILABLE_DETAIL) from error
        if subject is None or topic is None:
            # No session selected: `session_usd` is 0 and only the day total drives the flags.
            binding = LedgerBinding(vault=vault, subject="", topic="")
        else:
            try:
                get_topic(vault, subject, topic)
            except (SubjectNotFoundError, TopicNotFoundError) as error:
                raise HTTPException(status_code=404, detail=UNKNOWN_TOPIC_DETAIL) from error
            binding = LedgerBinding(vault=vault, subject=subject, topic=topic, session=session)
        return cost_status(binding, settings)

    return router
