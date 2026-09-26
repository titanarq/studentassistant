"""The voice-command grammar: packaged default, `[stt] commands_path`, errors, normalisation."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from studentassistant.config import Settings
from studentassistant.stt.commands import (
    DEFAULT_GRAMMAR_PATH,
    CommandGrammar,
    GrammarError,
    load_grammar,
    normalise,
)

DEFAULT_COMMANDS = {
    "capture": (["mira aquí", "mira esto", "captura", "haz foto"], ["mira, aquí no"]),
    "next_page": (["siguiente", "pasamos página"], []),
    "important": (["esto es importante", "importante"], []),
    "source_book": (["ahora el libro", "mira el libro"], []),
    "source_notes": (["vuelvo a mis apuntes"], []),
    "source_pdf": (["ahora el pdf", "mira el pdf"], []),
    "pause": (["pausa", "para un momento"], []),
    "resume": (["reanuda", "seguimos"], []),
    "end_and_prepare": (["ya está, prepárame el tema"], []),
    "web_search": (["busca en internet"], []),
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """`SA_CONFIG` at a file under `tmp_path`, with every `SA_*` of the environment dropped."""
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    path = tmp_path / "config.toml"
    monkeypatch.setenv("SA_CONFIG", str(path))
    return path


def write(path: Path, contents: str) -> Path:
    path.write_text(textwrap.dedent(contents), encoding="utf-8")
    return path


def test_default_grammar_is_package_data() -> None:
    assert DEFAULT_GRAMMAR_PATH.name == "commands.yaml"
    assert DEFAULT_GRAMMAR_PATH.is_file()
    assert DEFAULT_GRAMMAR_PATH.parent.name == "stt"


@pytest.mark.parametrize(("name", "expected"), DEFAULT_COMMANDS.items())
def test_default_grammar_defines_command(name: str, expected: tuple[list[str], list[str]]) -> None:
    phrases, exclude = expected
    spec = load_grammar(None).commands[name]

    assert set(phrases) <= set(spec.phrases)
    assert set(exclude) <= set(spec.exclude)


def test_default_grammar_has_every_command() -> None:
    assert set(DEFAULT_COMMANDS) <= set(load_grammar().commands)


def test_override_path_wins(tmp_path: Path) -> None:
    path = write(
        tmp_path / "mine.yaml",
        """
        capture:
          phrases: ["foto ya"]
          exclude: ["foto ya no"]
        """,
    )

    grammar = load_grammar(path)

    assert list(grammar.commands) == ["capture"]
    assert grammar.commands["capture"].phrases == ["foto ya"]
    assert grammar.commands["capture"].exclude == ["foto ya no"]
    assert grammar.commands["capture"].normalised_exclude() == ["foto ya no"]


def test_exclude_defaults_to_empty(tmp_path: Path) -> None:
    grammar = load_grammar(write(tmp_path / "g.yaml", "pause:\n  phrases: [pausa]\n"))

    assert grammar.commands["pause"].exclude == []


def test_commands_path_setting_defaults_to_unset(clean_env: Path) -> None:
    assert Settings().stt.commands_path is None


def test_commands_path_from_toml_and_env(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write(clean_env, f'[stt]\ncommands_path = "{tmp_path / "toml.yaml"}"\n')
    assert Settings().stt.commands_path == tmp_path / "toml.yaml"

    monkeypatch.setenv("SA_STT__COMMANDS_PATH", str(tmp_path / "env.yaml"))
    assert Settings().stt.commands_path == tmp_path / "env.yaml"


def test_commands_path_expands_user(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SA_STT__COMMANDS_PATH", "~/grammar.yaml")

    assert Settings().stt.commands_path == Path("~/grammar.yaml").expanduser()


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param("capture: [unclosed", id="not-yaml"),
        pytest.param("- capture\n- pause\n", id="not-a-mapping"),
        pytest.param("", id="empty-file"),
        pytest.param("{}\n", id="no-commands"),
        pytest.param("capture: {}\n", id="missing-phrases"),
        pytest.param("capture:\n  phrases: []\n", id="empty-phrases"),
        pytest.param("capture:\n  phrases: ['¡!']\n", id="phrase-without-words"),
        pytest.param("capture:\n  phrases: [captura]\n  typo: [x]\n", id="unknown-key"),
        pytest.param("Capture Now:\n  phrases: [captura]\n", id="bad-command-name"),
    ],
)
def test_invalid_grammar_error_names_the_file(tmp_path: Path, contents: str) -> None:
    path = write(tmp_path / "broken.yaml", contents)

    with pytest.raises(GrammarError, match="broken.yaml"):
        load_grammar(path)


def test_unreadable_grammar_error_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"

    with pytest.raises(GrammarError, match="missing.yaml"):
        load_grammar(path)


def test_grammar_is_a_value_error() -> None:
    assert issubclass(GrammarError, ValueError)
    assert isinstance(load_grammar(), CommandGrammar)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mira aquí", "mira aqui"),
        ("MIRA AQUÍ", "mira aqui"),
        ("¡Mira, aquí!", "mira aqui"),
        ("mira,aquí", "mira aqui"),
        ("  mira \t\n aquí  ", "mira aqui"),
        ("Pasamos página.", "pasamos pagina"),
        ("Ya está, prepárame el tema", "ya esta preparame el tema"),
        ("¿Qué es la desamortización?", "que es la desamortizacion"),
        ("pingüino", "pinguino"),
        ("año", "ano"),
        ("mira — aquí...", "mira aqui"),
        ("mira_aquí", "mira aqui"),
        ("", ""),
        ("¡¿?!", ""),
    ],
)
def test_normalise(text: str, expected: str) -> None:
    assert normalise(text) == expected


def test_phrase_and_transcript_normalise_alike() -> None:
    spec = load_grammar().commands["end_and_prepare"]

    assert normalise("YA ESTÁ... prepárame el tema!") in spec.normalised_phrases()
