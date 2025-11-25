#!/usr/bin/env bash
set -euo pipefail

# ensure eventlet monkey-patch runs only for IO worker
python - <<'PY'
import eventlet
eventlet.monkey_patch()
print("eventlet monkeypatched")
PY

# forward any args; use module form so executable is found when not on PATH
exec python -m celery "$@"
