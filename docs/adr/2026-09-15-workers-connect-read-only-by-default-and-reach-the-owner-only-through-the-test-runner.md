# Workers connect read-only by default, and reach the owner only through the test runner

- Date: 2026-09-15
- Status: accepted
- Modules: workers

## Context
Issue #350's original criterion asked for a dedicated role that could only create and drop the
`db_sandbox` schema, so a worker's DB tests would run under a role that cannot touch the shared
tables at all. That criterion cannot hold, as written: 41 of the 58 test files use the
`db_sandbox` fixture, which writes real rows (fake-CIK-prefixed) into the shared tables for the
duration of a test, and the `engine` fixture runs `db.migrate` before anything else — both need
the owner role. A worktree also carries its own `.env`, gitignored and copied nowhere, holding the
owner's own credentials, and `roedor/db.py`'s `get_engine()` defaults to the owner URL when nothing
else says otherwise. No `GRANT` can make a worker's *DB-test* connection read-only while those
tests still have to run against the real, shared database — and shipping workers with no DB tests
at all (`pytest -m "not db"` only) would mean a worker's pipeline-adjacent work arrives untested
against the database it actually reads and writes in production.

## Decision
- **Read-only by default**, everywhere except a DB-test run. `config/agents.yaml`'s
  `project.worker_environment` (a mapping of environment variable name to value, exported into
  every worker's own backend process before it starts) sets `DATABASE_URL` to the `roedor_ro`
  role — `postgresql+psycopg://roedor_ro:roedor_ro@localhost:5435/roedor`. `worker_task.sh` reads
  and exports it through the existing config loader (`scripts/agent_lib.py`'s new
  `worker-environment` CLI subcommand), never as a literal in the mechanism itself
  (agent_os/docs/adr/2026-09-14-the-agent-mechanism-is-project-agnostic-and-configured-not-coded.md).
- **The owner is reached only through the test runner.** `scripts/test.sh` (roedor-specific, named
  as such in `project.test_command`, injected into the worker RULES as `__TEST_COMMAND__`) is the
  one door back to the owner role: it upgrades `DATABASE_URL` to the owner connection for the
  pytest invocation it runs (unsetting a non-owner `DATABASE_URL` so `roedor/db.py`'s own default
  applies, or honouring an explicit `ROEDOR_TEST_DATABASE_URL` when one is set), and leaves the
  script's existing compact-output contract unchanged.
- **A bare `pytest` under the read-only role stops loudly and immediately, not mid-migration.**
  `tests/conftest.py`'s `engine` fixture checks `SELECT current_user` before calling `db.migrate`;
  if it is `roedor_ro`, it calls `pytest.exit` with a one-line message pointing back at
  `scripts/test.sh`, instead of letting the migration fail with a permission-denied traceback.
- **The read-only property is a GRANT for the analyst's evaluation script too (#333).**
  `roedor/db.py` gains `READONLY_ROLE = "roedor_ro"` and `readonly_url()`, mirroring
  `application_url()`; `scripts/analyst_eval.py` sets the process's `DATABASE_URL` to it, before
  any tool call, so the read-only property stops being only code inspection and discipline.
- **This guards against an accident, not against intent.** A worker running a pipeline command
  that would write to the shared database — and, during the freeze, move the `m2` stamp — now
  fails by construction instead of by a rule it has to remember. It is not a security boundary: the
  worktree's own `.env` and `roedor/db.py`'s default still hold the owner's credentials, and a
  worker that deliberately overrides `DATABASE_URL` back to them is not stopped by anything here.
  The worker's own RULES still say plainly never to do that; this ADR is what backs that instruction
  with a GRANT for everything except a deliberate override.

### Rejected alternatives
- **A role scoped to create/drop only the `db_sandbox` schema.** This was the original criterion.
  It cannot hold: the `engine` fixture's migration and `db_sandbox`'s real writes both need the
  owner, and 41 of 58 test files depend on `db_sandbox`. A role that narrow would fail nearly every
  DB test a worker runs, which is worse than the accident it would guard against.
- **Workers run with no DB tests at all (`pytest -m "not db"` only).** Simple, and it would hold
  under a GRANT with no exception. Rejected because a worker's pipeline-adjacent work would then
  land untested against the database it actually operates on — exactly the kind of gap the
  worker/validator contract exists to close.
- **A separate `roedor_test` database, cloned from the real one.** Would let a worker's own
  connection stay read-only even through a DB-test run, with no owner-role exception anywhere.
  Rejected: the real database is roughly 13 GB, a clone drifts from the data the tests actually
  read the moment either one changes, and keeping it synchronized is materially more operational
  work than the test-runner door this ADR chose instead.

## Consequences
- A worker's own backend process cannot write to the shared database except through a DB test run
  via `scripts/test.sh`, without depending on the worker remembering a rule.
- `scripts/analyst_eval.py`'s read-only property is now a GRANT, closing #333.
- A second project adopting the mechanism sets its own `project.worker_environment` and
  `project.test_command` (or neither, and gets none of this) — nothing in `worker_task.sh`,
  `agent_lib.py` or `tests/conftest.py`'s guard names roedor's own role or database.
- The four curated files, `roedor/metrics`, `config/metrics*` and `db/migrations` are untouched by
  this change — nothing here moves the `m2` stamp
  (docs/adr/2026-09-10-freeze-m2-until-the-cut.md).

## Source
Decided by the owner in conversation on 2026-09-15, on issue #350 (the DB-role criterion, quoted in
full on the issue) and issue #333.
