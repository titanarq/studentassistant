"""Retention purge of a topic's raw and derived data (ADR-0003, "Purge (b) Storage").

Once a topic's notes are accepted, what was only needed to build them can go, so the vault does not
grow forever. The candidates, each switched on or off by `VaultPurgeSettings`
(`[vault.purge]`), are:

- `burst-original`: the stills of a capture burst other than the page kept, stored by capture
  processing as the derived files `sources/notes/page-NNN.burst<K>.<ext>` of the page.
- `observer-conversation`: `conversations/observer-<session-id>.jsonl` of an ended session; the
  observer rolls it over to snapshot + digest at every session end, so nothing is lost.
- `folded-events`: the events a caller-given `Compaction` covers -- every event of the topic up to
  and including its cursor `(session_id, seq)` -- replaced by one event carrying the snapshot,
  written at the cursor's `seq`. Sessions before the cursor's keep an empty `events.jsonl`; events
  after the cursor are kept as they were. The vault never imports the observer: the caller builds
  the compaction (kind and payload) from the observer's snapshot.
- `old-generated`: files under `generated/` last committed more than `generated_max_age_days` ago.

Never a candidate: the transcripts, `session.yaml`, the notes and their history, sources other
than burst originals, and anything the current `notes/apuntes.md` names by path (a footnote link
such as `../sources/notes/page-004.jpg`); such a candidate is listed as `protected` instead. A
topic is only planned when every session has ended, it has notes and -- unless
`require_notes_tag` is off -- a notes version tag `<subject-slug>/<topic-slug>/apuntes-vN` says
they were accepted; otherwise the plan says why it was skipped.

`plan_topic_purge` only reads (it is what `--dry-run` prints). `apply_purge` is the soft purge:
the files are removed or rewritten in the working tree and committed (`purga: ...`), so all of it
stays recoverable from git history. With `hard=True` it then rewrites the history of the branch
and its tags (`git filter-branch`) so that every file a purge commit ever deleted in those topics
disappears from every commit, drops the old objects (`reflog expire`, `gc --prune=now`) and
force-pushes the branch and the tags, each with a lease on what the remote had, so a branch or a
tag another PC pushed meanwhile is refused instead of overwritten. Every other clone of the vault
then holds a history the
remote no longer has: it must be cloned again (or reset to the remote once nothing of it is
unpushed), because a pull or push from it would bring the purged files back.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from studentassistant.config import VaultPurgeSettings
from studentassistant.vault.errors import VaultError
from studentassistant.vault.files import write_bytes_atomic
from studentassistant.vault.git import GitRunner
from studentassistant.vault.jsonl import encode_line
from studentassistant.vault.notes import list_generated, read_notes
from studentassistant.vault.secrets import guard
from studentassistant.vault.session_models import Event, Origin, SessionMeta
from studentassistant.vault.sessions import EVENTS_FILE_NAME, list_sessions, sessions_directory
from studentassistant.vault.sources import sources_directory
from studentassistant.vault.sync import GitSync
from studentassistant.vault.topics import require_topic, topic_directory
from studentassistant.vault.vault import MAIN_BRANCH

PurgeReason = Literal["burst-original", "observer-conversation", "folded-events", "old-generated"]

REASON_TEXT: dict[PurgeReason, str] = {
    "burst-original": "original de ráfaga",
    "observer-conversation": "conversación del observador ya reciclada",
    "folded-events": "eventos ya plegados en la instantánea",
    "old-generated": "material generado antiguo",
}
"""What each reason is called when shown to the student."""

PURGE_COMMIT_PREFIX = "purga:"
"""Every purge commit's subject starts with this; `--hard` looks for their deletions."""

CONVERSATIONS_DIRNAME = "conversations"
BURST_ORIGINAL = re.compile(r"^page-\d{3,}\.burst\d+\.[a-z0-9]+$")
OBSERVER_CONVERSATION = re.compile(r"^observer-(?P<session>\d{8}-\d{6})\.jsonl$")
# A topic-relative path in the notes: the target of a provenance link (ADR-0005), with or without
# the `../` that makes it relative to `notes/apuntes.md`, up to a fragment or the end of the link.
_CITED = re.compile(r"(?:sources|sessions|generated|conversations|state|review)/[^\s()\[\]<>\"'#]+")
_SECONDS_PER_DAY = 86_400
_FILTER_BRANCH_ENVIRONMENT = {"FILTER_BRANCH_SQUELCH_WARNING": "1"}
_PATHS_FILE_NAME = "studentassistant-purge-paths"


class PurgeError(VaultError):
    """A purge that cannot be planned or applied; the message (Spanish) says why."""


@dataclass(frozen=True)
class Compaction:
    """Replace every event of a topic up to `(session_id, seq)` by one `kind` event of `payload`.

    Built by whoever owns the fold (the observer): the vault writes it and never reads it back.
    """

    session_id: str
    seq: int
    kind: str
    payload: Mapping[str, Any]
    origin: Origin = "observer"


@dataclass(frozen=True)
class PurgeItem:
    """One file a purge removes (`size_after` is `None`) or rewrites smaller (folded events)."""

    path: str
    reason: PurgeReason
    size_before: int
    size_after: int | None = None

    @property
    def removed(self) -> bool:
        return self.size_after is None

    @property
    def saved_bytes(self) -> int:
        return self.size_before - (self.size_after or 0)


@dataclass(frozen=True)
class TopicPurgePlan:
    """What a purge would do to one topic; `skipped` (Spanish) says why it would do nothing.

    `path`s are vault-relative POSIX paths. `protected` lists the candidates kept because the
    current notes cite them.
    """

    subject_slug: str
    topic_slug: str
    items: tuple[PurgeItem, ...] = ()
    protected: tuple[str, ...] = ()
    skipped: str | None = None
    rewrites: Mapping[str, bytes] = field(default_factory=dict, repr=False, compare=False)

    @property
    def saved_bytes(self) -> int:
        return sum(item.saved_bytes for item in self.items)

    @property
    def compacts_events(self) -> bool:
        return any(item.reason == "folded-events" for item in self.items)


@dataclass(frozen=True)
class HistoryRewrite:
    """What `--hard` did: the paths dropped from history and the pack size before and after."""

    paths: tuple[str, ...]
    size_before: int
    size_after: int
    pushed: bool

    @property
    def saved_bytes(self) -> int:
        return max(self.size_before - self.size_after, 0)


@dataclass(frozen=True)
class PurgeResult:
    """What `apply_purge` did: the purge commit (`None`: nothing changed) and `--hard`'s part."""

    commit: str | None
    items: tuple[PurgeItem, ...] = ()
    history: HistoryRewrite | None = None

    @property
    def saved_bytes(self) -> int:
        return sum(item.saved_bytes for item in self.items)


def format_size(size: int) -> str:
    """A byte count as the student reads it: `512 B`, `3.4 KB`, `1.2 MB`."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def cited_paths(vault_relative_topic: str, notes: str) -> set[str]:
    """The vault-relative paths the notes text names, under the topic `vault_relative_topic`.

    Deliberately broad: any topic-relative path that appears anywhere in the notes counts as
    cited, whether or not it is a well-formed footnote, because keeping a file too many is safe and
    removing one the notes point to is not.
    """
    return {f"{vault_relative_topic}/{match}" for match in _CITED.findall(notes)}


def plan_topic_purge(
    sync: GitSync,
    subject_slug: str,
    topic_slug: str,
    policy: VaultPurgeSettings | None = None,
    compaction: Compaction | None = None,
    now: datetime | None = None,
) -> TopicPurgePlan:
    """What the retention `policy` removes from one topic now. Reads files and git; writes nothing.

    `compaction` is where the folded events end (`None`: no event is touched); `now` is the time
    generated material is aged against.

    Raises:
        SubjectNotFoundError, TopicNotFoundError, ...: what `require_topic` raises.
        PurgeError: the compaction names a session the topic does not list or a `seq` its log
            does not hold, or an event log is not readable.
    """
    policy = policy or VaultPurgeSettings()
    vault = sync.vault
    require_topic(vault, subject_slug, topic_slug)
    topic_root = topic_directory(vault, subject_slug, topic_slug)
    topic_rel = topic_root.relative_to(vault.path).as_posix()

    def skipped(reason: str) -> TopicPurgePlan:
        return TopicPurgePlan(subject_slug, topic_slug, skipped=reason)

    sessions = list_sessions(vault, subject_slug, topic_slug)
    still_open = [meta.id for meta in sessions if meta.ended_at is None]
    if still_open:
        return skipped(f"tiene una sesión abierta ({still_open[-1]})")
    notes = read_notes(vault, subject_slug, topic_slug)
    if notes is None:
        return skipped("aún no tiene apuntes")
    if policy.require_notes_tag and not sync.list_notes_tags(subject_slug, topic_slug):
        return skipped(
            "sus apuntes aún no están aceptados"
            f" (no hay etiqueta {subject_slug}/{topic_slug}/apuntes-vN)"
        )
    cited = cited_paths(topic_rel, notes)

    candidates: list[PurgeItem] = []
    rewrites: dict[str, bytes] = {}

    def relative(path: Path) -> str:
        return path.relative_to(vault.path).as_posix()

    if policy.burst_originals:
        notes_sources = sources_directory(vault, subject_slug, topic_slug, "notes")
        for path in _files(notes_sources):
            if BURST_ORIGINAL.match(path.name):
                candidates.append(PurgeItem(relative(path), "burst-original", path.stat().st_size))
    if policy.observer_conversations:
        ended = {meta.id for meta in sessions}
        for path in _files(topic_root / CONVERSATIONS_DIRNAME):
            match = OBSERVER_CONVERSATION.match(path.name)
            if match and match["session"] in ended:
                candidates.append(
                    PurgeItem(relative(path), "observer-conversation", path.stat().st_size)
                )
    if policy.generated_max_age_days is not None:
        moment = now or datetime.now(UTC)
        limit = policy.generated_max_age_days * _SECONDS_PER_DAY
        for path_text in list_generated(vault, subject_slug, topic_slug):
            committed = _last_commit_time(sync.git, path_text)
            if committed is not None and moment.timestamp() - committed > limit:
                size = (vault.path / path_text).stat().st_size
                candidates.append(PurgeItem(path_text, "old-generated", size))
    if policy.folded_events and compaction is not None:
        root = sessions_directory(vault, subject_slug, topic_slug)
        for path, old, new in _compacted_logs(root, sessions, compaction):
            path_text = relative(path)
            candidates.append(PurgeItem(path_text, "folded-events", len(old), len(new)))
            rewrites[path_text] = new

    kept = [item for item in candidates if item.path not in cited]
    protected = tuple(sorted(item.path for item in candidates if item.path in cited))
    return TopicPurgePlan(
        subject_slug,
        topic_slug,
        items=tuple(sorted(kept, key=lambda item: item.path)),
        protected=protected,
        rewrites={path: content for path, content in rewrites.items() if path not in cited},
    )


def _files(directory: Path) -> list[Path]:
    """The regular files directly in `directory`, sorted; symlinks never count."""
    if directory.is_symlink() or not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if not path.is_symlink() and path.is_file())


def _last_commit_time(git: GitRunner, path: str) -> int | None:
    """The Unix time of the last commit touching `path`; `None` when it was never committed."""
    result = git.run("log", "-1", "--format=%ct", "--", path)
    text = result.stdout.strip()
    return int(text) if result.ok and text.isdigit() else None


def _log_lines(path: Path) -> list[bytes]:
    """The complete lines of an event log, without their `\\n` (a torn last line left out)."""
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise PurgeError(f"no se puede leer {path}: {error}") from error
    lines = content.split(b"\n")
    return [line for line in lines[:-1] if line.strip()]


def _event(line: bytes, path: Path) -> Event:
    try:
        return Event.model_validate(json.loads(line))
    except ValueError as error:
        raise PurgeError(f"{path} tiene una línea que no es un evento: {error}") from error


def _compacted_logs(
    root: Path, sessions: Sequence[SessionMeta], compaction: Compaction
) -> Iterable[tuple[Path, bytes, bytes]]:
    """`(path, old, new)` of every event log the compaction changes, sessions in id order."""
    ids = [meta.id for meta in sessions]
    if compaction.session_id not in ids:
        raise PurgeError(
            f"la compactación nombra una sesión que el tema no tiene: {compaction.session_id}"
        )
    for session_id in ids:
        if session_id > compaction.session_id:
            break
        path = root / session_id / EVENTS_FILE_NAME
        if not path.exists():
            continue
        old = path.read_bytes()
        if session_id < compaction.session_id:
            new = b""
        else:
            events = [(_event(line, path), line) for line in _log_lines(path)]
            at = [event for event, _ in events if event.seq == compaction.seq]
            if not at:
                raise PurgeError(
                    f"la compactación termina en el evento {compaction.seq} de la sesión"
                    f" {session_id}, que su registro no tiene"
                )
            replacement = Event(
                seq=compaction.seq,
                t=at[0].t,
                origin=compaction.origin,
                kind=compaction.kind,
                payload=dict(compaction.payload),
            )
            tail = sorted(
                ((event.seq, line) for event, line in events if event.seq > compaction.seq),
                key=lambda pair: pair[0],
            )
            new = encode_line(replacement) + b"".join(line + b"\n" for _, line in tail)
        if new != old:
            yield path, old, new


def purge_commit_subject(items: Sequence[PurgeItem]) -> str:
    """`purga: 3 archivos borrados, 2 registros compactados (1.2 MB)`."""
    removed = sum(1 for item in items if item.removed)
    rewritten = len(items) - removed
    parts = []
    if removed:
        parts.append(f"{removed} {'archivo borrado' if removed == 1 else 'archivos borrados'}")
    if rewritten:
        parts.append(
            f"{rewritten} {'registro compactado' if rewritten == 1 else 'registros compactados'}"
        )
    saved = format_size(sum(item.saved_bytes for item in items))
    return f"{PURGE_COMMIT_PREFIX} {', '.join(parts)} ({saved})"


def apply_purge(
    sync: GitSync,
    plans: Sequence[TopicPurgePlan],
    *,
    hard: bool = False,
    before_commit: Callable[[TopicPurgePlan], None] | None = None,
) -> PurgeResult:
    """Carry out `plans`: remove and rewrite their files and commit them as one purge commit.

    `before_commit(plan)` runs for every plan with items once its files are written and before
    the commit, so a caller can refresh what depends on them (the observer snapshot); whatever it
    writes goes into the same commit. The commit is local: the next push carries it. Pending
    changes are best committed first (`sync.checkpoint`), or they end up in the purge commit.

    With `hard=True` the history of `main` and the tags is then rewritten to drop every file a
    purge commit ever deleted in the planned topics (skipped ones excepted), the old objects are
    pruned and, when the vault has its remote, `main` and the tags are force-pushed (see the
    module docstring for what that means for the other clones).

    Raises:
        PurgeError: the commit, the history rewrite or the forced push failed.
        SecretRefused: a rewritten log looks like it carries a secret (nothing is written then).
    """
    vault = sync.vault
    items = tuple(item for plan in plans for item in plan.items)
    for plan in plans:
        for item in plan.items:
            if not item.removed:
                guard(plan.rewrites[item.path])
    for plan in plans:
        for item in plan.items:
            if not item.removed:
                write_bytes_atomic(vault.path / item.path, plan.rewrites[item.path])
    for plan in plans:
        for item in plan.items:
            if item.removed:
                (vault.path / item.path).unlink(missing_ok=True)
    commit = None
    if items:
        if before_commit is not None:
            for plan in plans:
                if plan.items:
                    before_commit(plan)
        commit = sync.checkpoint(purge_commit_subject(items))
        if commit is None:
            raise PurgeError(f"no se ha podido guardar la purga: {sync.status().last_error}")
    history = None
    if hard:
        topics = [
            topic_directory(vault, plan.subject_slug, plan.topic_slug)
            for plan in plans
            if plan.skipped is None
        ]
        paths = purged_history_paths(sync, topics)
        if paths:
            history = rewrite_history(sync, paths)
    return PurgeResult(commit=commit, items=items, history=history)


def purged_history_paths(sync: GitSync, topic_roots: Sequence[Path]) -> tuple[str, ...]:
    """Every path under `topic_roots` a purge commit deleted and that is not back in the tree."""
    if not topic_roots:
        return ()
    vault = sync.vault
    relative = [root.relative_to(vault.path).as_posix() for root in topic_roots]
    result = sync.git.run(
        "log",
        "--format=",
        "--name-only",
        "--no-renames",
        "--diff-filter=D",
        f"--grep=^{PURGE_COMMIT_PREFIX}",
        MAIN_BRANCH,
        "--",
        *relative,
    )
    if not result.ok:
        raise PurgeError(f"no se puede leer el historial: {result.describe()}")
    paths = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return tuple(sorted(path for path in paths if not (vault.path / path).exists()))


def _pack_size(git: GitRunner) -> int:
    """Bytes git keeps for the repository's objects (loose and packed)."""
    result = git.run("count-objects", "-v")
    sizes = {}
    for line in result.stdout.splitlines() if result.ok else []:
        key, _, value = line.partition(":")
        if value.strip().isdigit():
            sizes[key.strip()] = int(value.strip())
    return (sizes.get("size", 0) + sizes.get("size-pack", 0)) * 1024


def _remote_tags(git: GitRunner, remote: str) -> dict[str, str]:
    """`refs/tags/<name>` -> the object the remote's tag points to (the tag object, not peeled)."""
    result = git.run("ls-remote", "--tags", "--refs", remote)
    if not result.ok:
        raise PurgeError(f"no se pueden leer las etiquetas del remoto: {result.describe()}")
    tags = {}
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref:
            tags[ref.strip()] = sha.strip()
    return tags


def _remote_exists(git: GitRunner, remote: str) -> bool:
    return git.run("remote", "get-url", remote).ok


def rewrite_history(sync: GitSync, paths: Sequence[str]) -> HistoryRewrite:
    """Drop `paths` from every commit of `main` and the tags, prune, force-push. The `--hard` part.

    The working tree must be clean (the purge commit just made it so). The branch is pushed with
    `--force-with-lease` against the remote-tracking `main`, so a remote that moved on since the
    last fetch refuses it instead of losing another PC's commits; every local tag is pushed with a
    lease on the value `git ls-remote` gave before the rewrite (absent: it must still be absent).

    Raises:
        PurgeError: git refused a step; the message names it and what to do.
    """
    vault = sync.vault
    git = GitRunner(
        vault.path,
        sync.identity,
        timeout=sync.settings.timeout_seconds,
        environment=_FILTER_BRANCH_ENVIRONMENT,
    )

    def must(*args: str) -> str:
        result = git.run(*args)
        if not result.ok:
            raise PurgeError(f"la reescritura del historial ha fallado: {result.describe()}")
        return result.stdout

    size_before = _pack_size(git)
    remote = sync.settings.remote
    has_remote = _remote_exists(git, remote)
    # What the remote's tags point to before the rewrite: every tag is then pushed with a lease on
    # that value, so a tag another PC created or moved meanwhile is refused, never overwritten.
    remote_tags = _remote_tags(git, remote) if has_remote else {}
    git_dir = Path(must("rev-parse", "--absolute-git-dir").strip())
    paths_file = git_dir / _PATHS_FILE_NAME
    paths_file.write_bytes(b"\0".join(path.encode("utf-8") for path in paths) + b"\0")
    try:
        index_filter = (
            "git rm -r --cached --ignore-unmatch --quiet"
            f" --pathspec-from-file={shlex.quote(str(paths_file))} --pathspec-file-nul"
        )
        must(
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "filter-branch",
            "--force",
            "--index-filter",
            index_filter,
            "--tag-name-filter",
            "cat",
            "--",
            MAIN_BRANCH,
            "--tags",
        )
    finally:
        paths_file.unlink(missing_ok=True)
    for ref in must("for-each-ref", "--format=%(refname)", "refs/original/").split():
        must("update-ref", "-d", ref)

    pushed = False
    if has_remote:
        tracking = git.run(
            "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{MAIN_BRANCH}"
        )
        lease = (
            f"--force-with-lease=refs/heads/{MAIN_BRANCH}:{tracking.stdout.strip()}"
            if tracking.ok and tracking.stdout.strip()
            else "--force-with-lease"
        )
        tags = must("for-each-ref", "--format=%(refname)", "refs/tags/").split()
        pushes: list[tuple[str, ...]] = [("push", "--porcelain", lease, remote, MAIN_BRANCH)]
        if tags:
            pushes.append(
                (
                    "push",
                    "--porcelain",
                    *(f"--force-with-lease={tag}:{remote_tags.get(tag, '')}" for tag in tags),
                    remote,
                    *(f"{tag}:{tag}" for tag in tags),
                )
            )
        for arguments in pushes:
            result = git.run(*arguments)
            if not result.ok:
                raise PurgeError(
                    "el historial se ha reescrito aquí pero no se ha podido subir"
                    f" ({result.describe()}); comprueba que ningún otro PC ha subido cambios o"
                    f" etiquetas y súbelo con `git push --force-with-lease {remote} {MAIN_BRANCH}"
                    " --tags` antes de usar la bóveda en otro PC"
                )
        pushed = True
    must("reflog", "expire", "--expire=now", "--all")
    must("gc", "--prune=now", "--quiet")
    return HistoryRewrite(
        paths=tuple(paths), size_before=size_before, size_after=_pack_size(git), pushed=pushed
    )


__all__ = [
    "PURGE_COMMIT_PREFIX",
    "REASON_TEXT",
    "Compaction",
    "HistoryRewrite",
    "PurgeError",
    "PurgeItem",
    "PurgeReason",
    "PurgeResult",
    "TopicPurgePlan",
    "apply_purge",
    "cited_paths",
    "format_size",
    "plan_topic_purge",
    "purge_commit_subject",
    "purged_history_paths",
    "rewrite_history",
]
