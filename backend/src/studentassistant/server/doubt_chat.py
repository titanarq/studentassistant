"""Doubts marked in the notes and shown in the workspace chat on demand (#325, #516): `DoubtChat`.

Doubts never go into the notes: the editor writes what the sources support and reports every
unresolved point as a pending doubt (`editor.doubts.raise_doubts`), and the observer queues its
own. Since #516 they are **not asked one after another** in the chat: the web marks each open
doubt in the notes viewer (`editor.doubt_marks`) and the student opens the one they want.

- After every applied editor write -- a chat turn (typed or spoken), "prepárame el tema", a doubt
  answered or dismissed -- the caller `schedule`s the topic. The preparer then runs as its own
  task (one per topic; a schedule while it runs makes it run once more). It does not take the
  topic's notes lock (`NotesGenerator.claim`), so the student's next turn is never refused because
  of it: a review applies its edits under the short write lock on the latest notes and re-asks the
  editor when they changed, like a chat turn. It does nothing while "prepárame el tema" rewrites
  the notes (the generation schedules the topic again when it ends).
- Without notes it does nothing. Otherwise the open doubts relevant to the notes (their refs
  overlap the sources the notes cite, `editor.doubts.ask_plan`) that have no question yet are
  reviewed by the editor (`review_doubts` scoped to at most `REVIEW_BATCH` of them): those the
  sources settle are auto-resolved and reported as one short line (`doubts.auto_resolved`
  `{pending_ids, summary}`, plus `notes.changed` origin `editor` when the notes changed), the
  others get their question recorded, ready for when the student opens them. Then
  `doubts.marked` `{count}` announces how many doubts are marked in the notes, so the web reads
  the marks again. Items whose pages are all set aside by capture triage are never marked.
- `show(...)` brings one doubt to the chat (a badge of the notes clicked, or «Ver la siguiente»):
  reviewed first when it has no question yet and Claude is available (it may be auto-resolved
  then, and is not asked), then asked (`ask_in_chat`) and announced as `doubt.asked`
  `{pending_id, kind, text, question, suggestions, options, refs}`. The doubt last asked and still
  open is announced again without a new event. Without Claude a doubt without a question is
  asked with a generic one.
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
from typing import Any, Literal

from pydantic import BaseModel, Field

from studentassistant.editor.doubt_marks import doubt_marks
from studentassistant.editor.doubts import (
    AskedDoubt,
    LiveSink,
    ResolutionResult,
    ReviewResult,
    ask_in_chat,
    ask_plan,
    doubt_chat_turns,
    open_doubt,
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
    DOUBTS_MARKED,
    WorkspaceHub,
)
from studentassistant.vault import Origin

logger = logging.getLogger(__name__)

REVIEW_BATCH = 5
"""The most unreviewed doubts one review before asking covers."""
SHUTDOWN_TIMEOUT_SECONDS = 5.0


class ShownDoubt(BaseModel):
    """What `DoubtChat.show` did: the doubt asked in the chat, or auto-resolved by its review."""

    pending_id: str
    asked: bool
    status: Literal["open", "auto_resolved"] = "open"
    summary: str | None = Field(default=None, description="The review's line when auto-resolved.")


class DoubtChat:
    """Prepares the topic's open doubts and shows one in the chat on demand (module docstring)."""

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
        """`[editor] doubts_in_chat`: off, `schedule` reviews nothing (announcements still go)."""
        self._tasks: dict[tuple[str | None, str, str], asyncio.Task[None]] = {}
        self._again: set[tuple[str | None, str, str]] = set()

    # -- the live session ------------------------------------------------------------------

    def live(self, subject_id: str, topic_id: str, *, user_id: str | None = None) -> LiveSink:
        """Publishes a doubts event in the topic's active session (through the bus).

        With `user_id` the session has to be that student's too: another student's session of a
        same-named topic is not this one (#566)."""

        async def publish(kind: str, origin: Origin, payload: dict[str, Any]) -> str:
            active = self.sessions.active
            if (
                active is None
                or (active.subject_id, active.topic_id) != (subject_id, topic_id)
                or (user_id is not None and active.user_id != user_id)
            ):
                raise SessionNotAttachedError(
                    f"no active session of {subject_id}/{topic_id} on this backend"
                )
            event = await self.sessions.bus.publish(active.session_id, kind, origin, payload)
            return event.session_id

        return publish

    # -- announcing ------------------------------------------------------------------------

    def resolved(
        self,
        subject_id: str,
        topic_id: str,
        result: ResolutionResult,
        *,
        user_id: str | None = None,
    ) -> None:
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
            user_id=user_id,
        )
        if result.notes_changed:
            self.hub.notes_changed(
                subject_id,
                topic_id,
                revision=result.revision,
                origin="editor",
                summary=result.resolution,
                user_id=user_id,
            )

    def reviewed(
        self, subject_id: str, topic_id: str, result: ReviewResult, *, user_id: str | None = None
    ) -> None:
        """The short line of the auto-resolved doubts (and `notes.changed`), for a review."""
        if result.auto_resolved:
            self.hub.publish(
                subject_id,
                topic_id,
                DOUBTS_AUTO_RESOLVED,
                {"pending_ids": result.auto_resolved, "summary": result.summary},
                user_id=user_id,
            )
        if result.notes_changed:
            self.hub.notes_changed(
                subject_id,
                topic_id,
                revision=result.revision,
                origin="editor",
                summary=result.summary,
                user_id=user_id,
            )

    # -- scheduling ------------------------------------------------------------------------

    def after(
        self,
        subject_id: str,
        topic_id: str,
        result: BaseModel | Mapping[str, Any],
        *,
        user_id: str | None = None,
    ) -> None:
        """Schedule the topic when an editor write's `result` changed the notes or raised doubts."""
        data = result if isinstance(result, Mapping) else result.model_dump(mode="json")
        changed = data.get("notes_changed") or (
            "draft" in data and not data.get("draft") and data.get("version") is not None
        )
        if changed or data.get("doubts"):
            self.schedule(subject_id, topic_id, user_id=user_id)

    def schedule(self, subject_id: str, topic_id: str, *, user_id: str | None = None) -> None:
        """Ask the topic's next doubt soon (after the lock the caller may still hold is free)."""
        if not self.enabled:
            return
        key = (self.hub.resolve_user(user_id), subject_id, topic_id)
        if key in self._tasks:
            self._again.add(key)
            return
        task = asyncio.create_task(
            self._work(key, user_id), name=f"doubt-chat-{subject_id}-{topic_id}"
        )
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

    async def _work(self, key: tuple[str | None, str, str], user_id: str | None) -> None:
        try:
            while True:
                self._again.discard(key)
                try:
                    await self.prepare(key[1], key[2], user_id=user_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("the doubt chat of %s/%s failed", key[1], key[2])
                if key not in self._again:
                    return
        finally:
            self._tasks.pop(key, None)

    # -- preparing -------------------------------------------------------------------------

    def _client(self, generator: NotesGenerator, vault: Any, subject_id: str, topic_id: str) -> Any:
        return get_client(
            "editor",
            settings=generator.settings,
            transport=generator.transport,
            ledger=LedgerBinding(vault, subject_id, topic_id),
        )

    async def _review(
        self,
        vault: Any,
        sync: Any,
        subject_id: str,
        topic_id: str,
        pending_ids: list[str],
        user_id: str | None,
    ) -> ReviewResult | None:
        """Review those doubts with the editor and announce it; `None` when it failed."""
        generator = self.generator
        if generator is None or not pending_ids:
            return None
        try:
            reviewed = await review_doubts(
                vault,
                subject_id,
                topic_id,
                client=self._client(generator, vault, subject_id, topic_id),
                sync=sync,
                host=self.sessions.host,
                pending_ids=pending_ids,
                live=self.live(subject_id, topic_id, user_id=user_id),
            )
        except Exception as error:
            # A reached cost cap, a Claude failure: the doubt keeps no question (a generic one).
            logger.warning("the doubts of %s/%s were not reviewed: %s", subject_id, topic_id, error)
            return None
        self.reviewed(subject_id, topic_id, reviewed, user_id=user_id)
        return reviewed

    async def marked(self, subject_id: str, topic_id: str, *, user_id: str | None = None) -> int:
        """Announce `doubts.marked` `{count}`: how many doubts the notes mark now."""
        vault, _ = await self.sessions.consumer_scope(user_id)
        marks = await asyncio.to_thread(doubt_marks, vault, subject_id, topic_id)
        self.hub.publish(
            subject_id, topic_id, DOUBTS_MARKED, {"count": marks.count}, user_id=user_id
        )
        return marks.count

    async def prepare(
        self, subject_id: str, topic_id: str, *, user_id: str | None = None
    ) -> int | None:
        """Review the relevant doubts without a question and announce the marks; their count."""
        vault, sync = await self.sessions.consumer_scope(user_id)
        generator = self.generator
        if (
            generator is not None
            and generator.holder(subject_id, topic_id, user_id=user_id) == "editor"
        ):
            return None  # "prepárame el tema" is rewriting the notes; it schedules us again
        plan = await asyncio.to_thread(ask_plan, vault, subject_id, topic_id)
        if plan.to_review:
            await self._review(
                vault,
                sync,
                subject_id,
                topic_id,
                plan.to_review[: self.review_batch],
                user_id,
            )
        return await self.marked(subject_id, topic_id, user_id=user_id)

    # -- showing one -----------------------------------------------------------------------

    async def show(
        self, subject_id: str, topic_id: str, pending_id: str, *, user_id: str | None = None
    ) -> ShownDoubt:
        """Bring the open doubt `pending_id` to the chat (module docstring).

        Raises:
            UnknownDoubtError, DoubtClosedError, OpenSessionError: nothing asked.
        """
        vault, sync = await self.sessions.consumer_scope(user_id)
        doubt = await asyncio.to_thread(open_doubt, vault, subject_id, topic_id, pending_id)
        item_id = doubt.item.id
        generator = self.generator
        if (
            doubt.question is None
            and generator is not None
            and generator.holder(subject_id, topic_id, user_id=user_id) != "editor"
        ):
            reviewed = await self._review(vault, sync, subject_id, topic_id, [item_id], user_id)
            if reviewed is not None and item_id in reviewed.auto_resolved:
                await self.marked(subject_id, topic_id, user_id=user_id)
                return ShownDoubt(
                    pending_id=item_id,
                    asked=False,
                    status="auto_resolved",
                    summary=reviewed.summary,
                )
        turns = await asyncio.to_thread(doubt_chat_turns, vault, subject_id, topic_id)
        open_turns = [turn for turn in turns if turn.status == "open"]
        if open_turns and open_turns[-1].pending_id == item_id:
            last = open_turns[-1]
            asked = AskedDoubt(
                pending_id=item_id,
                kind=last.kind,
                text=last.text,
                question=last.question,
                suggestions=last.suggestions,
                options=last.options,
                refs=last.refs,
            )
        else:
            asked = await ask_in_chat(
                vault,
                subject_id,
                topic_id,
                item_id,
                sync=sync,
                host=self.sessions.host,
                live=self.live(subject_id, topic_id, user_id=user_id),
            )
        self.hub.publish(
            subject_id,
            topic_id,
            DOUBT_ASKED,
            asked.model_dump(
                mode="json",
                include={
                    "pending_id",
                    "kind",
                    "text",
                    "question",
                    "suggestions",
                    "options",
                    "refs",
                },
            ),
            user_id=user_id,
        )
        return ShownDoubt(pending_id=item_id, asked=True)


__all__ = ["REVIEW_BATCH", "DoubtChat", "ShownDoubt"]
