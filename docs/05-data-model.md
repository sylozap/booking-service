# Модель данных

Один инстанс PostgreSQL, отдельная база и отдельный пользователь на сервис.
Межсервисных внешних ключей нет ни одного: `master_id` в `booking` — просто UUID.

Все временные метки — `timestamptz` в UTC. Все первичные ключи — UUID, генерируются в приложении (`uuid4`). В проекте
закладывался UUIDv7 ради локальности индекса; в стандартной библиотеке
Python 3.12 `uuid7` нет, и он не внедрён.

## База auth

| Таблица | Ключевые поля | Индексы и ограничения |
|---|---|---|
| `users` | id, email, phone, password_hash, email_confirmed_at, is_active, created_at | `UNIQUE(lower(email))`, `UNIQUE(phone)` |
| `user_roles` | user_id, role, salon_id | Суррогатный `id` + два частичных уникальных индекса, см. ниже; `salon_id IS NULL` для глобальных ролей |
| `refresh_tokens` | token_hash, user_id, family_id, expires_at, revoked_at, replaced_by, user_agent, ip | `UNIQUE(token_hash)`, индекс `(user_id, revoked_at)`; повторное использование отозванного гасит всю family |
| `email_confirmations` | token_hash, user_id, expires_at, used_at | Одноразовый, TTL 24 ч |
| `signing_keys` | kid, public_pem, is_active, created_at | Приватная часть — в k8s Secret; ротация через второй активный ключ |
| `service_clients` | client_id, secret_hash, scopes | m2m-доступ для межсервисных вызовов |
| `outbox` | id, topic, aggregate_type, aggregate_id, event_type, event_version, payload, correlation_id, causation_id, traceparent, created_at, published_at | Частичный индекс `WHERE published_at IS NULL`. `id` служит `event_id` конверта: повторная публикация после сбоя несёт тот же идентификатор, и консьюмер видит дубль. `topic` хранится, а не выводится из агрегата, иначе версия топика оказывается зашитой в relay |

### Ключ user_roles

Тройка `(user_id, role, salon_id)` описывает идентичность записи, но первичным
ключом быть не может: у глобальной роли `salon_id` пуст, а колонка первичного
ключа в PostgreSQL обязана быть `NOT NULL`. Поэтому идентичность держат
суррогатный `id` и два частичных уникальных индекса:

```sql
CREATE UNIQUE INDEX uq_user_roles_user_id_role_global
    ON user_roles (user_id, role) WHERE salon_id IS NULL;
CREATE UNIQUE INDEX uq_user_roles_user_id_role_salon_id
    ON user_roles (user_id, role, salon_id) WHERE salon_id IS NOT NULL;
```

Гарантия та же — один и тот же грант дважды не выдать, — и при этом глобальная
роль и роль в салоне сосуществуют у одного пользователя.

## База catalog

| Таблица | Ключевые поля | Индексы и ограничения |
|---|---|---|
| `salons` | id, name, description, address, city, phone, **timezone** (IANA), slot_step_min, booking_min_lead_min, booking_horizon_days, cancel_deadline_min, is_active | Политики записи живут здесь, применяются в `booking` |
| `masters` | id, salon_id, user_id, display_name, bio, photo_url, specialization, is_active | `FK(salon_id)`, индекс `(salon_id, is_active)`, `UNIQUE(user_id, salon_id)` |
| `services` | id, salon_id, name, description, base_duration_min, base_price, currency, is_archived | `FK(salon_id)`, `CHECK(base_duration_min > 0)` |
| `master_services` | master_id, service_id, price_override, duration_override, is_active | `PK(master_id, service_id)`; итог = `COALESCE(override, base)` |
| `outbox` | — | Как в `auth` |

`user_id` в `masters` — ссылка на `auth.users` без внешнего ключа.

## База booking

| Таблица | Ключевые поля | Индексы и ограничения |
|---|---|---|
| `master_settings` | master_id (PK), salon_id, user_id, timezone, buffer_after_min, is_active | Создаётся по событию `master.created`. Собственный агрегат, не реплика профиля. `user_id` нужен, чтобы узнать мастера — владельца графика |
| `schedule_templates` | id, master_id, weekday 0–6, start_time, end_time, valid_from, valid_to | Локальное время салона. `CHECK(end_time > start_time)`, `valid_to` включительно, индекс `(master_id, weekday)` |
| `schedule_exceptions` | id, master_id, effective_on, kind, start_time, end_time, reason | `kind IN ('day_off','custom_hours','break')`; у `day_off` времени нет, у остальных оно обязательно; индекс `(master_id, effective_on)` |
| `bookings` | см. ниже | **Главный инвариант системы** |
| `outbox` | — | Как в `auth` |
| `processed_events` | event_id, consumer_group, processed_at | `PK(event_id, consumer_group)` — дедупликация консьюмера |
| `idempotency_keys` | user_id, key, request_hash, response_status, response_body, created_at, expires_at | `PK(user_id, key)`, TTL 24 ч. Просроченная строка удаляется при следующем обращении с тем же ключом: фоновой чистки пока нет |

### Таблица bookings

```sql
CREATE TABLE bookings (
    id                uuid PRIMARY KEY,
    salon_id          uuid NOT NULL,
    master_id         uuid NOT NULL,
    client_user_id    uuid NOT NULL,
    service_id        uuid NOT NULL,

    -- снимок услуги на момент создания: история не переписывается при смене прайса
    service_name      text           NOT NULL,
    price             numeric(10,2)  NOT NULL,
    currency          text           NOT NULL,
    duration_min      int            NOT NULL CHECK (duration_min > 0),
    buffer_min        int            NOT NULL DEFAULT 0 CHECK (buffer_min >= 0),
    -- политика салона на момент создания: клиент отменяет на тех условиях,
    -- на которых записывался, и отмене не нужен catalog
    cancel_deadline_min int          NOT NULL DEFAULT 240 CHECK (cancel_deadline_min >= 0),

    start_at          timestamptz NOT NULL,
    end_at            timestamptz NOT NULL,
    -- заполняется приложением; CHECK ниже не даёт ему разойтись со временем
    occupied_range    tstzrange NOT NULL,

    status            text NOT NULL,
    created_by        uuid NOT NULL,          -- кто создал: клиент или админ
    cancelled_by      uuid,
    cancel_reason     text,
    cancelled_at      timestamptz,

    reminder_at       timestamptz,            -- start_at − N часов
    reminder_sent_at  timestamptz,

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CHECK (end_at > start_at),
    CHECK (occupied_range = tstzrange(start_at,
                                      end_at + make_interval(mins => buffer_min),
                                      '[)')),
    CHECK (status IN ('pending','confirmed','completed','no_show',
                      'cancelled_by_client','cancelled_by_salon'))
);
```

### Главный инвариант системы

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- один мастер не может быть занят дважды;
-- отменённые брони время освобождают
ALTER TABLE bookings ADD CONSTRAINT ex_bookings_no_overlapping
EXCLUDE USING gist (
    master_id      WITH =,
    occupied_range WITH &&
) WHERE (status IN ('pending','confirmed','completed','no_show'));
```

Нарушение приходит как SQLSTATE `23P01` (`exclusion_violation`) и транслируется
в `409 slot_taken`.

### Почему occupied_range — не генерируемый столбец

Проверено на PostgreSQL 17 до написания логики (`test_exclusion_spike.py`).
Генерируемый столбец требует `IMMUTABLE`-выражения, а оператор
`timestamptz + interval` помечен `STABLE`: интервал в днях зависит от таймзоны.
PostgreSQL отвечает `generation expression is not immutable`.

Поэтому `occupied_range` — обычный столбец, который заполняет модель
SQLAlchemy. Разойтись со временем брони ему не даёт `CHECK` с тем же
выражением: проверочное ограничение может быть `STABLE`, и строка, где диапазон
не совпадает с `start_at`, `end_at` и `buffer_min`, отклоняется базой.
Арбитром остаётся база, а не дисциплина путей записи.

Тот же тест доказывает главное: две параллельные транзакции с пересекающимися
интервалами — вторая ждёт первую и после её коммита получает `23P01`;
отменённая бронь время освобождает.

### Остальные индексы bookings

```sql
CREATE INDEX ON bookings (master_id, start_at);
CREATE INDEX ON bookings (client_user_id, start_at DESC);
CREATE INDEX ON bookings (salon_id, start_at);
CREATE INDEX ON bookings (reminder_at)
    WHERE reminder_sent_at IS NULL
      AND status IN ('pending','confirmed');
```

Последний индекс обслуживает планировщик напоминаний: он сканирует таблицу
раз в минуту и должен читать только кандидатов.

## База notification

| Таблица | Ключевые поля | Индексы и ограничения |
|---|---|---|
| `recipients` | user_id (PK), email, phone, telegram_chat_id, email_confirmed, is_active, preferences, contacts_updated_at | Наполняется событиями `auth.users.v1`. Контакты могут отсутствовать: событие об аккаунте может обогнать описывающее его, а Telegram привязывается раньше обоих |
| `notifications` | id, user_id, event_id, topic, channel, template, payload, address, traceparent, correlation_id, status, attempts, last_error, next_attempt_at, created_at, updated_at, sent_at, dedup_key | `UNIQUE(dedup_key)` — второй раз то же уведомление не уйдёт; `channel IN ('email','telegram')`; `status IN ('pending','sending','sent','failed')`; частичный индекс `(next_attempt_at) WHERE status IN ('pending','sending')`, индекс `(user_id, created_at)` |
| `telegram_link_codes` | code_hash (PK), user_id, expires_at, used_at, created_at | Одноразовый код привязки Telegram-чата; хранится хэш, индекс `(user_id)` |
| `processed_events` | — | Как в `booking` |
| `outbox` | — | Как в `auth`; пишется только запись в DLQ |

`dedup_key` строится как `{event_id}:{channel}` — повторная доставка события
не приводит к повторной отправке.

`notifications` — одновременно журнал и очередь доставки. Консьюмер пишет строку
`pending` в той же транзакции, что и `processed_events`; воркер доставки забирает
её через `FOR UPDATE SKIP LOCKED`, переводит в `sending` с арендой
(`next_attempt_at` — конец аренды), отправляет вне транзакции и ставит `sent`
или `failed`; с `failed` в той же транзакции пишется запись в `<topic>.dlq`
исходного события, отсюда колонка `topic`. Строка, застрявшая в `sending` после падения пода, по истечении
аренды забирается снова: доставка на этом шаге at-least-once. `payload` хранит
поля для подстановки в шаблон, а не текст: адрес и текст определяются в момент
отправки. `traceparent` и `correlation_id` — контекст, в котором уведомление
поставлено в очередь: воркер отправляет в нём, и трасса идёт от запроса до
ухода сообщения.

`preferences` — матрица переключателей `{"bookings": {"email": true, "telegram": false},
"reminders": {...}}`; выключает только явный `false`, всё нечитаемое считается
включённым.

`contacts_updated_at` — момент снимка контактов, применённого последним.
Более старый снимок, пришедший позже, его не перезаписывает.

## Общие таблицы (шаблон в libs/common)

```sql
CREATE TABLE outbox (
    id             uuid PRIMARY KEY,
    topic          varchar(255) NOT NULL,
    aggregate_type text        NOT NULL,
    aggregate_id   uuid        NOT NULL,
    event_type     text        NOT NULL,
    event_version  int         NOT NULL,
    payload        jsonb       NOT NULL,
    correlation_id text,
    causation_id   uuid,
    created_at     timestamptz NOT NULL DEFAULT now(),
    published_at   timestamptz,
    traceparent    varchar(128)  -- W3C trace context записи; отдельной миграцией
);
CREATE INDEX ON outbox (created_at) WHERE published_at IS NULL;

CREATE TABLE processed_events (
    event_id       uuid        NOT NULL,
    consumer_group text        NOT NULL,
    processed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, consumer_group)
);
```

Обе таблицы разворачиваются миграцией `0001_shared_tables` в каждой из четырёх баз
(DDL — `barber_common.db.migrations`); `traceparent` добавляет отдельная ревизия
`*_outbox_traceparent`. DDL `idempotency_keys` тоже лежит в общей библиотеке,
но создаётся только ревизией `booking`.
