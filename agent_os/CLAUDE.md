@AGENTS.md

## Claude Code specifics

- **MCP servers.** This repository needs none: the suite has no database and no network, and the
  data servers a host declares in its own `.mcp.json` read that host's data, not this code.
  Library documentation comes from the user-level Context7 server.
- **Long runs in the background.** Launch the full suite detached and check its exit code rather
  than polling the log; `pytest -q` block-buffers its dots.
- **Hosts are siblings of this checkout.** When a bug report comes from a host, reproduce it here
  with a test; reading the host's checkout is fine, editing its `agent_os/` is not — that copy only
  changes through a `git subtree pull`.
