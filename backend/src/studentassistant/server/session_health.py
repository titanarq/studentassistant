"""Per-session health (#262): what failed behind the scenes while the student was capturing.

Observer calls, page transcriptions and vault pushes that fail are otherwise only in the logs, so a
twenty-minute session could end with nothing understood and no one noticing. `SessionHealth`
keeps, per session, the count and the latest Spanish message of

- failed observer calls (the observer's `observer.call_failed` notices, `observer/live.py`),
- failed page transcriptions (`page.transcription_failed`, `sources/transcriber.py`, counted in
  the session whose log records them),

and whether the observer is paused by a cost cap (its latest `observer.status`). Vault push
failures are not per session: `GET /api/sessions/{id}/health` reads the current streak of the
vault's `GitSync` (`consecutive_push_failures`, `last_push_failure`), which a successful push
clears.

No task runs for it: the tracker holds a bus subscription created with the app (only the kinds
above plus `session.ended`, which forgets the session) and folds what it queued whenever a summary
is asked for. Held in memory only; a restarted backend starts every count at 0.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from studentassistant.observer.live import CALL_FAILED_EVENT_KIND, STATUS_EVENT_KIND
from studentassistant.server.bus import BusEvent, SessionBus, Subscription
from studentassistant.vault import SyncStatus

TRANSCRIPTION_FAILED_KIND = "page.transcription_failed"
SESSION_ENDED_KIND = "session.ended"
HEALTH_KINDS = frozenset(
    {CALL_FAILED_EVENT_KIND, STATUS_EVENT_KIND, TRANSCRIPTION_FAILED_KIND, SESSION_ENDED_KIND}
)
QUEUE_SIZE = 1024
"""The subscription's bound: failures are rare, and the queue is folded on every poll."""

OBSERVER_MESSAGES = {
    "unavailable": "Claude no responde (sin conexión o saturado)",
    "refused": "Claude ha rechazado la petición",
    "invalid": "Claude ha dado una respuesta que no se puede usar",
    "error": "un error inesperado del servidor",
}
TRANSCRIPTION_MESSAGES = {
    "cost_cap": "se ha alcanzado el límite de gasto",
    "refused": "Claude ha rechazado la página",
    "error": "Claude no ha podido transcribir la página",
}
PUSH_MESSAGES = {
    "offline": "no hay conexión con GitHub",
    "auth": "GitHub no acepta las credenciales de este equipo",
    "rejected": "GitHub tiene cambios que este equipo todavía no tiene",
    "error": "git ha devuelto un error",
}
PAUSED_MESSAGES = {
    "session": "se ha alcanzado el límite de gasto de la sesión",
    "day": "se ha alcanzado el límite de gasto del día",
}
PAUSED_FALLBACK = "se ha alcanzado un límite de gasto"


class FailureSummary(BaseModel):
    """How many times something failed in the session and why, the last time, in Spanish."""

    count: int = 0
    message: str | None = None


class SessionHealthResponse(BaseModel):
    """`GET /api/sessions/{id}/health`: every count 0 and `ok` true for a healthy session."""

    session_id: str
    ok: bool
    observer: FailureSummary
    observer_paused: bool
    observer_paused_message: str | None
    transcription: FailureSummary
    push: FailureSummary


@dataclass
class _Counts:
    observer_failures: int = 0
    observer_message: str | None = None
    transcription_failures: int = 0
    transcription_message: str | None = None
    paused_message: str | None = None


class SessionHealth:
    """The per-session failure counts, folded from the bus on demand."""

    def __init__(self, bus: SessionBus) -> None:
        self._subscription: Subscription = bus.subscribe(
            name="session-health", kinds=HEALTH_KINDS, maxsize=QUEUE_SIZE
        )
        self._sessions: dict[str, _Counts] = {}

    def record(self, event: BusEvent) -> None:
        """Fold one bus event into its session's counts."""
        if event.kind == SESSION_ENDED_KIND:
            self._sessions.pop(event.session_id, None)
            return
        counts = self._sessions.setdefault(event.session_id, _Counts())
        payload = event.payload
        if event.kind == CALL_FAILED_EVENT_KIND:
            counts.observer_failures += 1
            counts.observer_message = _message(OBSERVER_MESSAGES, payload.get("kind"))
        elif event.kind == TRANSCRIPTION_FAILED_KIND:
            counts.transcription_failures += 1
            counts.transcription_message = _message(TRANSCRIPTION_MESSAGES, payload.get("reason"))
        elif event.kind == STATUS_EVENT_KIND:
            paused = payload.get("status") == "paused"
            counts.paused_message = (
                PAUSED_MESSAGES.get(str(payload.get("cap")), PAUSED_FALLBACK) if paused else None
            )

    def _fold_queued(self) -> None:
        while True:
            try:
                event = self._subscription.get_nowait()
            except asyncio.QueueEmpty:
                return
            self.record(event)

    def summary(self, session_id: str, sync_status: SyncStatus) -> SessionHealthResponse:
        """The session's health now; the push streak comes from the vault's sync status."""
        self._fold_queued()
        counts = self._sessions.get(session_id, _Counts())
        failure = sync_status.last_push_failure
        push = FailureSummary()
        if sync_status.consecutive_push_failures and failure is not None:
            push = FailureSummary(
                count=sync_status.consecutive_push_failures,
                message=_message(PUSH_MESSAGES, failure.kind),
            )
        observer = FailureSummary(count=counts.observer_failures, message=counts.observer_message)
        transcription = FailureSummary(
            count=counts.transcription_failures, message=counts.transcription_message
        )
        paused = counts.paused_message is not None
        return SessionHealthResponse(
            session_id=session_id,
            ok=not (observer.count or transcription.count or push.count or paused),
            observer=observer,
            observer_paused=paused,
            observer_paused_message=counts.paused_message,
            transcription=transcription,
            push=push,
        )

    def close(self) -> None:
        self._subscription.close()


def _message(messages: Mapping[str, str], key: Any) -> str:
    return messages.get(str(key), messages["error"])
