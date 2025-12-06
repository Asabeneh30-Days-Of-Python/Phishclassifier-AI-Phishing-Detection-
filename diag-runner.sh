#!/bin/sh
# diag-runner.sh — prints CELERY_BROKER_URL parsing fields then runs the mounted start-worker-wait.sh

printf "CELERY_BROKER_URL=%s\n" "$CELERY_BROKER_URL"
proto_and_rest=${CELERY_BROKER_URL#*://}
hostport=${proto_and_rest%%/*}
suffix=${hostport##*:}

printf "proto_and_rest=%s\nhostport=%s\nsuffix=%s\n" "$proto_and_rest" "$hostport" "$suffix"

# now run the repo script to observe runtime behaviour
sh /app/start-worker-wait.sh -- echo RUN
