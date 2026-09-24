#!/usr/bin/env bash
# Pages the project's one ntfy.sh topic -- the channel that means "only a human can move this
# forward now" (agent_os/docs/adr/2026-09-14-ntfy-pages-only-when-nothing-can-proceed-without-a-human.md).
# Called by agent_os.guard for the one trigger it can decide mechanically. Fails fast if
# the topic secret is missing, the same style as gh_app_token.py's load_secrets.
#
#   agent_os/bin/notify.sh "<message>"
set -euo pipefail
# shellcheck source=agent_os/bin/_python.sh
source "$(dirname "${BASH_SOURCE[0]}")/_python.sh"
cd "$(agent_os_host_root)"

# Where the topic lives is project config, not a literal here (agent_os/docs/adr/2026-09-14-the-agent-
# mechanism-is-project-agnostic-and-configured-not-coded.md); the topic itself is a secret and
# never leaves that gitignored file.
topic_file=$("$(agent_os_python)" -m agent_os.lib project-value notify_topic_file) || {
  echo "notify.sh: could not read project.notify_topic_file from config/agents.yaml" >&2
  exit 1
}
[ -f "$topic_file" ] || {
  echo "notify.sh: no $topic_file -- generate one first (<project>-<8 random hex>)" >&2
  exit 1
}
[ -n "${1:-}" ] || { echo "usage: $0 <message>"; exit 2; }

curl -s -d "$1" "ntfy.sh/$(cat "$topic_file")" >/dev/null
