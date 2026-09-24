BUDGET CLASS AND SHAPE OF WORKER TASKS (studentassistant)
   Every worker task goes to a Qwen class: `mechanical-qwen` when the change is small and fully
   specified, `complex-qwen` in every other case. Each task names exactly one `module:<name>`
   label matching a `docs/modules/<name>.md`, and its acceptance criteria include
   `scripts/test.sh` staying green. Tasks were written directly under their epic: keep the
   parent epic as the child's parent when you split one, keep its `Blocked by #N` lines on every
   child that still needs them, and prefix every task title with `[task] `.
   A task spanning backend and web or android is split per module. Anything that calls Claude is
   tested with `FakeClaude`; anything that needs speech uses `FakeTranscriber`.

SCRATCH FILES (studentassistant; workaround for titanarq/agent-os#33)
   Put any scratch file you need (original body, new body, summary) in a directory made with
   `mktemp -d` and remove only that directory. NEVER create, write or delete anything under
   `.cache/refiner/` or anywhere else under `.cache/`: that directory holds the driver's own run
   log, PID file and `runs.tsv` cost ledger, and removing it destroys this run's record.
