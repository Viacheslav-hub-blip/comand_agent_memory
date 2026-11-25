# -*- coding: utf-8 -*-
"""
C01: расчёт на CSV. Строит отдельную локальную SQLite по schema.sql,
загружает CSV, считает показатели для ОТЧЁТА v1 (тестовые заявки входят).
Исходные файлы не изменяются (только чтение).
"""
import os
import csv
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
TEAM = os.path.abspath(os.path.join(BASE, "..", ".."))          # synthetic_team
INPUTS = os.path.join(TEAM, "inputs")
SCHEMA = os.path.join(TEAM, "sql", "schema.sql")
DB = os.path.join(BASE, "north.db")

T = "2026-10-03 00:00:00"                 # срез знания (верхняя граница исключена)
COHORT_LO = "2026-10-01 00:00:00"
COHORT_HI = "2026-10-03 00:00:00"

TABLES = {
    "applications": ["application_id", "created_at", "requested_amount_rub", "is_test", "channel"],
    "decision_events": ["event_id", "application_id", "status", "occurred_at", "loaded_at"],
    "payments": ["payment_id", "application_id", "payment_type", "amount_rub", "booked_at"],
    "fraud_labels": ["label_id", "application_id", "label", "recorded_at", "reason"],
    "rule_hits": ["rule_id", "application_id", "hit_at"],
}


def build_db():
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    for table, cols in TABLES.items():
        path = os.path.join(INPUTS, table + ".csv")
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        header, data = rows[0], rows[1:]
        assert header == cols, (table, header, cols)
        placeholders = ",".join("?" * len(cols))
        conn.executemany(
            "INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), placeholders),
            data,
        )
        conn.commit()
    conn.close()


def q(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return cols, cur.fetchall()


def show(title, cols, rows):
    print("\n== %s ==" % title)
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))


def main():
    build_db()
    conn = sqlite3.connect(DB)

    # ---- 1. Заявки в когорте ----
    cols, apps = q(conn, """
        SELECT COUNT(*) AS applications,
               SUM(is_test = 0) AS real_apps,
               SUM(is_test = 1) AS test_apps
        FROM applications
        WHERE created_at >= ? AND created_at < ?
    """, (COHORT_LO, COHORT_HI))
    show("1. Заявки когорты (created_at в окне)", cols, apps)

    # ---- 2. Согласованный срез решений на T: 1 строка на заявку ----
    cols, snap = q(conn, """
        WITH known AS (
            SELECT d.application_id, d.event_id, d.status, d.occurred_at, d.loaded_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.application_id
                       ORDER BY d.occurred_at DESC, d.event_id DESC
                   ) AS rn
            FROM decision_events d
            WHERE d.occurred_at < ? AND d.loaded_at < ?
        )
        SELECT a.application_id, a.is_test, a.requested_amount_rub,
               k.event_id, k.status, k.occurred_at, k.loaded_at
        FROM applications a
        LEFT JOIN known k ON k.application_id = a.application_id AND k.rn = 1
        WHERE a.created_at >= ? AND a.created_at < ?
        ORDER BY a.application_id
    """, (T, T, COHORT_LO, COHORT_HI))
    show("2. Срез решений на T (последний известный статус)", cols, snap)

    # ---- 3. Одобренные заявки на срез (v1: тесты ВХОДЯТ) ----
    cols, approved = q(conn, """
        WITH known AS (
            SELECT d.application_id, d.event_id, d.status,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.application_id
                       ORDER BY d.occurred_at DESC, d.event_id DESC) AS rn
            FROM decision_events d
            WHERE d.occurred_at < ? AND d.loaded_at < ?
        )
        SELECT a.application_id, a.is_test, a.requested_amount_rub
        FROM applications a
        JOIN known k ON k.application_id = a.application_id AND k.rn = 1
        WHERE a.created_at >= ? AND a.created_at < ?
          AND k.status = 'approved'
        ORDER BY a.application_id
    """, (T, T, COHORT_LO, COHORT_HI))
    show("3. Одобренные на срез T (v1, тесты входят)", cols, approved)
    approved_n = len(approved)
    approved_sum = sum(r[2] for r in approved)

    # ---- 4. approval_rate v1 ----
    n_all = apps[0][0]
    print("\n== 4. approval_rate v1 ==")
    print("numerator=%d denominator=%d value=%.6f" % (approved_n, n_all, approved_n / n_all))

    # ---- 5. Деньги (booked_at < T), v1: тесты входят ----
    cols, money = q(conn, """
        SELECT
            SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END) AS disbursement,
            SUM(CASE WHEN p.payment_type = 'refund'       THEN p.amount_rub ELSE 0 END) AS refund,
            SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END)
          - SUM(CASE WHEN p.payment_type = 'refund'       THEN p.amount_rub ELSE 0 END) AS net
        FROM payments p
        JOIN applications a ON a.application_id = p.application_id
        WHERE a.created_at >= ? AND a.created_at < ?
          AND p.booked_at < ?
    """, (COHORT_LO, COHORT_HI, T))
    show("5. Деньги когорты (booked_at < T, v1: тесты входят)", cols, money)
    disb, ref, net = money[0]

    # ---- 6. Старый запрос как есть (контроль) ----
    cols, legacy = q(conn, """
        SELECT COUNT(*) AS approved_count, SUM(a.requested_amount_rub) AS approved_amount_rub
        FROM applications a
        JOIN decision_events d ON d.application_id = a.application_id
        WHERE a.created_at >= ? AND a.created_at < ? AND d.status = 'approved'
    """, (COHORT_LO, COHORT_HI))
    show("6. Старый запрос (как есть)", cols, legacy)

    # ---- 7. Причины расхождения: вклад по заявкам ----
    cols, causes = q(conn, """
        WITH known AS (
            SELECT d.application_id, d.status,
                   ROW_NUMBER() OVER (
                       PARTITION BY d.application_id
                       ORDER BY d.occurred_at DESC, d.event_id DESC) AS rn
            FROM decision_events d
            WHERE d.occurred_at < ? AND d.loaded_at < ?
        )
        SELECT a.application_id, a.requested_amount_rub, a.is_test,
               COALESCE(k.status, 'no_event') AS status_at_T
        FROM applications a
        LEFT JOIN known k ON k.application_id = a.application_id AND k.rn = 1
        WHERE a.created_at >= ? AND a.created_at < ?
          AND EXISTS (SELECT 1 FROM decision_events d
                      WHERE d.application_id = a.application_id AND d.status = 'approved')
          AND COALESCE(k.status, 'no_event') <> 'approved'
        ORDER BY a.application_id
    """, (T, T, COHORT_LO, COHORT_HI))
    show("7. Лишние строки старого запроса (approved-событие есть, но статус на T иной)",
         cols, causes)

    print("\n---- ИТОГ ----")
    print("Заявок когорты:            %d" % n_all)
    print("Одобрено на срез (v1):     %d" % approved_n)
    print("Сумма requested по ним:    %d" % approved_sum)
    print("Выдача (disbursement):     %d" % disb)
    print("Возвраты (refund):         %d" % ref)
    print("Чистая выдача:             %d" % net)
    print("Старый запрос:             %d заявок / %d руб." % (legacy[0][0], legacy[0][1]))

    conn.close()


if __name__ == "__main__":
    main()
