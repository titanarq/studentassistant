"""The live observer loop (role `observer`, Sonnet): bus events in, validated state ops out.

`ObserverLoop` subscribes to the session bus and, per active session, keeps:

- the topic's folded state (`ObserverSnapshot`), loaded from the vault when it first sees the
  session (`load_observer_snapshot`) and advanced with every persisted event it receives;
- the pending batch: final segments, captures, page transcriptions, buttons, markers, commands
  and the student's own state ops (`context.batch_item`);
- an append-only conversation with Claude, persisted to `conversations/observer-<session>.jsonl`.

Triggers: a batch is sent once `batch_segments` final segments or `batch_speech_seconds` of speech
are waiting, or at once for a capture, a page transcription or a source switch. One call per
session is in flight at a time; what arrives meanwhile coalesces into the next batch. The consumer
never waits for Claude, and the bus never waits for the consumer, so capture is never slowed down.

Each answer must call the strict tool `apply_state_ops` (`tool_choice: auto` plus the instruction
of the `observer` prompt, ADR-0004). Every op is parsed (`parse_op`) and validated against the
current state (`validate_op`, in order, on a working copy); valid ones are published as
`observer.state_op` events (origin `observer`). Invalid ones (or a missing call) are re-asked once,
with the errors as the tool result; what is still invalid then is logged and dropped. The next
batch waits until the consumer has folded the ops just published.

Caching: the system prompt and the topic block (subject, title, digest) plus the tool form the
cached prefix; the conversation opens with the state at the start (`render_state`) and grows by
one user turn per batch, with a second breakpoint on its newest block, so each call re-reads the
conversation from the cache. Resuming a session (or starting a new one of the topic) always builds
a fresh conversation from snapshot + digest, never from the full history.

Cost caps (#120): the client is bound to the session's ledger, so a reached cap raises
`CostCapReachedError` before anything is sent. The observer then pauses: the batch is kept, an
`observer.status` event (`status: paused`, `cap`, `limit_usd`, `total_usd`) is published once, and
the next trigger tries again; `status: running` is published when a call succeeds again. Any other
Claude failure keeps the batch too and publishes `status: error`. Every failed call (any
`LLMError` but a reached cap, or an unexpected error of the batch) is also published as a notice
`observer.call_failed` (not persisted; `kind`: `unavailable` | `refused` | `invalid` | `error`,
and `reason`, the error's text for the logs), which the server counts per session (#262).

Pending-review queue (#55): whenever a folded event changes the topic's pending items (an
`add_pending`, a merge into an open item, a `resolve_pending` of any origin), the loop publishes a
`notice` (not persisted; the gateway forwards it to the phone as the protocol `notice`) with the
open count as `pending_count`, and regenerates `review/pending.yaml` from the fold
(`write_pending_review`). The count is also published when the loop starts observing a session, so
the phone's counter is right from the start. The student is never interrupted: only the counter.

Ending: `flush(session_id)` is the `add_before_ended` hook -- it waits for the call in flight and
sends what is still waiting, so the ops land before `session.ended`. `session.ended` forgets the
session.

Catch-up (#176): every answered batch is acknowledged with a persisted `observer.ack` event
(`catchup.py`). A batch whose call outlives the end hook's timeout, or that a crash or a stop
interrupts, stays unacknowledged, so the next time the loop opens a session of the topic (the next
session, a resume, a restart) it sends those events first, as the catch-up batch. No batch is lost,
whatever the end hook's timeout.

Context purge (#60): the conversation never grows without bound. After an answered batch whose
call left the conversation at `context_max_tokens` or more (the call's prompt plus its answer), the
conversation is dropped and the next call opens a new one: the same system blocks and tool (so the
cached prefix still hits), then one user turn with the state folded so far (`render_state`), the
newest `context_tail_segments` answered segment lines and the batch. Nothing is lost, because the
state is the fold of the events and every dropped batch was acknowledged before the drop, so the
catch-up is unaffected. The rollover is published as a persisted `observer.context_rolled` event
(`before_tokens`, and `after_tokens`: the new conversation's first prompt) once the first call of
the new conversation is answered, and a `context` record (reason `rollover`) marks it in the
conversation file, which keeps the dropped turns as history and is never read back into context.
At session end (`flush`) the conversation is always dropped the same way (reason `session_end`,
`after_tokens: 0`): the next session of the topic starts from snapshot + digest.

The observer reaches the bus only through the protocols below (it never imports
`studentassistant.server`) and the vault only through `studentassistant.vault`.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, get_args

from pydantic import BaseModel, ValidationError

from studentassistant.config import ObserverSettings, Settings
from studentassistant.llm import (
    CostCapReachedError,
    LedgerBinding,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMRetriesExhaustedError,
    LLMTransientError,
    RefusalError,
    StructuredOutputError,
    Transport,
    get_client,
    load_prompt,
    strict_tool,
)
from studentassistant.observer.catchup import ACK_EVENT_KIND, CatchUp, ack_payload, unanswered
from studentassistant.observer.context import (
    BATCH_KINDS,
    OBSERVER_ORIGIN,
    BatchItem,
    batch_item,
    render_batch,
    render_state,
    render_topic,
)
from studentassistant.observer.fold import ObserverStateError, apply_op
from studentassistant.observer.loader import load_observer_snapshot
from studentassistant.observer.ops import (
    STATE_OP_EVENT_KIND,
    StateOp,
    op_payload,
    parse_op,
)
from studentassistant.observer.pending import pending_review
from studentassistant.observer.snapshot import ObserverSnapshot, advance_snapshot
from studentassistant.observer.state import EventRef, TopicState
from studentassistant.vault import (
    ConversationRecord,
    Event,
    SecretRefused,
    Session,
    Vault,
    VaultError,
    append_conversation_record,
    get_subject,
    get_topic,
    read_topic_events,
    write_pending_review,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "apply_state_ops"
TOOL_DESCRIPTION = (
    "Apply state ops to the record of the session's topic. Call it exactly once per batch, "
    "with every op the batch calls for (an empty list when nothing changes)."
)
PROMPT_NAME = "observer"
STATUS_EVENT_KIND = "observer.status"
CONTEXT_ROLLED_EVENT_KIND = "observer.context_rolled"
"""The persisted event of a context purge (#60): `reason`, `before_tokens`, `after_tokens`..."""
NOTICE_EVENT_KIND = "notice"
CALL_FAILED_EVENT_KIND = "observer.call_failed"
"""The notice of one failed observer call (#262): `kind` (`CallFailureKind`) and `reason`."""
"""The transient bus event the gateway forwards to the phone as the protocol `notice`."""
SESSION_STARTED = "session.started"
SESSION_RESUMED = "session.resumed"
SESSION_ENDED = "session.ended"
OBSERVER_KINDS = BATCH_KINDS | {SESSION_STARTED, SESSION_RESUMED, SESSION_ENDED}

OBSERVER_QUEUE_SIZE = 1024
"""The observer's bus subscription bound (persisted events are never dropped; see the bus)."""

Status = Literal["running", "paused", "error"]
CallFailureKind = Literal["unavailable", "refused", "invalid", "error"]
DigestReader = Callable[[Vault, str, str], str | None]
ClientFactory = Callable[[LedgerBinding], LLMClient]


class ApplyStateOps(BaseModel):
    """The input of the `apply_state_ops` tool."""

    ops: list[StateOp]


def state_ops_tool() -> dict[str, Any]:
    """The strict `apply_state_ops` tool, each op's `op` a required one-value enum.

    The SDK's schema transform turns a `Literal` into a description only; the discriminator must
    be a real constraint for the model to pick the right op shape, so it is set here.
    """
    tool = strict_tool(TOOL_NAME, TOOL_DESCRIPTION, ApplyStateOps)
    definitions = tool["input_schema"].get("$defs", {})
    for name, model in _OP_MODELS.items():
        definition = definitions.get(name)
        if definition is None or "op" not in definition.get("properties", {}):
            continue
        definition["properties"]["op"] = {
            "type": "string",
            "enum": [model.model_fields["op"].default],
        }
        required = definition.setdefault("required", [])
        if "op" not in required:
            required.insert(0, "op")
    return tool


# Every op model by class name (the `$defs` key of the tool schema).
_OP_MODELS: dict[str, type[BaseModel]] = {
    model.__name__: model for model in get_args(get_args(StateOp)[0])
}


# -- the bus, as the observer sees it ----------------------------------------------------------


class BusEventLike(Protocol):
    """What the observer reads of a bus event (`studentassistant.server.bus.BusEvent`)."""

    @property
    def session_id(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def origin(self) -> Any: ...
    @property
    def t(self) -> int: ...
    @property
    def payload(self) -> Mapping[str, Any]: ...
    @property
    def seq(self) -> int | None: ...


class SubscriptionLike(Protocol):
    def __aiter__(self) -> AsyncIterator[BusEventLike]: ...
    def __len__(self) -> int: ...
    def close(self) -> None: ...


class EventBus(Protocol):
    """The bus surface the observer uses (`SessionBus` has it)."""

    def subscribe(
        self,
        *,
        name: str = ...,
        session_id: str | None = ...,
        kinds: Collection[str] | None = ...,
        maxsize: int | None = ...,
    ) -> SubscriptionLike: ...

    def publish(
        self,
        session_id: str,
        kind: str,
        origin: Any,
        payload: Mapping[str, Any] | None = ...,
        *,
        persist: bool = ...,
        t: int | None = ...,
    ) -> Awaitable[BusEventLike]: ...


SessionLookup = Callable[[str], Session | None]


def _no_digest(_vault: Vault, _subject: str, _topic: str) -> str | None:
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def default_client_factory(
    settings: Settings | None = None, transport: Transport | None = None
) -> ClientFactory:
    """Observer clients from `[llm.roles.observer]`, capped and recorded on the given ledger."""

    def build(binding: LedgerBinding) -> LLMClient:
        return get_client("observer", settings=settings, transport=transport, ledger=binding)

    return build


# -- one observed session ----------------------------------------------------------------------


@dataclass
class _Ask:
    """The outcome of checking one answer: ops to publish and why the rest were refused."""

    ops: list[StateOp] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    called: bool = False


@dataclass(frozen=True)
class _Rollover:
    """A context purge whose new conversation has not been answered yet."""

    before_tokens: int
    dropped_turns: int
    tail_segments: int


def _failure_kind(error: LLMError) -> CallFailureKind:
    """What a failed call is to the student: Claude out of reach, a refusal, a bad answer."""
    if isinstance(error, LLMTransientError | LLMRetriesExhaustedError):
        return "unavailable"
    if isinstance(error, RefusalError):
        return "refused"
    if isinstance(error, StructuredOutputError):
        return "invalid"
    return "error"


def _prompt_tokens(response: LLMResponse) -> int:
    """Every token of the prompt of a call, cached or not."""
    usage = response.usage
    return usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens


@dataclass
class _Observed:
    session: Session
    client: LLMClient
    snapshot: ObserverSnapshot
    system: list[str]
    # The text opening the conversation; `None` after a rollover, rendered by the next call.
    context: str | None
    conversation: list[dict[str, Any]] = field(default_factory=list)
    items: list[BatchItem] = field(default_factory=list)
    # tool_use ids of the last answer, with the result text the next user turn owes each of them.
    owed_results: list[tuple[str, str, bool]] = field(default_factory=list)
    call: asyncio.Task[None] | None = None
    awaiting: EventRef | None = None
    status: Status = "running"
    # After a failed call (a cost cap, an API error) the kept batch waits for a new item, so a
    # failing call is never retried in a loop.
    blocked: bool = False
    batches: int = 0
    # Context purge (#60): the conversation's size after its newest answer, the batch lines of the
    # newest answered segments (bounded by `context_tail_segments`) and a pending rollover.
    context_tokens: int = 0
    tail: deque[str] = field(default_factory=deque)
    rollover: _Rollover | None = None

    @property
    def id(self) -> str:
        return self.session.id

    @property
    def conversation_name(self) -> str:
        return f"observer-{self.session.id}"

    def folded_past(self, ref: EventRef) -> bool:
        cursor = self.snapshot.cursor
        return cursor is not None and cursor.key() >= ref.key()


class ObserverLoop:
    """The app-wide live observer: `start()` subscribes, `stop()` ends it, `flush()` ends a session.

    `lookup` gives an attached session's vault handle; `client_factory` builds the observer client
    of a session from its ledger binding (tests pass `default_client_factory(transport=fake)`);
    `digest` reads a topic's digest (`digest.topic_digest`); `clock` stamps the
    conversation records.
    """

    def __init__(
        self,
        bus: EventBus,
        lookup: SessionLookup,
        *,
        settings: ObserverSettings | None = None,
        client_factory: ClientFactory | None = None,
        digest: DigestReader = _no_digest,
        clock: Callable[[], datetime] = _utc_now,
        queue_size: int = OBSERVER_QUEUE_SIZE,
    ) -> None:
        self.bus = bus
        self.lookup = lookup
        self.settings = settings or ObserverSettings()
        self.client_factory = client_factory or default_client_factory()
        self.digest = digest
        self.clock = clock
        self.queue_size = queue_size
        self.prompt = load_prompt(PROMPT_NAME)
        self.tool = state_ops_tool()
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._observed: dict[str, _Observed] = {}
        self._ignored: set[str] = set()
        self._busy = False
        self._settled = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Subscribe to the bus and start consuming; call it inside the running event loop."""
        if self._subscription is not None:
            return
        self._subscription = self.bus.subscribe(
            name="observer", kinds=OBSERVER_KINDS, maxsize=self.queue_size
        )
        self._task = asyncio.create_task(self._run(self._subscription), name="observer")

    async def stop(self) -> None:
        """Stop receiving, fold what was delivered, wait for the calls in flight, end the task."""
        subscription, task = self._subscription, self._task
        if subscription is None or task is None:
            return
        subscription.close()
        try:
            await task
        finally:
            self._subscription = None
            self._task = None
            self._settled.set()
        calls = [o.call for o in self._observed.values() if o.call is not None]
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        self._observed.clear()

    async def drain(self) -> None:
        """Return once every event delivered to the observer so far has been consumed."""
        await asyncio.sleep(0)
        while (
            self.running
            and self._subscription is not None
            and (len(self._subscription) or self._busy)
        ):
            self._settled.clear()
            await self._settled.wait()

    async def wait_idle(self, session_id: str) -> None:
        """Return once `session_id` has no call in flight and its published ops are folded.

        Calls are awaited shielded, so cancelling this wait (a timeout) never cancels a call.
        """
        await self.drain()
        while True:
            observed = self._observed.get(session_id)
            if observed is None or observed.call is None:
                return
            await asyncio.shield(observed.call)
            await self.drain()

    async def flush(self, session_id: str) -> None:
        """Send what `session_id` still has waiting and wait for it (the before-ended hook).

        A call in flight is waited for, never cancelled (the end hook's timeout leaves it running);
        its ops are published if the session is still on the bus by then.
        """
        await self.wait_idle(session_id)
        observed = self._observed.get(session_id)
        if observed is not None and observed.call is None and observed.items:
            self._start_call(observed)
            await self.wait_idle(session_id)
        observed = self._observed.get(session_id)
        if observed is not None and observed.call is None:
            await self._end_context(observed)

    def status(self, session_id: str) -> Status | None:
        """`running`, `paused` (a cost cap) or `error` for an observed session, else `None`."""
        observed = self._observed.get(session_id)
        return None if observed is None else observed.status

    # -- consumer ----------------------------------------------------------------------------

    async def _run(self, subscription: SubscriptionLike) -> None:
        async for event in subscription:
            self._busy = True
            try:
                await self._on_event(event)
            except Exception:
                logger.exception(
                    "the observer failed on a %s event of session %s", event.kind, event.session_id
                )
            finally:
                self._busy = False
                if not len(subscription):
                    self._settled.set()

    async def _on_event(self, event: BusEventLike) -> None:
        session_id = event.session_id
        if event.kind == SESSION_ENDED:
            self._observed.pop(session_id, None)
            self._ignored.discard(session_id)
            return
        if session_id in self._ignored:
            return
        observed = self._observed.get(session_id)
        if observed is None or event.kind in (SESSION_STARTED, SESSION_RESUMED):
            if observed is not None and observed.call is not None:
                return  # a resume while a call is in flight: keep the conversation going
            observed = await self._open(
                session_id, resumed=event.kind == SESSION_RESUMED, trigger=_ref(event)
            )
            if observed is None:
                return
        if event.seq is not None and self._fold(observed, event):
            await self._pending_changed(observed)
        item = batch_item(event.kind, str(event.origin), event.t, event.payload)
        if item is not None:
            observed.items.append(item.at(_ref(event)))
            observed.blocked = False
        self._maybe_start(observed)

    async def _open(
        self, session_id: str, *, resumed: bool, trigger: EventRef | None
    ) -> _Observed | None:
        session = self.lookup(session_id)
        if session is None:
            logger.warning("no open vault session %s for the observer; not observed", session_id)
            self._ignored.add(session_id)
            return None
        vault, subject, topic = session.vault, session.subject_slug, session.topic_slug
        try:
            snapshot, subject_name, topic_title, digest, catch_up = await asyncio.to_thread(
                self._read_topic, vault, subject, topic, session_id, trigger
            )
        except (VaultError, ObserverStateError):
            logger.exception("the observer cannot load topic %s/%s; not observed", subject, topic)
            self._ignored.add(session_id)
            return None
        observed = _Observed(
            session=session,
            client=self.client_factory(LedgerBinding(vault, subject, topic, session_id)),
            snapshot=snapshot,
            system=[self.prompt.content, render_topic(subject_name, topic_title, digest)],
            context=render_state(snapshot.state, session_id=session_id, resumed=resumed),
            tail=deque(maxlen=self.settings.context_tail_segments),
        )
        self._observed[session_id] = observed
        observed.items = self._catch_up_items(observed, catch_up)
        await self._notify_pending(observed)
        await self._record(
            observed,
            ConversationRecord(
                time=self.clock(),
                kind="context",
                model=observed.client.model,
                prompt_hash=self.prompt.hash,
                detail={
                    "reason": "resume" if resumed else "start",
                    "event_count": snapshot.event_count,
                    "cursor": None if snapshot.cursor is None else snapshot.cursor.model_dump(),
                    "catch_up": len(catch_up.events),
                },
            ),
        )
        if not catch_up.acknowledged:
            # The topic's first acknowledgement: what came before is not replayed.
            await self._acknowledge(observed, catch_up.last, baseline=True)
        return observed

    def _read_topic(
        self, vault: Vault, subject: str, topic: str, session_id: str, trigger: EventRef | None
    ) -> tuple[ObserverSnapshot, str, str, str | None, CatchUp]:
        snapshot = load_observer_snapshot(vault, subject, topic)
        catch_up = unanswered(
            read_topic_events(vault, subject, topic), session_id=session_id, before=trigger
        )
        subject_name = get_subject(vault, subject).subject.name
        topic_title = get_topic(vault, subject, topic).topic.title
        digest = self.digest(vault, subject, topic)
        return snapshot, subject_name, topic_title, digest, catch_up

    def _catch_up_items(self, observed: _Observed, catch_up: CatchUp) -> list[BatchItem]:
        """The unanswered events as the first batch, sent at once; `[]` when there are none."""
        items = [
            item.at(EventRef(session_id=event_session, seq=event.seq))
            for event_session, event in catch_up.events
            if (item := batch_item(event.kind, str(event.origin), event.t, event.payload))
            is not None
        ]
        limit = self.settings.catch_up_max_items
        if len(items) > limit:
            logger.warning(
                "observer of session %s: %d unanswered events, only the newest %d are caught up",
                observed.id,
                len(items),
                limit,
            )
            items = items[len(items) - limit :] if limit else []
        if not items:
            return []
        sessions = sorted({item.ref.session_id for item in items if item.ref is not None})
        header = BatchItem(
            line=(
                f"catch-up: {len(items)} events of session {', '.join(sessions)} were stored but"
                " never answered (a session end or an interruption came first); handle them as"
                " any batch"
            ),
            immediate=True,
        )
        logger.info("observer of session %s: catching up %d events", observed.id, len(items))
        return [header, *items]

    def _fold(self, observed: _Observed, event: BusEventLike) -> bool:
        """Fold one persisted event; whether it changed the topic's pending items."""
        assert event.seq is not None
        ref = EventRef(session_id=event.session_id, seq=event.seq)
        if observed.folded_past(ref):
            return False  # already in the snapshot loaded from the vault
        before = observed.snapshot.state.pending
        stored = Event(
            seq=event.seq,
            t=event.t,
            origin=event.origin,
            kind=event.kind,
            payload=dict(event.payload),
        )
        try:
            observed.snapshot = advance_snapshot(observed.snapshot, [(event.session_id, stored)])
        except ObserverStateError:
            logger.exception("the observer cannot fold an event of session %s", observed.id)
            # Keep the cursor moving, so the batches that wait for this event are not stuck.
            observed.snapshot = observed.snapshot.model_copy(
                update={"cursor": ref, "event_count": observed.snapshot.event_count + 1}
            )
        return observed.snapshot.state.pending != before

    # -- pending-review queue ----------------------------------------------------------------

    async def _pending_changed(self, observed: _Observed) -> None:
        """Publish the open count and regenerate `review/pending.yaml` from the fold."""
        await self._notify_pending(observed)
        session = observed.session
        review = pending_review(observed.snapshot.state)
        try:
            await asyncio.to_thread(
                write_pending_review,
                session.vault,
                session.subject_slug,
                session.topic_slug,
                review,
            )
        except SecretRefused:
            logger.warning("the pending review of session %s looks like a secret", observed.id)
        except (VaultError, OSError):
            logger.exception("the pending review of session %s cannot be written", observed.id)

    async def _notify_pending(self, observed: _Observed) -> None:
        count = len(observed.snapshot.state.open_pending())
        try:
            await self.bus.publish(
                observed.id,
                NOTICE_EVENT_KIND,
                OBSERVER_ORIGIN,
                {"pending_count": count},
                persist=False,
            )
        except Exception as error:  # the session ended meanwhile
            logger.warning(
                "observer of session %s: pending count not published: %s", observed.id, error
            )

    # -- batches -----------------------------------------------------------------------------

    def _due(self, items: list[BatchItem]) -> bool:
        if any(item.immediate for item in items):
            return True
        segments = sum(1 for item in items if item.segment)
        speech = sum(item.speech_seconds for item in items)
        return (
            segments >= self.settings.batch_segments or speech >= self.settings.batch_speech_seconds
        )

    def _maybe_start(self, observed: _Observed) -> None:
        if observed.call is not None or not observed.items or observed.blocked:
            return
        if observed.awaiting is not None and not observed.folded_past(observed.awaiting):
            return  # the ops just published are not folded yet
        if self._due(observed.items):
            self._start_call(observed)

    def _start_call(self, observed: _Observed) -> None:
        items, observed.items = observed.items, []
        observed.call = asyncio.create_task(
            self._call(observed, items), name=f"observer:{observed.id}"
        )

    async def _call(self, observed: _Observed, items: list[BatchItem]) -> None:
        try:
            await self._send_batch(observed, items)
        except Exception as error:
            logger.exception("the observer call of session %s failed", observed.id)
            await self._report_failure(observed, "error", repr(error))
        finally:
            observed.call = None
            if self._observed.get(observed.id) is observed:
                self._maybe_start(observed)

    async def _send_batch(self, observed: _Observed, items: list[BatchItem]) -> None:
        observed.batches += 1
        content = self._owed_results(observed)
        if not observed.conversation:
            if observed.context is None:
                observed.context = await self._rolled_context(observed)
            content.append({"type": "text", "text": observed.context})
        content.append({"type": "text", "text": render_batch(items, observed.batches)})
        turn = {"role": "user", "content": content}
        response = await self._ask(observed, turn)
        if response is None:
            observed.batches -= 1
            observed.items[:0] = items  # kept for the next trigger
            observed.blocked = True
            return
        try:
            await self._answer(observed, items, response)
        finally:
            # Answered (even if a re-ask failed): never caught up again.
            await self._acknowledge(observed, _newest(items))
        observed.tail.extend(item.line for item in items if item.segment)
        if observed.context_tokens >= self.settings.context_max_tokens:
            self._roll_over(observed)

    # -- context purge (#60) -----------------------------------------------------------------

    def _roll_over(self, observed: _Observed) -> None:
        """Drop the conversation; the next call opens a new one from the state and the tail.

        Only between batches: the batch that reached the threshold is answered and acknowledged,
        so its tool results are owed to nobody and the catch-up never needs the dropped turns.
        """
        logger.info(
            "observer of session %s: context at %d tokens, rolled over to snapshot + digest + tail",
            observed.id,
            observed.context_tokens,
        )
        observed.rollover = _Rollover(
            before_tokens=observed.context_tokens,
            dropped_turns=len(observed.conversation),
            tail_segments=len(observed.tail),
        )
        observed.conversation = []
        observed.owed_results = []
        observed.context = None

    async def _rolled_context(self, observed: _Observed) -> str:
        """The opening text of the conversation after a rollover, recorded as a `context` record.

        Rendered when the next call is made, so it holds every op folded since the rollover.
        """
        snapshot = observed.snapshot
        rollover = observed.rollover
        await self._record(
            observed,
            ConversationRecord(
                time=self.clock(),
                kind="context",
                model=observed.client.model,
                prompt_hash=self.prompt.hash,
                detail={
                    "reason": "rollover",
                    "before_tokens": None if rollover is None else rollover.before_tokens,
                    "event_count": snapshot.event_count,
                    "cursor": None if snapshot.cursor is None else snapshot.cursor.model_dump(),
                    "tail_segments": len(observed.tail),
                },
            ),
        )
        return render_state(
            snapshot.state, session_id=observed.id, resumed=False, rolled=True, tail=observed.tail
        )

    async def _rolled(self, observed: _Observed, response: LLMResponse) -> None:
        """The first call after a rollover was answered: publish `observer.context_rolled`."""
        rollover, observed.rollover = observed.rollover, None
        assert rollover is not None
        await self._publish_rolled(
            observed,
            {
                "reason": "threshold",
                "before_tokens": rollover.before_tokens,
                "after_tokens": _prompt_tokens(response),
                "threshold_tokens": self.settings.context_max_tokens,
                "dropped_turns": rollover.dropped_turns,
                "tail_segments": rollover.tail_segments,
            },
        )

    async def _end_context(self, observed: _Observed) -> None:
        """At session end the conversation is always dropped (the next session starts afresh)."""
        if not observed.conversation and observed.rollover is None:
            return  # no call yet, or already dropped
        payload = {
            "reason": "session_end",
            "before_tokens": observed.context_tokens,
            "after_tokens": 0,
            "threshold_tokens": self.settings.context_max_tokens,
            "dropped_turns": len(observed.conversation),
            "tail_segments": 0,
        }
        observed.conversation = []
        observed.owed_results = []
        observed.rollover = None
        observed.context = None
        observed.context_tokens = 0
        await self._record(
            observed,
            ConversationRecord(
                time=self.clock(),
                kind="context",
                model=observed.client.model,
                prompt_hash=self.prompt.hash,
                detail={"reason": "session_end", "before_tokens": payload["before_tokens"]},
            ),
        )
        await self._publish_rolled(observed, payload)

    async def _publish_rolled(self, observed: _Observed, payload: dict[str, Any]) -> None:
        try:
            await self.bus.publish(observed.id, CONTEXT_ROLLED_EVENT_KIND, OBSERVER_ORIGIN, payload)
        except Exception as error:  # the session ended under the call
            logger.warning(
                "observer of session %s: context rollover not recorded: %s", observed.id, error
            )

    async def _answer(
        self, observed: _Observed, items: list[BatchItem], response: LLMResponse
    ) -> None:
        working = observed.snapshot.state
        checked, working = self._check(response, working)
        published = await self._publish(observed, checked.ops)
        if checked.errors or not checked.called:
            reasons = checked.errors or [f"you did not call the `{TOOL_NAME}` tool"]
            observed.owed_results = [
                (call.id, "Errors:\n" + "\n".join(reasons), True) for call in response.tool_calls
            ]
            text = (
                f"{len(checked.ops)} ops were applied. These were refused:\n"
                + "\n".join(f"- {reason}" for reason in reasons)
                + f"\nCall `{TOOL_NAME}` again with only the corrected ops (or an empty list)."
            )
            content = [*self._owed_results(observed), {"type": "text", "text": text}]
            retry = await self._ask(observed, {"role": "user", "content": content})
            if retry is None:
                return
            again, _ = self._check(retry, working)
            published += await self._publish(observed, again.ops)
            if again.errors or not again.called:
                logger.warning(
                    "observer of session %s: dropped after one re-ask: %s",
                    observed.id,
                    "; ".join(again.errors or ["no apply_state_ops call"]),
                )
            response, checked = retry, again
        observed.owed_results = [
            (call.id, f"Applied {len(checked.ops)} ops.", False) for call in response.tool_calls
        ]
        logger.info(
            "observer of session %s: batch %d, %d items, %d ops published",
            observed.id,
            observed.batches,
            len(items),
            published,
        )

    @staticmethod
    def _owed_results(observed: _Observed) -> list[dict[str, Any]]:
        results = [
            {"type": "tool_result", "tool_use_id": tool_id, "content": text}
            | ({"is_error": True} if is_error else {})
            for tool_id, text, is_error in observed.owed_results
        ]
        observed.owed_results = []
        return results

    async def _ask(self, observed: _Observed, turn: dict[str, Any]) -> LLMResponse | None:
        """One call with `turn` appended; `None` (and the turn not kept) when it failed."""
        messages = [*observed.conversation, _with_breakpoint(turn)]
        try:
            response = await observed.client.create(
                messages,
                system=observed.system,
                tools=[self.tool],
                tool_choice={"type": "auto"},
                prompt_hash=self.prompt.hash,
            )
        except CostCapReachedError as error:
            await self._set_status(
                observed,
                "paused",
                str(error),
                {"cap": error.cap, "limit_usd": error.limit_usd, "total_usd": error.total_usd},
            )
            self._restore_owed(observed, turn)
            return None
        except LLMError as error:
            logger.warning("observer call of session %s failed: %s", observed.id, error)
            await self._report_failure(observed, _failure_kind(error), str(error))
            await self._set_status(observed, "error", str(error), {})
            self._restore_owed(observed, turn)
            return None
        observed.conversation.append(turn)
        observed.conversation.append(response.assistant_turn())
        observed.context_tokens = _prompt_tokens(response) + response.usage.output_tokens
        await self._record(
            observed, ConversationRecord(time=self.clock(), kind="user", message=turn)
        )
        await self._record(
            observed,
            ConversationRecord(
                time=self.clock(),
                kind="assistant",
                message=response.assistant_turn(),
                model=response.model,
                prompt_hash=self.prompt.hash,
                usage=response.usage.model_dump(),
            ),
        )
        if observed.status != "running":
            await self._set_status(observed, "running", "", {})
        if observed.rollover is not None:
            await self._rolled(observed, response)
        return response

    @staticmethod
    def _restore_owed(observed: _Observed, turn: dict[str, Any]) -> None:
        # The tool results the failed turn carried are still owed by the next one.
        observed.owed_results = [
            (block["tool_use_id"], block["content"], bool(block.get("is_error")))
            for block in turn["content"]
            if block.get("type") == "tool_result"
        ]

    @staticmethod
    def _check(response: LLMResponse, state: TopicState) -> tuple[_Ask, TopicState]:
        """The valid ops of `response`, in order, and the state after them; errors for the rest."""
        result = _Ask()
        if response.stop_reason == "refusal":
            result.called = True
            result.errors.append("the request was declined")
            return result, state
        for call in response.tool_calls:
            if call.name != TOOL_NAME:
                result.errors.append(f"unknown tool `{call.name}`")
                continue
            result.called = True
            try:
                data = call.parsed_input()
            except ValueError as error:
                result.errors.append(f"the tool input is not valid JSON: {error}")
                continue
            raw_ops = data.get("ops") if isinstance(data, dict) else None
            if not isinstance(raw_ops, list):
                result.errors.append("the tool input has no `ops` list")
                continue
            for index, raw in enumerate(raw_ops):
                if not isinstance(raw, dict):
                    result.errors.append(f"op {index} is not an object")
                    continue
                try:
                    op = parse_op(raw)
                except ValidationError as error:
                    detail = "; ".join(
                        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
                    )
                    result.errors.append(f"op {index} is malformed ({detail})")
                    continue
                try:
                    state = apply_op(state, op, _PENDING_REF)
                except ObserverStateError as error:
                    result.errors.append(f"op {index} ({op.op}) cannot be applied: {error}")
                    continue
                result.ops.append(op)
        return result, state

    async def _publish(self, observed: _Observed, ops: list[StateOp]) -> int:
        count = 0
        for op in ops:
            try:
                event = await self.bus.publish(
                    observed.id, STATE_OP_EVENT_KIND, OBSERVER_ORIGIN, op_payload(op)
                )
            except Exception as error:  # the session ended under the call, a refused secret...
                logger.warning(
                    "observer of session %s could not publish a %s op: %s",
                    observed.id,
                    op.op,
                    error,
                )
                continue
            count += 1
            if event.seq is not None:
                observed.awaiting = EventRef(session_id=observed.id, seq=event.seq)
        return count

    async def _acknowledge(
        self, observed: _Observed, through: EventRef | None, *, baseline: bool = False
    ) -> None:
        """Publish `observer.ack` up to `through` (a batch with no stored event needs none)."""
        if through is None and not baseline:
            return
        try:
            await self.bus.publish(
                observed.id, ACK_EVENT_KIND, OBSERVER_ORIGIN, ack_payload(through)
            )
        except Exception as error:  # the session ended under the call: caught up next time
            logger.warning(
                "observer of session %s: batch not acknowledged, it will be caught up: %s",
                observed.id,
                error,
            )

    async def _report_failure(
        self, observed: _Observed, kind: CallFailureKind, reason: str
    ) -> None:
        try:
            await self.bus.publish(
                observed.id,
                CALL_FAILED_EVENT_KIND,
                OBSERVER_ORIGIN,
                {"kind": kind, "reason": reason},
                persist=False,
            )
        except Exception as error:  # the session ended meanwhile
            logger.warning("observer of session %s: failure not published: %s", observed.id, error)

    async def _set_status(
        self, observed: _Observed, status: Status, reason: str, detail: Mapping[str, Any]
    ) -> None:
        if observed.status == status:
            return
        observed.status = status
        try:
            await self.bus.publish(
                observed.id,
                STATUS_EVENT_KIND,
                OBSERVER_ORIGIN,
                {"status": status, "reason": reason, **detail},
            )
        except Exception as error:
            logger.warning(
                "observer of session %s: status %s not published: %s", observed.id, status, error
            )
        await self._record(
            observed,
            ConversationRecord(
                time=self.clock(), kind="status", detail={"status": status, "reason": reason}
            ),
        )

    async def _record(self, observed: _Observed, record: ConversationRecord) -> None:
        session = observed.session
        try:
            await asyncio.to_thread(
                append_conversation_record,
                session.vault,
                session.subject_slug,
                session.topic_slug,
                observed.conversation_name,
                record,
            )
        except SecretRefused:
            logger.warning(
                "an observer conversation record of session %s looks like a secret; not written",
                observed.id,
            )
        except (VaultError, OSError):
            logger.exception(
                "the observer conversation of session %s cannot be written", observed.id
            )


def _ref(event: BusEventLike) -> EventRef | None:
    return None if event.seq is None else EventRef(session_id=event.session_id, seq=event.seq)


def _newest(items: list[BatchItem]) -> EventRef | None:
    refs = [item.ref for item in items if item.ref is not None]
    return max(refs, key=EventRef.key) if refs else None


# Validation applies ops to a working copy of the state; the ref only fills the models' field.
_PENDING_REF = EventRef(session_id="pending", seq=1)


def _with_breakpoint(turn: dict[str, Any]) -> dict[str, Any]:
    """A copy of `turn` whose last block is a cache breakpoint (the conversation so far)."""
    marked = copy.deepcopy(turn)
    content = marked["content"]
    if content:
        content[-1]["cache_control"] = {"type": "ephemeral"}
    return marked


__all__ = [
    "CONTEXT_ROLLED_EVENT_KIND",
    "NOTICE_EVENT_KIND",
    "OBSERVER_KINDS",
    "STATUS_EVENT_KIND",
    "TOOL_NAME",
    "ApplyStateOps",
    "ObserverLoop",
    "default_client_factory",
    "state_ops_tool",
]
