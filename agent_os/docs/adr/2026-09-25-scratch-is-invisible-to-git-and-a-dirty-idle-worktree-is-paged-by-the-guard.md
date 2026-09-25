# Scratch is invisible to git, and a dirty idle worktree is paged by the guard

- Date: 2026-09-25
- Status: accepted
- Modules: worker driver (`bin/worker_task.sh`), guard (`agent_os/guard.py`)
- Issue: agent-os#86; follows agent-os#18, #22, #75, #84 and roedor#407, #417

## Context

`worker_task.sh start`, `resume` and `branch` refuse to run over a dirty worker worktree. Two
things turned that refusal into a deadlock nobody was told about:

- **Most of the dirt was `scratchpad/`**, the directory the worker's RULES send its diary and
  intermediate results to, and which no commit may carry (#407). #407 read the diary's uncommitted
  lines as "a run is not over". Every run state then needed its own exemption -- #18 (a finished
  run's diary on `start`), #22 (a cut run's diary on `resume`), #75 (a finished run's other
  scratch), #84 (a `DONE` run's diary on `resume`) -- and the next state not covered was the next
  deadlock. Hosts worked around it by hand with `scratchpad/` in `.git/info/exclude`. The same
  gap let `freeze_uncommitted_work` (#417) sweep scratch drafts into a cut's WIP commit.
- **A refusal over real dirt cleared itself on no event.** The planner is told a refused `start`
  means "wait for the next event"; no event commits or cleans a worktree, so every
  `idle_dispatchable` woke a planner run that was refused again, and no one was paged.

## Decision

1. **The driver hides `scratchpad/` from git in every worker worktree it touches**
   (`hide_scratchpad_from_git`): `scratchpad/.gitignore` containing `*`, written idempotently
   before any dirty check in `start`, `resume` and `branch`, and before the cut's freeze. It
   ignores itself, and it lives in that worktree only. Not `.git/info/exclude`: that file is
   shared by every checkout of the repository, the host's main checkout and its human included. A
   `scratchpad/.gitignore` the host tracks is the host's own and is left alone. Files git tracks
   under `scratchpad/` (the diary of an old branch) still show as modified; #84's exemption keeps
   covering `resume` over them.
2. **`start` and `branch` still give each run an empty scratchpad.** The #75 archive to
   `.cache/diaries/` keeps its conditions (an ending in `.state`, nothing alive, nothing else
   dirty) and now lists its candidates with `git ls-files --others`, which sees ignored files,
   skipping the `.gitignore` itself.
3. **The guard, not the driver, pages a dirty idle worktree.** On every tick
   `backend_worktree_dirt` reads each backend's worktree the way the driver's `uncommitted_work`
   does (its `.env` link and untracked scratch are not dirt; a live run's work in progress is not
   dirt; a `git status` that fails is). `dispatchable_scan` leaves a dirty backend's ready issues
   out of the dispatchable set (`DispatchableScan.dirty_worktree`), the way #392 leaves out a
   backend with no worktree, and `_page_dirty_worktree_if_due` pages `project.messages
   .backend_worktree_dirty` once per distinct listing: the same listing pages nothing more, a
   changed one pages at once, and a clean worktree resets it.

The issue asked for the page from the driver, through `bin/notify.sh`, at the moment it refuses.
The guard was chosen instead because the driver's page would have left the livelock in place:
the refused issue would still be announced as dispatchable, and each idle wake would still spend a
planner run on the same refusal. In the guard, one read does both jobs: the issue leaves the
dispatchable set (so no planner run is woken for it, and cleaning the worktree makes it newly
dispatchable, which wakes the planner by itself), and the page goes through
`render_human_message` like every other page, in the human's language. Nothing the driver path
had is lost:

- A `worker_task.sh` run by hand prints its refusal to the human who ran it.
- The planner's `resume` of an issue in review is not in the dispatchable set, so the guard reads
  every backend, not only those holding ready issues back; the page says how many ready issues
  wait, zero included.
- The guard sees the dirt within one tick whether or not anyone tries a dispatch. The planner's
  retries cannot duplicate a page, because none of them pages.

## Consequences

- An untracked diary or scratch file never refuses a dispatch again, in any run state, and never
  reaches a freeze's commit. The per-state exemptions (#18, #22, #75, #84) stay for the tracked
  diary and the archive, and nothing new needs one.
- A worktree that is dirty for any other reason stops its backend with the human paged once, and
  the planner is not woken to retry it. A human editing an idle backend worktree by hand is paged
  each time the listing changes; the ADR 2026-09-14 on ntfy pages allows it because on that
  backend nothing can proceed until they finish.
- The guard runs one `git status` per backend per tick.
- The guard leaves out every untracked path under `scratchpad/` without writing the ignore file
  itself (it does not write into worktrees). The two readings differ only for a host that TRACKS a
  `scratchpad/.gitignore` that does not ignore everything: the driver refuses there over untracked
  scratch, and the guard reads the worktree as clean. No host is known to do this.
- A host whose `project.messages` lacks `backend_worktree_dirty` gets the journal line and a
  `no page for the dirty worktree` notice instead of the page, and must add the key.
