"""Vault models: the defaults of each file, the format version gate and the required fields."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from studentassistant.vault.models import (
    DEFAULT_FIDELITY_MODE,
    FORMAT_VERSION,
    Subject,
    Topic,
    VaultFileModel,
    VaultMeta,
)

CREATED_AT = datetime(2026, 9, 24, 18, 30, tzinfo=UTC)


def test_vault_meta_defaults_to_the_format_version_this_backend_writes() -> None:
    meta = VaultMeta(created_at=CREATED_AT, student="Ana")

    assert meta.format_version == FORMAT_VERSION == 1


@pytest.mark.parametrize("format_version", [0, 2, 99])
def test_vault_meta_refuses_a_format_version_it_does_not_know(format_version: int) -> None:
    with pytest.raises(ValidationError, match="unsupported vault format version"):
        VaultMeta(format_version=format_version, created_at=CREATED_AT, student="Ana")


def test_subject_defaults_to_no_style_guide() -> None:
    assert Subject(name="Matemáticas").style_guide is None
    assert Subject(name="Matemáticas", style_guide="Define cada símbolo").style_guide == (
        "Define cada símbolo"
    )


def test_topic_defaults_to_strict_fidelity_and_no_session_yet() -> None:
    topic = Topic(title="Límites y continuidad", created_at=CREATED_AT)

    assert topic.fidelity_mode == DEFAULT_FIDELITY_MODE == "estricto"
    assert topic.sessions == []


def test_a_topic_keeps_the_sessions_it_is_given() -> None:
    topic = Topic(
        title="Límites y continuidad",
        fidelity_mode="flexible",
        created_at=CREATED_AT,
        sessions=["20260924-183000"],
    )

    assert topic.fidelity_mode == "flexible"
    assert topic.sessions == ["20260924-183000"]


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (VaultMeta, {"created_at": CREATED_AT, "student": "Ana"}),
        (Subject, {"name": "Matemáticas"}),
        (Topic, {"title": "Límites y continuidad", "created_at": CREATED_AT}),
    ],
)
def test_every_field_a_file_needs_is_required(
    model: type[VaultFileModel], fields: dict[str, datetime | str]
) -> None:
    for required in fields:
        incomplete = {name: value for name, value in fields.items() if name != required}

        with pytest.raises(ValidationError, match=required):
            model(**incomplete)


def test_a_key_the_model_does_not_declare_is_refused_instead_of_dropped() -> None:
    with pytest.raises(ValidationError, match="owner"):
        Topic(title="Límites y continuidad", created_at=CREATED_AT, owner="Ana")


@pytest.mark.parametrize(
    ("model", "order"),
    [
        (VaultMeta, ["format_version", "created_at", "student"]),
        (Subject, ["name", "style_guide"]),
        (Topic, ["title", "fidelity_mode", "created_at", "sessions"]),
    ],
)
def test_the_fields_are_declared_in_the_order_the_file_shows_them(
    model: type[VaultFileModel], order: list[str]
) -> None:
    assert list(model.model_fields) == order
