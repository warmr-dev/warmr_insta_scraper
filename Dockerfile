# Образ для хостинга (Railway / Fly / любой Docker-раннер).
#
# Запускает ОДИН цикл и завершается - рассчитан на Cron, а не на постоянный
# процесс. Так дешевле и надёжнее: зависший процесс не остаётся висеть, а
# следующий запуск начинает с чистого состояния (всё состояние в Postgres).

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

# Один цикл: собрать сторис -> классифицировать новые фото -> выйти.
CMD ["python", "scripts/run_once.py", "--limit", "25"]
