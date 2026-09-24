"""The slugs the vault layout names its directories and files after."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_RUNS_THAT_ARE_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Turn a Spanish name into a lowercase ASCII slug: "Matemáticas II" -> "matematicas-ii".

    Accents are stripped (`ñ` becomes `n` too), every run of punctuation or whitespace becomes a
    single hyphen and the hyphens at either end disappear, so the result is a directory name that
    survives git, GitHub and a shell at any depth of the layout in `docs/modules/vault.md`.

    Raises:
        ValueError: when nothing of the name is a letter or a digit, because an empty slug is not
            a directory name and silently falling back to one would hide the student's mistake.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    ascii_only = without_accents.encode("ascii", "ignore").decode("ascii").lower()
    slug = _RUNS_THAT_ARE_NOT_SLUG.sub("-", ascii_only).strip("-")
    if not slug:
        raise ValueError(f"cannot make a slug out of {name!r}: it has no letter or digit in it")
    return slug


def unique_slug(base: str, taken: Iterable[str]) -> str:
    """Return `base` when it is free and the first `base-N` that is when it is not.

    `taken` is every slug that already exists among the future siblings: a subject's slug is unique
    among the subjects, a topic's slug among the topics of its own subject. Numbering starts at 2
    because the unnumbered slug is the first one.
    """
    already_taken = frozenset(taken)
    if base not in already_taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in already_taken:
        suffix += 1
    return f"{base}-{suffix}"
