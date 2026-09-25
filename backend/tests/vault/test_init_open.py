"""`Vault.init` and `Vault.open`: the files a vault starts with, and every refusal to open one."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from studentassistant.vault import (
    Vault,
    VaultError,
    VaultFormatError,
    VaultMetaError,
    VaultNotFoundError,
)
from studentassistant.vault.files import read_yaml
from studentassistant.vault.models import FORMAT_VERSION, VaultMeta
from studentassistant.vault.vault import (
    GITATTRIBUTES_CONTENT,
    GITATTRIBUTES_NAME,
    MAIN_BRANCH,
    VAULT_META_NAME,
)

STUDENT = "Ana García"

# Read at import time, before any fixture has moved it: what the environment outside the tests
# calls home, and where no test here may leave git looking for a configuration.
HOME_OUTSIDE_THE_TESTS = os.environ.get("HOME")


@pytest.fixture(autouse=True)
def git_never_sees_the_students_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a vault runs `git init`, and git reads the global config of whoever runs it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def test_init_creates_the_directory_it_is_given_including_the_parents_that_do_not_exist(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "cursos" / "2026" / "vault", student=STUDENT)

    assert vault.path.is_dir()


def test_a_new_vault_holds_its_meta_file_its_gitattributes_and_its_repository(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)

    assert sorted(entry.name for entry in vault.path.iterdir()) == [
        ".git",
        GITATTRIBUTES_NAME,
        VAULT_META_NAME,
    ]


def test_vault_yaml_records_the_layout_version_whose_vault_it_is_and_since_when(
    tmp_path: Path,
) -> None:
    before = datetime.now(UTC)

    vault = Vault.init(tmp_path / "vault", student=STUDENT)

    after = datetime.now(UTC)
    meta = read_yaml(vault.path / VAULT_META_NAME, VaultMeta)
    assert meta.format_version == FORMAT_VERSION
    assert meta.student == STUDENT
    assert before <= meta.created_at <= after
    assert meta == vault.meta


def test_vault_yaml_writes_its_fields_in_the_order_the_layout_shows_them(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)

    text = (vault.path / VAULT_META_NAME).read_text(encoding="utf-8")

    assert text.startswith(f"format_version: {FORMAT_VERSION}\n")
    assert [line.split(":", 1)[0] for line in text.splitlines()] == list(VaultMeta.model_fields)


def test_gitattributes_keeps_both_sides_of_an_append_only_jsonl_file(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)

    content = (vault.path / GITATTRIBUTES_NAME).read_text(encoding="utf-8")

    assert content == GITATTRIBUTES_CONTENT
    assert GITATTRIBUTES_CONTENT == "*.jsonl merge=union\n.sa/active.yaml merge=sa-active\n"


def test_init_starts_the_repository_on_the_main_branch(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)

    head = (vault.path / ".git" / "HEAD").read_text(encoding="utf-8")

    assert head == f"ref: refs/heads/{MAIN_BRANCH}\n"


def test_init_refuses_a_path_that_already_holds_something(tmp_path: Path) -> None:
    already_there = tmp_path / "vault"
    already_there.mkdir()
    (already_there / "apuntes.md").write_text("# Apuntes que ya existían\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        Vault.init(already_there, student=STUDENT)

    assert sorted(entry.name for entry in already_there.iterdir()) == ["apuntes.md"]
    assert (already_there / "apuntes.md").read_text(encoding="utf-8") == (
        "# Apuntes que ya existían\n"
    )


def test_open_hands_back_the_vault_init_just_wrote(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)

    assert Vault.open(vault.path) == vault


def test_open_refuses_a_path_where_there_is_no_directory(tmp_path: Path) -> None:
    with pytest.raises(VaultNotFoundError, match="no such directory"):
        Vault.open(tmp_path / "vault")


def test_open_refuses_an_ordinary_file_given_where_a_vault_should_be(tmp_path: Path) -> None:
    a_file = tmp_path / "vault.yaml"
    a_file.write_text(f"format_version: {FORMAT_VERSION}\n", encoding="utf-8")

    with pytest.raises(VaultNotFoundError, match="no such directory"):
        Vault.open(a_file)


def test_open_refuses_a_directory_that_has_no_vault_yaml_in_it(tmp_path: Path) -> None:
    not_a_vault = tmp_path / "vault"
    not_a_vault.mkdir()

    with pytest.raises(VaultMetaError, match=f"{VAULT_META_NAME} is missing"):
        Vault.open(not_a_vault)


def test_open_refuses_a_vault_yaml_it_cannot_read_at_all(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)
    (vault.path / VAULT_META_NAME).unlink()
    (vault.path / VAULT_META_NAME).mkdir()

    with pytest.raises(VaultMetaError, match="cannot be read"):
        Vault.open(vault.path)


def test_open_refuses_a_vault_yaml_that_is_not_text_it_can_decode(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)
    (vault.path / VAULT_META_NAME).write_bytes(b"format_version: 1\nstudent: \xff\n")

    with pytest.raises(VaultMetaError, match="cannot be read"):
        Vault.open(vault.path)


def test_open_refuses_a_vault_yaml_that_is_not_yaml(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)
    (vault.path / VAULT_META_NAME).write_text("{ sin cerrar\n", encoding="utf-8")

    with pytest.raises(VaultMetaError, match="not YAML"):
        Vault.open(vault.path)


def test_open_refuses_an_empty_vault_yaml_instead_of_inventing_a_meta_for_it(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)
    (vault.path / VAULT_META_NAME).write_text("", encoding="utf-8")

    with pytest.raises(VaultMetaError, match="does not hold a vault"):
        Vault.open(vault.path)


def test_open_refuses_a_vault_yaml_with_a_field_this_backend_does_not_declare(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)
    meta_path = vault.path / VAULT_META_NAME
    meta_path.write_text(
        meta_path.read_text(encoding="utf-8") + "owner: otra persona\n", encoding="utf-8"
    )

    with pytest.raises(VaultMetaError, match="does not hold a vault"):
        Vault.open(vault.path)


@pytest.mark.parametrize("format_version", [0, FORMAT_VERSION + 1, 99])
def test_open_refuses_a_format_version_other_than_the_one_this_backend_writes(
    tmp_path: Path, format_version: int
) -> None:
    vault = Vault.init(tmp_path / "vault", student=STUDENT)
    meta_path = vault.path / VAULT_META_NAME
    written_by_another_version = meta_path.read_text(encoding="utf-8").replace(
        f"format_version: {FORMAT_VERSION}", f"format_version: {format_version}"
    )
    meta_path.write_text(written_by_another_version, encoding="utf-8")

    with pytest.raises(VaultFormatError, match=f"declares format_version {format_version}"):
        Vault.open(vault.path)

    assert meta_path.read_text(encoding="utf-8") == written_by_another_version, (
        "opening a vault it refuses leaves it exactly as it found it"
    )


def test_every_refusal_is_a_vault_error_a_caller_can_catch_as_one_type() -> None:
    assert issubclass(VaultNotFoundError, VaultError)
    assert issubclass(VaultMetaError, VaultError)
    assert issubclass(VaultFormatError, VaultMetaError)


def test_the_shared_fixture_hands_out_an_open_vault_of_its_own(
    tmp_vault: Vault, tmp_path: Path
) -> None:
    assert tmp_vault.path == tmp_path / "vault"
    assert tmp_vault == Vault.open(tmp_vault.path)
    assert tmp_vault.meta.format_version == FORMAT_VERSION
    assert tmp_vault.meta.student
    assert (tmp_vault.path / ".git" / "HEAD").read_text(encoding="utf-8") == (
        f"ref: refs/heads/{MAIN_BRANCH}\n"
    )
    assert os.environ["HOME"] == str(tmp_path)
    assert os.environ["HOME"] != HOME_OUTSIDE_THE_TESTS, (
        "a test that asks for a vault never leaves the student's home directory in reach of git"
    )
