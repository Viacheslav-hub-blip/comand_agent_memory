# -*- coding: utf-8 -*-
"""
C01: раннер воспроизводимой сверки.

Читает блоки из reconciliation.sql (маркеры "-- @name: <id>"), строит локальную
SQLite по ../../sql/schema.sql, загружает CSV из ../../inputs стандартным Python
и выполняет блоки с параметрами когорты. Исходные файлы только читаются.

Примеры:
    python run_reconciliation.py
    python run_reconciliation.py --cohort-lo "2026-09-01 00:00:00" \
        --cohort-hi "2026-10-01 00:00:00" --as-of "2026-10-03 00:00:00" --definition v2
"""
import argparse
import csv
import os
import re
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
TEAM = os.path.abspath(os.path.join(BASE, "..", ".."))
INPUTS = os.path.join(TEAM, "inputs")
SCHEMA = os.path.join(TEAM, "sql", "schema.sql")
SQL_FILE = os.path.join(BASE, "reconciliation.sql")
DEFAULT_DB = os.path.join(BASE, "north.db")

TABLES = {
    "applications": ["application_id", "created_at", "requested_amount_rub", "is_test", "channel"],
    "decision_events": ["event_id", "application_id", "status", "occurred_at", "loaded_at"],
    "payments": ["payment_id", "application_id", "payment_type", "amount_rub", "booked_at"],
    "fraud_labels": ["label_id", "application_id", "label", "recorded_at", "reason"],
    "rule_hits": ["rule_id", "application_id", "hit_at"],
}

NAME_RE = re.compile(r"^--\s*@name:\s*(\S+)\s*$")


def load_blocks(path):
    """Разбирает .sql на блоки по маркерам -- @name: <id>."""
    blocks = []
    current = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = NAME_RE.match(line.rstrip("\n"))
            if m:
                current = {"name": m.group(1), "lines": []}
                blocks.append(current)
                continue
            if current is not None:
                current["lines"].append(line)
    for b in blocks:
        b["sql"] = "".join(b["lines"]).strip().rstrip(";").strip()
    return blocks


def build_db(db_path):
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    with open(SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    for table, cols in TABLES.items():
        with open(os.path.join(INPUTS, table + ".csv"), encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0] == cols, (table, rows[0], cols)
        conn.executemany(
            "INSERT INTO %s (%s) VALUES (%s)"
            % (table, ",".join(cols), ",".join("?" * len(cols))),
            rows[1:],
        )
        conn.commit()
    return conn


def render(title, cols, rows):
    print("\n== %s ==" % title)
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort-lo", default="2026-10-01 00:00:00")
    ap.add_argument("--cohort-hi", default="2026-10-03 00:00:00")
    ap.add_argument("--as-of", default="2026-10-03 00:00:00")
    ap.add_argument("--definition", choices=["v1", "v2"], default="v1",
                    help="v1 — тестовые заявки входят; v2 — исключаются")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--keep-db", action="store_true")
    args = ap.parse_args()

    params = {
        "cohort_lo": args.cohort_lo,
        "cohort_hi": args.cohort_hi,
        "as_of": args.as_of,
        "include_test": 1 if args.definition == "v1" else 0,
    }
    print("Параметры: когорта created_at в [%s, %s); срез T = %s (не включается); "
          "определение = %s (include_test=%d)"
          % (params["cohort_lo"], params["cohort_hi"], params["as_of"],
             args.definition, params["include_test"]))

    conn = build_db(args.db)
    blocks = load_blocks(SQL_FILE)
    for b in blocks:
        try:
            cur = conn.execute(b["sql"], params)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
            render(b["name"], cols, rows)
        except sqlite3.Error as e:
            print("\n== %s ==\nОШИБКА: %s" % (b["name"], e))
            sys.exit(1)
    conn.close()
    if not args.keep_db and os.path.abspath(args.db) == os.path.abspath(DEFAULT_DB):
        pass  # оставляем базу для воспроизводимости


if __name__ == "__main__":
    main()
