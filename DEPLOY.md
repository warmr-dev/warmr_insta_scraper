# Деплой: Supabase + запуск раз в минуту

Два режима, выбор зависит от платформы:

| Платформа | Что запускать | Почему |
|---|---|---|
| **Out Plane**, Fly, Render | `run_loop.py` (в `Dockerfile` по умолчанию) | cron нет — процесс должен работать постоянно |
| Railway, k8s CronJob, systemd | `run_once.py` | есть cron — дешевле: платите только за секунды работы |

`run_loop.py` настраивается переменными `LOOP_INTERVAL_SEC` (по умолчанию 60)
и `LOOP_PHOTO_LIMIT` (25). Он переживает ошибку в цикле, корректно завершается
по SIGTERM и разрежает попытки при стойких отказах — если куки мертвы, долбить
Instagram раз в минуту вредно.

Схема: куки лежат в Supabase, Railway раз в минуту запускает один цикл
«собрать сторис → классифицировать новые фото → выйти».

---

## 1. Supabase

Строка подключения: **Project Settings → Database → Connection string → URI**.

```bash
DATABASE_URL=postgresql+psycopg://postgres.XXX:ПАРОЛЬ@aws-0-РЕГИОН.pooler.supabase.com:5432/postgres
```

Обратите внимание на префикс `postgresql+psycopg://` — SQLAlchemy требует именно его,
а Supabase показывает `postgresql://`.

Создать таблицы:

```bash
DATABASE_URL="..." .venv/bin/alembic upgrade head
```

Появится 12 таблиц, включая `cookies`.

### Таблица `cookies`

| Колонка | Назначение |
|---|---|
| `username` | чьи куки (первичный ключ) |
| `sessionid`, `csrftoken`, `ds_user_id`, `ig_did`, `mid`, `datr`, `rur` | семь куки веб-API |
| `is_active` | выключить аккаунт, не удаляя строку |
| `updated_at` | когда обновляли — видно, какие протухают |
| `last_error` | почему аккаунт перестал работать |

Строку можно править **прямо в интерфейсе Supabase**. Это не роскошь: веб-куки
нельзя продлить из кода, поэтому ручное обновление — штатная операция.

Добавить аккаунт:

```bash
DATABASE_URL="..." .venv/bin/python -m stories_monitor.cli web-add yrsayl7 --cookies-file cookies.txt
DATABASE_URL="..." .venv/bin/python -m stories_monitor.cli web-list --check
```

---

## 2. Out Plane

```bash
outplane login

# GitHub нужно подключить в браузере — из терминала нельзя:
# https://github.com/apps/out-plane-connect-run/installations/select_target

outplane app create instascraper --repo warmr-dev/warmr_insta_scraper --branch ers

outplane env set DATABASE_URL='postgresql+psycopg://...' \
                 SECRET_KEY='...' AI_PROVIDER=gemini GEMINI_API_KEY='...' \
                 CHEAP_MODEL=gemini-flash-lite-latest \
                 SMART_MODEL=gemini-pro-latest OCR_ENGINE=vision --deploy

outplane logs --follow
```

Порт открывать не нужно: это фоновый процесс, HTTP он не обслуживает.

---

## 2b. Railway

1. New Project → Deploy from GitHub → выбрать репозиторий, ветку `ers`
2. Railway увидит `Dockerfile` и `railway.toml` (расписание `* * * * *`)
3. Variables → добавить:

```bash
DATABASE_URL=postgresql+psycopg://postgres.XXX:ПАРОЛЬ@...pooler.supabase.com:5432/postgres
SECRET_KEY=<из `cli gen-key`>
AI_PROVIDER=gemini
GEMINI_API_KEY=<ключ>
CHEAP_MODEL=gemini-flash-lite-latest
SMART_MODEL=gemini-pro-latest
OCR_ENGINE=vision
IG_TRANSPORT=fixture
```

`IG_TRANSPORT=fixture` — веб-путь ходит в Instagram напрямую, мимо этой настройки,
а `fixture` страхует от случайных мобильных вызовов.

Redis не нужен: цикл работает последовательно, без очередей.

---

## 3. Сколько это занимает

Замерено на живых данных (Supabase в Сиднее, 27 подписок со сторис):

| Этап | Время |
|---|---|
| Чтение куки | ~2с |
| `reels_tray` | ~1с |
| `reels_media` (27 аккаунтов) | ~5с |
| Запись сторис пачкой | ~12с |
| Классификация | ~10с на фото |

**Холостой цикл — 21 секунда**, с классификацией трёх фото — 52 секунды.

Ограничение `--limit 25` в `Dockerfile` держит цикл в пределах минуты и страхует
от всплеска расходов.

> Первая версия писала каждую сторис отдельным запросом — на удалённой БД
> (~2.3с на round-trip) цикл занимал **8 минут**. Пакетная запись сократила
> его до 21 секунды. Если соберётесь добавлять запись в цикл — пишите пачками.

---

## 4. Что смотреть после деплоя

Логи в формате JSON, каждый цикл заканчивается событием `cycle_done`:

```json
{"accounts_ok": 1, "accounts_failed": 0, "users_with_stories": 27,
 "duration_sec": 21.4, "event": "cycle_done"}
```

Запросы к БД:

```sql
-- лиды
select t.username, a.final_score, a.service_category, a.analyzed_at
from story_analysis a
join stories s on s.story_id = a.story_id
join targets t on t.user_id = s.target_user_id
where a.final_score >= 7 order by a.analyzed_at desc;

-- состояние аккаунтов
select username, is_active, updated_at, last_error from cookies;

-- расход
select metric, round(sum(value)::numeric, 4)
from metric_samples where metric like 'ai_%' group by 1;
```

---

## 5. Когда куки истекут

Веб-сессия живёт несколько недель и умирает сразу при смене пароля или выходе
из аккаунта в браузере. **Продлить её из кода нельзя.**

Признаки:

- в логах `all_accounts_failed`, код выхода 1
- в таблице `cookies` появился `last_error`, `is_active` стал `false`

Лечение: зайти в браузер, скопировать семь куки, обновить строку в Supabase
(или `web-add` заново). Цикл подхватит их со следующего запуска.

Именно поэтому веб-путь — **диагностика, а не продакшн**. Мобильный вход
перелогинивается сам, а с `IG_WORKER_TOTP_SECRET` — даже при 2FA. Для постоянной
работы нужны отлежавшиеся аккаунты, residential-прокси и мобильный путь.

---

## 6. Сколько стоит

| Статья | В месяц |
|---|---|
| Railway (Cron, короткие запуски) | ~$5 |
| Supabase | free tier хватает на старте |
| AI (Gemini) | ~$0.0006 за фото |

Расход на AI зависит от числа новых **фото**: видео (около 70% всех сторис)
отсекаются до скачивания и до модели, поэтому не стоят ничего.
