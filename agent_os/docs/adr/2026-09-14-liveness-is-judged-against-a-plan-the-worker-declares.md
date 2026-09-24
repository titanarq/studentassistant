# Liveness is judged against a plan the worker declares, not a fixed timeout

- Date: 2026-09-14
- Status: accepted
- Modules: workers

## Context
A fixed global timeout (e.g. "cut after 5h of silence") is wrong in both directions: a worker
legitimately watching a 40-minute rebuild is silent without being stuck, and a worker that hangs on
its second turn should not get five hours of grace just because that number fits the slowest
legitimate task. The two cases look identical from outside unless the worker itself says which one
it is doing.

## Decision
On its first turn, before anything else, a worker states an `EXPECT`: what it is about to do, how
long that is normally, and after how long silence means something is wrong (e.g.
`EXPECT rebuild-wait normal=4h cutoff=5h`). This is mandatory — it is in the injected rules, not
left to the brief. If stating a real number needs its own short analysis first, `EXPECT` may name a
temporary cutoff of 30 minutes while that analysis runs, and must be replaced by a real one before
the 30 minutes are up. Every subsequent `HEARTBEAT` may restate it. Until the first `EXPECT` is
posted, the guard applies that same 30-minute default. The guard's liveness check is then only
ever: *has more time passed since the last `HEARTBEAT`/`EXPECT` than that line's own cutoff?* —
never a number the guard chose itself.

## Consequences
- A worker that goes quiet inside its own declared window is left alone, however long that window
  is — including one that is mostly watching an external process and barely spending tokens.
- A worker that never declares anything gets the tightest possible grace period, which is the
  correct default: declaring is cheap, and silence about silence should not be rewarded.
- The guard's liveness logic never changes per task — only the declarations do.

## Source
Decided in conversation on 2026-09-14, issue #336.
