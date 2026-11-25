-- Эталон проверяющего для учебного набора «Север». Не передавать рабочему агенту.
-- Вход: таблицы из synthetic_team/sql/schema.sql с импортированными CSV.
-- Выход: четыре набора строк: срез статусов, версии метрик, деньги и оценка правил.
-- Функций и процедур нет. Все временные границы верхние, исключающие.

-- query: application_states
WITH ranked_events AS (
    SELECT
        application_id,
        status,
        ROW_NUMBER() OVER (
            PARTITION BY application_id
            ORDER BY occurred_at DESC, event_id DESC
        ) AS rn
    FROM decision_events
    WHERE occurred_at < '2026-10-03 00:00:00'
      AND loaded_at < '2026-10-03 00:00:00'
)
SELECT a.application_id, a.is_test, a.requested_amount_rub, e.status
FROM applications AS a
LEFT JOIN ranked_events AS e ON e.application_id = a.application_id AND e.rn = 1
WHERE a.created_at >= '2026-10-01 00:00:00'
  AND a.created_at < '2026-10-03 00:00:00'
ORDER BY a.application_id;

-- query: metric_versions
WITH ranked_events AS (
    SELECT
        application_id,
        status,
        ROW_NUMBER() OVER (
            PARTITION BY application_id
            ORDER BY occurred_at DESC, event_id DESC
        ) AS rn
    FROM decision_events
    WHERE occurred_at < '2026-10-03 00:00:00'
      AND loaded_at < '2026-10-03 00:00:00'
), cohort AS (
    SELECT a.application_id, a.is_test, a.requested_amount_rub, e.status
    FROM applications AS a
    LEFT JOIN ranked_events AS e ON e.application_id = a.application_id AND e.rn = 1
    WHERE a.created_at >= '2026-10-01 00:00:00'
      AND a.created_at < '2026-10-03 00:00:00'
)
SELECT 'v1' AS metric,
       SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS numerator,
       COUNT(*) AS denominator,
       SUM(CASE WHEN status = 'approved' THEN requested_amount_rub ELSE 0 END) AS approved_amount_rub
FROM cohort
UNION ALL
SELECT 'v2',
       SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END),
       COUNT(*),
       SUM(CASE WHEN status = 'approved' THEN requested_amount_rub ELSE 0 END)
FROM cohort WHERE is_test = 0
UNION ALL
SELECT 'final_decision_approval_rate',
       SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END),
       COUNT(*),
       SUM(CASE WHEN status = 'approved' THEN requested_amount_rub ELSE 0 END)
FROM cohort WHERE is_test = 0 AND status IN ('approved', 'rejected');

-- query: money
SELECT
    CASE WHEN a.is_test = 1 THEN 'test' ELSE 'real' END AS population,
    SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END) AS disbursement,
    SUM(CASE WHEN p.payment_type = 'refund' THEN p.amount_rub ELSE 0 END) AS refund,
    SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE -p.amount_rub END) AS net
FROM payments AS p
JOIN applications AS a ON a.application_id = p.application_id
WHERE p.booked_at >= '2026-10-01 00:00:00'
  AND p.booked_at < '2026-10-03 00:00:00'
GROUP BY a.is_test
ORDER BY a.is_test;

-- query: rule_metrics
WITH snapshots AS (
    SELECT '2026-10-05 00:00:00' AS snapshot
    UNION ALL SELECT '2026-10-07 00:00:00'
), cohort AS (
    SELECT application_id FROM applications
    WHERE is_test = 0
      AND created_at >= '2026-10-01 00:00:00'
      AND created_at < '2026-10-03 00:00:00'
), ranked_labels AS (
    SELECT s.snapshot, l.application_id, l.label,
           ROW_NUMBER() OVER (
               PARTITION BY s.snapshot, l.application_id
               ORDER BY l.recorded_at DESC, l.label_id DESC
           ) AS rn
    FROM snapshots AS s
    JOIN fraud_labels AS l ON l.recorded_at < s.snapshot
), population AS (
    SELECT s.snapshot, c.application_id, l.label
    FROM snapshots AS s CROSS JOIN cohort AS c
    LEFT JOIN ranked_labels AS l ON l.snapshot = s.snapshot
        AND l.application_id = c.application_id AND l.rn = 1
), denominators AS (
    SELECT snapshot, COUNT(*) AS cohort_size, COUNT(label) AS labeled_count,
           SUM(CASE WHEN label = 'confirmed_fraud' THEN 1 ELSE 0 END) AS total_fraud
    FROM population GROUP BY snapshot
), hit_counts AS (
    SELECT p.snapshot, h.rule_id, COUNT(*) AS hits,
           SUM(CASE WHEN p.label = 'confirmed_fraud' THEN 1 ELSE 0 END) AS tp,
           SUM(CASE WHEN p.label = 'legitimate' THEN 1 ELSE 0 END) AS fp,
           SUM(CASE WHEN p.label IS NULL THEN 1 ELSE 0 END) AS unknown_count
    FROM population AS p
    JOIN rule_hits AS h ON h.application_id = p.application_id AND h.hit_at < p.snapshot
    GROUP BY p.snapshot, h.rule_id
)
SELECT h.snapshot, h.rule_id, h.hits, h.tp, h.fp, h.unknown_count,
       d.cohort_size, d.labeled_count, d.total_fraud,
       1.0 * h.tp / NULLIF(h.tp + h.fp, 0) AS precision,
       1.0 * h.tp / NULLIF(d.total_fraud, 0) AS recall_labeled,
       1.0 * h.hits / NULLIF(d.cohort_size, 0) AS coverage
FROM hit_counts AS h
JOIN denominators AS d ON d.snapshot = h.snapshot
ORDER BY h.snapshot, h.rule_id;
