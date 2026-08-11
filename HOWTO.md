# Как это работает и как запускать

Практическая инструкция. Архитектура — в `README.md`, эксплуатация — в `RUNBOOK.md`.

---

## 1. Идея за 30 секунд

Мы **не** опрашиваем 57 000 аккаунтов по одному. ~20 рабочих аккаунтов подписаны на все
цели, и каждый делает **один** запрос `feed/reels_tray/` — он возвращает ленту сторис
сразу по всем своим подпискам.

```
57 000 отслеживаемых объектов  →  ~20 запросов
```

Это не оптимизация, а единственный способ уложиться в $650/мес. Поштучный опрос
(`user_stories` в цикле) запрещён спекой (§1, §11) и защищён тестом
`test_no_loop_over_per_target_story_calls`.

---

## 2. Видео не анализируются — только фото

**Это уже реализовано.** Видео отсекается в фетчере, ещё **до** скачивания и до любого
обращения к AI:

`src/stories_monitor/workers/fetcher.py`
```python
state = "skipped_video" if item.media_type == 2 else "new"
...
if item.media_type == 2:
    record_metric("videos_skipped", 1, None)
    continue          # не скачиваем, не ставим в очередь, не платим за AI
```

- `media_type == 1` → фото → скачивается → AI
- `media_type == 2` → видео → `pipeline_state = 'skipped_video'`, конец

Видео **никогда** не попадает в `q:analyze`, поэтому денег не стоит. Проверяется тестами:

- `test_videos_never_leave_skipped_video_and_are_never_pushed_to_analyze`
- `test_video_story_fixture_flows_through_the_fetcher_as_skipped_video`

Проверить на живых данных:
```bash
psql -d stories_monitor -c "select pipeline_state, count(*) from stories group by 1;"
```

---

## 3. Установка (один раз)

```bash
uv venv --python 3.11
uv pip install -e ".[dev]"

createdb stories_monitor
.venv/bin/alembic upgrade head

cp .env.example .env
.venv/bin/python -m stories_monitor.cli gen-key    # → вставить в SECRET_KEY
```

Проверка:
```bash
.venv/bin/pytest -q          # должно быть 124 passed
```

---

## 4. Куда вставлять API-ключи

Все ключи — **только** в `.env` (файл в `.gitignore`, в git не попадает).

### Gemini

```bash
AI_PROVIDER=gemini
GEMINI_API_KEY=ваш_ключ
CHEAP_MODEL=gemini-flash-lite-latest
SMART_MODEL=gemini-pro-latest
```

> Важно: модели `gemini-2.5-*` и `gemini-2.0-*` **отключены для новых ключей** и отдают
> 404. Работают только псевдонимы `-latest`.

### Anthropic

```bash
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=ваш_ключ
CHEAP_MODEL=claude-haiku-4-5-20251001
SMART_MODEL=claude-sonnet-5
```

### Прочее

```bash
SLACK_BOT_TOKEN=xoxb-...        # нужен scope chat:write
SLACK_CHANNEL=#leads

OCR_ENGINE=vision               # vision = через дешёвую модель
                                # tesseract = локальный бинарник (нужна установка)
```

Если ключ активного провайдера пустой — включается детерминированная заглушка
`FakeAIClient`, и весь конвейер всё равно работает (для тестов).

Проверить конфиг (секреты скрыты):
```bash
.venv/bin/python -m stories_monitor.cli config
```

---

## 5. Два режима работы

```bash
IG_TRANSPORT=fixture   # без сети: играет JSON из fixtures/. Slack пишет в stdout
IG_TRANSPORT=live      # реальные вызовы Instagram
```

**Держите `fixture` по умолчанию.** Переключайте на `live` только на время реальной
операции и возвращайте обратно — так случайный запуск не потратит аккаунт.

Важно: режим AI **не зависит** от `IG_TRANSPORT`. Реальный ключ Gemini + `fixture`
= настоящая классификация на фикстурных сторис. Самый дешёвый способ править промпты.

---

## 6. Подключить рабочий аккаунт

### Шаг 1 — завести аккаунт (офлайн, сети нет)

```bash
# в .env: IG_WORKER_USERNAME, IG_WORKER_PASSWORD, IG_WORKER_PROXY_URL
.venv/bin/python -m stories_monitor.cli seed-worker
```

Пароль шифруется Fernet, device_settings генерируются **один раз**, прокси привязывается
навсегда. Повторный запуск не перегенерирует их (§8).

Без прокси (только для тестов с домашнего IP):
```bash
.venv/bin/python -m stories_monitor.cli seed-worker --allow-no-proxy
```

### Шаг 2 — войти (один раз за всю жизнь аккаунта)

```bash
IG_TRANSPORT=live   # в .env
.venv/bin/python scripts/login_now.py
```

Скрипт спросит 6-значный код 2FA **в самом конце**, когда всё остальное уже готово —
у кода жизнь ~30 секунд, поэтому вся медленная работа делается до запроса.

Сессия сохраняется в БД. Повторные запуски вернут `session_reused` и **не** будут
логиниться заново — повторные входы это главный сигнал бана (§8).

Чтобы входы шли автоматически (нужно для follower/warden на недели вперёд), добавьте
TOTP-секрет:
```bash
IG_WORKER_TOTP_SECRET=JBSWY3DPEHPK3PXP    # base32 «setup key», не 6 цифр
```

---

## 6b. Несколько аккаунтов

Пароли лежат в БД в зашифрованном виде, поэтому переключение не требует вводить их снова.
Вместе с аккаунтом переезжают его неизменяемые `device_settings` и привязанный прокси (§8).

```bash
.venv/bin/python -m stories_monitor.cli accounts               # список, -> активный
.venv/bin/python -m stories_monitor.cli use-account yrsayl7    # переключить .env
```

`use-account` подставляет в `.env` логин, пароль и прокси выбранного аккаунта. Если у
него уже есть сессия — повторный вход не нужен.

---

## 7. Проверить сторис

```bash
.venv/bin/python scripts/check_stories.py
.venv/bin/python scripts/check_stories.py --username alice191451
```

Вывод:
```
tray: 18 entries | 6 accounts with live stories | 12 highlights skipped | truncated=False

@fcbarcelona    2 stories  [tray-prefetch]
     [PHOTO] 3712... 09:15 UTC (0.1h ago)
     [video] 3712... 08:40 UTC (0.7h ago)

TOTAL: 4 photos -> AI pipeline | 2 videos -> skipped (SPEC 1)
```

Только чтение. `media/seen/` не вызывается — мы не попадаем в список просмотревших,
иначе на нас пожалуются (§11).

---

## 7b. Веб-путь: сторис через куки браузера

Запасной путь, когда мобильный вход недоступен. Работает на живых данных, но это
**диагностика, а не продакшн**: веб-API жёстче лимитирован и не умеет продлевать
сессию сам — истекла, идти в браузер за новыми куки.

### Шаг 1 — взять куки

instagram.com → **F12** → **Application** → **Cookies** → `https://www.instagram.com`

Нужны все семь: `sessionid`, `csrftoken`, `ds_user_id`, `ig_did`, `mid`, `datr`, `rur`.
Одного `sessionid` **не хватит** — ленты ответят 302.

### Шаг 2 — сохранить в файл

```bash
cat > cookies.txt <<'EOF'
sessionid=...; csrftoken=...; ds_user_id=...; ig_did=...; mid=...; datr=...; rur=...
EOF
chmod 600 cookies.txt
```

`cookies*.txt` в `.gitignore` — в репозиторий не попадёт.

### Шаг 3 — запускать

```bash
# посмотреть сторис: кто, сколько, фото или видео (без AI, бесплатно)
.venv/bin/python scripts/web_stories.py --cookies-file cookies.txt

# классифицировать через AI
.venv/bin/python scripts/web_classify.py --cookies-file cookies.txt --limit 20
```

Скрипт скажет, каких куки не хватает, если что-то забыли.

### Повторные запуски ничего не стоят

Сторис пишутся в БД через `ON CONFLICT (story_id) DO NOTHING`, поэтому одна и та же
картинка анализируется ровно один раз:

```
уже анализировали ранее: 45 | новых к анализу: 0
Новых фото нет - все уже проходили через AI. Платить второй раз не за что.
```

Это принципиально: при опросе раз в 2 минуты одна сторис попадала бы в трей ~720 раз
за сутки жизни. Без дедупликации — 720 оплат вместо одной.

### Что смотреть после прогона

```bash
psql -d stories_monitor -c "select pipeline_state, count(*) from stories group by 1;"
psql -d stories_monitor -c "select metric, round(sum(value)::numeric,4) from metric_samples where metric like 'ai_%' group by 1;"
```

---

## 8. Запуск конвейера

Семь процессов, каждый перезапускается независимо:

```bash
.venv/bin/python -m stories_monitor.cli poller     # reels_tray → diff → очередь
.venv/bin/python -m stories_monitor.cli fetcher    # reels_media → таблица stories
.venv/bin/python -m stories_monitor.cli analyzer   # OCR + дешёвая + умная модель
.venv/bin/python -m stories_monitor.cli bizcheck   # вендор / гео / сообщество
.venv/bin/python -m stories_monitor.cli notifier   # Slack
.venv/bin/python -m stories_monitor.cli follower   # подписки, 150/день
.venv/bin/python -m stories_monitor.cli warden     # здоровье аккаунтов
```

Для `live` нужен Redis (`brew install redis && brew services start redis`).
В `fixture` очереди работают в памяти.

Диагностика:
```bash
... cli queues      # глубина очередей (в live требует запущенный Redis)
... cli latency     # детекция p50/p95 — главная метрика проекта
... cli health      # алерты
... cli progress    # прогресс подписок по шардам
```

---

## 9. Как работает AI (§7.4)

Только для **фото**:

1. **OCR** — текст с картинки
2. **Дешёвая модель** → `score` 0–10
3. Маршрутизация:
   - `0–4` → отказ, умная модель не вызывается
   - `5–6` → умная модель уточняет
   - `7+` → умную модель **пропускаем**, экономим деньги
4. Временный файл удаляется в `finally` — медиа не хранится никогда (§7.4, §11)
5. Дальше бизнес-проверки → Slack

Лид уходит в Slack только если `final_score >= 7` **и** все бизнес-проверки пройдены.
Одной оценки AI недостаточно.

Расходы:
```bash
psql -d stories_monitor -c "select metric, sum(value) from metric_samples where metric like 'ai_%' group by 1;"
```

---

## 10. Целевые аккаунты

```bash
.venv/bin/python -m stories_monitor.cli import-targets targets.csv
```

CSV: колонки `user_id` / `username` / `instagram_url` в любом порядке. Дубликаты
отбрасываются, шарды назначаются автоматически (минимум 8). Повторный импорт
идемпотентен и **не** затирает `last_reel_media_ts`.

---

## 11. Что известно по опыту живых запусков

**Новые аккаунты не работают.** Свежесозданный аккаунт отдаёт `467` на всех приватных
эндпоинтах — проверено с домашнего IP, через прокси, мобильным входом и браузерной
сессией. Аккаунт с историей (6 постов) заработал сразу.

**Датацентровые прокси не работают.** Один и тот же аккаунт: через LA-прокси
(`AS212238 Datacamp`) — `467`, напрямую — работает. Нужны **US residential / ISP**
прокси, ~$3–8 за IP в месяц.

**Браузерные куки не подходят.** `reels_tray` с ними работает, но `reels_media` даёт
403 **и аннулирует сессию** — проверено дважды. Нужен мобильный вход (`login_now.py`).

Итого для продакшна: **отлежавшиеся аккаунты + residential прокси + мобильный вход**.

---

## 12. Открытые вопросы

1. **Обрезается ли `reels_tray`** на тысячах подписок — определяет размер шардов.
   Пагинация написана, включается `TRAY_PAGINATION_ENABLED=true`.
2. **Схема БД Recommend.us** — пока заглушка `StubVendorRepository`.
3. **Одна сторис = один медиафайл или вся серия аккаунта** — влияет на формат Slack.
4. **Порог Service Fit** — 70 или 75.
5. **Список разрешённых категорий услуг.**

---

## 13. За какими репозиториями следить

Instagram меняет приватный API без предупреждения. Эти два репозитория — способ узнать
об этом. Подпишитесь на релизы обоих: если запрос вдруг перестал работать, сначала
смотрите туда, а не в наш код.

- **[subzeroid/instagrapi](https://github.com/subzeroid/instagrapi)** — клиент, на котором
  всё построено. Классы исключений между релизами переименовывают и переносят — поэтому
  в `live.py` каждое имя резолвится через `getattr` с запасным вариантом, а не импортом.
  Перед использованием метода стоит заглянуть в исходники: `reels_tray` в instagrapi
  **нет**, поэтому мы зовём `private_request("feed/reels_tray/")` напрямую.
- **[dilame/instagram-private-api](https://github.com/dilame/instagram-private-api)** —
  TypeScript-клиент с расписанными payload'ами и форматами ответов. Именно на него
  ссылается спека (§7.1). Лучший источник правды о том, **что реально шлёт приложение**.

Это не формальность. Сверка с `ReelsMediaFeed` из второго репозитория нашла настоящую
ошибку: наш `reels_media` слал 3 поля вместо 7. Не хватало `supported_capabilities_new`
(сообщает Instagram, какие форматы медиа мы умеем читать), `_uid` и `device_id`.

**Правило:** добавляете или меняете эндпоинт — сверяйтесь с обоими. instagrapi — как
вызывать, dilame — что должно быть внутри запроса.

---

## 14. Безопасность

- Секреты только в `.env`, он в `.gitignore`
- Пароли шифруются Fernet, в логах скрыты
- `device_settings` и прокси **никогда** не меняются у существующего аккаунта
- Челленджи **никогда** не решаются автоматически — только человек (§7.8)
- `media/seen/` и любые write-эндпоинты не вызываются (§11)
- `instagrapi` импортируется только в `transport/live.py`
