# The mechanism's own files are not the host project's protected paths

- Date: 2026-09-16
- Status: accepted
- Modules: workers

## Context
`worker_task.sh` refuses to let a worker's changes land on a fixed set of paths, and
`docs/modules/workers.md` says that set holds "regardless of what the brief says". The set mixes
two things that are not alike:

- The host project's protected paths — `config/cik_chains.yaml`, `config/concepts.yaml`,
  `config/proposals/`, `baselines/`, `docker-compose.yml`, `AGENTS.md`, `CLAUDE.md`,
  `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/DOMAIN.md`, `docs/adr/`, and the roedor scripts
  whose edit would silently move a stamp. An agent editing one of these invalidates a rebuild or
  rewrites a decision it was not asked to take.
- The mechanism's own files — `scripts/worker_task.sh`, `scripts/qwen_task.sh` and `.claude/`.
  These are not roedor's; they are the machinery under development, and the tracking epic #336
  exists to change them.

Treating them as one list makes the mechanism unable to develop itself. #363 was dispatched with a
body naming `scripts/worker_task.sh` in four of its six acceptance criteria and four of its five
stages; the worker did the work, and the result then failed the merge check the same driver
enforces. #388 and #389 are in the same position, and so is every future issue under #336.

The safety argument that put the driver on the list does not survive inspection. A stage runs as
`$main/scripts/worker_task.sh` — the copy in the MAIN checkout — while the worker edits the copy in
its own worktree. Editing the worktree's copy cannot change the process already running. The real
protection is a separate rule that is already absolute: a worker never writes in the main checkout.

## Decision
The set is split in two, and only one of them is unconditional.

- **The host project's protected paths** hold regardless of what the brief says. A brief that names
  one of them is a defect in the brief, and the worker stops and says so.
- **The mechanism's own files** may be written IN THE WORKTREE when the issue body names the path as
  the target of the work, and never otherwise. The worker says in a comment that it is doing so,
  before the first commit that touches one. Writing them in the main checkout stays forbidden, by
  the rule that already forbids writing there at all.

Until the two lists exist as two configured keys, a brief that names a mechanism file authorizes it
and the reviewer records the fact at merge time, which is what was done for #386.

## Consequences
- `config/agents.yaml` gains a second key alongside `project.forbidden_paths`, and the comment above
  it that claims "nothing here is the mechanism's own, it is all roedor's" stops being false.
- The audit `worker_task.sh` runs over a worker's diff consults the issue body for the second list
  and only for it; the first list needs no lookup, which keeps the expensive check on the rare path.
- A reviewer no longer has to choose between a brief the human wrote and a rule the driver enforces.
  That choice cost #386 a stop at the merge gate with the work already correct and CI green.
- The mechanism is meant to be exported to other projects
  (`agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md`). A
  host project configures the first list; the second belongs to the mechanism and travels with it.
