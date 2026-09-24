"""Git-backed content store: layout, writers, commit/push/pull, setup, SQLite index."""

from studentassistant.vault.subjects import (
    StoredSubject,
    SubjectError,
    SubjectFileError,
    SubjectNotFoundError,
    create_subject,
    get_subject,
    list_subjects,
    subject_directory,
)
from studentassistant.vault.topics import (
    StoredTopic,
    TopicError,
    TopicFileError,
    TopicNotFoundError,
    create_topic,
    get_topic,
    list_topics,
    topic_directory,
    topics_directory,
)
from studentassistant.vault.vault import (
    Vault,
    VaultError,
    VaultFormatError,
    VaultMetaError,
    VaultNotFoundError,
)

__all__ = [
    "StoredSubject",
    "StoredTopic",
    "SubjectError",
    "SubjectFileError",
    "SubjectNotFoundError",
    "TopicError",
    "TopicFileError",
    "TopicNotFoundError",
    "Vault",
    "VaultError",
    "VaultFormatError",
    "VaultMetaError",
    "VaultNotFoundError",
    "create_subject",
    "create_topic",
    "get_subject",
    "get_topic",
    "list_subjects",
    "list_topics",
    "subject_directory",
    "topic_directory",
    "topics_directory",
]
