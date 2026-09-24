#!/usr/bin/env bash
# What the workers have done lately and what they have spent, in one screen.
#
#   agent_os/bin/worker_progress.sh                 # every configured backend, the last hour
#   agent_os/bin/worker_progress.sh 390             # that issue, resolving its backend
#   agent_os/bin/worker_progress.sh --hours 3       # a wider window
#
# A backend whose stream carries no `total_cost_usd` (Qwen, #387) has spend only readable as
# tokens: this sums them over the issue's archived stage logs plus the live one, the same set
# `max_cost_usd` is checked against. The narrative half is the worker's own
# `scratchpad/progress.log`.
set -euo pipefail

# shellcheck source=agent_os/bin/_python.sh
source "$(dirname "${BASH_SOURCE[0]}")/_python.sh"
main=$(agent_os_host_root)
python=$(agent_os_python)
hours=1
max_lines=40
issue=""
backends=""

# Every `project.backends` entry with a worktree -- never a hardcoded pair of names, so a third
# backend needs no edit here (#510, #514).
mapfile -t all_backends < <("$python" -m agent_os.lib worktree-backends)

while [ $# -gt 0 ]; do
  case "$1" in
    --hours) hours=$2; shift 2 ;;
    --max-lines) max_lines=$2; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *)
      is_backend=""
      for candidate in "${all_backends[@]}"; do
        [ "$1" = "$candidate" ] && is_backend=1
      done
      if [ -n "$is_backend" ]; then
        backends=$1
      else
        issue=$1
      fi
      shift
      ;;
  esac
done

# One `backend<TAB>worktree path` line per configured backend, resolved once here rather than by
# the interpreter below -- `agent_os.lib worktree-path` is the one place that reads
# `project.backends` (#510, #514).
worktree_lines=""
for backend in "${all_backends[@]}"; do
  worktree_lines+="$backend"$'\t'"$("$python" -m agent_os.lib worktree-path "$backend")"$'\n'
done

exec "$python" - \
  "${WORKER_CACHE_DIR:-$main/.cache}" "$hours" "$max_lines" "$issue" "$backends" \
  "${all_backends[*]}" "$worktree_lines" <<'PY'
import datetime as dt
import json
import pathlib
import subprocess
import sys

cache, hours, max_lines, issue, backends, all_backends, worktree_lines = sys.argv[1:8]
cache = pathlib.Path(cache)
hours, max_lines = float(hours), int(max_lines)
all_backends = all_backends.split()
worktrees = dict(line.split("\t", 1) for line in worktree_lines.splitlines() if line)
cutoff = dt.datetime.now() - dt.timedelta(hours=hours)
window = f"since {cutoff:%H:%M} ({hours:g} h)"


def recent_progress(backend):
    """The worker's own lines inside the window. Its timestamps are `YYYY-MM-DD HH:MM` in local
    time, so the cutoff formats the same way and the comparison is a string one."""
    path = pathlib.Path(worktrees[backend]) / "scratchpad" / "progress.log"
    if not path.is_file():
        return [f"(missing {path})"]
    stamp = f"{cutoff:%Y-%m-%d %H:%M}"
    lines = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
    inside = [line for line in lines if line[:16] >= stamp]
    if not inside:
        last = lines[-1] if lines else "(empty)"
        return [f"(nothing in the window; the last line is older) {last}"]
    dropped = len(inside) - max_lines
    inside = inside[-max_lines:]
    if dropped > 0:
        inside.insert(0, f"(... {dropped} earlier line(s) trimmed by --max-lines)")
    return inside


def spend(issue_number, backend):
    """Only this backend's logs, and only this issue's. A stage log is named
    `<ts>-<backend>-stage<N>.jsonl`; the live log belongs to whatever issue the backend is on right
    now, named in `.cache/worker_<backend>.issue`, so it is added only when that is this issue --
    otherwise a finished issue's total silently grows with the next one's turns."""
    paths = sorted((cache / "spend" / issue_number).glob(f"*-{backend}-stage*.jsonl"))
    if not paths:
        return []
    live = cache / f"worker_{backend}.jsonl"
    issuefile = cache / f"worker_{backend}.issue"
    on_this_issue = issuefile.is_file() and issuefile.read_text().strip() == issue_number
    if live.is_file() and on_this_issue:
        paths.append(live)
    rows, grand, grand_reported = [], 0, 0
    for path in paths:
        billed = turns = 0
        reported = None
        cost = "ABSENT"
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # The `result` event is the run's OWN summary, not another turn: adding its usage to
            # the per-turn sum counts the whole run twice, which is what made these figures read
            # about double until 2026-09-16.
            if event.get("type") == "result":
                summary = event.get("usage") or {}
                reported = summary.get("total_tokens")
                if event.get("total_cost_usd") is not None:
                    cost = f"{event['total_cost_usd']:.4f} USD"
                continue
            usage = (event.get("message") or {}).get("usage") or event.get("usage") or {}
            if usage:
                turns += 1
                billed += sum(
                    usage.get(field, 0)
                    for field in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "output_tokens",
                    )
                )
        grand += billed
        grand_reported += reported or 0
        shown = f"{reported:,}" if reported is not None else "no result"
        rows.append(
            f"  {path.name:42s} turns={turns:>3}  per turn={billed:>12,}  from result={shown:>12}"
            f"  cost={cost}"
        )
    rows.append(
        f"  {'ISSUE TOTAL':42s}          per turn={grand:>12,} "
        f" from result={grand_reported:>12,}"
    )
    rows.append(
        "  (#387's token ceiling is measured against the 'from result' column; a cut stage never"
        " writes it and counts as 0)"
    )
    return rows


def doing_issue():
    """The issue a worker is on right now, so the common call needs no argument."""
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--label", "status:doing", "--json", "number", "-q", ".[].number"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.split()
        return out[0] if out else ""
    except (subprocess.SubprocessError, OSError):
        return ""


for backend in ([backends] if backends else all_backends):
    print(f"== worker {backend} — {window} ==")
    for line in recent_progress(backend):
        print(f"  {line}")
    number = issue or doing_issue()
    rows = spend(number, backend) if number else []
    if rows:
        print(f"  -- spend on #{number} --")
        for row in rows:
            print(row)
    elif number:
        print(f"  -- no {backend} stages on #{number} --")
    print()
PY
