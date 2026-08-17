#!/usr/bin/env bash
# Запуск на Railway.
#
# По умолчанию поднимает ВЕБ-ПУТЬ: один процесс run_loop.py, который каждую
# минуту читает трей, забирает новые фото и классифицирует их. Ему не нужны ни
# Redis, ни очереди - он последовательный.
#
# Мобильный путь (семь процессов из ТЗ §6) включается через MODE=workers, но
# ему нужен рабочий Redis, иначе процессы не увидят общую очередь.
#
# Почему миграции не запускаются по умолчанию: `alembic upgrade head` падал с
# "Can't locate revision identified by '0002'", когда база оказывалась новее
# образа, и контейнер уходил в цикл падений. Схема меняется редко - применяйте
# вручную или задайте RUN_MIGRATIONS=true.

set -uo pipefail

PYTHON="/app/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python"
fi

MODE="${MODE:-loop}"

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  echo "RUN_MIGRATIONS=true - применяю миграции"
  if ! "$PYTHON" -m alembic upgrade head; then
    # Не выходим: скорее всего база новее образа, и это не мешает работать.
    echo "ВНИМАНИЕ: миграция не удалась, продолжаю запуск" >&2
    echo "  проверьте: alembic current / alembic history" >&2
  fi
fi

if [ "$MODE" = "workers" ]; then
  # Мобильный путь. Требует USE_REDIS=true и доступного Redis - без общей
  # очереди poller и fetcher друг друга не увидят.
  echo "MODE=workers - запускаю семь процессов (ТЗ §6)"
  if [ "${USE_REDIS:-false}" != "true" ]; then
    echo "ВНИМАНИЕ: USE_REDIS!=true - очереди будут в памяти, процессы не" >&2
    echo "  увидят работу друг друга. Задайте USE_REDIS=true и REDIS_URL." >&2
  fi

  pids=()
  for worker in poller fetcher analyzer bizcheck notifier follower warden; do
    "$PYTHON" -m stories_monitor.cli "$worker" &
    pids+=("$!")
  done

  trap 'kill "${pids[@]}" 2>/dev/null; wait "${pids[@]}" 2>/dev/null' SIGTERM SIGINT
  # Если умер любой процесс - выходим, платформа перезапустит контейнер.
  wait -n
  exit $?
fi

# Веб-путь: один процесс, сам держит интервал, Redis не нужен.
echo "MODE=loop - запускаю run_loop.py (веб-путь, без Redis)"
exec "$PYTHON" scripts/run_loop.py
