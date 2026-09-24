"""Vault purge: retention candidates, what is never removed, soft commit and `--hard` rewrite."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from git_helpers import ManualClock, clone_vault, git

from studentassistant.config import VaultGitSettings, VaultPurgeSettings
from studentassistant.vault import (
    GitSync,
    Session,
    Vault,
    create_subject,
    create_topic,
    end_session,
    put_source,
    read_session_transcript,
    start_session,
    topic_directory,
)
from studentassistant.vault.purge import (
    Compaction,
    PurgeError,
    apply_purge,
    cited_paths,
    plan_topic_purge,
    purged_history_paths,
)

BURST = b"\xff\xd8burst-original" * 500
PAGE = b"\xff\xd8chosen-page"


@dataclass
class Topic:
    vault: Vault
    sync: GitSync
    subject: str
    topic: str
    sessions: list[Session]

    @property
    def root(self) -> Path:
        return topic_directory(self.vault, self.subject, self.topic)

    @property
    def rel(self) -> str:
        return self.root.relative_to(self.vault.path).as_posix()

    def write(self, relative: str, content: str | bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path


def record_session(vault: Vault, subject: str, topic: str, kinds: list[str]) -> Session:
    session = start_session(vault, subject, topic, host="ubuntu-pc", protocol_version="1.0")
    for index, kind in enumerate(kinds):
        session.append_event(kind, "phone", {"n": index})
    session.append_transcript(0, 1_000, "la derivada es un límite")
    end_session(session, ended_at=datetime(2026, 9, 26, tzinfo=UTC))
    return session


NOTES = """# Derivadas

## 1. Definición {#definicion}

La derivada es el límite del cociente incremental.[^p1]

[^p1]: [Apuntes, página 1](../sources/notes/page-001.jpg)
"""


@pytest.fixture
def topic(tmp_vault: Vault, git_origin: Path) -> Topic:
    """An accepted topic: two ended sessions, two captured pages with burst originals, observer
    conversations, notes citing page 1 and a notes tag."""
    subject = create_subject(tmp_vault, "Matemáticas").slug
    slug = create_topic(tmp_vault, subject, "Derivadas").slug
    sessions = [
        record_session(tmp_vault, subject, slug, ["session.started", "capture.stored", "x"]),
        record_session(tmp_vault, subject, slug, ["session.started", "y", "z", "w"]),
    ]
    for page in range(2):
        put_source(
            tmp_vault,
            subject,
            slug,
            "notes",
            "still.jpg",
            PAGE,
            {"capture_id": "c"},
            # Distinct bytes per still, so each is its own blob in git.
            derived={f"burst{k}.jpg": BURST + bytes([page, k]) for k in (1, 2)},
        )
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())
    stored = Topic(tmp_vault, sync, subject, slug, sessions)
    for session in sessions:
        stored.write(f"conversations/observer-{session.id}.jsonl", '{"role":"user"}\n')
    stored.write("conversations/editor.jsonl", '{"role":"user"}\n')
    stored.write("notes/apuntes.md", NOTES)
    sync.checkpoint("tema grabado")
    sync.create_notes_tag(slug)
    sync.push_now()
    return stored


def paths_of(plan) -> dict[str, str]:
    return {item.path: item.reason for item in plan.items}


def test_plan_lists_bursts_and_rolled_over_conversations_only(topic: Topic) -> None:
    plan = plan_topic_purge(topic.sync, topic.subject, topic.topic)

    rel = topic.rel
    assert plan.skipped is None
    assert paths_of(plan) == {
        f"{rel}/sources/notes/page-001.burst1.jpg": "burst-original",
        f"{rel}/sources/notes/page-001.burst2.jpg": "burst-original",
        f"{rel}/sources/notes/page-002.burst1.jpg": "burst-original",
        f"{rel}/sources/notes/page-002.burst2.jpg": "burst-original",
        **{
            f"{rel}/conversations/observer-{s.id}.jsonl": "observer-conversation"
            for s in topic.sessions
        },
    }
    assert plan.saved_bytes == 4 * (len(BURST) + 2) + 2 * len('{"role":"user"}\n')
    assert all(item.removed for item in plan.items)


def test_planning_writes_nothing(topic: Topic) -> None:
    plan_topic_purge(
        topic.sync,
        topic.subject,
        topic.topic,
        compaction=Compaction(topic.sessions[1].id, 2, "observer.compacted", {"state": {}}),
    )

    assert git(topic.vault.path, "status", "--porcelain") == ""


def test_policy_switches_turn_each_kind_off(topic: Topic) -> None:
    policy = VaultPurgeSettings(burst_originals=False, observer_conversations=False)

    assert plan_topic_purge(topic.sync, topic.subject, topic.topic, policy).items == ()


def test_what_the_notes_cite_is_protected(topic: Topic) -> None:
    burst = "sources/notes/page-002.burst1.jpg"
    topic.write("notes/apuntes.md", NOTES + f"\n[^p2]: [Ráfaga](../{burst})\n")

    plan = plan_topic_purge(topic.sync, topic.subject, topic.topic)

    assert f"{topic.rel}/{burst}" not in paths_of(plan)
    assert plan.protected == (f"{topic.rel}/{burst}",)


def test_cited_paths_reads_links_and_bare_ids() -> None:
    notes = (
        "[^a]: [x](../sources/notes/page-004.jpg)\n"
        "[^b]: [y](../sessions/20260924-180000/transcript.jsonl#t=00:00:01-00:00:02)\n"
        "ver sources/pdf/page-001.pdf#page=3\n"
    )

    assert cited_paths("t", notes) == {
        "t/sources/notes/page-004.jpg",
        "t/sessions/20260924-180000/transcript.jsonl",
        "t/sources/pdf/page-001.pdf",
    }


def test_a_topic_with_an_open_session_is_skipped(topic: Topic) -> None:
    start_session(topic.vault, topic.subject, topic.topic, host="ubuntu-pc", protocol_version="1.0")

    plan = plan_topic_purge(topic.sync, topic.subject, topic.topic)

    assert plan.items == ()
    assert plan.skipped is not None and "sesión abierta" in plan.skipped


def test_a_topic_without_accepted_notes_is_skipped(tmp_vault: Vault, git_origin: Path) -> None:
    subject = create_subject(tmp_vault, "Física").slug
    slug = create_topic(tmp_vault, subject, "Cinemática").slug
    sync = GitSync(tmp_vault, VaultGitSettings(), clock=ManualClock())

    no_notes = plan_topic_purge(sync, subject, slug)
    (topic_directory(tmp_vault, subject, slug) / "notes").mkdir()
    (topic_directory(tmp_vault, subject, slug) / "notes" / "apuntes.md").write_text("# C\n")
    untagged = plan_topic_purge(sync, subject, slug)
    allowed = plan_topic_purge(sync, subject, slug, VaultPurgeSettings(require_notes_tag=False))

    assert no_notes.skipped == "aún no tiene apuntes"
    assert untagged.skipped is not None and "apuntes-vN" in untagged.skipped
    assert allowed.skipped is None


def test_old_generated_material_is_aged_by_its_last_commit(topic: Topic) -> None:
    topic.write("generated/quiz.yaml", "preguntas: []\n")
    topic.sync.checkpoint("cuestionario")
    policy = VaultPurgeSettings(generated_max_age_days=30)
    later = datetime.now(UTC) + timedelta(days=31)

    fresh = plan_topic_purge(topic.sync, topic.subject, topic.topic, policy)
    old = plan_topic_purge(topic.sync, topic.subject, topic.topic, policy, now=later)

    quiz = f"{topic.rel}/generated/quiz.yaml"
    assert quiz not in paths_of(fresh)
    assert paths_of(old)[quiz] == "old-generated"


def compaction_at(session: Session, seq: int) -> Compaction:
    return Compaction(session.id, seq, "observer.compacted", {"state_version": 1, "state": {}})


def events_of(topic: Topic, session: Session) -> list[dict]:
    lines = session.events_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_folded_events_are_replaced_by_the_snapshot_at_the_cursor(topic: Topic) -> None:
    first, second = topic.sessions
    cursor_t = events_of(topic, second)[1]["t"]
    tail = events_of(topic, second)[2:]

    plan = plan_topic_purge(
        topic.sync, topic.subject, topic.topic, compaction=compaction_at(second, 2)
    )
    apply_purge(topic.sync, [plan])

    assert first.events_path.read_bytes() == b""
    events = events_of(topic, second)
    assert events[0] == {
        "seq": 2,
        "t": cursor_t,
        "origin": "observer",
        "kind": "observer.compacted",
        "schema_version": 1,
        "payload": {"state_version": 1, "state": {}},
    }
    assert events[1:] == tail
    # Transcripts are never touched.
    transcript = read_session_transcript(topic.vault, topic.subject, topic.topic, first.id)
    assert [segment.text for segment in transcript] == ["la derivada es un límite"]


def test_compacting_twice_changes_nothing_more(topic: Topic) -> None:
    compaction = compaction_at(topic.sessions[1], 4)
    apply_purge(
        topic.sync,
        [plan_topic_purge(topic.sync, topic.subject, topic.topic, compaction=compaction)],
    )

    again = plan_topic_purge(topic.sync, topic.subject, topic.topic, compaction=compaction)

    assert again.items == ()


def test_a_compaction_outside_the_log_is_refused(topic: Topic) -> None:
    with pytest.raises(PurgeError):
        plan_topic_purge(
            topic.sync, topic.subject, topic.topic, compaction=compaction_at(topic.sessions[1], 99)
        )
    with pytest.raises(PurgeError):
        plan_topic_purge(
            topic.sync,
            topic.subject,
            topic.topic,
            compaction=Compaction("20000101-000000", 1, "observer.compacted", {}),
        )


def test_soft_purge_is_one_commit_recoverable_from_history(topic: Topic) -> None:
    plan = plan_topic_purge(topic.sync, topic.subject, topic.topic)
    before = git(topic.vault.path, "rev-parse", "HEAD").strip()

    result = apply_purge(topic.sync, [plan])

    assert result.commit == git(topic.vault.path, "rev-parse", "HEAD").strip()
    assert git(topic.vault.path, "rev-parse", "HEAD~1").strip() == before
    subject = git(topic.vault.path, "log", "-1", "--format=%s").strip()
    assert subject.startswith("purga: 6 archivos borrados")
    assert git(topic.vault.path, "status", "--porcelain") == ""
    burst = f"{topic.rel}/sources/notes/page-001.burst1.jpg"
    assert not (topic.vault.path / burst).exists()
    assert subprocess_ok(topic.vault.path, "cat-file", "-e", f"{before}:{burst}")
    # Kept: the page itself, its sidecar, the editor conversation, the notes.
    assert (topic.root / "sources/notes/page-001.jpg").read_bytes() == PAGE
    assert (topic.root / "sources/notes/page-001.yaml").exists()
    assert (topic.root / "conversations/editor.jsonl").exists()
    assert (topic.root / "notes/apuntes.md").read_text(encoding="utf-8") == NOTES
    assert purged_history_paths(topic.sync, [topic.root]) == tuple(
        sorted(item.path for item in plan.items)
    )


def test_nothing_to_purge_makes_no_commit(topic: Topic) -> None:
    apply_purge(topic.sync, [plan_topic_purge(topic.sync, topic.subject, topic.topic)])
    head = git(topic.vault.path, "rev-parse", "HEAD").strip()

    result = apply_purge(topic.sync, [plan_topic_purge(topic.sync, topic.subject, topic.topic)])

    assert result.commit is None
    assert git(topic.vault.path, "rev-parse", "HEAD").strip() == head


def blob_of(repository: Path, rev: str, path: str) -> str:
    return git(repository, "rev-parse", f"{rev}:{path}").strip()


def reachable_objects(repository: Path) -> str:
    return git(repository, "rev-list", "--objects", "--all")


def test_hard_purge_rewrites_history_and_force_pushes(
    topic: Topic, git_origin: Path, tmp_path: Path
) -> None:
    burst = f"{topic.rel}/sources/notes/page-001.burst1.jpg"
    blob = blob_of(topic.vault.path, "HEAD", burst)
    tag = f"{topic.topic}/apuntes-v1"
    tagged_notes = blob_of(topic.vault.path, tag, f"{topic.rel}/notes/apuntes.md")
    plan = plan_topic_purge(topic.sync, topic.subject, topic.topic)

    result = apply_purge(topic.sync, [plan], hard=True)

    assert result.history is not None
    assert result.history.pushed
    assert set(result.history.paths) == {item.path for item in plan.items if item.removed}
    assert result.history.size_after < result.history.size_before
    for repository in (topic.vault.path, git_origin):
        assert burst not in reachable_objects(repository)
        # The notes tag was carried over to the rewritten history, notes untouched.
        assert blob_of(repository, tag, f"{topic.rel}/notes/apuntes.md") == tagged_notes
    assert not subprocess_ok(topic.vault.path, "cat-file", "-e", blob)
    assert (
        git(topic.vault.path, "rev-parse", "HEAD").strip()
        == git(git_origin, "rev-parse", "main").strip()
    )
    assert git(topic.vault.path, "status", "--porcelain") == ""
    # A fresh clone (another PC) has the page and the notes, and no burst anywhere.
    other = clone_vault(git_origin, tmp_path / "otro-pc")
    assert (topic_directory(other, topic.subject, topic.topic) / "notes/apuntes.md").exists()
    assert burst not in reachable_objects(other.path)


def subprocess_ok(cwd: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def test_hard_purge_reclaims_earlier_soft_purges(topic: Topic, git_origin: Path) -> None:
    apply_purge(topic.sync, [plan_topic_purge(topic.sync, topic.subject, topic.topic)])
    topic.sync.push_now()
    burst = f"{topic.rel}/sources/notes/page-002.burst2.jpg"
    assert burst in reachable_objects(git_origin)

    later = plan_topic_purge(topic.sync, topic.subject, topic.topic)
    result = apply_purge(topic.sync, [later], hard=True)

    assert later.items == ()
    assert result.commit is None
    assert result.history is not None and burst in result.history.paths
    assert burst not in reachable_objects(git_origin)


def test_hard_purge_refuses_to_overwrite_a_remote_that_moved_on(
    topic: Topic, git_origin: Path, tmp_path: Path
) -> None:
    other = clone_vault(git_origin, tmp_path / "otro-pc")
    (other.path / "nota.txt").write_text("de otro PC\n")
    git(other.path, "add", "nota.txt")
    git(other.path, "commit", "-q", "-m", "otro PC")
    git(other.path, "push", "-q", "origin", "main")
    remote_head = git(git_origin, "rev-parse", "main").strip()

    with pytest.raises(PurgeError, match="no se ha podido subir"):
        apply_purge(
            topic.sync, [plan_topic_purge(topic.sync, topic.subject, topic.topic)], hard=True
        )

    assert git(git_origin, "rev-parse", "main").strip() == remote_head
