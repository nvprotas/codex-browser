#!/usr/bin/env sh
set -eu

export DISPLAY="${DISPLAY:-:99}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-chromium}"

SCREEN_WIDTH="${SCREEN_WIDTH:-1440}"
SCREEN_HEIGHT="${SCREEN_HEIGHT:-900}"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-/tmp/chrome-profile}"

mkdir -p "$XDG_RUNTIME_DIR" "$CHROME_PROFILE_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

cleanup() {
  kill "${WEBSOCKIFY_PID:-}" "${SOCAT_PID:-}" "${CHROME_PID:-}" "${X11VNC_PID:-}" "${FLUXBOX_PID:-}" "${XVFB_PID:-}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

rm -f /tmp/.X99-lock

Xvfb "$DISPLAY" -screen 0 "${SCREEN_WIDTH}x${SCREEN_HEIGHT}x24" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

# Ждём, пока Xvfb поднимет unix-сокет дисплея, иначе x11vnc может упасть на старте.
DISPLAY_NUM="$(printf '%s' "$DISPLAY" | sed 's/^://')"
DISPLAY_SOCK="/tmp/.X11-unix/X${DISPLAY_NUM}"
for _ in $(seq 1 60); do
  if [ -S "$DISPLAY_SOCK" ]; then
    break
  fi
  sleep 0.2
done

if [ ! -S "$DISPLAY_SOCK" ]; then
  echo "browser-sidecar: Xvfb не поднял сокет $DISPLAY_SOCK" >&2
  tail -n 50 /tmp/xvfb.log >&2 || true
  exit 1
fi

fluxbox >/tmp/fluxbox.log 2>&1 &
FLUXBOX_PID=$!

x11vnc -display "$DISPLAY" -rfbport 5900 -localhost -forever -shared -nopw >/tmp/x11vnc.log 2>&1 &
X11VNC_PID=$!

chromium \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --no-first-run \
  --no-default-browser-check \
  --remote-allow-origins=* \
  --password-store=basic \
  --user-data-dir="$CHROME_PROFILE_DIR" \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=9222 \
  --window-size="${SCREEN_WIDTH},${SCREEN_HEIGHT}" \
  about:blank >/tmp/chromium.log 2>&1 &
CHROME_PID=$!

for _ in $(seq 1 60); do
  if curl -sf http://127.0.0.1:9222/json/version >/dev/null; then
    break
  fi
  sleep 1
done

# Chromium в Debian может слушать только loopback, поэтому прокидываем порт для соседних контейнеров.
socat TCP-LISTEN:9223,reuseaddr,fork,bind=0.0.0.0 TCP:127.0.0.1:9222 >/tmp/socat.log 2>&1 &
SOCAT_PID=$!

NOVNC_WEB=/usr/share/novnc
if [ ! -f "$NOVNC_WEB/vnc.html" ] && [ ! -f "$NOVNC_WEB/vnc_lite.html" ]; then
  echo "browser-sidecar: noVNC entrypoint не найден в $NOVNC_WEB" >&2
  ls -la "$NOVNC_WEB" >&2 || true
  exit 1
fi

websockify --web="$NOVNC_WEB" 0.0.0.0:6901 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &
WEBSOCKIFY_PID=$!

wait "$CHROME_PID"
