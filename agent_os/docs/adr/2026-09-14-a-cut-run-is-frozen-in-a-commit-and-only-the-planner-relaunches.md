# A cut run is frozen in a commit; only the planner relaunches it, and never more than twice

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
`worker_task.sh stop` already kills by PID/PGID; what it leaves behind is whatever the worker had
uncommitted, which is exactly what has blocked branch reuse after a rushed stop before. The guard
that does the cutting has none of the context needed to decide whether relaunching is the right
call — whether the brief itself is broken or the worker was just unlucky — so relaunching is not
its job.

## Decision
When the guard cuts a run (budget, stall, or liveness — see the sibling ADRs of 2026-09-14), it
commits whatever is staged/tracked as `WIP: cut by guard (<reason>)` on the worker's branch (never
`git add -A` — only the paths already tracked and modified, per the existing ownership rules) and
writes `CUT_BY_GUARD` to `.state`. It never restarts anything itself. Only the planner relaunches a
cut issue, and only after reading why it was cut and what the last `HEARTBEAT`/commits show. An
issue that gets cut and relaunched twice without reaching `DONE` is not tried a third time
automatically: the planner labels it `status:blocked-on-human` (see
`agent_os/docs/adr/2026-09-14-a-humans-reply-wakes-the-planner-never-the-worker-that-asked.md`) with a
comment explaining the two attempts, instead of retrying a brief that has now twice failed the same
way.

## Consequences
- A cut worktree is always safe to inspect or resume from — never mid-write.
- Retrying is a planning decision made with the two prior attempts as evidence, not a reflex.
- A systematically bad brief surfaces to a human after two tries, not after silently consuming
  budget forever.

## Source
Decided in conversation on 2026-09-14, issue #336.

## Amendment, 2026-09-15 (#362)
The cap itself -- counting `WIP: cut by guard` commits since the branch's fork point from its base
and refusing a third relaunch -- is now enforced by `scripts/worker_task.sh <backend> resume`
itself, against `planner.relaunch_cap` (`config/agents.yaml`, default 2), instead of depending on
the planner LLM to run the same `git log | grep -c` by hand and getting it right every time. A
refused `resume` prints one line naming the issue and the count, writes nothing (no pid, no state,
no events), and exits non-zero; the planner's rules now say what a refusal means -- label
`status:blocked-on-human`, comment the question, never retry -- rather than how to count. The
planner still decides *whether* to resume at all (a cut worth relaunching vs. one that is not); it
just no longer decides *whether the cap allows it*.
