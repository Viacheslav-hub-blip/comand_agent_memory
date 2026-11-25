# -*- coding: utf-8 -*-
"""C01: сверка старого запроса approved_amount_rub с чистой выдачей.

Все пути локальные; сети/БД нет. Только стандартная библиотека sqlite3 + csv.
"""
import csv
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
TEAM = os.path.abspath(os.path.join(BASE, "..", ".."))
INPUTS = os.path.join(TEAM, "inputs")
SQL_DIR = os.path.join(TEAM, "sql")
DB_PATH = os.path.join(BASE, "north.db")

T = "2026-10-03 00:00:00"          # верхняя граница среза (не включается)
COHORT_LO = "2026-10-01 00:00:00"  # нижняя граница когорты (включается)


def load(conn, table, path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    placeholders = ",".join("?" * len(header))
    conn.executemany(
        "INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(header), placeholders),
        body,
    )


def build_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    with open(os.path.join(SQL_DIR, "schema.sql"), "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    for table in ("applications", "decision_events", "payments", "fraud_labels", "rule_hits"):
        load(conn, table, os.path.join(INPUTS, table + ".csv"))
    conn.commit()
    return conn


def q(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return cols, cur.fetchall()


def show(title, cols, rows):
    print("\n== %s ==" % title)
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))
    if not rows:
        print("(пусто)")


def main():
    conn = build_db()

    # 0. Сырой старый запрос как есть
    with open(os.path.join(SQL_DIR, "legacy_approval.sql"), "r", encoding="utf-8") as f:
        legacy = f.read()
    cols, rows = q(conn, legacy)
    show("LEGACY: approved_count / approved_amount_rub", cols, rows)

    # 0b. Те же строки детально (видно дубли заявок)
    cols, rows = q(conn, """
        SELECT a.application_id, a.is_test, a.requested_amount_rub,
               d.event_id, d.status, d.occurred_at, d.loaded_at
        FROM applications a
        JOIN decision_events d ON d.application_id = a.application_id
        WHERE a.created_at >= ? AND a.created_at < ? AND d.status = 'approved'
        ORDER BY a.application_id, d.event_id
    """, (COHORT_LO, T))
    show("LEGACY detail", cols, rows)

    # 1. Согласованный срез: первое событие по occurred_at DESC, event_id DESC, при occurred_at<T AND loaded_at<T
    conn.execute("DROP VIEW IF EXISTS decision_snapshot")
    conn.execute("""
        CREATE VIEW decision_snapshot AS
        SELECT application_id, event_id, status, occurred_at, loaded_at FROM (
            SELECT d.*,
                   ROW_NUMBER() OVER (PARTITION BY application_id
                                      ORDER BY occurred_at DESC, event_id DESC) rn
            FROM decision_events d
            WHERE d.occurred_at < :T AND d.loaded_at < :T
        ) WHERE rn = 1
    """.replace(":T", "'%s'" % T))

    cols, rows = q(conn, "SELECT * FROM decision_snapshot ORDER BY application_id")
    show("Срез решений на T (одна строка на заявку)", cols, rows)

    # 2. approval_rate по v1 (с тестами) и v2 (без тестов) на срезе T
    cols, rows = q(conn, """
        WITH c AS (
            SELECT a.application_id, a.is_test, s.status
            FROM applications a
            LEFT JOIN decision_snapshot s ON s.application_id = a.application_id
            WHERE a.created_at >= ? AND a.created_at < ?
        )
        SELECT
            SUM(CASE WHEN is_test = 0 THEN 1 ELSE 0 END) AS n_real,
            SUM(CASE WHEN is_test = 1 THEN 1 ELSE 0 END) AS n_test,
            SUM(CASE WHEN is_test = 0 AND status = 'approved' THEN 1 ELSE 0 END) AS num_v2,
            SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS num_v1
        FROM c
    """, (COHORT_LO, T))
    show("approval_rate числители/знаменатели на T", cols, rows)

    # 2b. final_decision_approval_rate = approved/(approved+rejected), без тестов
    cols, rows = q(conn, """
        SELECT
            SUM(CASE WHEN s.status = 'approved' THEN 1 ELSE 0 END) AS approved_n,
            SUM(CASE WHEN s.status = 'rejected' THEN 1 ELSE 0 END) AS rejected_n
        FROM applications a
        JOIN decision_snapshot s ON s.application_id = a.application_id
        WHERE a.created_at >= ? AND a.created_at < ? AND a.is_test = 0
    """, (COHORT_LO, T))
    show("final_decision_approval_rate состав", cols, rows)

    # 3. Денежные показатели на T по booked_at (когорта = все реальные заявки)
    cols, rows = q(conn, """
        SELECT
            SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END) AS disbursement,
            SUM(CASE WHEN p.payment_type = 'refund' THEN p.amount_rub ELSE 0 END) AS refund,
            SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END)
              - SUM(CASE WHEN p.payment_type = 'refund' THEN p.amount_rub ELSE 0 END) AS net
        FROM payments p
        WHERE p.booked_at < ?
    """, (T,))
    show("Деньги по booked_at < T (все заявки)", cols, rows)

    # 3b. Деньги по booked_at < T только по реальным заявкам
    cols, rows = q(conn, """
        SELECT
            SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END) AS disbursement,
            SUM(CASE WHEN p.payment_type = 'refund' THEN p.amount_rub ELSE 0 END) AS refund,
            SUM(CASE WHEN p.payment_type = 'disbursement' THEN p.amount_rub ELSE 0 END)
              - SUM(CASE WHEN p.payment_type = 'refund' THEN p.amount_rub ELSE 0 END) AS net
        FROM payments p
        JOIN applications a ON a.application_id = p.application_id
        WHERE p.booked_at < ? AND a.is_test = 0
    """, (T,))
    show("Деньги по booked_at < T (только is_test=0)", cols, rows)

    # 4. Сумма requested_amount одобренных на срез (одна строка на заявку) — не деньги
    cols, rows = q(conn, """
        SELECT
            SUM(a.requested_amount_rub) AS approved_requested_sum,
            COUNT(*) AS approved_n
        FROM applications a
        JOIN decision_snapshot s ON s.application_id = a.application_id
        WHERE a.created_at >= ? AND a.created_at < ?
          AND a.is_test = 0 AND s.status = 'approved'
    """, (COHORT_LO, T))
    show("Сумма requested_amount одобренных (is_test=0), срез T", cols, rows)

    conn.close()


if __name__ == "__main__":
    main()
