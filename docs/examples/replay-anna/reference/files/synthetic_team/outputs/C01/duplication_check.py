# -*- coding: utf-8 -*-
import os, sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "north.db")
conn = sqlite3.connect(DB)

def run(title, sql):
    print("\n== %s ==" % title)
    cur = conn.execute(sql)
    cols = [c[0] for c in cur.description]
    print(" | ".join(cols))
    for r in cur.fetchall():
        print(" | ".join("" if v is None else str(v) for v in r))

run("1. Событий на заявку", """
    SELECT application_id, COUNT(*) AS events_total,
           SUM(status='approved') AS approved_events
    FROM decision_events GROUP BY application_id ORDER BY application_id
""")
run("2. Строк от JOIN (полное соединение) на заявку", """
    SELECT a.application_id, COUNT(*) AS joined_rows
    FROM applications a JOIN decision_events d ON d.application_id=a.application_id
    GROUP BY a.application_id ORDER BY a.application_id
""")
run("3. Заявки с >1 approved-строкой (размножение в approved)", """
    SELECT a.application_id, COUNT(*) AS approved_rows
    FROM applications a JOIN decision_events d ON d.application_id=a.application_id
    WHERE d.status='approved' GROUP BY a.application_id HAVING COUNT(*)>1
""")
run("4. Сводка размножения по когорте", """
    WITH ar AS (
      SELECT a.application_id, COUNT(*) AS n, a.requested_amount_rub AS amt
      FROM applications a JOIN decision_events d ON d.application_id=a.application_id
      WHERE a.created_at>='2026-10-01 00:00:00' AND a.created_at<'2026-10-03 00:00:00'
        AND d.status='approved'
      GROUP BY a.application_id)
    SELECT SUM(n) AS total_rows, COUNT(*) AS distinct_apps,
           SUM(n>1) AS apps_multiplied, SUM(amt*n) AS inflated, SUM(amt) AS dedup
    FROM ar
""")
run("5. События с одинаковым occurred_at у одной заявки", """
    SELECT application_id, occurred_at, COUNT(*) AS same_time
    FROM decision_events GROUP BY application_id, occurred_at HAVING COUNT(*)>1
""")
run("6. Итог старого запроса (контроль)", """
    SELECT COUNT(*) AS approved_count, SUM(a.requested_amount_rub) AS amt
    FROM applications a JOIN decision_events d ON d.application_id=a.application_id
    WHERE a.created_at>='2026-10-01 00:00:00' AND a.created_at<'2026-10-03 00:00:00'
      AND d.status='approved'
""")
conn.close()
