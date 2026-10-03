"""The retention purge as one user sees it: their topic, their tag, their paths, nobody else's.

A purge is planned and applied through the handle its sync has, so `GitSync.for_user` plans inside
one student's folder and reports the paths that student's callers open, while what git is asked is
the repository's, because git runs at the root: a `git log` after a generated file's last commit
and a `--hard` rewrite both need the `users/<id>/` prefix, and the second rewrites the one history
every user of the vault shares. Two students with the same subject and topic slugs -- which is what
one vault for several of them makes ordinary -- are what keeps all of that honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from git_helpers import ManualClock, git
from user_helpers import add_user

from studentassistant.config import VaultGitSettings, VaultPurgeSettings
from studentassistant.vault import (
    GitSync,
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    start_session,
    write_notes,
)
from studentassistant.vault.purge import apply_purge, plan_topic_purge
from studentassistant.vault.sync import UserGitSync

SUBJECT = "Matemáticas"
TOPIC = "Derivadas"
BURST = b"\xff\xd8burst-original" * 200
PAGE = b"\xff\xd8chosen-page"
NOTES = """# Derivadas

La derivada es un límite.[^p1]

[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)
"""


@dataclass
class TwoUsers:
    """One repository, its sync, and two students whose notes are each accepted by their own tag."""

    vault: Vault
    sync: GitSync
    subject: str
    topic: str

    @property
    def rel(self) -> str:
        """The topic as a user's own handle names it: relative to their folder, not to the vault."""
        return f"subjects/{self.subject}/topics/{self.topic}"

    def view(self, user_id: str) -> UserGitSync:
        return self.sync.for_user(user_id)

    def user(self, user_id: str) -> Vault:
        return self.vault.for_user(user_id)

    def bursts(self, user_id: str) -> list[Path]:
        """The burst originals of one user's topic: what a purge of theirs may remove."""
        sources = self.user(user_id).path / self.rel / "sources/notes"
        return sorted(sources.glob("page-001.burst*.jpg"))


def burst(user_id: str, index: int) -> bytes:
    """A burst's bytes: distinct per user and per index, so no two of them share one git blob.

    Two users' identical files are one object in git, and `rev-list --objects` then names it with
    one of their paths only -- which is not what a test that looks for one user's file wants.
    """
    return BURST + user_id.encode("utf-8") + bytes([index])


def accepted_topic(vault: Vault, user_id: str, name: str) -> tuple[Vault, str, str]:
    """A user with one topic whose notes are written: an ended session, a page and its bursts."""
    user = add_user(vault, user_id, name)
    subject = create_subject(user, SUBJECT).slug
    topic = create_topic(user, subject, TOPIC).slug
    session = start_session(user, subject, topic, host="ubuntu-pc", protocol_version="1.0")
    session.append_event("capture.stored", "phone", {"n": 0})
    session.append_transcript(0, 1_000, "la derivada es un límite")
    end_session(session, ended_at=datetime(2026, 9, 26, tzinfo=UTC))
    put_source(
        user,
        subject,
        topic,
        "notes",
        "still.jpg",
        PAGE,
        {"capture_id": "c"},
        derived={f"burst{k}.jpg": burst(user_id, k) for k in (1, 2)},
    )
    write_notes(user, subject, topic, NOTES)
    return user, subject, topic


@pytest.fixture
def two(tmp_vault: Vault, git_origin: Path) -> TwoUsers:
    """Two students with the same two slugs, both accepted and pushed, on a clock a test moves."""
    _, subject, topic = accepted_topic(tmp_vault, "ana", "Ana")
    accepted_topic(tmp_vault, "bia", "Bia")
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    sync.checkpoint("dos temas grabados")
    for user_id in ("ana", "bia"):
        sync.for_user(user_id).create_notes_tag(subject, topic)
    sync.push_now()
    return TwoUsers(tmp_vault, sync, subject, topic)


def reachable_objects(repository: Path) -> str:
    """Every object git can still reach, with the path it is stored at: what a rewrite dropped."""
    return git(repository, "rev-list", "--objects", "--all")


def test_a_plan_reports_the_paths_of_the_user_it_was_planned_for(two: TwoUsers) -> None:
    plan = plan_topic_purge(two.view("ana"), two.subject, two.topic)

    assert plan.skipped is None
    assert {item.path for item in plan.items} == {
        f"{two.rel}/sources/notes/page-001.burst1.jpg",
        f"{two.rel}/sources/notes/page-001.burst2.jpg",
    }
    assert plan.saved_bytes == len(burst("ana", 1)) + len(burst("ana", 2))


def test_a_topic_is_planned_only_when_its_own_user_accepted_its_notes(two: TwoUsers) -> None:
    _, subject, topic = accepted_topic(two.vault, "cid", "Cid")
    two.sync.checkpoint("un tercer tema, sin aceptar")

    plan = plan_topic_purge(two.sync.for_user("cid"), subject, topic)

    assert plan.skipped is not None
    # Neither ana's nor bia's tag of the same two slugs accepts this student's topic.
    assert f"cid/{subject}/{topic}/apuntes-vN" in plan.skipped
    assert plan_topic_purge(two.view("ana"), subject, topic).skipped is None
    assert plan_topic_purge(two.view("bia"), subject, topic).skipped is None


def test_a_soft_purge_removes_one_users_files_and_commits_them_as_theirs(
    two: TwoUsers,
) -> None:
    view = two.view("ana")
    plan = plan_topic_purge(view, two.subject, two.topic)
    ana_bursts, bia_bursts = two.bursts("ana"), two.bursts("bia")
    assert len(ana_bursts) == len(bia_bursts) == 2

    result = apply_purge(view, [plan])

    assert result.commit is not None
    assert [path.exists() for path in ana_bursts] == [False, False]
    assert [path.exists() for path in bia_bursts] == [True, True]
    changed = git(two.vault.root, "show", "--name-only", "--format=", result.commit)
    assert "users/ana/" in changed and "users/bia/" not in changed


def test_generated_material_is_aged_by_its_last_commit_under_a_user(two: TwoUsers) -> None:
    view = two.view("ana")
    quiz = two.user("ana").path / two.rel / "generated/quiz.yaml"
    quiz.parent.mkdir(parents=True, exist_ok=True)
    quiz.write_text("preguntas: []\n", encoding="utf-8")
    two.sync.checkpoint("cuestionario")
    policy = VaultPurgeSettings(generated_max_age_days=30)
    later = datetime.now(UTC) + timedelta(days=31)

    fresh = plan_topic_purge(view, two.subject, two.topic, policy)
    old = plan_topic_purge(view, two.subject, two.topic, policy, now=later)

    reasons = {item.path: item.reason for item in old.items}
    assert f"{two.rel}/generated/quiz.yaml" not in {item.path for item in fresh.items}
    assert reasons[f"{two.rel}/generated/quiz.yaml"] == "old-generated"


def test_a_hard_purge_drops_one_users_files_from_the_history_they_share(
    two: TwoUsers, git_origin: Path
) -> None:
    view = two.view("ana")
    plan = plan_topic_purge(view, two.subject, two.topic)
    purged = f"users/ana/{two.rel}/sources/notes/page-001.burst1.jpg"
    kept = f"users/bia/{two.rel}/sources/notes/page-001.burst1.jpg"
    bia_tag = two.view("bia").list_notes_tags(two.subject, two.topic)[0].name
    assert bia_tag == f"bia/{two.subject}/{two.topic}/apuntes-v1"
    assert purged in reachable_objects(git_origin)

    result = apply_purge(view, [plan], hard=True)

    assert result.history is not None and result.history.pushed
    # The paths a rewrite reports are the handle's, as the plan items they came from are.
    assert set(result.history.paths) == {item.path for item in plan.items if item.removed}
    for repository in (two.vault.root, git_origin):
        assert purged not in reachable_objects(repository)
        assert kept in reachable_objects(repository)  # another user's burst is not this plan's
        # bia's tag was carried over to the rewritten history, her notes untouched.
        assert git(repository, "rev-parse", f"{bia_tag}:users/bia/{two.rel}/notes/apuntes.md")
    assert [path.exists() for path in two.bursts("bia")] == [True, True]
    assert git(two.vault.root, "status", "--porcelain") == ""


def test_a_hard_purge_reclaims_an_earlier_soft_purge_of_the_same_user(
    two: TwoUsers, git_origin: Path
) -> None:
    view = two.view("ana")
    apply_purge(view, [plan_topic_purge(view, two.subject, two.topic)])
    two.sync.push_now()
    burst = f"users/ana/{two.rel}/sources/notes/page-001.burst2.jpg"
    assert burst in reachable_objects(git_origin)

    later = plan_topic_purge(view, two.subject, two.topic)
    result = apply_purge(view, [later], hard=True)

    assert later.items == () and result.commit is None
    assert result.history is not None
    assert f"{two.rel}/sources/notes/page-001.burst2.jpg" in result.history.paths
    assert burst not in reachable_objects(git_origin)
    assert f"users/bia/{two.rel}/sources/notes/page-001.burst2.jpg" in reachable_objects(git_origin)
