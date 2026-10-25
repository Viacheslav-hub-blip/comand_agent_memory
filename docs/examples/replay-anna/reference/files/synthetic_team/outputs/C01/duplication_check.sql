-- C01b: проверка размножения заявок при соединении с историей решений.
-- Вопрос: сколько строк порождает JOIN applications x decision_events.
-- Диалект: SQLite.

-- 1. Число событий на заявку в исходной истории
SELECT application_id,
       COUNT(*)                     AS events_total,
       SUM(status = 'approved')     AS approved_events,
       COUNT(DISTINCT status)       AS distinct_statuses
FROM decision_events
GROUP BY application_id
ORDER BY application_id;

-- 2. Размножение на ПОЛНОМ соединении (как в старом запросе): строк на заявку
SELECT a.application_id,
       COUNT(*) AS joined_rows
FROM applications a
JOIN decision_events d ON d.application_id = a.application_id
GROUP BY a.application_id
ORDER BY a.application_id;

-- 3. Только approved-строки: сколько заявок дают >1 строки
SELECT a.application_id, COUNT(*) AS approved_rows, SUM(a.requested_amount_rub) AS summed_amount
FROM applications a
JOIN decision_events d ON d.application_id = a.application_id
WHERE d.status = 'approved'
GROUP BY a.application_id
HAVING COUNT(*) > 1;

-- 4. Сводка: сколько заявок размножилось (approved >1) и насколько завышена сумма
WITH approved_rows AS (
    SELECT a.application_id, COUNT(*) AS n, a.requested_amount_rub AS amt
    FROM applications a
    JOIN decision_events d ON d.application_id = a.application_id
    WHERE a.created_at >= '2026-10-01 00:00:00'
      AND a.created_at <  '2026-10-03 00:00:00'
      AND d.status = 'approved'
    GROUP BY a.application_id
)
SELECT SUM(n)                                                       AS total_approved_rows,
       COUNT(*)                                                     AS distinct_apps,
       SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END)                       AS apps_multiplied,
       SUM(amt * n)                                                 AS inflated_amount,
       SUM(amt)                                                     AS dedup_amount
FROM approved_rows;

-- 5. Проверка: сколько заявок имеют несколько событий с одинаковым occurred_at
SELECT application_id, occurred_at, COUNT(*) AS same_time_events
FROM decision_events
GROUP BY application_id, occurred_at
HAVING COUNT(*) > 1;
