"""The per-topic LLM cost ledger: appending entries, reading one topic's and the whole vault's."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from secret_samples import ANTHROPIC_KEY

from studentassistant.vault import (
    LedgerEntry,
    LedgerError,
    SecretRefused,
    SubjectNotFoundError,
    TopicNotFoundError,
    Vault,
    append_ledger_entry,
    create_subject,
    create_topic,
    ledger_path,
    read_all_ledgers,
    read_ledger,
)


def entry(subject: str, topic: str, **overrides: object) -> LedgerEntry:
    """A priced editor call on `subject`/`topic`, with any field replaced by `overrides`."""
    fields: dict[str, object] = {
        "time": datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
        "role": "editor",
        "model": "claude-opus-test",
        "prompt_hash": "abc123",
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_tokens": 800,
        "cache_write_tokens": 0,
        "estimated_usd": 0.042,
        "subject": subject,
        "topic": topic,
        "session": "20260924-100000",
    }
    fields.update(overrides)
    return LedgerEntry.model_validate(fields)


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    """`(subject slug, topic slug)` of one topic of `tmp_vault`."""
    subject = create_subject(tmp_vault, "Matemáticas II").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


def test_entries_round_trip_in_append_order(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, slug = topic
    first = entry(subject, slug)
    second = entry(subject, slug, role="observer", estimated_usd=None, session=None)
    append_ledger_entry(tmp_vault, subject, slug, first)
    append_ledger_entry(tmp_vault, subject, slug, second)

    assert read_ledger(tmp_vault, subject, slug) == [first, second]
    lines = ledger_path(tmp_vault, subject, slug).read_text(encoding="utf-8").splitlines()
    assert list(json.loads(lines[0])) == list(LedgerEntry.model_fields)


def test_a_topic_nobody_spent_on_reads_as_empty_and_has_no_file(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, slug = topic
    assert read_ledger(tmp_vault, subject, slug) == []
    assert not ledger_path(tmp_vault, subject, slug).exists()


def test_an_unknown_topic_or_subject_is_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, slug = topic
    with pytest.raises(TopicNotFoundError):
        append_ledger_entry(tmp_vault, subject, "integrales", entry(subject, "integrales"))
    with pytest.raises(TopicNotFoundError):
        read_ledger(tmp_vault, subject, "integrales")
    with pytest.raises(SubjectNotFoundError):
        append_ledger_entry(tmp_vault, "fisica", slug, entry("fisica", slug))
    with pytest.raises(SubjectNotFoundError):
        read_ledger(tmp_vault, "fisica", slug)


def test_an_entry_of_another_topic_is_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, slug = topic
    with pytest.raises(LedgerError):
        append_ledger_entry(tmp_vault, subject, slug, entry(subject, "otra"))
    assert not ledger_path(tmp_vault, subject, slug).exists()


def test_a_line_carrying_a_secret_is_refused_and_nothing_is_written(
    tmp_vault: Vault, topic: tuple[str, str]
) -> None:
    subject, slug = topic
    with pytest.raises(SecretRefused):
        append_ledger_entry(tmp_vault, subject, slug, entry(subject, slug, model=ANTHROPIC_KEY))
    assert not ledger_path(tmp_vault, subject, slug).exists()


def test_a_torn_last_line_is_ignored_on_read(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, slug = topic
    kept = entry(subject, slug)
    append_ledger_entry(tmp_vault, subject, slug, kept)
    with ledger_path(tmp_vault, subject, slug).open("ab") as log:
        log.write(b'{"time":"2026-09-24T10:05:00Z","role":"edi')

    assert read_ledger(tmp_vault, subject, slug) == [kept]


def test_the_whole_vault_reader_covers_every_topic_of_every_subject(tmp_vault: Vault) -> None:
    maths = create_subject(tmp_vault, "Matemáticas II").slug
    physics = create_subject(tmp_vault, "Física").slug
    derivatives = create_topic(tmp_vault, maths, "Derivadas").slug
    integrals = create_topic(tmp_vault, maths, "Integrales").slug
    optics = create_topic(tmp_vault, physics, "Óptica").slug
    create_topic(tmp_vault, physics, "Ondas")  # no ledger: contributes nothing
    written = [
        entry(maths, derivatives),
        entry(maths, derivatives, role="observer"),
        entry(maths, integrals),
        entry(physics, optics),
    ]
    for item in written:
        append_ledger_entry(tmp_vault, item.subject, item.topic, item)

    assert sorted(read_all_ledgers(tmp_vault), key=written.index) == written
    assert list(read_all_ledgers(Vault.open(tmp_vault.path))) == [
        entry(physics, optics),
        entry(maths, derivatives),
        entry(maths, derivatives, role="observer"),
        entry(maths, integrals),
    ]


def test_an_empty_vault_has_no_ledger_entries(tmp_vault: Vault) -> None:
    assert list(read_all_ledgers(tmp_vault)) == []


def test_time_must_be_timezone_aware_and_is_kept_in_utc(topic: tuple[str, str]) -> None:
    subject, slug = topic
    with pytest.raises(ValidationError):
        entry(subject, slug, time=datetime(2026, 9, 24, 10, 0))
    madrid = timezone(timedelta(hours=2))
    stored = entry(subject, slug, time=datetime(2026, 9, 24, 12, 0, tzinfo=madrid))
    assert stored.time == datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    assert stored.time.utcoffset() == timedelta(0)


def test_negative_token_counts_are_refused(topic: tuple[str, str]) -> None:
    subject, slug = topic
    with pytest.raises(ValidationError):
        entry(subject, slug, input_tokens=-1)
