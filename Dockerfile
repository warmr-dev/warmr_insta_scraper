# Образ для хостинга (Railway / Fly / любой Docker-раннер).
#
# По умолчанию - постоянный процесс с циклом раз в минуту (run_loop.py).
# Если платформа умеет cron, дешевле переопределить команду на
# `python scripts/run_once.py` и запускать по расписанию.

FROM python:3.11-slim

# tesseract не ставим: OCR_ENGINE=vision читает текст моделью, а лишний
# бинарник тянет ~150 МБ.
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir -e . && rm -rf /root/.cache

COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./
COPY fixtures ./fixtures

# Логи должны появляться сразу, а не после завершения процесса.
ENV PYTHONUNBUFFERED=1

# Постоянный цикл: Out Plane и подобные платформы держат процесс запущенным,
# cron у них нет. Для платформ с cron есть scripts/run_once.py - один цикл и
# выход.
CMD ["python", "scripts/run_loop.py"]
