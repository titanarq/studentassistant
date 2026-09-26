"""Voice-command grammar (ADR-0006): configurable Spanish phrases -> command names.

The grammar is a YAML mapping of command name -> `phrases` (any of them fires the command) and an
optional `exclude` (a matching exclude phrase suppresses it). The packaged `commands.yaml` is the
default; `[stt] commands_path` points at a replacement. The file is loaded and validated when the
app is built, so a broken grammar fails there with an error naming the file, never at the first
utterance.

Phrases and transcripts are compared after the same `normalise`: lower case, accents stripped,
punctuation removed, whitespace collapsed.
"""

from __future__ import annotations

import re
import unicodedata
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
