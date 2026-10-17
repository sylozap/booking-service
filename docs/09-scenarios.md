# Ключевые сценарии

Три диаграммы, которые описывают систему целиком: удачный путь, конкурентный конфликт
и каскадная компенсация через события.

## 1. Успешная запись

```mermaid
sequenceDiagram
  autonumber
  actor C as Клиент
  participant GW as api-gateway
  participant BK as booking
  participant CA as catalog
  participant DB as PostgreSQL booking
  participant RL as outbox-relay
  participant K as Kafka
  participant NT as notification
  C->>GW: POST /api/v1/bookings + JWT + Idempotency-Key
  GW->>GW: подпись JWT, rate limit, correlation-id
  GW->>BK: POST /bookings + X-User-Id + X-Correlation-Id
  BK->>DB: поиск ключа идемпотентности
  DB-->>BK: не найден
  BK->>CA: GET /internal/v1/masters/M/services/S (если нет в Redis)
  CA-->>BK: duration 45, price 3500, tz, политики
  Note over BK,DB: одна транзакция
  BK->>DB: INSERT bookings (EXCLUDE проверяется)
  BK->>DB: INSERT outbox booking.created
  BK->>DB: INSERT idempotency_keys
  BK->>DB: COMMIT
  BK-->>GW: 201 Created
  GW-->>C: 201 + запись
  RL->>DB: выборка неопубликованных
  RL->>K: publish key=booking_id
  RL->>DB: пометить published_at
  K-->>NT: booking.created
  NT->>NT: processed_events + INSERT notifications (pending)
  NT->>NT: воркер доставки: SKIP LOCKED, sending, отправка в Telegram/email, sent
```

Ответ клиенту не ждёт Kafka: гарантию даёт outbox в той же транзакции.

## 2. Гонка двух клиентов за один слот

```mermaid
sequenceDiagram
  autonumber
  actor A as Клиент А
  actor B as Клиент Б
  participant BK as booking
  participant DB as PostgreSQL booking
  A->>BK: GET /availability -> 15:00 свободно
  B->>BK: GET /availability -> 15:00 свободно
  A->>BK: POST /bookings 15:00
  B->>BK: POST /bookings 15:00
  BK->>DB: BEGIN (A) INSERT 15:00-15:45
  BK->>DB: BEGIN (Б) INSERT 15:00-15:45
  Note over DB: gist-индекс блокирует Б<br/>до исхода транзакции А
  BK->>DB: COMMIT (A)
  DB-->>BK: ok, бронь А создана
  DB-->>BK: SQLSTATE 23P01 exclusion_violation (Б)
  BK->>DB: ROLLBACK (Б)
  BK->>DB: пересчёт ближайших свободных слотов
  BK-->>A: 201 Created
  BK-->>B: 409 problem+json code=slot_taken<br/>+ alternatives [15:45, 16:30]
```

Арбитр — только база. Redis-кэш доступности в этом сценарии не участвует.
Этот сценарий проверяется автотестом на этапе Э3 — см. [12-testing.md](12-testing.md).

## 3. Деактивация мастера и компенсация

```mermaid
sequenceDiagram
  autonumber
  actor AD as salon_admin
  participant CA as catalog
  participant K as Kafka
  participant BK as booking
  participant DB as PostgreSQL booking
  participant NT as notification
  actor CL as Клиенты
  AD->>CA: POST /masters/M/deactivate
  Note over CA: одна транзакция
  CA->>CA: is_active = false + outbox master.deactivated
  CA-->>AD: 202 Accepted
  CA->>K: catalog.masters.v1 (relay)
  K-->>BK: master.deactivated
  BK->>DB: проверка processed_events
  Note over BK,DB: одна транзакция
  BK->>DB: master_settings.is_active = false
  BK->>DB: SELECT будущие активные брони FOR UPDATE
  BK->>DB: status = cancelled_by_salon
  BK->>DB: outbox booking.cancelled на каждую бронь
  BK->>DB: INSERT processed_events
  BK->>DB: COMMIT
  BK->>K: booking.bookings.v1 (relay)
  K-->>NT: booking.cancelled x N
  NT->>CL: уведомления об отмене
```

Ответ администратору — `202 Accepted`: отмена броней произойдёт асинхронно,
обычно за доли секунды. Повторная доставка события безопасна.
