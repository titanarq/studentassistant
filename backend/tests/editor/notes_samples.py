"""The sample notes the editor tests read, under `tests/fixtures/notes/`."""

from __future__ import annotations

from pathlib import Path

NOTES_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "notes"
# The session id the fixtures' transcript footnotes cite.
FIXTURE_SESSION = "20260924-183000"


def read_fixture(name: str) -> str:
    """The fixture's exact text: bytes decoded as UTF-8, newlines untouched."""
    return (NOTES_FIXTURES / name).read_bytes().decode("utf-8")
