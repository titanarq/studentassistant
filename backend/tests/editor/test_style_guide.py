"""The subject style guide (`editor.style_guide`): rules, confirmed additions, edits, use."""

from __future__ import annotations

import subprocess

import pytest

from generate_topic import GenerateTopic, make_topic
from studentassistant.editor.inputs import assemble_input
from studentassistant.editor.style_guide import (
    MAX_RULES,
    InvalidStyleRuleError,
    add_style_rules,
    parse_rules,
    read_style_guide,
    replace_style_rules,
)
from studentassistant.llm import load_prompt
from studentassistant.vault import (
    GitSync,
    SubjectNotFoundError,
    Vault,
    create_subject,
    get_subject,
)


@pytest.fixture
def sync(tmp_vault: Vault) -> GitSync:
    return GitSync(tmp_vault)


def _subject(vault: Vault, guide: str | None = None) -> str:
    return create_subject(vault, "Historia", style_guide=guide).slug


def _log(vault: Vault, commit: str) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s", commit],
        cwd=vault.path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_rules_are_the_lines_of_the_guide_without_list_markers() -> None:
    assert parse_rules(None) == []
    assert parse_rules("Fechas en negrita.\n\n-  Sin   abreviaturas.\n* Tablas.\n• Ejemplos.") == [
        "Fechas en negrita.",
        "Sin abreviaturas.",
        "Tablas.",
        "Ejemplos.",
    ]


def test_confirmed_rules_are_appended_and_committed(tmp_vault: Vault, sync: GitSync) -> None:
    subject = _subject(tmp_vault, "Fechas en negrita.")

    guide = add_style_rules(
        tmp_vault,
        subject,
        ["  Usa tablas   para comparar. ", "fechas en negrita.", "Usa tablas para comparar."],
        sync=sync,
    )

    assert guide.rules == ["Fechas en negrita.", "Usa tablas para comparar."]
    assert guide.added == ["Usa tablas para comparar."]
    assert get_subject(tmp_vault, subject).subject.style_guide == (
        "Fechas en negrita.\n- Usa tablas para comparar.\n"
    )
    assert guide.commit is not None
    assert _log(tmp_vault, guide.commit) == (
        f"Guía de estilo de {subject}: «Usa tablas para comparar.»"
    )
    # Nothing new: nothing written, no commit.
    again = add_style_rules(tmp_vault, subject, ["Usa tablas para comparar."], sync=sync)
    assert again.added == [] and again.commit is None


def test_the_whole_list_can_be_edited_or_cleared(tmp_vault: Vault, sync: GitSync) -> None:
    subject = _subject(tmp_vault, "Fechas en negrita.\n- Tablas.\n")

    unchanged = replace_style_rules(
        tmp_vault, subject, ["Fechas en negrita.", "Tablas."], sync=sync
    )
    assert unchanged.commit is None
    assert get_subject(tmp_vault, subject).subject.style_guide == "Fechas en negrita.\n- Tablas.\n"

    edited = replace_style_rules(
        tmp_vault, subject, ["Tablas para comparar.", "Fechas en negrita."], sync=sync
    )
    assert edited.rules == ["Tablas para comparar.", "Fechas en negrita."]
    assert get_subject(tmp_vault, subject).subject.style_guide == (
        "- Tablas para comparar.\n- Fechas en negrita.\n"
    )
    assert edited.commit is not None
    assert _log(tmp_vault, edited.commit) == f"Guía de estilo de {subject} editada"

    cleared = replace_style_rules(tmp_vault, subject, [], sync=sync)
    assert cleared.rules == [] and get_subject(tmp_vault, subject).subject.style_guide is None


def test_bad_rules_write_nothing(tmp_vault: Vault, sync: GitSync) -> None:
    subject = _subject(tmp_vault, "Fechas en negrita.")

    with pytest.raises(InvalidStyleRuleError, match="vacía"):
        add_style_rules(tmp_vault, subject, ["Tablas.", "  - "], sync=sync)
    with pytest.raises(InvalidStyleRuleError, match="demasiado larga"):
        replace_style_rules(tmp_vault, subject, ["x" * 301], sync=sync)
    with pytest.raises(InvalidStyleRuleError, match=f"como mucho {MAX_RULES}"):
        add_style_rules(tmp_vault, subject, [f"Regla {n}." for n in range(MAX_RULES)], sync=sync)
    with pytest.raises(SubjectNotFoundError):
        read_style_guide(tmp_vault, "no-existe")
    assert read_style_guide(tmp_vault, subject).rules == ["Fechas en negrita."]


def test_a_confirmed_rule_is_given_to_the_editor_for_every_topic(
    tmp_vault: Vault, sync: GitSync
) -> None:
    topic: GenerateTopic = make_topic(tmp_vault)
    add_style_rules(tmp_vault, topic.subject, ["Pon siempre un ejemplo."], sync=sync)

    assembled = assemble_input(
        tmp_vault, topic.subject, topic.topic, prompt=load_prompt("editor_generate")
    )

    block = assembled.system[1]
    assert "Guía de estilo de la asignatura:" in block
    assert "Pon las definiciones en negrita.\n- Pon siempre un ejemplo." in block
