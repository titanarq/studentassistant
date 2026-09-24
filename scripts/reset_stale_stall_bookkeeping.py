#!/usr/bin/env python3
"""Re-anchor the guard's stall bookkeeping when it belongs to an earlier worker run.

Host-side workaround for titanarq/agent-os#52. `guard.turns_since_commit` counts, on a backend with
no event timestamps (Qwen), the commits in `<startref>..HEAD` of the CURRENT run, but it compares
them with `commit_count` in `.cache/agent_guard_<backend>.json`, and nothing resets that file when
a new issue is dispatched. The next run inherits the old `commit_count`/`turn_count_at_commit`:
turns-since-commit goes negative (the stall cut comes late), and the run's first commits never
move the anchor, so a worker that commits regularly can still be cut as stalled later on.

For each worker backend with a live run (state not ended, PID alive), this script takes the same
`.lock` the guard tick takes and compares the run's identity (issue + startref) with the one it
recorded last time in `.cache/agent_guard_<backend>.run`. If they differ, if that file is missing,
or if `turn_count_at_commit` is ahead of the live turn count (a new stage process after a resume),
it sets `commit_count` to the run's real commit count, sets `turn_count_at_commit` to the current
turn count and clears `warned_at_turn_count`. Every other field is kept, `last_quota_status`
included. Anchoring at the current turn can never trigger a cut: the next tick sees 0 turns since
the last commit. It is idempotent and prints only when it changes something.

It runs on the mechanism's interpreter (`agent_os/.venv/bin/python`) as the guard unit's
`ExecStartPre` (drop-in
`scripts/systemd/studentassistant-guard.service.d/reset-stale-stall-bookkeeping.conf`). Remove the
script, the drop-in and the runbook note once agent-os#52 is fixed and the subtree is pulled.
"""

import fcntl
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(os.environ.get("AGENT_OS_HOST_ROOT") or Path(__file__).resolve().parent.parent)
os.environ["AGENT_OS_HOST_ROOT"] = str(ROOT)
sys.path.insert(0, str(ROOT / "agent_os"))

from agent_os import guard  # noqa: E402 -- needs AGENT_OS_HOST_ROOT set first


def run_identity(paths: guard.WorkerPaths) -> dict | None:
    """Issue + startref of the live run, or None if there is no live run to re-anchor."""
    if not (paths.statefile.is_file() and paths.issuefile.is_file() and paths.startref.is_file()):
        return None
    state, _, _ = guard.read_state_marker(paths.statefile)
    if state.startswith(guard.RUN_ENDED_STATES) or not guard._is_alive(paths.pidfile):
        return None
    return {
        "issue": paths.issuefile.read_text().strip(),
        "startref": paths.startref.read_text().strip(),
    }


def reset_backend(backend: str) -> None:
    paths = guard.worker_paths(backend, ROOT)
    if not paths.bookkeeping.parent.is_dir():
        return
    run_file = paths.bookkeeping.with_suffix(".run")
    with guard._bookkeeping_lock_path(paths.bookkeeping).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity = run_identity(paths)
        if identity is None:
            return
        try:
            recorded = json.loads(run_file.read_text())
        except (OSError, ValueError):
            recorded = None
        bookkeeping = guard._load_bookkeeping(paths.bookkeeping)
        events = guard.read_events(paths.events)
        turns = guard.usage_summary(events, parser=guard.backend_stream_parser(backend)).turns
        commits = len(guard._commit_timestamps(paths.worktree, paths.startref))
        stale = recorded != identity or bookkeeping.turn_count_at_commit > turns
        if stale and (
            bookkeeping.commit_count != commits
            or bookkeeping.turn_count_at_commit != turns
            or bookkeeping.warned_at_turn_count is not None
        ):
            before = asdict(bookkeeping)
            bookkeeping.commit_count = commits
            bookkeeping.turn_count_at_commit = turns
            bookkeeping.warned_at_turn_count = None
            guard._save_bookkeeping(paths.bookkeeping, bookkeeping)
            print(
                f"{backend}: re-anchored stall bookkeeping for issue #{identity['issue']} "
                f"(was {before}, now {asdict(bookkeeping)}) -- workaround agent-os#52"
            )
        if recorded != identity:
            temporary = run_file.with_name(f".{run_file.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(identity))
            os.replace(temporary, run_file)


def main() -> None:
    for backend in guard.BACKENDS:
        try:
            reset_backend(backend)
        except Exception as error:  # noqa: BLE001 -- never block the guard tick that follows
            print(f"{backend}: stall bookkeeping check skipped: {error!r}", file=sys.stderr)


if __name__ == "__main__":
    main()
