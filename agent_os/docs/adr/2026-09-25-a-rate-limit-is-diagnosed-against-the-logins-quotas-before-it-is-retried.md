# A rate limit is diagnosed against the login's quotas before it is retried

- Date: 2026-09-25
- Status: accepted
- Modules: tracker CLI (`agent_os/issues.py`)
- Issue: agent-os#70; follows agent-os#27

## Context

A host reported that, under concurrent mechanism load, every GraphQL-backed `gh` subcommand
(`gh issue view --json`, `gh pr checks`, `gh label list`, `issues.py validate`...) failed with
`API rate limit already exceeded for user ID ...` while `gh api rate_limit` read `remaining: 5000`
at the same moment, and read it as a false positive -- a race between concurrent callers.

It is not one. GitHub gives each API its own bucket: `core` (REST, 5000 an hour), `graphql`
(5000 points an hour), `search`, and more. The top-level `.rate` that `gh api rate_limit` prints
first is `core` alone; the empty one was `.resources.graphql`, and that text is GitHub's answer
when the GraphQL bucket is at zero. Its secondary limit (concurrency, points per minute) says
`secondary rate limit` instead. The GraphQL bucket is shared by every host, role and interactive
session running under the same login, and the host's vendored `issues.py move` still spent
~100-224 points a move on its board (fixed in #27, not yet pulled there).

Two things in `issues.py` made it worse. `gh_text` retried every rate limit five times with
1+2+4+8+16 s of backoff: useless against a bucket that refills up to an hour later, and the final
failure carried gh's bare text, which is what led to the misreading. And `validate` -- run by
`worker_task.sh start` before every dispatch -- read the issue and the open-issue list over
GraphQL, so an empty GraphQL bucket stopped every dispatch while REST had its whole quota.

## Decision

1. **A primary rate limit is checked against the quotas before it is retried.** `gh_text` asks
   `gh api rate_limit` (free: it counts against no bucket) which buckets are at zero. If one is,
   it exits at once, naming the bucket, its limit and its reset time, followed by gh's own text.
   If none is, the refusal was transient and is retried with backoff as before; if the quotas
   cannot be read, it is retried too.
2. **A secondary rate limit waits at least a minute** before each retry, GitHub's documented
   minimum when it sends no `Retry-After`; its buckets are not read, since they say nothing about
   it.
3. **A bare 403 is not a rate limit.** Only GitHub's wording (`rate limit`) or a 429 is retried; a
   permission refused fails on the first answer.
4. **`validate` reads over REST**, like `move` since #27: the issue with `GET
   repos/{o}/{r}/issues/{n}`, the open issues (only when the body names a blocker) with the
   paginated REST listing, pull requests left out as `gh issue list` left them out.

## Consequences

- An exhausted quota reads as what it is, with the time it refills, and costs no minute of
  sleeping; the host's "false positive" is diagnosable from the error alone.
- `validate` and `move` no longer depend on the GraphQL bucket except for `move`'s board mirror,
  which runs after the labels are written: an empty GraphQL bucket fails a move there, with the
  state already carried by the labels, and the error now says why.
- The guard, the drivers' shell `gh issue view` calls and the agents' own `gh pr`/`gh issue`
  commands still draw on GraphQL; the bucket is still one per login across every host. Moving
  those is not decided here: each one is cheap (~1 point), and the heavy spender was the board
  listing #27 removed.
