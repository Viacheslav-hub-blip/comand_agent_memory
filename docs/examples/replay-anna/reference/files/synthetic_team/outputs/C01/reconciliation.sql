-- ============================================================================
-- C01: воспроизводимая сверка заявочных и денежных величин
-- Диалект: SQLite (оконные функции). application_id в decision_events НЕ уникален.
--
-- Файл НЕ привязан к конкретной когорте. Значения подставляются как именованные
-- параметры (:cohort_lo, :cohort_hi, :as_of, :include_test) — см. run_reconciliation.py.
-- Для другой когорты меняйте только параметры запуска, а не текст запросов.
--
--   :cohort_lo / :cohort_hi  — окно регистрации заявок, [lo, hi), по created_at
--   :as_of                   — дата среза знания T, верхняя граница НЕ включается
--   :include_test            — 1 = определение v1 (тестовые заявки входят),
--                              0 = определение v2 (is_test=1 исключается)
--
-- Каждый блок помечен маркером "-- @name: <id>" и выполняется раннером отдельно.
-- Порядок блоков повторяет метод сверки:
--   (1) определения и периоды -> (2) гранулярность -> (3) согласованный срез
--   -> (4) сравнение компонент -> (5) объяснение остатка.
-- ============================================================================


-- @name: 01_definitions_periods
-- Этап 1. Определения и периоды.
-- Когорта по времени регистрации; тестовые заявки показаны отдельно, чтобы
-- включение/исключение тестов было видимым, а не зашитым.
SELECT
    COUNT(*)                                     AS applications,
    SUM(CASE WHEN is_test = 0 THEN 1 ELSE 0 END) AS real_apps,
    SUM(CASE WHEN is_test = 1 THEN 1 ELSE 0 END) AS test_apps
FROM applications
WHERE created_at >= :cohort_lo AND created_at < :cohort_hi;


-- @name: 01b_definition_version_and_mode
-- Этап 1 (проверка). Версия определения и режим воспроизведения.
-- Версия ВЫВОДИТСЯ из :include_test (1 -> v1, 0 -> v2), а не предполагается.
-- Режим фиксируется как историческое воспроизведение знания на T:
--   occurred_at < T AND loaded_at < T — событие, загруженное позднее T, не меняет
--   картину, известную на T. Это отдельный режим от пересчёта прошлого с учётом
--   поздних событий; смешивать их нельзя.
-- Контроль режима: показано, сколько строк отсекает фильтр loaded_at (они были бы
--   видны при фильтрации только по occurred_at) — индикатор соблюдения режима.
SELECT
    CASE WHEN :include_test = 1 THEN 'v1' ELSE 'v2' END                     AS definition_version,
    CASE WHEN :include_test = 1 THEN 'тестовые входят'
         ELSE 'тестовые исключены' END                                     AS test_rule,
    'historical_reproduction'                                              AS reproduction_mode,
    :as_of                                                                 AS as_of,
    (SELECT COUNT(*) FROM decision_events d
        WHERE d.occurred_at < :as_of)                                      AS rows_by_occurred_only,
    (SELECT COUNT(*) FROM decision_events d
        WHERE d.occurred_at < :as_of AND d.loaded_at < :as_of)             AS rows_by_occurred_and_loaded,
    (SELECT COUNT(*) FROM decision_events d
        WHERE d.occurred_at < :as_of AND d.loaded_at >= :as_of)            AS rows_late_loaded_excluded;


-- @name: 02_granularity_check
-- Этап 2. Гранулярность. Проверяем, не размножаются ли заявки при соединении
-- applications с decision_events, и не даёт ли одна заявка более одного
-- approved-события (иначе наивный JOIN завышает сумму).
SELECT
    a.application_id,
    COUNT(d.event_id)                                      AS event_rows,
    SUM(CASE WHEN d.status = 'approved' THEN 1 ELSE 0 END) AS approved_rows
FROM applications a
LEFT JOIN decision_events d ON d.application_id = a.application_id
WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
GROUP BY a.application_id
ORDER BY a.application_id;


-- @name: 03_granularity_summary
-- Этап 2 (сводка). Заявок с >1 approved-строкой и разница сумм при наивном JOIN.
SELECT
    (SELECT COUNT(*) FROM (
        SELECT d.application_id
        FROM decision_events d
        JOIN applications a ON a.application_id = d.application_id
        WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
          AND d.status = 'approved'
        GROUP BY d.application_id
        HAVING COUNT(*) > 1))                       AS apps_with_multi_approved,
    (SELECT COUNT(*) FROM decision_events d
        JOIN applications a ON a.application_id = d.application_id
        WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
          AND d.status = 'approved')                AS approved_rows_naive,
    (SELECT COUNT(DISTINCT d.application_id) FROM decision_events d
        JOIN applications a ON a.application_id = d.application_id
        WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
          AND d.status = 'approved')                AS approved_apps_distinct;


-- @name: 04_decision_snapshot
-- Этап 3. Согласованный срез истории решений: одна строка на заявку.
-- Сначала occurred_at < T AND loaded_at < T (поздняя загрузка не переписывает
-- прошлое знание), затем первая строка в порядке occurred_at DESC, event_id DESC.
SELECT
    a.application_id,
    a.is_test,
    a.requested_amount_rub,
    s.event_id,
    s.status              AS status_at_T,
    s.occurred_at,
    s.loaded_at
FROM applications a
LEFT JOIN (
    SELECT application_id, event_id, status, occurred_at, loaded_at FROM (
        SELECT d.application_id, d.event_id, d.status, d.occurred_at, d.loaded_at,
               ROW_NUMBER() OVER (
                   PARTITION BY d.application_id
                   ORDER BY d.occurred_at DESC, d.event_id DESC) AS rn
        FROM decision_events d
        WHERE d.occurred_at < :as_of AND d.loaded_at < :as_of
    ) WHERE rn = 1
) s ON s.application_id = a.application_id
WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
ORDER BY a.application_id;


-- @name: 05_approved_on_snapshot
-- Этап 4. Одобренные заявки на срез (по согласованному срезу, не по любому событию).
-- :include_test = 1 -> v1 (тесты входят), 0 -> v2 (тесты исключены).
SELECT
    a.application_id,
    a.is_test,
    a.requested_amount_rub
FROM applications a
JOIN (
    SELECT application_id, status FROM (
        SELECT d.application_id, d.status,
               ROW_NUMBER() OVER (
                   PARTITION BY d.application_id
                   ORDER BY d.occurred_at DESC, d.event_id DESC) AS rn
        FROM decision_events d
        WHERE d.occurred_at < :as_of AND d.loaded_at < :as_of
    ) WHERE rn = 1
) s ON s.application_id = a.application_id
WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
  AND s.status = 'approved'
  AND (:include_test = 1 OR a.is_test = 0)
ORDER BY a.application_id;


-- @name: 06_approval_rate
-- Этап 4. approval_rate по выбранному определению.
-- Знаменатель — все зарегистрированные заявки когорты (в v1 с тестами, в v2 без).
-- Числитель — заявки с последним известным на срез статусом approved.
SELECT
    SUM(CASE WHEN a.is_test = 0 THEN 1 ELSE 0 END)                        AS n_real,
    SUM(CASE WHEN a.is_test = 1 THEN 1 ELSE 0 END)                        AS n_test,
    SUM(CASE WHEN (:include_test = 1 OR a.is_test = 0) THEN 1 ELSE 0 END) AS denominator,
    SUM(CASE WHEN (:include_test = 1 OR a.is_test = 0) AND s.status = 'approved'
             THEN 1 ELSE 0 END)                                           AS numerator,
    ROUND(
        1.0 * SUM(CASE WHEN (:include_test = 1 OR a.is_test = 0) AND s.status = 'approved'
                       THEN 1 ELSE 0 END)
        / NULLIF(SUM(CASE WHEN (:include_test = 1 OR a.is_test = 0) THEN 1 ELSE 0 END), 0),
        6)                                                                AS approval_rate
FROM applications a
LEFT JOIN (
    SELECT application_id, status FROM (
        SELECT d.application_id, d.status,
               ROW_NUMBER() OVER (
                   PARTITION BY d.application_id
                   ORDER BY d.occurred_at DESC, d.event_id DESC) AS rn
        FROM decision_events d
        WHERE d.occurred_at < :as_of AND d.loaded_at < :as_of
    ) WHERE rn = 1
) s ON s.application_id = a.application_id
WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi;


-- @name: 07_final_decision_approval_rate
-- Этап 4 (справочно). Другая метрика: approved / (approved + rejected), без тестов.
-- Она НЕ заменяет approval_rate (знаменатель не сокращается до финальных решений).
SELECT
    SUM(CASE WHEN s.status = 'approved' THEN 1 ELSE 0 END) AS approved_n,
    SUM(CASE WHEN s.status = 'rejected' THEN 1 ELSE 0 END) AS rejected_n,
    ROUND(1.0 * SUM(CASE WHEN s.status = 'approved' THEN 1 ELSE 0 END)
        / NULLIF(SUM(CASE WHEN s.status IN ('approved','rejected') THEN 1 ELSE 0 END), 0), 6)
        AS final_decision_approval_rate
FROM applications a
JOIN (
    SELECT application_id, status FROM (
        SELECT d.application_id, d.status,
               ROW_NUMBER() OVER (
                   PARTITION BY d.application_id
                   ORDER BY d.occurred_at DESC, d.event_id DESC) AS rn
        FROM decision_events d
        WHERE d.occurred_at < :as_of AND d.loaded_at < :as_of
    ) WHERE rn = 1
) s ON s.application_id = a.application_id
WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
  AND a.is_test = 0;


-- @name: 08_money_components
-- Этап 4. Денежные компоненты по времени проведения booked_at < T.
-- Основание времени денег отличается от основания когорты и от occurred_at.
-- Выдача и возвраты — независимые потоки; чистая выдача = выдача - возвраты.
SELECT
    SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END) AS disbursement,
    SUM(CASE WHEN p.payment_type = 'refund'       THEN p.amount_rub ELSE 0 END) AS refund,
    SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END)
      - SUM(CASE WHEN p.payment_type = 'refund'   THEN p.amount_rub ELSE 0 END) AS net_disbursement
FROM payments p
JOIN applications a ON a.application_id = p.application_id
WHERE p.booked_at < :as_of
  AND a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
  AND (:include_test = 1 OR a.is_test = 0);


-- @name: 09_legacy_query_as_is
-- Этап 5 (воспроизведение). Старый запрос как есть, без правок.
-- Служит точкой отсчёта остатка; он не является корректной метрикой.
SELECT
    COUNT(*)                    AS approved_count,
    SUM(a.requested_amount_rub) AS approved_amount_rub
FROM applications a
JOIN decision_events d ON d.application_id = a.application_id
WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
  AND d.status = 'approved';


-- @name: 10_residual_decomposition
-- Этап 5. Объяснение остатка: разложение по заявкам, БЕЗ зашитых сумм.
-- Для каждой заявки когорты сравниваем «любое approved-событие» (старый запрос)
-- с согласованным срезом и классифицируем вклад в остаток.
--   in_both              — учтена и старым запросом, и корректно
--   legacy_extra_status  — approved-событие есть, но последний статус на T иной
--                          (включая случай поздней загрузки loaded_at >= T)
--   legacy_extra_test    — одобрена на срез, но тестовая (лишняя только при v2)
--   snapshot_only        — одобрена на срез, но старого approved-события не видно
--                          (в учебном снимке не встречается; индикатор аномалии)
SELECT
    a.application_id,
    a.is_test,
    a.requested_amount_rub,
    CASE
        WHEN EXISTS (SELECT 1 FROM decision_events d
                     WHERE d.application_id = a.application_id AND d.status = 'approved')
             AND s.status = 'approved' AND a.is_test = 0 THEN 'in_both'
        WHEN EXISTS (SELECT 1 FROM decision_events d
                     WHERE d.application_id = a.application_id AND d.status = 'approved')
             AND s.status = 'approved' AND a.is_test = 1 THEN 'legacy_extra_test'
        WHEN EXISTS (SELECT 1 FROM decision_events d
                     WHERE d.application_id = a.application_id AND d.status = 'approved')
             AND COALESCE(s.status, 'no_event') <> 'approved' THEN 'legacy_extra_status'
        WHEN s.status = 'approved' THEN 'snapshot_only'
        ELSE 'not_counted'
    END AS bucket
FROM applications a
LEFT JOIN (
    SELECT application_id, status FROM (
        SELECT d.application_id, d.status,
               ROW_NUMBER() OVER (
                   PARTITION BY d.application_id
                   ORDER BY d.occurred_at DESC, d.event_id DESC) AS rn
        FROM decision_events d
        WHERE d.occurred_at < :as_of AND d.loaded_at < :as_of
    ) WHERE rn = 1
) s ON s.application_id = a.application_id
WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
ORDER BY a.application_id;


-- @name: 11_residual_totals
-- Этап 5 (итог). Сумма остатка по корзинам + контроль сходимости.
-- legacy_amount - сумма(in_both, legacy_extra_test) = вклад причин статуса/загрузки.
SELECT
    bucket,
    COUNT(*)                  AS applications,
    SUM(requested_amount_rub) AS requested_sum
FROM (
    SELECT
        a.application_id,
        a.requested_amount_rub,
        CASE
            WHEN EXISTS (SELECT 1 FROM decision_events d
                         WHERE d.application_id = a.application_id AND d.status = 'approved')
                 AND s.status = 'approved' AND a.is_test = 0 THEN 'in_both'
            WHEN EXISTS (SELECT 1 FROM decision_events d
                         WHERE d.application_id = a.application_id AND d.status = 'approved')
                 AND s.status = 'approved' AND a.is_test = 1 THEN 'legacy_extra_test'
            WHEN EXISTS (SELECT 1 FROM decision_events d
                         WHERE d.application_id = a.application_id AND d.status = 'approved')
                 AND COALESCE(s.status, 'no_event') <> 'approved' THEN 'legacy_extra_status'
            WHEN s.status = 'approved' THEN 'snapshot_only'
            ELSE 'not_counted'
        END AS bucket
    FROM applications a
    LEFT JOIN (
        SELECT application_id, status FROM (
            SELECT d.application_id, d.status,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.application_id
                       ORDER BY d.occurred_at DESC, d.event_id DESC) AS rn
            FROM decision_events d
            WHERE d.occurred_at < :as_of AND d.loaded_at < :as_of
        ) WHERE rn = 1
    ) s ON s.application_id = a.application_id
    WHERE a.created_at >= :cohort_lo AND a.created_at < :cohort_hi
)
GROUP BY bucket
ORDER BY bucket;
