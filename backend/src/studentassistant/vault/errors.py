"""The root of every refusal the vault raises, in a module any vault module can import.

`vault.py` re-exports `VaultError`, which is where callers have always imported it from; it lives
here so the low-level writers (`files.py`, `secrets.py`, `jsonl.py`) can raise a `VaultError` too
without importing `vault.py`, which imports them.
"""

from __future__ import annotations


class VaultError(Exception):
    """A vault this backend refuses to create or to use; the message says why."""
