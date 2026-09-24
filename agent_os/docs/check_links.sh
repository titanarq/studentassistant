#!/usr/bin/env bash
# Walks every *.md under the host root and checks that each relative markdown link
# (`](path)` or `](path#anchor)`) resolves to a file that exists, relative to the linking
# file's own directory. An absolute link (starts with `/`) or one with a URL scheme
# (`http://`, `mailto:`, ...) is not a file path and is skipped, same for a bare `#anchor`
# link within the same file.
#
# Exits non-zero and lists every broken link ("<file>:<line>: <broken-target>") when at
# least one is found; prints nothing and exits 0 when the tree is clean.
#
#   agent_os/docs/check_links.sh
set -euo pipefail

host_root=$(git rev-parse --show-toplevel)
cd "$host_root"

exclude_prefixes=(
  "./docs/history/"
  "./.venv/"
  "./agent_os/.venv/"
  "./node_modules/"
  "./.cache/"
  "./scratchpad/"
)

is_excluded() {
  local path="$1"
  for prefix in "${exclude_prefixes[@]}"; do
    [[ "$path" == "$prefix"* ]] && return 0
  done
  return 1
}

broken=0

while IFS= read -r -d '' file; do
  is_excluded "$file" && continue
  dir=$(dirname "$file")
  line_no=0
  while IFS= read -r line; do
    line_no=$((line_no + 1))
    # Extract every `](target)` occurrence on this line.
    remainder="$line"
    while [[ "$remainder" == *"]("* ]]; do
      remainder="${remainder#*](}"
      target="${remainder%%)*}"
      remainder="${remainder#*)}"
      [[ -z "$target" ]] && continue
      # Skip absolute paths, URLs and same-file anchors -- not a relative file path.
      case "$target" in
        /*|http://*|https://*|mailto:*|\#*) continue ;;
      esac
      # Drop a trailing #anchor, then decode the handful of percent-escapes markdown
      # links commonly carry (spaces and parentheses).
      target_path="${target%%#*}"
      target_path="${target_path//%20/ }"
      target_path="${target_path//%28/(}"
      target_path="${target_path//%29/)}"
      [[ -z "$target_path" ]] && continue
      resolved="$dir/$target_path"
      if [[ ! -e "$resolved" ]]; then
        echo "$file:$line_no: $target"
        broken=$((broken + 1))
      fi
    done
  done < "$file"
done < <(find . -name '*.md' -type f -print0)

if [[ "$broken" -gt 0 ]]; then
  echo "check_links.sh: $broken broken relative markdown link(s)" >&2
  exit 1
fi
exit 0
