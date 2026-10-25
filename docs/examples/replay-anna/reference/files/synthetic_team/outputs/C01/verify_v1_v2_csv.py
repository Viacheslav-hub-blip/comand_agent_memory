# -*- coding: utf-8 -*-
"""C01: независимая проверка пересчёта когорты на CSV (pandas).

Сверка v1 vs v2 при фиксированном историческом срезе знания.

Параметры (можно менять через аргументы, ничего не зашито в логику):
  --cohort-lo / --cohort-hi — окно регистрации по created_at, [lo, hi)
  --as-of                   — срез знания T (верхняя граница не включается)

Определение среза решений: сначала occurred_at < T AND loaded_at < T,
затем первая строка по (occurred_at DESC, event_id DESC).
"""
import argparse
import os
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.abspath(os.path.join(BASE, "..", "..", "inputs"))


def snapshot(events, T):
    e = events[(events["occurred_at"] < T) & (events["loaded_at"] < T)].copy()
    e = e.sort_values(["occurred_at", "event_id"], ascending=[False, False])
    return e.groupby("application_id", as_index=False).first()[["application_id", "status"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort-lo", default="2026-10-01 00:00:00")
    ap.add_argument("--cohort-hi", default="2026-10-03 00:00:00")
    ap.add_argument("--as-of", default="2026-10-03 00:00:00")
    args = ap.parse_args()

    apps = pd.read_csv(os.path.join(INPUTS, "applications.csv"))
    ev = pd.read_csv(os.path.join(INPUTS, "decision_events.csv"))

    coh = apps[(apps["created_at"] >= args.cohort_lo) &
               (apps["created_at"] < args.cohort_hi)].copy()
    snap = snapshot(ev, args.as_of)
    m = coh.merge(snap, on="application_id", how="left")

    def rate(df):
        den = len(df)
        num = int((df["status"] == "approved").sum())
        return num, den, (num / den if den else None)

    v1 = m
    v2 = m[m["is_test"] == 0]

    n1, d1, r1 = rate(v1)
    n2, d2, r2 = rate(v2)

    fin = v2[v2["status"].isin(["approved", "rejected"])]
    fdr = (int((fin["status"] == "approved").sum()) / len(fin)) if len(fin) else None

    print("cohort [%s, %s); as-of %s (excl)" % (args.cohort_lo, args.cohort_hi, args.as_of))
    print("snapshot:\n", snap.to_string(index=False))
    print("\napproved on snapshot v1:", list(m[m['status'] == 'approved']['application_id']))
    print("approved on snapshot v2:", list(v2[v2['status'] == 'approved']['application_id']))
    print("\nv1 approval_rate %d/%d = %.6f" % (n1, d1, r1))
    print("v2 approval_rate %d/%d = %.6f" % (n2, d2, r2))
    print("final_decision_approval_rate (v2) = %.6f" % fdr)


if __name__ == "__main__":
    main()
