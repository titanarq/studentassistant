"""The agent operating system: one directory a host project extends and never modifies.

`agent_os/docs/AGENT_OS.md` in the host describes what the mechanism does; the binding decision about its
shape is `agent_os/docs/adr/2026-09-21-the-mechanism-is-one-directory-extended-by-hosts-and-never-
modified.md`. Nothing under this package reads a literal that belongs to one project: every such
value comes from the host's `config/agents.yaml` (`agent_os.lib`), from a host-owned file that
config names, or from the environment.

The modules:

- `agent_os.lib` -- the `pydantic` models of `config/agents.yaml`, the budget-line parsing, the
  dispatchability predicates, the jsonl event reader, and the CLI the shell drivers read all of
  those through.
- `agent_os.guard` -- the deterministic guard: budget/quota/stall checks, planner events, `tick`.
- `agent_os.issues` -- the tracker CLI over `gh`.
- `agent_os.gh_app_token` -- one installation token per GitHub App identity.
- `agent_os.cli` -- where the package resolves its own interpreter and its host's root.
"""
