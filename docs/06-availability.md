# Расчёт доступности

Самая нагруженная и самая содержательная часть системы. Считается на лету одним SQL-запросом
— никаких материализованных слотов и никакой проекции, которая может отстать.

## Алгоритм

1. **Вход:** `master_id`, `service_id`, диапазон дат. Без `service_id` запрос
   бессмысленен — длительность зависит от пары «мастер × услуга».
2. **Из `catalog`** (`GET /internal/v1/masters/{id}/services/{id}`, кэш Redis
   `booking:offering:…`, TTL 5 мин, сбрасывается поколениями): длительность, цена, таймзона салона,
   шаг сетки, минимальный отступ, горизонт.
3. **Из собственной базы:** буфер мастера, шаблон на нужные дни недели,
   исключения на конкретные даты.
4. Локальные интервалы работы разворачиваются в `timestamptz` через таймзону салона;
   исключение на дату полностью перекрывает шаблон.
5. Свободное время = рабочие интервалы минус `occupied_range` активных броней.
6. Нарезка по сетке. Слот годен, если **услуга помещается в рабочий интервал**
   и **услуга вместе с буфером не пересекает чужую занятость**.
7. Отсечение по отступу (`now + booking_min_lead_min`) и горизонту.

## Запрос

```sql
WITH params AS (
    SELECT :master_id::uuid AS master_id, :day::date AS day, :tz::text AS tz,
           :duration_min::int AS dur, :buffer_min::int AS buf,
           :step_min::int AS step, :lead_min::int AS lead
),
work AS (  -- исключение на дату перекрывает недельный шаблон
    SELECT tstzrange((p.day + e.start_time) AT TIME ZONE p.tz,
                     (p.day + e.end_time)   AT TIME ZONE p.tz, '[)') AS r
    FROM params p
    JOIN schedule_exceptions e ON e.master_id = p.master_id
                              AND e.effective_on = p.day
                              AND e.kind = 'custom_hours'
    UNION ALL
    SELECT tstzrange((p.day + t.start_time) AT TIME ZONE p.tz,
                     (p.day + t.end_time)   AT TIME ZONE p.tz, '[)')
    FROM params p
    JOIN schedule_templates t ON t.master_id = p.master_id
                             AND t.weekday = EXTRACT(isodow FROM p.day) - 1
                             AND p.day BETWEEN t.valid_from
                                       AND COALESCE(t.valid_to, 'infinity'::date)
    WHERE NOT EXISTS (SELECT 1 FROM schedule_exceptions x
                      WHERE x.master_id = p.master_id AND x.effective_on = p.day)
),
grid AS (
    SELECT w.r,
           generate_series(lower(w.r),
                           upper(w.r) - make_interval(mins => p.dur),
                           make_interval(mins => p.step)) AS slot
    FROM work w, params p
)
SELECT g.slot
FROM grid g, params p
WHERE tstzrange(g.slot, g.slot + make_interval(mins => p.dur), '[)') <@ g.r
  AND g.slot >= now() + make_interval(mins => p.lead)
  AND NOT EXISTS (
        SELECT 1 FROM bookings b
        WHERE b.master_id = p.master_id
          AND b.status IN ('pending','confirmed','completed','no_show')
          AND b.occupied_range &&
              tstzrange(g.slot,
                        g.slot + make_interval(mins => p.dur + p.buf), '[)'))
ORDER BY g.slot;
```

Реализованный запрос отличается от приведённого выше в трёх местах: он считает
сразу диапазон дат (дни из `generate_series`), вычитает перерывы
мультидиапазонами (`range_agg(work) - range_agg(breaks)`, затем `unnest`),
а `now` приходит параметром (`not_before`, `not_after`) — иначе тест на отступ
недетерминирован. Соприкасающиеся интервалы `range_agg` склеивает в один,
и домен делает то же самое.

> **Исправление к запросу выше.** Условие `NOT EXISTS (любое исключение на дату)`
> у шаблона верно только для `day_off` и `custom_hours`. Перерыв (`kind = 'break'`)
> шаблон не заменяет, а **вычитается** из рабочих интервалов разностью диапазонов,
> и сетка каждого получившегося куска отсчитывается от его начала — так же, как
> в эталоне `domain/slots.py`. Из версий шаблона на дату берётся та, у которой
> `valid_from` наибольший. Реализованный запрос
> (`repositories/availability.py`) написан под эти правила.

## Три решения, которые стоит помнить

- **Буфер не обязан помещаться в рабочий день.** Уборка после последнего клиента
  может выходить за конец смены — буфер участвует только в проверке пересечений.
- **Сетка отсчитывается от начала рабочего интервала.** При типичном старте в `:00`
  это даёт круглые слоты; если понадобится жёсткая привязка к стенным часам,
  старт округляется вверх до кратного шага от начала суток салона.
- **Выдача — не резервация.** Между запросом доступности и созданием брони слот
  может уйти. Единственная защита — ограничение базы; клиент получает `409`.

## Кэширование

Ключ `booking:avail:{master_id}:{service_id}:{date}:{gm}.{gs}` с поколениями мастера и услуги, TTL 60 секунд
(`AVAILABILITY_CACHE_TTL_SECONDS`). Поколение — счётчик `booking:generation:master:{id}` /
`booking:generation:service:{id}`; `INCR` делает недостижимыми все ключи мастера сразу,
без перебора. Счётчик мастера увеличивают сами пути записи `booking` после коммита
(создание, отмена, перенос, график, настройки мастера) и консьюмер `catalog.*` —
не события `booking.*` и не по дню: перечислять ключи по дням дороже, чем
пересчитать сутки мастера. Ответ помечен как оценка. Кэш **не является источником истины** — см. [08-consistency.md](08-consistency.md).

## Производительность

Целевой p95 — менее 1 секунды на запрос по одному дню.
Узкие места и что с ними делать:

| Симптом | Причина | Действие |
|---|---|---|
| Медленный `NOT EXISTS` | Нет gist-индекса по `occupied_range` | Ограничение `EXCLUDE` уже создаёт нужный индекс |
| Много строк в `grid` | Запрошен широкий диапазон дат | Ограничить `date_to − date_from` 14 днями на уровне API |
| Постоянные попадания в БД | Кэш не работает | Проверить инвалидацию: она не должна срабатывать чаще, чем меняются брони |
