# Реестр архитектурных решений

Каждый файл — одно решение с контекстом, альтернативами и последствиями.
Формат: краткий ADR (Architecture Decision Record).

Все решения приняты 02.09.2026 при утверждении архитектуры v1. Сверка с реализацией —
17.10.2026: пересмотренные решения сохраняют исходный текст и раздел «Пересмотр»;
сводка — [17-design-vs-reality.md](../17-design-vs-reality.md).

| № | Решение | Статус |
|---|---|---|
| [0001](0001-merge-scheduling-into-booking.md) | Слить scheduling и booking в один сервис | Принято |
| [0002](0002-exclude-constraint-as-arbiter.md) | `EXCLUDE USING gist` как единственный арбитр занятости | Принято; пересмотрено 17.10.2026 (`occupied_range` — обычный столбец + `CHECK`) |
| [0003](0003-no-saga.md) | Саги в системе нет | Принято |
| [0004](0004-transactional-outbox.md) | Transactional outbox + собственный relay | Принято |
| [0005](0005-json-pydantic-events.md) | JSON + Pydantic-схемы вместо Avro и Schema Registry | Принято |
| [0006](0006-topic-per-aggregate.md) | Топик на агрегат, тип события внутри конверта | Принято |
| [0007](0007-at-least-once-with-dedup.md) | At-least-once + таблица `processed_events` | Принято |
| [0008](0008-own-auth-service.md) | Собственный auth вместо Keycloak | Принято |
| [0009](0009-own-api-gateway.md) | Собственный api-gateway на FastAPI | Принято; пересмотрено 17.10.2026 (маршрутизация по таблице шаблонов, Ingress без TLS) |
| [0010](0010-jwt-verified-in-services.md) | Проверка JWT и на гейтвее, и в сервисах через JWKS | Принято |
| [0011](0011-reminder-scheduler-in-db.md) | Планировщик напоминаний в базе, не в Redis и не в Kafka | Принято |
| [0012](0012-cache-is-not-arbiter.md) | Кэш доступности не является источником истины | Принято; пересмотрено 17.10.2026 (инвалидация поколениями, предохранитель к Redis) |
| [0013](0013-service-snapshot-in-booking.md) | Снимок услуги сохраняется в брони | Принято; дополнено 23.09.2026 (`cancel_deadline_min` в снимке) |
| [0014](0014-database-per-service.md) | Один PostgreSQL, база и роль на сервис | Принято |
| [0015](0015-migrations-as-helm-hook-job.md) | Миграции — k8s Job по Helm-хуку | Принято |
| [0016](0016-monorepo-with-shared-lib.md) | Монорепо с общей библиотекой libs/common | Принято |
| [0017](0017-master-services-in-catalog.md) | `master_services` живёт в catalog, график — в booking | Принято |
| [0018](0018-http-instead-of-grpc.md) | HTTP вместо gRPC для внутренних вызовов | Принято |
