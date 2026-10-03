"""Helpers the user tests share: a user a handle can be opened on, and where its folder lives."""

from __future__ import annotations

import json
from pathlib import Path

from studentassistant.vault import Vault
from studentassistant.vault.vault import USER_PROFILE_NAME, USERS_DIRNAME

# A profile's `created_at` is never what these tests assert on, so it is a fixed one: a user a
# test writes is the same bytes every time, which keeps a failure about the content and not the
# clock of the machine that ran it.
CREATED_AT = "2026-10-03T09:00:00+00:00"


def user_directory(vault: Vault, user_id: str) -> Path:
    """Where `user_id`'s folder lives in `vault`, whether or not anything has written it yet."""
    return vault.root / USERS_DIRNAME / user_id


def add_user(vault: Vault, user_id: str, name: str) -> Vault:
    """Give `vault` a user called `user_id`, and return the handle on that user's content.

    Writes the `profile.json` `for_user` looks for and nothing else: `create_user` (#546) is what
    creates a user in the product, with the id it derives from the name and the `subjects/` folder
    a clone needs, and the tests of the handle itself need a user to exist, not to be created the
    product's way.
    """
    directory = user_directory(vault, user_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / USER_PROFILE_NAME).write_text(
        json.dumps(
            {
                "id": user_id,
                "name": name,
                "email": None,
                "photo": None,
                "created_at": CREATED_AT,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return vault.for_user(user_id)


def everything_under(directory: Path) -> list[Path]:
    """Every path below `directory`, relative to it and sorted: a test's before/after of a disk."""
    return sorted(path.relative_to(directory) for path in directory.rglob("*"))
