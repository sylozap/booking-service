# Проект и реализация: расхождения

Сверка 17.10.2026 (T9.8): документы `docs/` и ADR сопоставлены с кодом
сервисов, миграциями, `libs/common`, `deploy/` и CI. Источник истины — код;
документы исправлены в месте расхождения, здесь — сводка.

Формат пункта: **было в проекте → как реализовано → почему**. Пометки:

- **пересмотр** — решение сознательно изменено при реализации;
- **ошибка документа** — код делает так с самого начала, документ описывал иначе;
- **не реализовано** — в проекте было, в коде нет.

## Данные

| Было в проекте | Как реализовано | Почему | Пометка |
|---|---|---|---|
| `occupied_range` — генерируемый столбец | Обычный `tstzrange`, заполняется моделью; `CHECK occupied_range_matches_times` с тем же выражением | PostgreSQL 17: `timestamptz + interval` — `STABLE`, генерируемый столбец отклонён; проверено до логики (`3e349c7`) | пересмотр ([ADR-0002](adr/0002-exclude-constraint-as-arbiter.md)) |
| Снимок услуги в брони: имя, цена, валюта, длительность, буфер | Плюс `cancel_deadline_min` (миграция `0004`) | Отмена не ходит в `catalog` и следует условиям на момент записи (`2bc418c`) | пересмотр ([ADR-0013](adr/0013-service-snapshot-in-booking.md), дополнено) |
| Первичные ключи UUIDv7 | `uuid4` в приложении | В Python 3.12 нет `uuid7`; выигрыш в локальности индекса не измерялся | ошибка документа |
| `outbox` без топика и трейса | Колонки `topic` и `traceparent` (ревизии `*_outbox_traceparent`) | `topic` не выводится из агрегата, иначе версия топика зашита в relay; `traceparent` — один трейс от записи события до обработчика (`c4d2c58`) | ошибка документа (общий DDL) |
| `notifications`: id … sent_at | Плюс `topic`, `address`, `traceparent`, `correlation_id`, `updated_at`, индекс `(user_id, created_at)` | DLQ сдавшихся пишется в `<topic>.dlq` исходного события (`c3f48ef`); отправка продолжает трейс запроса (`eff772b`) | пересмотр |
| База `notification`: `recipients`, `notifications`, `processed_events` | Плюс `telegram_link_codes` и `outbox` | Привязка Telegram одноразовым кодом (`6b83d9d`); outbox — для `notification.delivery_failed` | ошибка документа |
| `idempotency_keys`: «чистка — периодической задачей» | Фоновой чистки нет: просроченная строка удаляется при следующем обращении с тем же ключом | В реализацию не вошло (`990aff7`) | не реализовано (долг) |

## API

| Было в проекте | Как реализовано | Почему | Пометка |
|---|---|---|---|
| `GET` / `PATCH /api/v1/auth/me` | Эндпоинтов нет | В план реализации не вошли: смены контактов нет ни в одном сценарии. Контракт `user.contacts_updated` и обработчик в `notification` готовы | не реализовано |
| Ссылка на CODING_STANDARDS.md из раздела о доступе к каталогу | Простой текст | Файл локальный и в git не входит | ошибка документа |

Остальное в [04-api-contracts.md](04-api-contracts.md) сошлось с роутерами,
схемами и кодами ошибок.

## События

| Было в проекте | Как реализовано | Почему | Пометка |
|---|---|---|---|
| `auth` публикует `user.contacts_updated`, `user.deactivated` | Не публикует: нет `/auth/me` и деактивации аккаунта | См. API | не реализовано |
| Консьюмер `booking` — один, на `master.deactivated` | Две группы: `booking.masters` (`master.created`/`updated`/`deactivated` → `master_settings`, каскад) и `booking.catalog` (`catalog.masters.v1` + `catalog.services.v1` → поколения кэша) | Настройки мастера и каскад не должны ждать Redis | ошибка документа |
| Письмо DLQ: `dlq_reason`, `dlq_traceback`, … | Плюс `dlq_detail`; причины `invalid_message` и `handler_failed` | — | ошибка документа |
| Топики создаются брокером | Только `scripts/create_topics.sh`, автосоздание выключено в `kind` (в compose включено) | Опечатка в имени должна быть ошибкой, а не новым топиком | ошибка документа |

## Кэш и консистентность

| Было в проекте | Как реализовано | Почему | Пометка |
|---|---|---|---|
| Ключ доступности удаляется событием `booking.*` по мастеру и дню | Ключ несёт поколения мастера и услуги; `INCR` счётчика после коммита в путях записи `booking` и в консьюмере `catalog.*`, сбрасываются все дни мастера | `service.updated` не несёт `master_id`; один `INCR` вместо поиска и `SCAN` (`2c42514`, `b874fa0`) | пересмотр ([ADR-0012](adr/0012-cache-is-not-arbiter.md)) |
| Redis недоступен — сервисы читают базу напрямую | Плюс предохранитель кэша и лимитера: 3 сбоя подряд → 5 с без Redis | Остановка Redis дала 56 % ошибок: каждая операция ждала таймаут (`a20682a`) | пересмотр |
| Ключи лимитера `rl:{scope}:{key}` | `ratelimit:{scope}:{subject}:{window}`, TTL два окна | — | ошибка документа |
| SQL доступности: `schedule_exceptions.date` | Колонка `effective_on` | — | ошибка документа |

## Инфраструктура

| Было в проекте | Как реализовано | Почему | Пометка |
|---|---|---|---|
| HPA у `api-gateway` и `booking` | И у `catalog`: 2–5 подов, в `kind` 2–3 | Без кэша весь поток доступности ложится на `catalog`; один под упирался в CPU и размыкал предохранитель `booking` (`7cf2dce`) | пересмотр |
| В `kind` по одной реплике | gateway, booking, catalog — минимум 2 | С одним подом удаление и выкатка — простой (`dde9fdc`) | пересмотр |
| Инфраструктура без персистентности | Лог Kafka в `kind` на PVC | `emptyDir` стирал топики и offsets при остановке брокера (`01e40fe`) | пересмотр |
| Tempo — значения по умолчанию | Блок 100 МБ / 5 мин, лимит 1 ГиБ | На 50 RPS Tempo падал по OOM 18 раз за 2 часа (`f0b648b`) | пересмотр |
| Ingress: TLS и маршрутизация на гейтвей | Traefik, HTTP без TLS | Локальный стенд; ingress-nginx закрыт | пересмотр ([ADR-0009](adr/0009-own-api-gateway.md)) |
| Гейтвей маршрутизирует «по первому сегменту пути» | Таблица шаблонов сверху вниз (`e440888`) | `/masters/*` делят `catalog` и `booking` | ошибка документа ([ADR-0009](adr/0009-own-api-gateway.md)) |
| CI: lint → types → test → build → publish | Плюс `fuzz` (schemathesis), `alerts` (promtool), `helm` (lint + kubeconform), `changes`; push в GHCR только с `main`, отдельного `publish` нет | — | ошибка документа |
| Bitnami-чарты | Свой chart `infra` | Bitnami заморозил образы (уточнение 3 журнала) | пересмотр, отмечен раньше |

## Наблюдаемость

| Было в проекте | Как реализовано | Почему | Пометка |
|---|---|---|---|
| Обзорный дашборд: HPA, пулы БД и Redis | Пул БД, рестарты, память, CPU, медленные маршруты; HPA и Redis нет | Метрик Redis сервисы не отдают; kube-state-metrics нет в compose, дашборд общий | ошибка документа |
| Список метрик | Плюс `cache_operations_total{operation,outcome}`, `jwks_cache_age_seconds` | — | ошибка документа |
| Алерты описаны условием | Имена правил и `severity` из `rules.yaml`; у `ConsumerLag` окно 5 мин | — | ошибка документа |

## Тесты

| Было в проекте | Как реализовано | Почему | Пометка |
|---|---|---|---|
| Результат экспериментов — «README этапа Э9» | [15-load-test-results.md](15-load-test-results.md), [16-failure-drills.md](16-failure-drills.md) | — | ошибка документа |
| Redis: `kubectl delete pod` | `kubectl scale deployment redis --replicas=0` на 4 минуты; с третьего прогона 0 ошибок | Удалённый под пересоздаётся за секунды; останов проверяет простой | пересмотр |
| Уровни тестов | Плюс `tests/e2e`, `tests/contract`, `tests/deploy`, `tests/seed`, `tests/docs` | — | ошибка документа |

## Решения / ADR

Пересмотрены 17.10.2026; исходный текст сохранён, добавлен раздел «Пересмотр»:

- [ADR-0002](adr/0002-exclude-constraint-as-arbiter.md) — `occupied_range` обычный столбец + `CHECK`.
- [ADR-0009](adr/0009-own-api-gateway.md) — маршрутизация по таблице шаблонов, Ingress без TLS, предохранители.
- [ADR-0012](adr/0012-cache-is-not-arbiter.md) — инвалидация поколениями, предохранитель к Redis.

Дополнено раньше: [ADR-0013](adr/0013-service-snapshot-in-booking.md) — `cancel_deadline_min` в снимке (23.09.2026).
Журнал решений получил уточнения 9–15 и пометки у пересмотренных пунктов —
[00-decisions-log.md](00-decisions-log.md); статусы этапов — [14-roadmap.md](14-roadmap.md).

## Мелкие правки по документам

- [01-context-and-scope.md](01-context-and-scope.md): напоминание — за 4 ч; email уходит в лог; `/auth/me` — вне скоупа.
- [03-services.md](03-services.md): гейтвей ходит в `notification`; `booking` читает оба топика `catalog`; структура репозитория.
- [07-events-and-kafka.md](07-events-and-kafka.md): группы консьюмеров в таблице топиков; `currency` и `reminder_at` в примере `booking.created`.
- [08-consistency.md](08-consistency.md): `topic` и `aggregate_type = "bookings"` в псевдокоде outbox.
- [09-scenarios.md](09-scenarios.md): доставка уведомления через очередь `notifications`; кэш перед вызовом `catalog`.
- [13-risks.md](13-risks.md): сбывшиеся риски (генерируемый столбец, Redis); смена таймзоны салона доходит сразу.
- [14-roadmap.md](14-roadmap.md), [04-api-contracts.md](04-api-contracts.md): ссылки на локальные IMPLEMENTATION_PLAN.md и CODING_STANDARDS.md заменены текстом.
