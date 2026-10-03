"""The root of every refusal the vault raises, in a module any vault module can import.

`vault.py` re-exports `VaultError`, which is where callers have always imported it from; it lives
here so the low-level writers (`files.py`, `secrets.py`, `jsonl.py`) can raise a `VaultError` too
without importing `vault.py`, which imports them.

The user errors live here for the same reason, one step further along: `vault.py` raises
`UserNotFoundError` from `Vault.for_user`, and `users.py` (which imports `vault.py` for the handle)
raises it too, so neither of them can be where it is declared.
"""

from __future__ import annotations


class VaultError(Exception):
    """A vault this backend refuses to create or to use; the message says why."""


class UserError(VaultError):
    """A user of the vault this backend refuses to open, read or write."""


class UserNotFoundError(UserError):
    """There is no user of that id in the vault: no `users/<id>/profile.json`."""
