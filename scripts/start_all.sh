#!/usr/bin/env bash
set -euo pipefail

PYTHON="/app/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python"
fi

"$PYTHON" -m alembic upgrade head

pids=()
for worker in poller fetcher analyzer bizcheck notifier follower warden; do
  "$PYTHON" -m stories_monitor.cli "$worker" &
  pids+=("$!")
done

trap 'kill "${pids[@]}" 2>/dev/null; wait "${pids[@]}" 2>/dev/null' SIGTERM SIGINT

wait -n
exit $?
