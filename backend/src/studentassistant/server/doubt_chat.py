"""Doubts asked in the workspace chat, one at a time (#325): the `DoubtChat` of the app.

Doubts never go into the notes: the editor writes what the sources support and reports every
unresolved point as a pending doubt (`editor.doubts.raise_doubts`), and the observer queues its
own. This is what brings them to the student, in the study workspace's chat, **one at a time**:

- After every applied editor write -- a chat turn (typed or spoken), "prepárame el tema", a doubt
  answered or dismissed -- the caller `schedule`s the topic. The asker then runs as its own task
  (one per topic; a schedule while it runs makes it run once more). It does not take the topic's
  notes lock (`NotesGenerator.claim`), so the student's next turn is never refused because of it:
  a review applies its edits under the short write lock on the latest notes and re-asks the
  editor when they changed, like a chat turn. It does nothing while "prepárame el tema" rewrites
  the notes (the generation schedules the topic again when it ends).
- It asks nothing while a doubt asked in the chat is still open (`editor.doubts.ask_plan`), nor
  when the topic has no notes. Otherwise, the open doubts relevant to the notes (their refs
  overlap the sources the notes cite) that have no question yet are first reviewed by the editor
  (`review_doubts` scoped to at most `REVIEW_BATCH` of them): those the sources settle are
  auto-resolved and reported as one short line (`doubts.auto_resolved` `{pending_ids, summary}`,
  plus `notes.changed` origin `editor` when the notes changed), the others get their question
  recorded. Then the first relevant open doubt with a question is asked (`ask_in_chat`) and
  announced as `doubt.asked` `{pending_id, question, suggestions, options, refs}`. Items whose
  pages are all set aside by capture triage are never asked. Without Claude (no `NotesGenerator`)
  nothing is reviewed: a doubt without a question is asked with a generic one.
- `resolved(...)` announces an answered or dismissed doubt: `doubt.resolved` `{pending_id,
  status, resolution, notes_changed}` and, when the notes changed, `notes.changed` (origin
  `editor`).

Every doubt event is written in the topic's live session when it has one (`live`, through the
session bus, so the live observer folds it), else in a review session (`editor.doubts`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from studentassistant.editor.doubts import (
    LiveSink,
    ResolutionResult,
    ReviewResult,
    ask_in_chat,
    ask_plan,
    review_doubts,
)
from studentassistant.llm import LedgerBinding, get_client
from studentassistant.server.bus import SessionNotAttachedError
from studentassistant.server.notes_routes import NotesGenerator
from studentassistant.server.sessions import SessionService
from studentassistant.server.workspace import (
    DOUBT_ASKED,
    DOUBT_RESOLVED,
    DOUBTS_AUTO_RESOLVED,
    WorkspaceHub,
)
from studentassistant.vault import Origin

logger = logging.getLogger(__name__)

REVIEW_BATCH = 5
"""The most unreviewed doubts one review before asking covers."""
SHUTDOWN_TIMEOUT_SECONDS = 5.0


class DoubtChat:
    """Asks the topic's open doubts in the workspace chat, one at a time (module docstring)."""

    def __init__(
        self,
        sessions: SessionService,
        hub: WorkspaceHub,
        generator: NotesGenerator | None = None,
        *,
        review_batch: int = REVIEW_BATCH,
        enabled: bool = True,
    ) -> None:
        self.sessions, self.hub, self.generator = sessions, hub, generator
        self.review_batch = review_batch
        self.enabled = enabled
        """`[editor] doubts_in_chat`: off, `schedule` asks nothing (the announcements still go)."""
        self._tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._again: set[tuple[str, str]] = set()

    # -- the live session ------------------------------------------------------------------

    def live(self, subject_id: str, topic_id: str) -> LiveSink:
        """Publishes a doubts event in the topic's active session (through the bus)."""

        async def publish(kind: str, origin: Origin, payload: dict[str, Any]) -> str:
            active = self.sessions.active
            if active is None or (active.subject_id, active.topic_id) != (subject_id, topic_id):
                raise SessionNotAttachedError(
                    f"no active session of {subject_id}/{topic_id} on this backend"
                )
            event = await self.sessions.bus.publish(active.session_id, kind, origin, payload)
            return event.session_id

        return publish

    # -- announcing ------------------------------------------------------------------------

    def resolved(self, subject_id: str, topic_id: str, result: ResolutionResult) -> None:
        """`doubt.resolved` (and `notes.changed` when the notes changed) for an answer/dismissal."""
        self.hub.publish(
            subject_id,
            topic_id,
            DOUBT_RESOLVED,
            {
                "pending_id": result.pending_id,
                "status": result.status,
                "resolution": result.resolution,
                "notes_changed": result.notes_changed,
            },
        )
        if result.notes_changed:
            self.hub.notes_changed(
                subject_id,
                topic_id,
                revision=result.revision,
                origin="editor",
                summary=result.resolution,
            )

    def reviewed(self, subject_id: str, topic_id: str, result: ReviewResult) -> None:
        """The short line of the auto-resolved doubts (and `notes.changed`), for a review."""
        if result.auto_resolved:
            self.hub.publish(
                subject_id,
                topic_id,
                DOUBTS_AUTO_RESOLVED,
                {"pending_ids": result.auto_resolved, "summary": result.summary},
            )
        if result.notes_changed:
            self.hub.notes_changed(
                subject_id,
                topic_id,
                revision=result.revision,
                origin="editor",
                summary=result.summary,
            )

    # -- scheduling ------------------------------------------------------------------------

    def after(self, subject_id: str, topic_id: str, result: BaseModel | Mapping[str, Any]) -> None:
        """Schedule the topic when an editor write's `result` changed the notes or raised doubts."""
        data = result if isinstance(result, Mapping) else result.model_dump(mode="json")
        changed = data.get("notes_changed") or (
            "draft" in data and not data.get("draft") and data.get("version") is not None
        )
        if changed or data.get("doubts"):
            self.schedule(subject_id, topic_id)

    def schedule(self, subject_id: str, topic_id: str) -> None:
        """Ask the topic's next doubt soon (after the lock the caller may still hold is free)."""
        if not self.enabled:
            return
        key = (subject_id, topic_id)
        if key in self._tasks:
            self._again.add(key)
            return
        task = asyncio.create_task(self._work(key), name=f"doubt-chat-{subject_id}-{topic_id}")
        self._tasks[key] = task

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """Wait until no asker runs (tests)."""
        deadline = time.monotonic() + timeout
        while self._tasks:
            if time.monotonic() > deadline:
                raise TimeoutError("the doubt chat did not finish")
            await asyncio.sleep(0.01)

    async def stop(self, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Give the running askers `timeout` seconds, then cancel them (shutdown)."""
        self._again.clear()
        tasks = set(self._tasks.values())
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)

    async def _work(self, key: tuple[str, str]) -> None:
        try:
            while True:
                self._again.discard(key)
                try:
                    await self.ask_next(*key)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("the doubt chat of %s/%s failed", *key)
                if key not in self._again:
                    return
        finally:
            self._tasks.pop(key, None)

    # -- asking ----------------------------------------------------------------------------

    async def ask_next(self, subject_id: str, topic_id: str) -> str | None:
        """Review what needs it and ask the next doubt, unless one is asked; the id asked."""
        vault = await self.sessions.open_vault()
        sync = self.sessions.sync
        if sync is None:  # pragma: no cover - the vault opens with its sync
            return None
        generator = self.generator
        if generator is not None and generator.holder(subject_id, topic_id) == "editor":
            return None  # "prepárame el tema" is rewriting the notes; it schedules us again
        live = self.live(subject_id, topic_id)
        plan = await asyncio.to_thread(ask_plan, vault, subject_id, topic_id)
        if plan.asked is not None or not (plan.to_ask or plan.to_review):
            return None
        if plan.to_review and generator is not None:
            client = get_client(
                "editor",
                settings=generator.settings,
                transport=generator.transport,
                ledger=LedgerBinding(vault, subject_id, topic_id),
            )
            try:
                reviewed = await review_doubts(
                    vault,
                    subject_id,
                    topic_id,
                    client=client,
                    sync=sync,
                    host=self.sessions.host,
                    pending_ids=plan.to_review[: self.review_batch],
                    live=live,
                )
            except Exception as error:
                # A reached cost cap, a Claude failure: ask without reviewing.
                logger.warning(
                    "the doubts of %s/%s were not reviewed: %s", subject_id, topic_id, error
                )
            else:
                self.reviewed(subject_id, topic_id, reviewed)
                plan = await asyncio.to_thread(ask_plan, vault, subject_id, topic_id)
        candidates = plan.to_ask or plan.to_review
        if plan.asked is not None or not candidates:
            return None
        asked = await ask_in_chat(
            vault,
            subject_id,
            topic_id,
            candidates[0],
            sync=sync,
            host=self.sessions.host,
            live=live,
        )
        self.hub.publish(
            subject_id,
            topic_id,
            DOUBT_ASKED,
            asked.model_dump(
                mode="json",
                include={"pending_id", "question", "suggestions", "options", "refs"},
            ),
        )
        return asked.pending_id


__all__ = ["REVIEW_BATCH", "DoubtChat"]
