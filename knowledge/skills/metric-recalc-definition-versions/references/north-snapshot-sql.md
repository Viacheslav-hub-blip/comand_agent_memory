# Проверенный шаблон снимка состояния на дату T

Источник: `outputs/C02/snapshot_application_state.sql` (SQLite; конструкция CTE и оконных функций переносима в PostgreSQL). Использовать как основу для снимка «как было известно на T». Перед применением проверить соответствие своей схемы: имена таблиц и колонок (`applications`, `decision_events`, поля `application_id`, `event_id`, `status`, `occurred_at`, `loaded_at`, `is_test`) и наличие тай-брейка.

```sql
WITH
params AS (
    SELECT
        '2026-10-01 00:00:00' AS cohort_start_at,
        '2026-10-03 00:00:00' AS cohort_end_at,
        '2026-10-03 00:00:00' AS known_at
),

cohort AS (
    SELECT a.application_id, a.created_at, a.channel, a.is_test, a.requested_amount_rub
    FROM applications AS a
    CROSS JOIN params AS p
    WHERE a.created_at >= p.cohort_start_at
      AND a.created_at <  p.cohort_end_at
),

events_known_at_cutoff AS (
    SELECT d.application_id, d.event_id, d.status, d.occurred_at, d.loaded_at
    FROM decision_events AS d
    CROSS JOIN params AS p
    WHERE d.occurred_at < p.known_at
      AND d.loaded_at  < p.known_at
),

last_known_event AS (
    SELECT
        e.application_id, e.event_id, e.status, e.occurred_at, e.loaded_at,
        ROW_NUMBER() OVER (
            PARTITION BY e.application_id
            ORDER BY e.occurred_at DESC, e.event_id DESC
        ) AS recency_rank
    FROM events_known_at_cutoff AS e
),

state_at_cutoff AS (
    SELECT
        l.application_id,
        l.status      AS status_at_cutoff,
        l.event_id    AS last_event_id,
        l.occurred_at AS status_occurred_at,
        l.loaded_at   AS status_loaded_at
    FROM last_known_event AS l
    WHERE l.recency_rank = 1
)

SELECT
    c.application_id, c.created_at, c.channel, c.is_test, c.requested_amount_rub,
    s.status_at_cutoff, s.last_event_id, s.status_occurred_at, s.status_loaded_at
FROM cohort AS c
LEFT JOIN state_at_cutoff AS s
       ON s.application_id = c.application_id
ORDER BY c.application_id;
```

Ожидаемое поведение на учебном снимке (8 заявок, 13 событий): 8 строк, 8 уникальных `application_id`, `A006 manual_review` (поздняя загрузка), `A007 rejected` (событие 11 при равном `occurred_at`). Заявка без известных событий остаётся строкой со `status_at_cutoff IS NULL` — это состояние «неизвестно».

Метки фрода срезаются аналогично: `recorded_at < T`, затем `ROW_NUMBER() OVER (PARTITION BY application_id ORDER BY recorded_at DESC, label_id DESC)`, брать `rn = 1`.
