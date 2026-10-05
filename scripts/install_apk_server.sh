#!/usr/bin/env bash
# Install and enable the systemd USER unit that serves the APK folder on the LAN (port 8766),
# so it survives reboots. Idempotent; replaces a hand-started `http.server` on that port.
# Usage: scripts/install_apk_server.sh [--dir <dir>]   (default: $SA_APK_DIR, else ~/apk-publico)
# Env: SA_APK_PORT overrides the port (default 8766).
set -euo pipefail

dir="${SA_APK_DIR:-$HOME/apk-publico}"
port="${SA_APK_PORT:-8766}"
unit_name=studentassistant-apk.service
unit_dir="$HOME/.config/systemd/user"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)
      [ $# -ge 2 ] || { echo "--dir needs a value" >&2; exit 2; }
      dir="$2"
      shift
      ;;
    -h | --help)
      sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

mkdir -p "$dir" "$unit_dir"
dir="$(cd "$dir" && pwd)"

unit="[Unit]
Description=Static server for Student Assistant debug APK (LAN)

[Service]
ExecStart=/usr/bin/python3 -m http.server ${port} --bind 0.0.0.0 --directory ${dir}
Restart=on-failure

[Install]
WantedBy=default.target
"

changed=0
if [ "$(cat "$unit_dir/$unit_name" 2>/dev/null || true)" != "${unit%$'\n'}" ]; then
  printf '%s' "$unit" > "$unit_dir/$unit_name"
  systemctl --user daemon-reload
  changed=1
fi

# A server started by hand (not by this unit) holds the port: stop it so the unit can bind.
main_pid="$(systemctl --user show -p MainPID --value "$unit_name" 2>/dev/null || echo 0)"
holder="$(ss -ltnpH "sport = :${port}" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2 || true)"
if [ -n "$holder" ] && [ "$holder" != "$main_pid" ]; then
  echo "Stopping manual server on port ${port} (pid ${holder})"
  kill "$holder"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$holder" 2>/dev/null || break
    sleep 0.5
  done
fi

systemctl --user enable "$unit_name" >/dev/null 2>&1
if [ "$changed" -eq 1 ]; then
  systemctl --user restart "$unit_name"
else
  systemctl --user start "$unit_name"
fi
echo "Enabled ${unit_name}: serving ${dir} on port ${port}"
