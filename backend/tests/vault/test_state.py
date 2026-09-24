"""The observer snapshot store: `state/observer-snapshot.json` written and read back."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel
from secret_samples import ANTHROPIC_KEY

from studentassistant.vault import (
    SecretRefused,
    SnapshotFileError,
    TopicNotFoundError,
    Vault,
    VaultError,
    create_subject,
    create_topic,
    read_observer_snapshot,
    write_observer_snapshot,
)
from studentassistant.vault.state import observer_snapshot_path


class Snapshot(BaseModel):
    last_seq: dict[str, int]
    topics: list[str]
    note: str | None = None


@pytest.fixture
def topic(tmp_vault: Vault) -> tuple[str, str]:
    subject = create_subject(tmp_vault, "Matemáticas II").slug
    return subject, create_topic(tmp_vault, subject, "Derivadas").slug


def test_a_snapshot_round_trips(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    snapshot = Snapshot(last_seq={"20260924-180000": 12}, topics=["regla de la cadena"])

    path = write_observer_snapshot(tmp_vault, *topic, snapshot)

    assert path == observer_snapshot_path(tmp_vault, *topic)
    assert path.relative_to(tmp_vault.path).as_posix() == (
        "subjects/matematicas-ii/topics/derivadas/state/observer-snapshot.json"
    )
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot.model_dump(mode="json")
    assert path.read_text(encoding="utf-8").endswith("}\n")
    assert read_observer_snapshot(tmp_vault, *topic, Snapshot) == snapshot


def test_a_second_write_replaces_the_first(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    write_observer_snapshot(tmp_vault, *topic, Snapshot(last_seq={}, topics=[]))
    newer = Snapshot(last_seq={"20260924-180000": 3}, topics=["límites"])

    write_observer_snapshot(tmp_vault, *topic, newer)

    assert read_observer_snapshot(tmp_vault, *topic, Snapshot) == newer
    leftovers = [p.name for p in observer_snapshot_path(tmp_vault, *topic).parent.iterdir()]
    assert leftovers == ["observer-snapshot.json"]


def test_the_same_snapshot_is_the_same_bytes(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    snapshot = Snapshot(last_seq={"b": 2, "a": 1}, topics=["x"])
    path = write_observer_snapshot(tmp_vault, *topic, snapshot)
    first = path.read_bytes()

    write_observer_snapshot(tmp_vault, *topic, snapshot.model_copy(deep=True))

    assert path.read_bytes() == first


def test_a_missing_snapshot_is_none(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    assert read_observer_snapshot(tmp_vault, *topic, Snapshot) is None


@pytest.mark.parametrize(
    "content",
    [b"{not json", b'{"topics": "no es una lista"}', b"\xff\xfe"],
    ids=["not-json", "wrong-shape", "not-utf8"],
)
def test_an_unreadable_snapshot_is_a_vault_error(
    tmp_vault: Vault, topic: tuple[str, str], content: bytes
) -> None:
    path = observer_snapshot_path(tmp_vault, *topic)
    path.parent.mkdir()
    path.write_bytes(content)

    with pytest.raises(SnapshotFileError) as caught:
        read_observer_snapshot(tmp_vault, *topic, Snapshot)
    assert isinstance(caught.value, VaultError)
    assert str(path) in str(caught.value)


def test_a_snapshot_carrying_a_secret_is_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    good = Snapshot(last_seq={}, topics=["límites"])
    write_observer_snapshot(tmp_vault, *topic, good)

    with pytest.raises(SecretRefused):
        write_observer_snapshot(
            tmp_vault, *topic, Snapshot(last_seq={}, topics=[], note=ANTHROPIC_KEY)
        )

    assert read_observer_snapshot(tmp_vault, *topic, Snapshot) == good


def test_a_missing_topic_is_refused(tmp_vault: Vault, topic: tuple[str, str]) -> None:
    subject, _ = topic
    with pytest.raises(TopicNotFoundError):
        write_observer_snapshot(tmp_vault, subject, "integrales", Snapshot(last_seq={}, topics=[]))
    with pytest.raises(TopicNotFoundError):
        read_observer_snapshot(tmp_vault, subject, "integrales", Snapshot)
