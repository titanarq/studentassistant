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
from studentassistant.vault.vault import (
    Vault,
    VaultError,
    VaultFormatError,
    VaultMetaError,
    VaultNotFoundError,
)

__all__ = [
    "StoredSubject",
    "SubjectError",
    "SubjectFileError",
    "SubjectNotFoundError",
    "Vault",
    "VaultError",
    "VaultFormatError",
    "VaultMetaError",
    "VaultNotFoundError",
    "create_subject",
    "get_subject",
    "list_subjects",
    "subject_directory",
]
