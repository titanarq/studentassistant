"""Topic state: the derived files under a topic's `state/` directory.

What lives there is an optimisation, never the source of truth: `state/observer-snapshot.json` is
the observer's fold of the topic's event logs (ADR-0003), which can always be rebuilt by replaying
them. The vault stores it so the observer never touches a vault file itself (ADR-0002), but the
vault does not know what it holds: the caller passes the Pydantic model in, and this module only
writes it atomically, through the secret guard, and reads it back into the model it is given.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from studentassistant.vault.errors import VaultError
from studentassistant.vault.files import write_json_atomic
from studentassistant.vault.topics import get_topic, topic_directory
from studentassistant.vault.vault import Vault

STATE_DIRNAME = "state"
OBSERVER_SNAPSHOT_FILE_NAME = "observer-snapshot.json"


class StateError(VaultError):
    """A topic state file this backend cannot write or read; the message says why."""


class SnapshotFileError(StateError):
    """`state/observer-snapshot.json` exists but is not readable as the snapshot model asked for."""


def state_directory(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """Where a topic's state files live, whether or not it has any yet."""
    return topic_directory(vault, subject_slug, topic_slug) / STATE_DIRNAME


def observer_snapshot_path(vault: Vault, subject_slug: str, topic_slug: str) -> Path:
    """The path of a topic's observer snapshot, whether or not one was written."""
    return state_directory(vault, subject_slug, topic_slug) / OBSERVER_SNAPSHOT_FILE_NAME


def write_observer_snapshot(
    vault: Vault, subject_slug: str, topic_slug: str, snapshot: BaseModel
) -> Path:
    """Write `snapshot` as JSON to the topic's `state/observer-snapshot.json`; return its path.

    The write is atomic, so a reader sees the previous snapshot or the whole new one.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read; nothing is written.
        SecretRefused: when the snapshot looks like it carries a key; the previous one stays.
        OSError: when the directory or the file cannot be written.
    """
    get_topic(vault, subject_slug, topic_slug)
    path = observer_snapshot_path(vault, subject_slug, topic_slug)
    path.parent.mkdir(exist_ok=True)
    write_json_atomic(path, snapshot)
    return path


def read_observer_snapshot[M: BaseModel](
    vault: Vault, subject_slug: str, topic_slug: str, model: type[M]
) -> M | None:
    """The topic's observer snapshot validated into `model`, or `None` when none was written.

    Raises:
        SubjectNotFoundError, SubjectFileError, TopicNotFoundError, TopicFileError: when the topic
            is not one this backend can read.
        SnapshotFileError: when the file cannot be read, is not JSON or does not hold a `model`.
    """
    get_topic(vault, subject_slug, topic_slug)
    path = observer_snapshot_path(vault, subject_slug, topic_slug)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise SnapshotFileError(f"{path} cannot be read: {error}") from error
    try:
        return model.model_validate(json.loads(text))
    except json.JSONDecodeError as error:
        raise SnapshotFileError(f"{path} is not JSON: {error}") from error
    except ValidationError as error:
        raise SnapshotFileError(f"{path} does not hold a {model.__name__}: {error}") from error
