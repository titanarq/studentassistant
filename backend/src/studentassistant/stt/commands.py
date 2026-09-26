"""Voice-command grammar (ADR-0006): configurable Spanish phrases -> command names.

The grammar is a YAML mapping of command name -> `phrases` (any of them fires the command) and an
optional `exclude` (a matching exclude phrase suppresses it). The packaged `commands.yaml` is the
default; `[stt] commands_path` points at a replacement. The file is loaded and validated when the
app is built, so a broken grammar fails there with an error naming the file, never at the first
utterance.

Phrases and transcripts are compared after the same `normalise`: lower case, accents stripped,
punctuation removed, whitespace collapsed.

`CommandMatcher` applies a grammar to the partials and finals of transcript segments and says which
commands fire, each at most once per segment. `CommandDetector` runs one matcher per session over
the bus' `transcript.partial` / `transcript.final` events and publishes what fires as persisted
`voice.command` events (origin `stt`); `capture` and `next_page` also publish a persisted `command`
`capture_now`, which the WebSocket gateway forwards to the capture client. It depends on the bus
only through `stt.pipeline.EventBus`, so `stt` never imports `studentassistant.server`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from studentassistant.stt.pipeline import (
    SESSION_ENDED,
    TRANSCRIPT_FINAL,
    EventBus,
    SubscriptionLike,
)

logger = logging.getLogger(__name__)

# The grammar shipped as package data, used when `[stt] commands_path` is unset.
DEFAULT_GRAMMAR_PATH = Path(__file__).resolve().parent / "commands.yaml"

# A command name is what `voice.command` events carry: a lower-case identifier.
COMMAND_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

_PUNCTUATION_OR_SPACE = re.compile(r"[\W_]+")


def normalise(text: str) -> str:
    """`text` as the grammar compares it: lower case, no accents or punctuation, single spaces.

    "¡Mira, AQUÍ!" -> "mira aqui". Punctuation becomes a word break, so "mira,aquí" is two words.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _PUNCTUATION_OR_SPACE.sub(" ", stripped).strip()


# Commands whose phrase is followed by a free-text query: they fire only on a final, carrying the
# text after the phrase, and not at all when that text is empty.
QUERY_COMMANDS = frozenset({"web_search"})


class GrammarError(ValueError):
    """A grammar file that cannot be read or is not a valid grammar; the message names the file."""


class CommandSpec(BaseModel):
    """One command's phrases and the exclude phrases that suppress it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phrases: list[str] = Field(min_length=1)
    exclude: list[str] = Field(default_factory=list)

    @field_validator("phrases", "exclude")
    @classmethod
    def phrases_have_words(cls, phrases: list[str]) -> list[str]:
        for phrase in phrases:
            if not normalise(phrase):
                raise ValueError(f"phrase {phrase!r} has no words")
        return phrases

    def normalised_phrases(self) -> list[str]:
        return [normalise(p) for p in self.phrases]

    def normalised_exclude(self) -> list[str]:
        return [normalise(p) for p in self.exclude]


class CommandGrammar(BaseModel):
    """Command name -> `CommandSpec`, in file order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commands: dict[str, CommandSpec] = Field(min_length=1)

    @field_validator("commands")
    @classmethod
    def names_are_identifiers(cls, commands: dict[str, CommandSpec]) -> dict[str, CommandSpec]:
        for name in commands:
            if not re.match(COMMAND_NAME_PATTERN, name):
                raise ValueError(f"command name {name!r} must match {COMMAND_NAME_PATTERN}")
        return commands


def load_grammar(path: Path | str | None = None) -> CommandGrammar:
    """The grammar in `path`, or the packaged default when `path` is None.

    Raises `GrammarError` naming the file when it cannot be read, is not YAML, or is not a mapping
    of command name -> `{phrases, exclude}`.
    """
    source = Path(path).expanduser() if path is not None else DEFAULT_GRAMMAR_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GrammarError(f"cannot read voice-command grammar {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise GrammarError(f"voice-command grammar {source} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise GrammarError(
            f"voice-command grammar {source} must be a mapping of command name -> "
            f"{{phrases, exclude}}, got {type(raw).__name__}"
        )
    try:
        return CommandGrammar(commands=raw)
    except ValidationError as exc:
        raise GrammarError(f"invalid voice-command grammar {source}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class _Word:
    """One normalised word of a transcript and the span of `text` it came from."""

    word: str
    start: int
    end: int


def _words(text: str) -> list[_Word]:
    """The normalised words of `text`, each with its span in `text`.

    A token is a run of characters that normalise to something or are combining marks; a token
    that normalises to several words (a ligature, a vulgar fraction) yields them all on its span.
    """
    words: list[_Word] = []
    start: int | None = None
    for i, char in enumerate([*text, " "]):
        is_word = bool(normalise(char)) or unicodedata.combining(char) != 0
        if is_word and start is None:
            start = i
        elif not is_word and start is not None:
            words.extend(_Word(w, start, i) for w in normalise(text[start:i]).split())
            start = None
    return words


def _occurrences(words: list[str], phrase: list[str]) -> Iterable[int]:
    """Start indexes where `phrase` appears in `words` as a contiguous whole-word sequence."""
    n = len(phrase)
    for i in range(len(words) - n + 1):
        if words[i : i + n] == phrase:
            yield i


def _could_grow_into(words: list[str], start: int, exclude: list[str]) -> bool:
    """Whether a tail of `words` that contains the match starting at `start` is a proper prefix
    of `exclude`, so a later partial may still turn the match into the exclude phrase."""
    for k in range(len(words) - start, len(exclude)):
        if k <= len(words) and words[len(words) - k :] == exclude[:k]:
            return True
    return False


@dataclass(frozen=True, slots=True)
class FiredCommand:
    """A command a segment's text fired: `text` is that partial or final as received, `query`
    the original text after the phrase for a `QUERY_COMMANDS` command, else None."""

    command: str
    segment_id: str
    text: str
    query: str | None = None


@dataclass(frozen=True, slots=True)
class _CompiledCommand:
    name: str
    phrases: tuple[tuple[str, ...], ...]
    exclude: tuple[tuple[str, ...], ...]


class CommandMatcher:
    """Pure per-segment matching of a `CommandGrammar` (ADR-0006).

    `match(segment_id, text, is_final)` is called with every partial and the final of each
    utterance, in arrival order, and returns the commands that fire now:

    - a phrase matches only as a whole-word sequence of the normalised text;
    - a matching `exclude` phrase of the same command suppresses it;
    - on a partial, a phrase match in the tail of the text that could still grow into one of the
      command's exclude phrases ("mira aquí" -> "mira aquí no") is deferred to the next partial
      or the final;
    - each command fires at most once per `segment_id`, at the first text that matches;
    - `QUERY_COMMANDS` fire only on the final, with the text after the phrase as `query`, and not
      when that query is empty.

    The only state is the set of commands already fired per segment; `forget` drops it.
    """

    def __init__(self, grammar: CommandGrammar) -> None:
        self._commands = [
            _CompiledCommand(
                name=name,
                phrases=tuple(tuple(p.split()) for p in spec.normalised_phrases()),
                exclude=tuple(tuple(p.split()) for p in spec.normalised_exclude()),
            )
            for name, spec in grammar.commands.items()
        ]
        self._fired: dict[str, set[str]] = {}

    def match(self, segment_id: str, text: str, is_final: bool) -> list[FiredCommand]:
        """The commands `text` fires now for `segment_id`, in grammar order."""
        tokens = _words(text)
        words = [t.word for t in tokens]
        fired = self._fired.setdefault(segment_id, set())
        result: list[FiredCommand] = []
        for command in self._commands:
            if command.name in fired:
                continue
            if any(next(_occurrences(words, list(e)), None) is not None for e in command.exclude):
                continue
            if command.name in QUERY_COMMANDS:
                if not is_final:
                    continue
                query = self._query(text, tokens, command)
                if query is None:
                    continue
                result.append(FiredCommand(command.name, segment_id, text, query))
                fired.add(command.name)
            elif self._matches(words, command, is_final):
                result.append(FiredCommand(command.name, segment_id, text))
                fired.add(command.name)
        return result

    def forget(self, segment_id: str) -> None:
        """Drop the debounce state of `segment_id` (a no-op for an unknown segment)."""
        self._fired.pop(segment_id, None)

    @staticmethod
    def _matches(words: list[str], command: _CompiledCommand, is_final: bool) -> bool:
        for phrase in command.phrases:
            for start in _occurrences(words, list(phrase)):
                if is_final or not any(
                    _could_grow_into(words, start, list(e)) for e in command.exclude
                ):
                    return True
        return False

    @staticmethod
    def _query(text: str, tokens: list[_Word], command: _CompiledCommand) -> str | None:
        """The original text after the first phrase occurrence, trimmed; None if it is empty."""
        words = [t.word for t in tokens]
        ends = [
            start + len(phrase)
            for phrase in command.phrases
            for start in _occurrences(words, list(phrase))
        ]
        if not ends:
            return None
        # Everything after the earliest phrase occurrence is the query.
        cut = min(tokens[end - 1].end for end in ends)
        query = text[cut:].lstrip(" \t\n,.;:-").rstrip()
        return query if normalise(query) else None


TRANSCRIPT_PARTIAL = "transcript.partial"
VOICE_COMMAND = "voice.command"
COMMAND = "command"
CAPTURE_NOW = "capture_now"
DETECTOR_KINDS = frozenset({TRANSCRIPT_PARTIAL, TRANSCRIPT_FINAL, SESSION_ENDED})

# Voice commands that ask the capture client for a burst of stills.
CAPTURE_COMMANDS = frozenset({"capture", "next_page"})

# Source-switch commands -> the `source` their `voice.command` payload carries.
SOURCE_COMMANDS = {"source_book": "book", "source_notes": "notes", "source_pdf": "pdf"}

DETECTOR_QUEUE_SIZE = 1024
"""The detector's subscription bound. Partials are notices, which a lagging subscription drops
before any persisted event; a dropped partial only delays a command to a later partial or the
final of the same segment."""


def voice_command_payload(fired: FiredCommand) -> dict[str, str]:
    """The `voice.command` event payload of a fired command."""
    payload = {"command": fired.command, "segment_id": fired.segment_id, "text": fired.text}
    if fired.command in SOURCE_COMMANDS:
        payload["source"] = SOURCE_COMMANDS[fired.command]
    if fired.query is not None:
        payload["query"] = fired.query
    return payload


class CommandDetector:
    """Detect voice commands in the bus' transcript events and publish them (ADR-0006).

    One `CommandMatcher` per session (segment ids are only unique within a session);
    `session.ended` drops it. Each fired command is a persisted `voice.command` at `t` = the
    segment's `session_start_ms`; `capture` / `next_page` also publish a persisted `command` whose
    `command_id` is `voice-<seq of the voice.command>`, unique within the session's event log.

    `start()`, `stop()` and `drain()` behave as `TranscriptPipeline`'s. A failure on one event is
    logged and the detector carries on.
    """

    def __init__(
        self, bus: EventBus, grammar: CommandGrammar, *, queue_size: int = DETECTOR_QUEUE_SIZE
    ) -> None:
        self.bus = bus
        self.grammar = grammar
        self.queue_size = queue_size
        self._matchers: dict[str, CommandMatcher] = {}
        self._subscription: SubscriptionLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._busy = False
        self._settled = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Subscribe to the bus and start consuming; call it inside the running event loop."""
        if self._subscription is not None:
            return
        self._subscription = self.bus.subscribe(
            name="stt:commands", kinds=DETECTOR_KINDS, maxsize=self.queue_size
        )
        self._task = asyncio.create_task(self._run(self._subscription), name="stt:commands")

    async def stop(self) -> None:
        """Stop receiving, handle what was already delivered, then end the consumer task."""
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

    async def drain(self) -> None:
        """Return once every event delivered to the detector so far has been handled."""
        await asyncio.sleep(0)
        while (
            self.running
            and self._subscription is not None
            and (len(self._subscription) or self._busy)
        ):
            self._settled.clear()
            await self._settled.wait()

    # -- consumer ------------------------------------------------------------------------------

    async def _run(self, subscription: SubscriptionLike) -> None:
        async for event in subscription:
            self._busy = True
            try:
                if event.kind == SESSION_ENDED:
                    self._matchers.pop(event.session_id, None)
                else:
                    await self._on_segment(
                        event.session_id, event.payload, event.kind == TRANSCRIPT_FINAL
                    )
            except Exception:
                logger.exception(
                    "the voice-command detector failed on a %s event of session %s",
                    event.kind,
                    event.session_id,
                )
            finally:
                self._busy = False
                if not len(subscription):
                    self._settled.set()

    async def _on_segment(
        self, session_id: str, payload: Mapping[str, object], is_final: bool
    ) -> None:
        segment_id, text, start_ms = (
            payload.get("segment_id"),
            payload.get("text"),
            payload.get("session_start_ms"),
        )
        if not (
            isinstance(segment_id, str) and isinstance(text, str) and isinstance(start_ms, int)
        ):
            logger.warning("a transcript segment of session %s is malformed; skipped", session_id)
            return
        matcher = self._matchers.get(session_id)
        if matcher is None:
            matcher = self._matchers[session_id] = CommandMatcher(self.grammar)
        for fired in matcher.match(segment_id, text, is_final):
            await self._publish(session_id, fired, start_ms)

    async def _publish(self, session_id: str, fired: FiredCommand, start_ms: int) -> None:
        voice = await self.bus.publish(
            session_id, VOICE_COMMAND, "stt", voice_command_payload(fired), t=start_ms
        )
        logger.info(
            "voice command %s fired in segment %s of session %s",
            fired.command,
            fired.segment_id,
            session_id,
            extra={"session_id": session_id, "command": fired.command},
        )
        if fired.command in CAPTURE_COMMANDS:
            await self.bus.publish(
                session_id,
                COMMAND,
                "stt",
                {
                    "command_id": f"voice-{voice.seq}",
                    "command": CAPTURE_NOW,
                    "voice_command": fired.command,
                    "segment_id": fired.segment_id,
                },
            )
