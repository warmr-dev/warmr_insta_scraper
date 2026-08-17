#!/usr/bin/env bash
set -euo pipefail

python -m alembic upgrade head

pids=()
for worker in poller fetcher analyzer bizcheck notifier follower warden; do
  python -m stories_monitor.cli "$worker" &
  pids+=("$!")
done

trap 'kill "${pids[@]}" 2>/dev/null; wait "${pids[@]}" 2>/dev/null' SIGTERM SIGINT

wait -n
exit $?
