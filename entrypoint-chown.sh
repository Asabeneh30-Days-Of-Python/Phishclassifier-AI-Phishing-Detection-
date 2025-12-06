#!/bin/sh
set -e
TARGETS="/app/instance /app/instance/reports /home/app/.cache/nltk_data /app/models"
for d in $TARGETS; do
  if [ -d "$d" ]; then
    chown -R app:app "$d" || true
  else
    mkdir -p "$d" && chown -R app:app "$d" || true
  fi
done
exec "$@"
