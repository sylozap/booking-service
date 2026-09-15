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
стенда; вручную — `make migrate`. Профиль наблюдаемости (`make up-obs`) пока
пустой.

## Структура

```
libs/common/          # шасси barber_common: конфиг, логи, ошибки, БД, Kafka, health
services/             # api-gateway, auth, catalog, booking, notification
deploy/compose/       # локальная инфраструктура
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
