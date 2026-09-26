"""Voice-command grammar (ADR-0006): configurable Spanish phrases -> command names.

The grammar is a YAML mapping of command name -> `phrases` (any of them fires the command) and an
optional `exclude` (a matching exclude phrase suppresses it). The packaged `commands.yaml` is the
default; `[stt] commands_path` points at a replacement. The file is loaded and validated when the
app is built, so a broken grammar fails there with an error naming the file, never at the first
utterance.

Phrases and transcripts are compared after the same `normalise`: lower case, accents stripped,
punctuation removed, whitespace collapsed.

`CommandMatcher` applies a grammar to the partials and finals of transcript segments and says which
commands fire, each at most once per segment.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
