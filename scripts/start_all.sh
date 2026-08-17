#!/usr/bin/env bash
set -euo pipefail

alembic upgrade head

pids=()
for worker in poller fetcher analyzer bizcheck notifier follower warden; do
  stories "$worker" &
  pids+=("$!")
done

trap 'kill "${pids[@]}" 2>/dev/null; wait "${pids[@]}" 2>/dev/null' SIGTERM SIGINT

wait -n
exit $?
