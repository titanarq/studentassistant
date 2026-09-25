"""The subject style guide: the student's general preferences, learned while revising the notes.

`subjects/<s>/subject.yaml` keeps a free-text `style_guide`; the editor reads it (the topic block
of `inputs.assemble_input`) whenever it writes or revises notes of any topic of that subject. This
module is its list-of-rules view and its only writer besides subject creation:

- **Rules**: one per non-blank line of the guide, a leading list marker (`- `, `* `, `• `)
  dropped and the spaces collapsed. What this module appends or writes is `- <rule>` lines, so a
  guide typed as a paragraph at subject creation keeps its text (each of its lines reads as a
  rule).
- **Proposals**: while revising (`revise.py`) the editor proposes a rule when an instruction of
  the student looks general ("me gustan las tablas para comparar", "siempre un ejemplo"); nothing
  is written until the student confirms it -- through `add_style_rules` (the server's
  `POST /api/subjects/{s}/style-guide/rules`) or by saying so in the chat, where the editor
  passes the confirmed proposal back (`EditsOutput.confirmed_style_rules`).
- **Edits**: `replace_style_rules` writes the whole list (the web's edit and delete).

Every write goes through `vault.set_style_guide` and is committed at once with a Spanish message
(`GitSync.checkpoint`). The functions are blocking; the server runs them in a worker thread.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from studentassistant.vault import GitSync, Vault, get_subject, set_style_guide

MAX_RULE_CHARS = 300
"""The longest rule, after collapsing spaces."""
MAX_RULES = 50
"""The most rules a guide may hold through this module."""

_MARKER = re.compile(r"^[-*•](?:\s+|$)")


class StyleGuideError(ValueError):
    """A style guide change that cannot be made; the message is Spanish, for the student."""


class InvalidStyleRuleError(StyleGuideError):
    """A rule is empty, too long, or the guide would hold too many."""


class StyleGuide(BaseModel):
    """The subject's style guide as a list of rules; `commit` is set when a change committed it."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    rules: list[str] = Field(default_factory=list)
    added: list[str] = Field(
        default_factory=list, description="The rules this change added (`add_style_rules`)."
    )
    commit: str | None = None


def normalize_rule(rule: str) -> str:
    """One rule as stored and compared: no list marker, spaces collapsed."""
    return " ".join(_MARKER.sub("", rule.strip()).split())


def parse_rules(style_guide: str | None) -> list[str]:
    """The rules of a guide's text, in order: one per non-blank line."""
    rules = (normalize_rule(line) for line in (style_guide or "").splitlines())
    return [rule for rule in rules if rule]


def format_rules(rules: list[str]) -> str | None:
    """The text `replace_style_rules` stores: `- <rule>` lines, or `None` for no rule."""
    return "".join(f"- {rule}\n" for rule in rules) or None


def rule_errors(rules: list[str]) -> list[str]:
    """Spanish messages for each rule that cannot be stored (already normalized)."""
    errors: list[str] = []
    for rule in rules:
        if not rule:
            errors.append("Una regla de estilo está vacía.")
        elif len(rule) > MAX_RULE_CHARS:
            errors.append(
                f"La regla de estilo «{rule[:40]}…» es demasiado larga (más de"
                f" {MAX_RULE_CHARS} caracteres): resúmela."
            )
    return errors


def new_rules(current: list[str], rules: list[str]) -> list[str]:
    """`rules` not already in `current` (nor repeated among themselves), compared ignoring case."""
    known = {rule.casefold() for rule in current}
    added: list[str] = []
    for rule in rules:
        if rule.casefold() not in known:
            known.add(rule.casefold())
            added.append(rule)
    return added


def read_style_guide(vault: Vault, subject_slug: str) -> StyleGuide:
    """The subject's rules (blocking, reads only).

    Raises:
        SubjectNotFoundError, SubjectFileError: the vault's errors for an unknown subject.
    """
    subject = get_subject(vault, subject_slug).subject
    return StyleGuide(subject=subject_slug, rules=parse_rules(subject.style_guide))


def append_rules(vault: Vault, subject_slug: str, rules: list[str]) -> list[str]:
    """Append the new ones of `rules` (normalized, checked) to the guide's text; no commit.

    The text before is kept as it is. Returns the rules added.
    """
    current = get_subject(vault, subject_slug).subject.style_guide
    added = new_rules(parse_rules(current), rules)
    if not added:
        return []
    lines = (current or "").rstrip().splitlines()
    lines.extend(f"- {rule}" for rule in added)
    set_style_guide(vault, subject_slug, "\n".join(lines).strip() + "\n")
    return added


TOO_MANY_RULES = f"La guía de estilo puede tener como mucho {MAX_RULES} reglas: quita alguna antes."


def _checked(rules: list[str]) -> list[str]:
    normalized = [normalize_rule(rule) for rule in rules]
    errors = rule_errors(normalized)
    if errors:
        raise InvalidStyleRuleError(" ".join(errors))
    return normalized


def add_style_rules(
    vault: Vault, subject_slug: str, rules: list[str], *, sync: GitSync
) -> StyleGuide:
    """Add the student's confirmed rules to the subject's guide and commit (blocking).

    A rule already there (ignoring case) is skipped; nothing new, nothing written.

    Raises:
        InvalidStyleRuleError: an empty or too long rule, or more than `MAX_RULES` in all;
            nothing written.
        SubjectNotFoundError, SubjectFileError: an unknown subject.
    """
    current = read_style_guide(vault, subject_slug).rules
    normalized = _checked(rules)
    fresh = new_rules(current, normalized)
    if len(current) + len(fresh) > MAX_RULES:
        raise InvalidStyleRuleError(TOO_MANY_RULES)
    added = append_rules(vault, subject_slug, normalized)
    commit = None
    if added:
        sync.note_change()
        commit = sync.checkpoint(
            f"Guía de estilo de {subject_slug}: " + "; ".join(f"«{rule}»" for rule in added)
        )
    guide = read_style_guide(vault, subject_slug)
    return guide.model_copy(update={"added": added, "commit": commit})


def replace_style_rules(
    vault: Vault, subject_slug: str, rules: list[str], *, sync: GitSync
) -> StyleGuide:
    """Write the whole list of rules (an edit, a deletion, a reorder) and commit (blocking).

    Repeated rules are kept once; an empty list clears the guide. When the rules are what the
    guide already reads as, nothing is written (a free-text guide is not reformatted).

    Raises:
        InvalidStyleRuleError, SubjectNotFoundError, SubjectFileError: as `add_style_rules`.
    """
    current = read_style_guide(vault, subject_slug).rules
    normalized = new_rules([], _checked(rules))
    if len(normalized) > MAX_RULES:
        raise InvalidStyleRuleError(TOO_MANY_RULES)
    commit = None
    if normalized != current:
        set_style_guide(vault, subject_slug, format_rules(normalized))
        sync.note_change()
        commit = sync.checkpoint(f"Guía de estilo de {subject_slug} editada")
    return read_style_guide(vault, subject_slug).model_copy(update={"commit": commit})


__all__ = [
    "MAX_RULES",
    "MAX_RULE_CHARS",
    "InvalidStyleRuleError",
    "StyleGuide",
    "StyleGuideError",
    "add_style_rules",
    "append_rules",
    "format_rules",
    "new_rules",
    "normalize_rule",
    "parse_rules",
    "read_style_guide",
    "replace_style_rules",
    "rule_errors",
]
