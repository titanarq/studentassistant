"""The active-host record `.sa/active.yaml`: claim, release, the warning and its staleness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from user_helpers import add_user

from studentassistant.vault import (
    Vault,
    active_host_warning,
    check_active_host,
    claim_active_host,
    read_active_host,
    release_active_host,
)
from studentassistant.vault.active import active_host_path, ensure_active_host_attribute
from studentassistant.vault.vault import ACTIVE_HOST_GITATTRIBUTES_LINE, GITATTRIBUTES_NAME

SINCE = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
HOUR = 3600.0


def test_no_record_reads_as_none_and_warns_nothing(tmp_vault: Vault) -> None:
    assert read_active_host(tmp_vault) is None
    assert check_active_host(tmp_vault, "pc-a", HOUR) is None


def test_a_claim_is_written_and_read_back(tmp_vault: Vault) -> None:
    written = claim_active_host(tmp_vault, "pc-a", "20260925-100000", "fisica", "cinematica", SINCE)

    assert active_host_path(tmp_vault) == tmp_vault.path / ".sa" / "active.yaml"
    assert read_active_host(tmp_vault) == written
    assert written.host == "pc-a"
    assert written.session_id == "20260925-100000"
    assert (written.subject, written.topic) == ("fisica", "cinematica")
    assert written.claimed_at == SINCE
    assert not written.released


def test_release_marks_only_the_own_claim_of_the_same_session(tmp_vault: Vault) -> None:
    claim_active_host(tmp_vault, "pc-a", "s1", claimed_at=SINCE)

    assert release_active_host(tmp_vault, "pc-b", "s1") is None
    assert release_active_host(tmp_vault, "pc-a", "s2") is None
    assert not read_active_host(tmp_vault).released  # type: ignore[union-attr]

    released = release_active_host(tmp_vault, "pc-a", "s1", SINCE + timedelta(minutes=20))

    assert released is not None
    assert released.released_at == SINCE + timedelta(minutes=20)
    assert read_active_host(tmp_vault) == released
    assert release_active_host(tmp_vault, "pc-a", "s1") is None  # already released


def test_release_without_a_record_writes_nothing(tmp_vault: Vault) -> None:
    assert release_active_host(tmp_vault, "pc-a") is None
    assert not active_host_path(tmp_vault).exists()


def test_another_hosts_fresh_claim_warns_in_spanish(tmp_vault: Vault) -> None:
    claim_active_host(tmp_vault, "pc-b", "20260925-100000", "fisica", "cinematica", SINCE)

    warning = check_active_host(tmp_vault, "pc-a", HOUR, now=SINCE + timedelta(minutes=30))

    assert warning is not None
    assert warning.record.host == "pc-b"
    assert "«pc-b»" in warning.message
    assert "20260925-100000 (fisica/cinematica)" in warning.message
    assert "cambios sin subir" in warning.message


def test_no_warning_for_the_own_host_a_released_claim_or_a_stale_one(tmp_vault: Vault) -> None:
    record = claim_active_host(tmp_vault, "pc-b", "s1", claimed_at=SINCE)
    soon = SINCE + timedelta(minutes=30)

    assert active_host_warning(record, "pc-b", HOUR, soon) is None
    assert active_host_warning(record, "pc-a", HOUR, SINCE + timedelta(hours=2)) is None
    assert active_host_warning(record, "pc-a", HOUR, soon) is not None
    released = release_active_host(tmp_vault, "pc-b", "s1", soon)
    assert active_host_warning(released, "pc-a", HOUR, soon) is None


def test_an_unreadable_record_is_ignored(tmp_vault: Vault) -> None:
    path = active_host_path(tmp_vault)
    path.parent.mkdir()
    path.write_text("host: [\n", encoding="utf-8")

    assert read_active_host(tmp_vault) is None
    assert check_active_host(tmp_vault, "pc-a", HOUR) is None


def test_an_older_vault_gets_the_merge_driver_line_once(tmp_vault: Vault) -> None:
    attributes = tmp_vault.path / GITATTRIBUTES_NAME
    attributes.write_text("*.jsonl merge=union", encoding="utf-8")  # no final newline, no line

    claim_active_host(tmp_vault, "pc-a")

    assert attributes.read_text(encoding="utf-8") == (
        "*.jsonl merge=union\n" + ACTIVE_HOST_GITATTRIBUTES_LINE
    )
    assert not ensure_active_host_attribute(tmp_vault)
    assert attributes.read_text(encoding="utf-8").count(ACTIVE_HOST_GITATTRIBUTES_LINE) == 1


# -- several students, one record (#547) ---------------------------------------------------------


def test_a_users_claim_is_written_at_the_root_and_says_who_it_is(tmp_vault: Vault) -> None:
    ana = add_user(tmp_vault, "ana", "Ana")

    written = claim_active_host(ana, "pc-a", "20260925-100000", "fisica", "cinematica", SINCE)

    assert (
        active_host_path(ana) == active_host_path(tmp_vault) == tmp_vault.root / ".sa/active.yaml"
    )
    assert not (ana.path / ".sa").exists()  # a user's folder holds no record of its own
    assert written.user == "ana"
    assert read_active_host(tmp_vault) == written  # the root reads what the user wrote
    released = release_active_host(tmp_vault, "pc-a", "20260925-100000")
    assert released is not None and released.released and released.user == "ana"


def test_a_claim_on_the_root_names_the_user_it_is_given(tmp_vault: Vault) -> None:
    assert claim_active_host(tmp_vault, "pc-a", "s1", user_id="bia").user == "bia"
    assert claim_active_host(tmp_vault, "pc-a", "s1").user is None


def test_the_warning_names_the_student_the_other_pc_is_capturing_as(tmp_vault: Vault) -> None:
    ana = add_user(tmp_vault, "ana", "Ana")
    claim_active_host(ana, "pc-b", "20260925-100000", "fisica", "cinematica", SINCE)

    warning = check_active_host(tmp_vault, "pc-a", HOUR, now=SINCE + timedelta(minutes=30))

    assert warning is not None
    assert warning.record.user == "ana"
    assert "«pc-b»" in warning.message and "usuario «ana»" in warning.message
    assert "20260925-100000 (fisica/cinematica)" in warning.message
    assert "cambios sin subir" in warning.message


def test_a_record_from_before_there_were_users_still_reads_and_warns(tmp_vault: Vault) -> None:
    path = active_host_path(tmp_vault)
    path.parent.mkdir(parents=True)
    path.write_text(
        "host: pc-b\nsession_id: s1\nclaimed_at: 2026-09-25T10:00:00+00:00\n", encoding="utf-8"
    )

    record = read_active_host(tmp_vault)

    assert record is not None and record.user is None
    warning = active_host_warning(record, "pc-a", HOUR, SINCE + timedelta(minutes=30))
    assert warning is not None
    assert "«pc-b»" in warning.message and "usuario" not in warning.message
