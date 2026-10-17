# Наблюдаемость

Метрики, трейсы и логи сходятся в одной Grafana и связаны через `trace_id`
и `correlation_id` — от нажатия кнопки клиентом до сообщения в Telegram.

## Метрики

Стек — `kube-prometheus-stack` (Prometheus + Grafana + Alertmanager + kube-метрики).
Сервисы отдают `/metrics` через `prometheus-fastapi-instrumentator`,
сбор по `ServiceMonitor`.

| Метрика | Тип | Зачем |
|---|---|---|
| RED по HTTP: rate, errors, duration | counter, histogram | Базовое здоровье каждого сервиса, источник SLO |
| `bookings_created_total{salon,status}` | counter | Бизнес-пульс системы |
| `bookings_closed_total{salon,status}` | counter | Чем заканчиваются записи: `completed`, `no_show`, `cancelled_by_client`, `cancelled_by_salon`. Считается после коммита — повторённый обработчик каскадной отмены не удваивает счёт |
| `bookings_upcoming_seconds{salon}` | gauge | Загрузка мастеров: забронированное время открытых записей на 7 дней вперёд. Абсолютное, без доли от рабочих часов — доля требовала бы на каждом проходе считать график каждого мастера. Пустеющая неделя — салон теряет клиентов |
| `booking_conflicts_total{salon}` | counter | Проигранные гонки за слот — прямой индикатор устаревания кэша доступности |
| `availability_query_duration_seconds{source}` | histogram | Главный тяжёлый запрос; следим за p95. Лейбл `source` = `database` или `cache`: без него p95 смешивает попадания в кэш с чтением базы и прячет сам запрос |
| `outbox_pending_messages`, `outbox_publish_lag_seconds` | gauge | Расхождение базы и Kafka. Растёт — relay встал. Замеряется отдельным циклом relay раз в 5 с, а не проходом публикации: при пропавшем брокере клиент Kafka висит в отправке без дедлайна, и gauge прохода застыл бы на последнем значении, обычно нуле |
| `kafka_consumer_lag`, `events_processed_total{topic,result}` | gauge, counter | Здоровье асинхронного контура |
| `event_retries_total{topic,event_type}` | counter | Повторные попытки обработчика (первая попытка не в счёт). Ретраи при тихой DLQ — зависимость сбоит время от времени; ретраи с последующей DLQ — она лежит |
| `dlq_messages_total{topic,reason}` | counter | Любой рост — инцидент. Серии по топикам консьюмера заводятся нулём при его создании: `increase()` не видит рост серии, родившейся сразу единицей, и первое сообщение в DLQ прошло бы мимо алерта |
| `notifications_sent_total{channel,result}` | counter | Доставляемость уведомлений: одна попытка — одно увеличение, `result` — `sent`, `temporary_failure`, `permanent_failure` |
| `notification_send_duration_seconds{channel}` | histogram | Провайдер замедлился раньше, чем начал падать по таймауту |
| `notifications_pending_messages`, `notifications_oldest_due_age_seconds` | gauge | Очередь доставки с двух сторон, как у outbox: число неотправленных и возраст самого старого созревшего (ретрай на будущее не считается опоздавшим) |
| `reminder_scheduler_last_run_timestamp` | gauge | Тихо умерший планировщик иначе не заметен |
| `cache_operations_total{operation,outcome}` | counter | Чтения и записи кэша Redis по исходу: доля промахов и ошибок |
| `jwks_cache_age_seconds` | gauge | Время с последней удачной загрузки JWKS: растёт — сервис не видит новых ключей подписи |
| `db_pool_usage_ratio{database}` | gauge | Доля занятых соединений от `pool_size + max_overflow`. Минутами около 1 — запросы ждут соединение и скоро упадут по таймауту пула: увеличить пул или искать, кто держит соединение (сетевой вызов внутри транзакции — первый подозреваемый) |
| `process_start_time_seconds`, `process_resident_memory_bytes`, `process_cpu_seconds_total` | gauge, counter | Рестарты (`changes()` по времени старта — одинаково в compose и в кластере), утечка памяти, упор в CPU |

## Трассировка

OpenTelemetry с автоинструментацией FastAPI, httpx, SQLAlchemy, aiokafka и Redis;
экспорт OTLP в Tempo.

Trace-контекст протаскивается через заголовки Kafka-сообщений (`traceparent`),
поэтому трейс не рвётся на границе брокера: видно путь
`HTTP-запрос → транзакция → outbox → relay → консьюмер → отправка уведомления`.

Асинхронных границ на этом пути две — outbox и брокер, — и контекст переносится
через обе:

```
трейс запроса: POST /bookings → INSERT → create booking.bookings.v1 ─┐ traceparent в outbox
                                                                     └→ consume → INSERT notifications → send
трейс relay:   publish booking.bookings.v1 ──link──> create
```

- Запись события в outbox открывает span `create {topic}` в трейсе того, кто пишет,
  и сохраняет его `traceparent` в строке.
- Relay публикует позже и вне запроса, поэтому его span `publish {topic}` — корень
  своего трейса со **ссылкой** (`link`) на сохранённый контекст, а не потомок.
  В заголовки сообщения уходит сохранённый контекст, а не контекст публикации.
- Консьюмер продолжает трейс из заголовка: `consume {topic}` — потомок `create`.
  Событие, записанное обработчиком, получает `causation_id` = `event_id`
  обрабатываемого события (из `contextvars`, как `correlation_id`) и остаётся
  в том же трейсе.

Так соответствует OpenTelemetry messaging semantic conventions: контекст создания
сообщения передаётся получателю, отправка на него ссылается.

SQL-span'ы без родителя не экспортируются: relay и воркер доставки опрашивают
свои таблицы каждую секунду, и каждый опрос иначе становился бы отдельным трейсом.
Запрос к базе внутри HTTP-запроса или обработки сообщения свой span сохраняет.

## Логи

`structlog`, JSON в stdout, сбор Grafana Alloy → Loki.

Обязательные поля каждой записи: `service`, `env`, `trace_id`, `span_id`,
`correlation_id`, `user_id`. Из трейса в Tempo можно перейти в логи Loki
по `trace_id` в один клик.

Что логируется на уровне INFO: создание и изменение брони, отправка уведомления,
публикация и потребление события, попадание в DLQ, срабатывание rate limit.
Пароли, токены и хеши в логи не попадают никогда.

## Сквозной correlation-id

Гейтвей генерирует `X-Correlation-Id` (или принимает от клиента), кладёт
в `contextvars`. ASGI-мидлварь прокидывает его во все исходящие HTTP-вызовы,
в конверт события и в заголовки Kafka; консьюмер восстанавливает контекст
из сообщения. Один идентификатор проходит через синхронные вызовы, брокер
и отправку уведомления.

## Дашборды

Три дашборда, JSON в репозитории, provisioning в Grafana:

**Обзор системы** (`overview.json`) — RED по каждому сервису, коды ответов,
p50/p95/p99, самые медленные маршруты, рестарты, память, CPU, пул БД.
Панелей HPA и Redis нет: метрик Redis сервисы не отдают (только
`cache_operations_total`), kube-state-metrics в compose нет, а дашборд общий.

**Бизнес** (`business.json`) — записи созданы / отменены / no-show, конфликты слотов,
загрузка мастеров, доставка уведомлений по каналам.

**Асинхронка** (`async.json`) — лаг консьюмеров по группам, глубина и возраст outbox,
DLQ и ретраи, последний запуск планировщика.

## Алерты

| Алерт (`rules.yaml`) | Условие | `severity` |
|---|---|---|
| `ServiceDown` | `up == 0` более 1 мин | critical |
| `HighErrorRate` | 5xx > 5 % по сервису в течение 5 мин | critical |
| `SlowAvailability` | p95 `availability_query_duration_seconds` > 1 с в течение 10 мин | warning |
| `ConsumerLag` | сумма `kafka_consumer_lag` по группе > 1000 в течение 5 мин | warning |
| `OutboxNotDraining` | `outbox_publish_lag_seconds` > 30 с в течение 4 мин. Не `pending > 0`: под постоянным потоком скрейп почти всегда застаёт свежую запись, а у живого relay самое старое событие моложе секунды | critical |
| `DeadLetters` | `increase(dlq_messages_total[5m]) > 0` | critical |
| `NotificationDeliveryStalled` | `notifications_oldest_due_age_seconds` > 300 | critical |
| `ReminderSchedulerSilent` | `time() - reminder_scheduler_last_run_timestamp > 300` и процесс жив дольше 5 мин — до первого прохода свежий процесс отдаёт ноль | critical |
| `AuthCleanupSilent` | `time() - auth_cleanup_last_success_timestamp > 3 ч` (интервал — 1 ч), с той же оговоркой про старт процесса | warning |

Правила — `deploy/observability/alerts/rules.yaml`, у каждого `severity`
(`critical` — событие принято и не обрабатывается или сервис пропал,
`warning` — деградация без потерь), `summary`, `description` с действием и
ссылка на дашборд. Юнит-тесты правил — `rules.test.yaml`, `make alerts-check`
и CI. Доставка — Alertmanager: группировка по алерту и сервису, `ServiceDown`
подавляет остальные алерты своего сервиса; локально получатель без
интеграции, алерты видны в UI Alertmanager и в Grafana. Telegram — блок
`telegram_configs` и токен бота в файле, когда появится дежурный.


## Фоновая чистка auth

| Метрика | Тип | Решение, которое она обслуживает |
|---|---|---|
| `auth_cleanup_last_success_timestamp` | gauge | Перестала расти — задача встала, таблицы растут, нужен человек |
| `auth_cleanup_deleted_rows_total{table}` | counter | Отвечает, удаляется ли хоть что-то: отличает «задача выполняется» от «задача работает» |

Лейбл — имя таблицы (их три, и стареют они по-разному). Идентификаторов
в лейблах нет.

Чистка удаляет строки **по истечению срока, а не по состоянию**. Отозванный,
но ещё не истёкший refresh-токен — это ровно то, на чём держится детекция
кражи: повторное предъявление такого токена гасит family. Удалив его
раньше времени, мы превратили бы кражу в обычный `401` и оставили бы живой
ту копию family, которой владеет укравший. То же с использованным
подтверждением email: пока строка есть, повторный клик по ссылке — это
«токен уже использован», а не «такого токена не было».

## SLO

| Показатель | Цель |
|---|---|
| Доля успешных запросов | 99 % за 30 дней |
| p95 создания брони | < 500 мс |
| p95 запроса доступности | < 1 с |
