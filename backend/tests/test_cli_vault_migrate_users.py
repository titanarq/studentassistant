"""`studentassistant vault migrate-users [--name] [--email] [--dry-run]` (#548).

What the move does to a vault -- the one commit, the notes versions it re-names, what it refuses to
move and that nothing is lost -- is `tests/vault/test_migrate.py` and
`tests/vault/test_migrate_nothing_lost.py`. These are about the command: what it prints in Spanish,
what it exits with, and that it rebuilds the indexes of the vault it has just rearranged. Every
test points the CLI at a vault under `tmp_path` through `SA_CONFIG` and `SA_VAULT__PATH`, so none
of them reads this machine's configuration or its vault.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from studentassistant.cli import cli
from studentassistant.config import Settings, VaultGitSettings
from studentassistant.vault import (
    GitSync,
    Vault,
    create_subject,
    create_topic,
    end_session,
    start_session,
    write_notes,
)
from studentassistant.vault.index import user_index_path
from studentassistant.vault.migrate import migration_commit_subject
from studentassistant.vault.models import FORMAT_VERSION, LEGACY_FORMAT_VERSION
from studentassistant.vault.users import get_user
from studentassistant.vault.vault import MIGRATE_USERS_COMMAND

# `legacy_vault`'s `vault.yaml` says this, so this is the name the first user is created with.
STUDENT = "Ana García"
FIRST_USER = "ana-garcia"
SUBJECT = "Física"
TOPIC = "Cinemática"
SLUGS = ("fisica", "cinematica")
# The notes' id as a handle gives it: relative to the folder that handle points at, so it is the
# same string before the move (the root's) and after it (the user's).
NOTES = f"subjects/{SLUGS[0]}/topics/{SLUGS[1]}/notes/apuntes.md"
NOTES_V2 = "# Cinemática\n\n## Movimiento rectilíneo\n"
TAG_V1 = f"{SLUGS[0]}/{SLUGS[1]}/apuntes-v1"
TAG_V2 = f"{SLUGS[0]}/{SLUGS[1]}/apuntes-v2"


def git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` for a test's own inspection."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def commits(root: Path) -> int:
    return int(git(root, "rev-list", "--count", "HEAD").strip())


def migration_commits(root: Path) -> int:
    """How many commits of `root`'s history are a migration's own."""
    return git(root, "log", "--format=%s").count(migration_commit_subject(FIRST_USER))


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configuration that is not this machine's: `SA_CONFIG` in `tmp_path`, and no other `SA_*`.

    The file itself is absent, so every setting the command reads is a default or one the test sets
    with an environment variable of its own -- among them `HOME`, which `tmp_vault` moves into
    `tmp_path` and which is where the default `[vault] index_path` then lands.
    """
    for name in list(os.environ):
        if name.startswith("SA_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("SA_CONFIG", str(tmp_path / "absent.toml"))
    return tmp_path


@pytest.fixture
def vault(configured: Path, legacy_vault: Vault, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """The format-1 vault the CLI is pointed at: one ended session, two notes versions tagged.

    It has no remote, which is what a vault looks like on a PC that has not run `setup` against
    GitHub: the migration is local and the push it ends with is owed to the next sync.
    """
    monkeypatch.setenv("SA_VAULT__PATH", str(legacy_vault.root))
    subject = create_subject(legacy_vault, SUBJECT).slug
    topic = create_topic(legacy_vault, subject, TOPIC).slug
    session = start_session(legacy_vault, subject, topic, host="pc", protocol_version="1.0")
    session.append_transcript(0, 900, "la velocidad media")
    end_session(session)
    write_notes(legacy_vault, subject, topic, "# Cinemática\n")
    sync = GitSync(legacy_vault, VaultGitSettings())
    sync.checkpoint("apuntes de cinemática")
    sync.create_notes_tag(subject, topic, "Apuntes v1 de fisica/cinematica")
    write_notes(legacy_vault, subject, topic, NOTES_V2)
    sync.checkpoint("segunda versión de los apuntes")
    sync.create_notes_tag(subject, topic, "Apuntes v2 de fisica/cinematica")
    return legacy_vault


def test_the_command_moves_the_content_names_the_user_and_rebuilds_the_index(vault: Vault) -> None:
    result = CliRunner().invoke(cli, ["vault", "migrate-users"])

    assert result.exit_code == 0, result.output
    assert f"es ahora del usuario {FIRST_USER} ({STUDENT})" in result.output
    assert f"Asignaturas: {SLUGS[0]}" in result.output
    assert "Archivos movidos: " in result.output
    assert f"(a users/{FIRST_USER}/subjects/)" in result.output
    assert "Todo en un único commit: " in result.output
    assert f"{TAG_V1} -> {FIRST_USER}/{TAG_V1}" in result.output
    assert f"{TAG_V2} -> {FIRST_USER}/{TAG_V2}" in result.output
    # The sync it starts with found no remote, which is reported and is not a refusal.
    assert "Sincronización previa: el remoto no responde" in result.output
    assert "no se ha podido subir ahora" in result.output
    assert "Índice reconstruido: " in result.output
    assert user_index_path(Settings().vault.index_path, FIRST_USER).is_file()

    migrated = Vault.open(vault.root)
    assert migrated.meta.legacy_root_user == FIRST_USER
    assert (migrated.for_user(FIRST_USER).path / NOTES).read_text(encoding="utf-8") == NOTES_V2
    assert not (vault.root / "subjects").exists(), "the root keeps no content of anybody's"
    assert migration_commits(vault.root) == 1


def test_dry_run_lists_the_move_and_changes_nothing(vault: Vault) -> None:
    before = commits(vault.root)

    result = CliRunner().invoke(cli, ["vault", "migrate-users", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert f"pasaría al usuario {FIRST_USER} ({STUDENT})" in result.output
    assert "Archivos que se moverían: " in result.output
    assert f"{TAG_V1} -> {FIRST_USER}/{TAG_V1}" in result.output
    assert "No se ha cambiado nada." in result.output
    assert "Índice" not in result.output, "a dry run rebuilds nothing"

    assert commits(vault.root) == before
    assert not (vault.root / "users").exists(), "no user was created"
    assert Vault.open_for_migration(vault.root).meta.format_version == LEGACY_FORMAT_VERSION
    assert not user_index_path(Settings().vault.index_path, FIRST_USER).exists()


def test_an_unended_session_refuses_it_in_spanish_and_exits_1(vault: Vault) -> None:
    start_session(vault, SLUGS[0], SLUGS[1], host="pc", protocol_version="1.0")

    result = CliRunner().invoke(cli, ["vault", "migrate-users"])

    assert result.exit_code == 1
    assert f"{SLUGS[0]}/{SLUGS[1]}/" in result.output, "the session that is open is named"
    assert "sigue abierta" in result.output and MIGRATE_USERS_COMMAND in result.output
    assert not (vault.root / "users").exists()
    assert migration_commits(vault.root) == 0, "no migration was committed"
    assert Vault.open_for_migration(vault.root).meta.format_version == LEGACY_FORMAT_VERSION


def test_the_name_and_email_options_are_the_first_users(vault: Vault) -> None:
    result = CliRunner().invoke(
        cli, ["vault", "migrate-users", "--name", "Luis Martín", "--email", "luis@instituto.es"]
    )

    assert result.exit_code == 0, result.output
    assert "es ahora del usuario luis-martin (Luis Martín)" in result.output
    assert f"{TAG_V1} -> luis-martin/{TAG_V1}" in result.output
    profile = get_user(Vault.open(vault.root), "luis-martin")
    assert (profile.name, profile.email) == ("Luis Martín", "luis@instituto.es")
    assert user_index_path(Settings().vault.index_path, "luis-martin").is_file()


def test_a_vault_with_a_remote_is_pushed_with_its_new_tags(vault: Vault, git_origin: Path) -> None:
    result = CliRunner().invoke(cli, ["vault", "migrate-users"])

    assert result.exit_code == 0, result.output
    assert "Subida a GitHub: sí." in result.output
    assert "Sincronización previa" not in result.output, "an up-to-date sync has nothing to say"
    assert sorted(git(git_origin, "tag", "--list").split()) == [
        f"{FIRST_USER}/{TAG_V1}",
        f"{FIRST_USER}/{TAG_V2}",
        TAG_V1,
        TAG_V2,
    ], "the remote gets the versions under their user, and keeps the ones it had"
    assert (
        git(git_origin, "rev-parse", "main").strip() == git(vault.root, "rev-parse", "HEAD").strip()
    ), "the remote has the migration's commit"


def test_a_second_run_says_there_was_nothing_to_do(vault: Vault) -> None:
    assert CliRunner().invoke(cli, ["vault", "migrate-users"]).exit_code == 0
    before = commits(vault.root)

    result = CliRunner().invoke(cli, ["vault", "migrate-users"])
    dry = CliRunner().invoke(cli, ["vault", "migrate-users", "--dry-run"])

    for run in (result, dry):
        assert run.exit_code == 0, run.output
        assert f"ya es de formato {FORMAT_VERSION}" in run.output
        assert "no hay nada que migrar" in run.output
    assert commits(vault.root) == before
    assert migration_commits(vault.root) == 1, "the second run committed nothing"


def test_the_help_says_to_stop_the_backend_first(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rich (through typer) colours and wraps the help according to the terminal; pin a wide, plain
    # one and also strip ANSI and the box borders, so the check holds on CI as well as locally.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    result = CliRunner().invoke(cli, ["vault", "migrate-users", "--help"])

    assert result.exit_code == 0, result.output
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.output)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    text = " ".join(text.split())
    assert "Stop the backend first" in text
    for option in ("--name", "--email", "--dry-run"):
        assert option in text, f"{option} is missing from the help:\n{result.output}"


def test_a_vault_that_is_not_there_exits_1(
    configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SA_VAULT__PATH", str(configured / "nope"))

    result = CliRunner().invoke(cli, ["vault", "migrate-users"])

    assert result.exit_code == 1
    assert "No se puede abrir la bóveda" in result.output
