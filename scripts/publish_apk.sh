#!/usr/bin/env bash
# Publish the debug APK to the phone-facing download folder in one command.
# Usage: scripts/publish_apk.sh [--build] [--dir <dir>]
#   --build      run `scripts/test.sh android assembleDebug` first
#   --dir <dir>  target folder (default: $SA_APK_DIR, else ~/apk-publico)
# Env: SA_APK_SRC overrides the APK path (default android/app/build/outputs/apk/debug/app-debug.apk
# in the main checkout), SA_APK_PORT the port shown in the URL (default 8766).
# Copies the APK to <dir>/studentassistant.apk and regenerates <dir>/index.html (Spanish).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
dir="${SA_APK_DIR:-$HOME/apk-publico}"
port="${SA_APK_PORT:-8766}"
build=0

while [ $# -gt 0 ]; do
  case "$1" in
    --build) build=1 ;;
    --dir)
      [ $# -ge 2 ] || { echo "--dir needs a value" >&2; exit 2; }
      dir="$2"
      shift
      ;;
    -h | --help)
      sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$build" -eq 1 ]; then
  "$root/scripts/test.sh" android assembleDebug
fi

src="${SA_APK_SRC:-$root/android/app/build/outputs/apk/debug/app-debug.apk}"
[ -f "$src" ] || { echo "APK not found: $src (run with --build)" >&2; exit 1; }

commit="$(git -C "$root" rev-parse --short HEAD)"
bytes="$(stat -c %s "$src")"
mib="$(LC_ALL=C awk -v b="$bytes" 'BEGIN { printf "%.1f", b / 1048576 }')"
mib_es="${mib/./,}"
stamp="$(date '+%Y-%m-%d %H:%M')"

mkdir -p "$dir"
cp "$src" "$dir/studentassistant.apk.tmp"
mv "$dir/studentassistant.apk.tmp" "$dir/studentassistant.apk"

cat > "$dir/index.html.tmp" <<HTML
<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Student Assistant - APK</title>
<style>body{font-family:sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;line-height:1.5}a.b{display:inline-block;padding:.8rem 1.2rem;background:#1a73e8;color:#fff;border-radius:.5rem;text-decoration:none}</style></head>
<body>
<h1>Student Assistant</h1>
<p>Versi&oacute;n de depuraci&oacute;n. Commit compilado: <code>${commit}</code>. Tama&ntilde;o: ${mib_es} MiB. Publicado: ${stamp}.</p>
<p><a class="b" href="studentassistant.apk" download>Descargar studentassistant.apk</a></p>
<p>Instala permitiendo or&iacute;genes desconocidos; empareja con el servidor desde el QR de /pair.</p>
</body></html>
HTML
mv "$dir/index.html.tmp" "$dir/index.html"

ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
echo "Published ${commit} (${mib} MiB) to ${dir}"
echo "Phone URL: http://${ip:-<LAN-IP>}:${port}/"
