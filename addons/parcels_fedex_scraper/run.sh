#!/usr/bin/env bash
set -euo pipefail

export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8765}"

browser_ui="$(python - <<'PY'
import json
from pathlib import Path
try:
    data = json.loads(Path('/data/options.json').read_text())
except Exception:
    data = {}
print('true' if data.get('vinted_browser_ui') is True else 'false')
PY
)"

if [[ "${browser_ui}" == "true" ]]; then
  export DISPLAY="${DISPLAY:-:99}"
  Xvfb "${DISPLAY}" -screen 0 "${VINTED_BROWSER_UI_SCREEN:-1280x900x24}" -nolisten tcp >/tmp/xvfb.log 2>&1 &
  fluxbox >/tmp/fluxbox.log 2>&1 &

  vnc_password="$(python - <<'PY'
import json
from pathlib import Path
try:
    data = json.loads(Path('/data/options.json').read_text())
except Exception:
    data = {}
print(str(data.get('vinted_browser_ui_password') or '').strip())
PY
)"
  if [[ -n "${vnc_password}" ]]; then
    mkdir -p /data/vnc
    x11vnc -storepasswd "${vnc_password}" /data/vnc/passwd >/tmp/x11vnc-passwd.log 2>&1
    x11vnc -display "${DISPLAY}" -forever -shared -rfbport 5900 -rfbauth /data/vnc/passwd >/tmp/x11vnc.log 2>&1 &
  else
    x11vnc -display "${DISPLAY}" -forever -shared -rfbport 5900 -nopw >/tmp/x11vnc.log 2>&1 &
  fi
  websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
fi

exec python -m app.main
