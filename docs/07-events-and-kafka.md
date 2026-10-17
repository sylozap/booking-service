# События и Kafka

## Топики

| Топик | Продюсер | Типы событий | Консьюмеры |
|---|---|---|---|
| `auth.users.v1` | auth | `user.registered`, `user.email_confirmation_requested`, `user.email_confirmed`; `user.contacts_updated`, `user.deactivated` — контракт и обработчик есть, `auth` их пока не публикует (нет `/auth/me`) | notification (`notification.users`) |
| `catalog.masters.v1` | catalog | `master.created`, `master.updated`, `master.deactivated` | booking: `booking.masters` (настройки мастера, каскад отмены) и `booking.catalog` (кэш) |
| `catalog.services.v1` | catalog | `service.created`, `service.updated`, `service.archived` | booking: `booking.catalog` (инвалидация кэша) |
| `booking.bookings.v1` | booking | `booking.created`, `booking.rescheduled`, `booking.cancelled`, `booking.completed`, `booking.no_show` | notification (`notification.bookings`) |
| `booking.reminders.v1` | booking | `reminder.due` | notification (`notification.bookings`) |
| `<topic>.dlq` | все | Необработанные сообщения; от `notification` — ещё `notification.delivery_failed` | Ручной разбор |

**Гранулярность — топик на агрегат.** Тип события лежит в конверте и в заголовке
сообщения. Это сохраняет порядок событий одной сущности: иначе `booking.cancelled`
может обогнать `booking.created`.

**Ключ партиционирования** — id агрегата (`booking_id`, `user_id`, `master_id`).
3 партиции на топик, RF=1 на локальном стенде. Топики и их `.dlq` создаёт только
`scripts/create_topics.sh` (идемпотентно, число партиций доводит до `PARTITIONS`);
автосоздание у брокера выключено, чтобы опечатка в имени была ошибкой, а не
топиком, который никто не читает.

У `booking` две группы на `catalog.masters.v1`: настройки мастера и каскад отмены
не должны ждать Redis, которым занят консьюмер кэша.

## Конверт события

```json
{
  "event_id":       "0192f3c1-...",         // UUID, ключ дедупликации
  "event_type":     "booking.created",
  "event_version":  1,
  "occurred_at":    "2026-09-02T14:31:00Z", // всегда UTC
  "correlation_id": "c-8f2a...",            // сквозной, от гейтвея
  "causation_id":   "0192f3b0-...",         // event_id причины, если есть
  "producer":       "booking@1.4.0",
  "payload":        { }
}
```

Схемы конверта и всех `payload` — Pydantic-модели в `libs/common/events`.
Один источник правды для продюсера и консьюмера.

Заголовки Kafka-сообщения дублируют `event_type`, `correlation_id` и несут
W3C trace context (`traceparent`), чтобы трейс не рвался на границе брокера.
Это контекст момента записи события в outbox (колонка `outbox.traceparent`),
а не момента публикации: relay со своим span'ом стоит рядом с трейсом запроса
и ссылается на него — см. [11-observability.md](11-observability.md#трассировка).

`causation_id` обработчику передавать не нужно: событие, записанное в outbox
во время обработки другого, получает его `event_id` автоматически. Явный аргумент
важнее — так `notification.delivery_failed` ссылается на исходное событие.

## Примеры payload

```json
// booking.created — created_by отличается от client_user_id,
// когда клиента записал администратор; timezone — зона салона, есть во всех
// событиях booking.* (null, если у booking нет настроек мастера)
{ "booking_id": "...", "salon_id": "...", "master_id": "...",
  "client_user_id": "...", "service_id": "...", "service_name": "Мужская стрижка",
  "start_at": "2026-09-05T12:00:00Z", "end_at": "2026-09-05T12:45:00Z",
  "price": "3500.00", "currency": "RUB", "status": "confirmed",
  "reminder_at": "2026-09-05T08:00:00Z", "created_by": "...",
  "timezone": "Europe/Moscow" }

// booking.cancelled — cancelled_by: сторона ("client" | "salon"), не человек
{ "booking_id": "...", "salon_id": "...", "master_id": "...",
  "client_user_id": "...", "service_name": "Мужская стрижка",
  "start_at": "...", "end_at": "...", "cancelled_by": "salon",
  "reason": "master_deactivated" }

// booking.rescheduled — оба времени, чтобы сказать «перенесено с ... на ...»
{ "booking_id": "...", "salon_id": "...", "master_id": "...",
  "client_user_id": "...", "service_name": "Мужская стрижка",
  "previous_start_at": "...", "previous_end_at": "...",
  "start_at": "...", "end_at": "...", "reminder_at": "..." }

// booking.completed и booking.no_show — одинаковой формы
{ "booking_id": "...", "salon_id": "...", "master_id": "...",
  "client_user_id": "...", "service_name": "Мужская стрижка",
  "start_at": "...", "end_at": "..." }

// reminder.due — всё, что нужно тексту напоминания; timezone — зона салона
// из master_settings (null, если настроек мастера нет)
{ "booking_id": "...", "salon_id": "...", "master_id": "...",
  "client_user_id": "...", "service_name": "Мужская стрижка",
  "start_at": "...", "end_at": "...", "hours_before": 4,
  "timezone": "Europe/Moscow" }

// master.created
{ "master_id": "...", "salon_id": "...", "user_id": "...",
  "display_name": "Иван", "timezone": "Europe/Moscow", "is_active": true }

// master.updated — снимок целиком, а не набор изменившихся полей;
// timezone — таймзона салона
{ "master_id": "...", "salon_id": "...", "user_id": "...",
  "display_name": "Иван П.", "specialization": "Барбер", "is_active": true,
  "timezone": "Europe/Moscow" }

// master.deactivated — только идентичность: каскаду нужно знать «кто»
{ "master_id": "...", "salon_id": "..." }

// service.created и service.updated — одинаковой формы намеренно
{ "service_id": "...", "salon_id": "...", "name": "Мужская стрижка",
  "base_duration_min": 45, "base_price": "3500.00", "currency": "RUB" }

// service.archived
{ "service_id": "...", "salon_id": "..." }

// user.registered
{ "user_id": "...", "email": "...", "phone": "...", "email_confirmed": false }

// user.email_confirmation_requested
{ "user_id": "...", "email": "...", "token": "...",
  "expires_at": "2026-09-06T14:31:00Z" }
```

**О снимках в событиях `catalog`.** Payload `master.updated`, `service.created`
и `service.updated` — состояние сущности целиком, а не дельта. Причины две.
Потребитель, применяющий снимок, идемпотентен бесплатно — доставка at-least-once
([ADR-0007](adr/0007-at-least-once-with-dedup.md)), и повторное событие оставляет
то же состояние. И потребитель, подключившийся позже или перечитывающий топик
с начала, приходит в верное состояние из одного любого события, а не из всей
цепочки предшествующих.

Цену и длительность мастера события **не несут**. Итог считается как
`COALESCE(override, base)`, и единственное место, где он считается, — внутренний
эндпоинт `catalog` (`/internal/v1/masters/{id}/services/{sid}`). Прайс-лист внутри события дал бы платформе второй
ответ на этот вопрос.

У салона своего топика нет. Единственное его изменение, которое нужно другому
сервису, — таймзона: в ней записан график каждого мастера. Поэтому смена
таймзоны в `PATCH` салона публикует в той же транзакции `master.updated`
на каждого мастера салона.

`PUT` и `DELETE` привязки мастер × услуга публикуют `master.updated` с ключом
`master_id`. Привязка не является отдельным агрегатом, но снаружи «что мастер
оказывает» — часть мастера, и ровно это отдаёт внутренний эндпоинт. Без события
здесь потребитель, кэширующий этот ответ (`booking`), продолжал бы отдавать цену,
которую с тех пор переопределили.

Правка, ничего не изменившая, события не порождает: форма, сохранённая без
редактирования, отправляет тело целиком, и превращать каждое такое нажатие
в событие — значит заставлять всех потребителей его обрабатывать.

**Об одноразовом токене в payload.** `user.email_confirmation_requested` несёт сам
токен: получатель события — тот, кто отправляет письмо, и ссылка целиком состоит
из токена. Риск ограничен тремя способами: топик внутренний и наружу не публикуется,
TTL токена — 24 часа, в базе `auth` хранится только его SHA-256. В логи значение
не попадает ни на одном шаге; единственное место, где токен виден в открытом
виде, — тело письма. Письмо отправляет `notification`: событие потребляет группа
`notification.users` и ставит письмо в очередь на адрес из события, без учёта
предпочтений. Лог-провайдер печатает текст письма только в `local` и `test`;
после отправки поля шаблона (со ссылкой) из журнала `notifications` стираются.

## Доставка в notification

`notification` — единственный потребитель, чей эффект уходит во внешний мир,
поэтому обработка разделена на два шага:

1. **Консьюмер** (`notification.users`, `notification.bookings`) в транзакции
   раннера только пишет строки `notifications` со статусом `pending` —
   вместе с `processed_events`. Сети в транзакции нет.
2. **Воркер доставки** раз в секунду забирает созревшие строки через
   `FOR UPDATE SKIP LOCKED`, ставит `sending` с арендой, отправляет вне
   транзакции и записывает `sent`, повтор позже или `failed`.

Повторная доставка события отсекается дважды: `processed_events` и
`UNIQUE(dedup_key)`, где `dedup_key = {event_id}:{channel}`. Падение пода между
отправкой и записью результата приводит к повторной отправке после истечения
аренды — это единственный дубль, который допускает at-least-once, — но не к
потере. Временная ошибка (таймаут, `5xx`, `429` с `retry_after`) даёт повтор
с удваивающейся паузой, постоянная (бот заблокирован, чата нет) — `failed`
сразу; заблокированный бот ещё и отвязывает чат. Партиция Kafka отправкой
не блокируется вообще.

Строка `notifications` хранит `traceparent` и `correlation_id` события, из-за
которого появилась. Воркер отправляет в этом контексте: span `send {channel}` —
потомок консьюмера, каждая попытка — отдельный span того же трейса, а логи
отправки несут `correlation_id` исходного запроса.

**Сдавшиеся уведомления.** Строка, ушедшая в `failed` (попытки исчерпаны или
ошибка постоянная), в той же транзакции пишет в outbox событие
`notification.delivery_failed` в `<topic>.dlq` того события, из-за которого
появилась: топик хранится в `notifications.topic`. Через outbox, а не продюсером
напрямую: здесь есть изменение состояния, с которым запись должна быть атомарна, —
падение пода между отметкой `failed` и публикацией иначе потеряло бы её, а строку
`failed` воркер больше не берёт.

```json
// notification.delivery_failed — ключ: id уведомления;
// causation_id конверта = original_event_id
{ "notification_id": "...", "original_event_id": "...", "user_id": "...",
  "channel": "telegram", "template": "booking_created",
  "dlq_reason": "retries_exhausted",   // или "permanent_failure"
  "dlq_detail": "telegram: 502", "dlq_attempts": 3,
  "dlq_original_topic": "booking.bookings.v1", "dlq_at": "..." }
```

Исходный конверт не сохраняется — relay оборачивает запись в свой, — поэтому
поля `dlq_*` названы так же, как у письма раннера, а исходное событие находится
по `original_event_id`. Поля шаблона в запись не попадают: в письме подтверждения
там одноразовый токен. `dlq_messages_total{topic,reason}` растёт после коммита.

Сообщения уходят на `booking.created`, `booking.rescheduled`, `booking.cancelled`
(два шаблона: отмена клиентом и салоном) и на `reminder.due` — его читает та же
группа `notification.bookings`, подписанная на оба топика. Канал выбирается
по предпочтениям получателя для типа уведомления: `bookings` или `reminders`. `booking.completed` и
`booking.no_show` сообщения не дают: обработчика нет, раннер логирует и коммитит.

## Версионирование схем

Schema Registry не используется. Правила совместимости держатся дисциплиной:

- **Совместимое изменение** (добавление необязательного поля) — увеличивается
  `event_version`, топик не меняется. Консьюмер игнорирует незнакомые поля.
- **Ломающее изменение** (удаление или переименование поля, смена типа) — новый топик
  `<agg>.v2`, период двойной публикации, миграция консьюмеров, затем отключение `.v1`.
- Консьюмер **никогда** не падает на незнакомом `event_type` — логирует и коммитит.

## Гарантии доставки

**At-least-once.** Ручной коммит оффсета строго после успешной обработки.
Дедупликация — таблица `processed_events (event_id, consumer_group)`; запись в неё
и бизнес-эффект происходят в одной транзакции. Это даёт эффективный exactly-once
на стороне эффекта без Kafka-транзакций.

```python
# псевдокод обработчика
async with session.begin():
    if await already_processed(event.event_id, group):
        return  # дубль — молча пропускаем
    await handle(event)  # бизнес-эффект
    await mark_processed(event.event_id, group)
await consumer.commit()  # только после коммита транзакции
```

## Ретраи и DLQ

| Класс ошибки | Поведение |
|---|---|
| Невалидное сообщение: не разбирается схемой, нет обязательных полей | Сразу в `<topic>.dlq`, без ретраев — повтор ничего не изменит |
| Временная ошибка: недоступна БД, Telegram вернул 5xx | 3 попытки с экспоненциальной задержкой и джиттером, затем DLQ с причиной и трейсбеком |

Оффсет коммитится в обоих случаях: «ядовитое» сообщение не должно останавливать
партицию. Появление сообщений в DLQ — повод для алерта, а не для тихой потери.

Сообщение в DLQ сохраняет исходный конверт и добавляет:
`dlq_reason` (`invalid_message` или `handler_failed`), `dlq_detail`,
`dlq_traceback`, `dlq_attempts`, `dlq_original_topic`, `dlq_at`. Неразбираемое
тело сохраняется как текст. Задержки ретраев: 0.2 → 0.4 с (×2, не больше 2 с, джиттер 20 %).

## Отложенные напоминания

Kafka не умеет отложенную доставку, и имитировать её спящим консьюмером нельзя:
он заблокирует партицию и сломает лаг-метрики. Вместо этого — планировщик в `booking`:

1. При создании и при каждом переносе брони считается
   `reminder_at = start_at − N часов`.
2. Воркер раз в минуту выбирает брони с наступившим `reminder_at`,
   непроставленным `reminder_sent_at` и активным статусом.
3. В одной транзакции: пишет `reminder.due` в outbox и проставляет `reminder_sent_at`.

Напоминание, опоздавшее к началу визита (планировщик лежал), ничего клиенту
не сообщает: событие не пишется, `reminder_sent_at` проставляется всё равно,
чтобы бронь перестала быть кандидатом, — в лог уходит `skipped`. Полная пачка
сразу сменяется следующей, иначе следующий проход через минуту
(`REMINDER_SCHEDULER_INTERVAL_SECONDS`).

Состояние живёт в базе, поэтому рестарт пода ничего не теряет, а отменённая бронь
напоминание не получит.

**Принятая гонка.** Отмена, пришедшая после того, как `reminder.due` уже записан
в outbox, напоминание не отзывает: `notification` не связывает уведомления
с бронью и отправит его. Окно — от прохода планировщика до доставки, секунды;
закрывать его хранением `booking_id` в журнале и отзывом по `booking.cancelled`
дорого для такого случая. См. [ADR-0011](adr/0011-reminder-scheduler-in-db.md).
