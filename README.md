# Barber Platform

Микросервисная платформа онлайн-записи клиентов к парикмахерам.
Учебный проект, доводится до полностью рабочего состояния.

**Стек:** Python 3.12, FastAPI, SQLAlchemy 2.x (async), PostgreSQL, Redis, Kafka,
Kubernetes, OpenTelemetry, Prometheus, Grafana, Tempo, Loki.

## Требования

- [uv](https://docs.astral.sh/uv/) 0.12+ — управление зависимостями и окружением
- Docker с `docker compose` — локальная инфраструктура и `testcontainers`
- `make`

Python 3.12 ставить отдельно не нужно: `uv` скачает нужную версию сам.

## Быстрый старт

```bash
make sync     # развернуть окружение из lock-файла
make lint     # ruff check + ruff format --check
make type     # mypy --strict
make test     # pytest
```

`make test` включает интеграционные тесты на `testcontainers`; без доступного
Docker они помечаются как пропущенные. Прогнать только те, которым Docker не нужен:

```bash
make test-unit
```

Полный список команд — `make help`.

## Локальный стенд

```bash
make up       # PostgreSQL, Redis, Kafka, миграции и пять сервисов
make logs     # логи стенда
make down     # остановить и удалить тома
```

Проверка после `make up`:

```bash
curl localhost:8000/health/ready   # api-gateway
curl localhost:8003/health/ready   # booking, вместе с проверкой БД
```

Порты: api-gateway `8000`, auth `8001`, catalog `8002`, booking `8003`,
notification `8004`. Исходники смонтированы в контейнеры — правка файла
перезапускает сервис без пересборки образа.

Миграции применяются одноразовыми контейнерами `<service>-migrate` при подъёме
стенда; вручную — `make migrate`.

## Kubernetes в kind

```bash
make kind-up                  # кластер, Traefik, infra, пять сервисов, демо-данные
make kind-up OBSERVABILITY=1  # то же плюс Prometheus, Grafana, Tempo, Loki, Alloy
make kind-grafana             # Grafana кластера на http://localhost:3000
make kind-down                # удалить кластер
```

Нужны Docker, `kind` 0.33, `kubectl`, `helm` 3.22, `uv`. Для браузера —
строка `127.0.0.1 barber.local` в `/etc/hosts`; без неё:

```bash
curl --resolve barber.local:80:127.0.0.1 http://barber.local/api/v1/salons
```

Повторный `make kind-up` безопасен: недостающее создаётся, существующее
остаётся, пересобранные образы выкатываются. Секреты генерируются прямо в кластер
(`scripts/gen_secrets.sh`), в файлы не пишутся. Демо-данные — Job `api-gateway-seed`
(`scripts/seed.py`); на compose-стенде то же самое — `make seed`.

**Прогон 03.10.2026** (WSL2, 12 ядер, 16 ГБ для WSL, Docker 29, kind 0.33,
Kubernetes 1.37, `OBSERVABILITY=1`):

| | Время |
|---|---|
| С нуля, на пустом кэше образов в узле | 28 мин 20 с |
| из них: кластер · Traefik и metrics-server · сборка и загрузка образов | 1 мин 15 с · 1 мин 45 с · 6 мин 20 с |
| из них: секреты и infra · наблюдаемость · пять сервисов и seed | 6 мин · 8 мин 20 с · 4 мин 40 с |
| Повторный запуск на живом кластере | 13 мин 40 с |

Почти всё время — скачивание образов внутрь узла (Kafka, kube-prometheus-stack)
и их загрузка `kind load`. Узел `kind` со всеми тремя namespace после seed
занимает 4,7 ГБ памяти; без `OBSERVABILITY=1` — примерно на 1 ГБ меньше.
Комфортно — от 16 ГБ на машине; на 8 ГБ — без наблюдаемости.

etcd узла держит данные в памяти (на диске WSL записи шли по 150–450 мс,
и control plane перезапускался): после перезапуска Docker кластер
пересоздаётся — `make kind-down kind-up`.

## Наблюдаемость

```bash
make obs-up     # стенд + Prometheus, Alertmanager, Grafana, Tempo, Loki, Alloy; трейсинг в сервисах включается
make obs-down   # убрать профиль, сервисы остаются и перезапускаются без трейсинга
```

Профиль отдельный: вместе со стендом он не помещается в 8 ГБ памяти
(см. [docs/13-risks.md](docs/13-risks.md)), у каждого контейнера свой `mem_limit`.

| Что | Где |
|---|---|
| Grafana, вход без логина | http://localhost:3000 |
| Prometheus | http://localhost:9090 |
| Alertmanager | http://localhost:9093 |
| Tempo API | http://localhost:3200 |
| Alloy, граф сбора логов | http://localhost:12345 |

Конфигурация compose — `deploy/compose/observability/`, дашборды —
`deploy/observability/dashboards/`, алерты — `deploy/observability/alerts/`
и `deploy/observability/alertmanager.yaml` (общие с кластером, в Grafana
правятся только через git). `make alerts-check` проверяет правила, гоняет
их юнит-тесты (`rules.test.yaml`) и проверяет маршрутизацию — то же делает CI.

**Ручная проверка после `make obs-up`:**

1. Prometheus → Status → Targets: пять целей `UP`.
2. Grafana → Connections → Data sources: Prometheus, Tempo, Loki, Alertmanager.
3. Grafana → Dashboards → Barber: System overview — данные на всех панелях
   после трафика из шага 4; Async — outbox, лаг, DLQ, планировщики; Business —
   наполняется, когда в системе есть брони (seed-сценарий — T8.9).
4. Трафик (до seed-скрипта из T8.9 — вручную; анонимный лимит гейтвея
   невелик, поэтому часть запросов идёт в сервисы напрямую):

   ```bash
   for i in $(seq 1 5); do
     curl -s -o /dev/null -X POST localhost:8001/api/v1/auth/register \
       -H 'Content-Type: application/json' \
       -d "{\"email\":\"demo-$i-$RANDOM@example.com\",\"phone\":\"+7900$RANDOM$i\",\"password\":\"correct-horse-battery-7\"}"
   done
   for i in $(seq 1 50); do curl -s -o /dev/null localhost:8002/api/v1/salons; done
   for i in $(seq 1 10); do curl -s -o /dev/null localhost:8000/api/v1/salons; done
   ```

5. Grafana → Explore → Tempo, запрос
   `{ resource.service.name = "notification" && name =~ "consume.*" }`:
   трейс начинается с `POST /api/v1/auth/register` в `auth`, внутри —
   `create auth.users.v1` и под ним `consume auth.users.v1` в `notification`.
   Span `publish auth.users.v1` relay лежит отдельным трейсом со ссылкой
   на `create` — так задумано, см. [docs/11-observability.md](docs/11-observability.md#трассировка).
6. Логи трейса: в трейсе из шага 5 у span'а кнопка **Logs for this span** —
   открываются записи Loki всего трейса из всех сервисов (`auth` и
   `notification`). Обратно: в записи Loki поле `trace_id` → **Open the trace**.
   Метки Loki — только `service`, `env`, `level`; `trace_id` — structured
   metadata, `correlation_id` и `user_id` — поля записи.
7. Алерт на DLQ: «ядовитое» сообщение в топик, который читает `notification`,

   ```bash
   echo '{ not json' | docker compose -f deploy/compose/docker-compose.yml exec -T kafka \
     kafka-console-producer --bootstrap-server localhost:9092 --topic booking.bookings.v1
   ```

   В течение минуты Alertmanager (http://localhost:9093) показывает
   `DeadLetters` с `reason=invalid_message`, дашборд Async — сообщение в DLQ.
8. Алерт на вставший outbox: `docker compose -f deploy/compose/docker-compose.yml stop kafka`,
   затем запись, которая пишет событие (регистрация из шага 4). На дашборде
   Async «Waiting in the outbox» растёт в течение 5 с, через ~4,5 минуты
   срабатывает `OutboxNotDraining`. После `... start kafka` outbox пустеет
   и алерт гаснет.

## Структура

```
libs/common/          # шасси barber_common: конфиг, логи, ошибки, БД, Kafka, health
services/             # api-gateway, auth, catalog, booking, notification
deploy/compose/       # локальная инфраструктура и профиль наблюдаемости
deploy/observability/ # дашборды Grafana, общие для compose и кластера
```

## Конфигурация

Все настройки — переменные окружения, читаются через `pydantic-settings`.
Полный перечень с описанием — в [.env.example](.env.example). Значений по умолчанию
у секретов, адреса базы, Redis и брокера нет: приложение падает на старте,
если переменная не задана.

Для локальной разработки:

```bash
cp .env.example .env
```

Файл `.env` в git не попадает.
