#!/usr/bin/env python3
"""Clean up hive-harvest rows that were ingested (and paid) more than once.

WHY THEY EXIST
    The CSN mod reconstructs each sale's timestamp from "Xh Ym ago", which has only
    minute precision — so the same sale gets a slightly different absolute time on
    every export run (e.g. 14:18:21 on one run, 14:18:47 on the next). The
    `uq_hive_sale` unique index keys on the EXACT sale_ts, so each drifted timestamp
    slipped through as a brand-new sale and was paid again. Two ingest paths (the
    csn-hive webhook lines and the export CSV) made it worse, each drifting
    independently.

    Restocker_db.add_hive_harvest now rejects a row whose market+ign+item+qty matches
    an existing row within +/-120s, so NEW duplicates can't happen. This script cleans
    up the ones already in the database.

USAGE
    python fix_hive_duplicates.py                 # dry run - show what would change
    python fix_hive_duplicates.py --apply         # delete duplicate rows
    python fix_hive_duplicates.py --apply --claw-back
                                                  # also deduct the overpaid wages

    --claw-back moves real coins. Read the dry-run output first and decide whether you
    actually want to take wages back off your harvesters - "write it off" is a perfectly
    reasonable choice, and is the default precisely because it is not reversible.
"""
import argparse
import sqlite3
import sys
from datetime import datetime

WINDOW_SECONDS = 120        # same threshold the ingest guard uses


def ts_seconds(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def find_duplicate_clusters(conn):
    """Group rows by (market, ign, item, qty) and cluster them by time proximity.
    Returns [(keeper_row, [duplicate_rows...])] - the EARLIEST row of each cluster is
    kept, since that is the one the ledger saw first."""
    rows = conn.execute(
        "SELECT * FROM hive_harvests WHERE sale_ts IS NOT NULL"
    ).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r["market_id"], r["ign"], r["item"], r["qty"]), []).append(r)

    out = []
    for members in groups.values():
        members = sorted(members, key=lambda r: ts_seconds(r["sale_ts"]) or 0)
        cluster = [members[0]]
        for r in members[1:]:
            prev = ts_seconds(cluster[-1]["sale_ts"])
            cur = ts_seconds(r["sale_ts"])
            if prev is not None and cur is not None and abs(cur - prev) <= WINDOW_SECONDS:
                cluster.append(r)
            else:
                if len(cluster) > 1:
                    out.append((cluster[0], cluster[1:]))
                cluster = [r]
        if len(cluster) > 1:
            out.append((cluster[0], cluster[1:]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="restocker.db", help="path to restocker.db")
    ap.add_argument("--apply", action="store_true", help="actually delete duplicate rows")
    ap.add_argument("--claw-back", action="store_true",
                    help="also deduct overpaid wages from harvester balances")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    try:
        row = conn.execute(
            "SELECT value FROM bot_config WHERE key='hive_harvester_pct'").fetchone()
        wage_pct = float(row[0]) if row else 17.0
    except Exception:
        wage_pct = 17.0

    clusters = find_duplicate_clusters(conn)
    if not clusters:
        print("No duplicate hive rows found - nothing to do.")
        return 0

    dup_ids, overpaid, dup_value = [], {}, 0.0
    for _keeper, dups in clusters:
        for d in dups:
            dup_ids.append(d["id"])
            dup_value += d["qty"] * d["unit_value"]
            if d["paid"] == 1:
                wage = d["qty"] * d["unit_value"] * wage_pct / 100.0
                key = (str(d["user_id"] or ""), d["ign"])
                overpaid[key] = overpaid.get(key, 0.0) + wage

    print(f"Wage rate: {wage_pct:g}%   Window: +/-{WINDOW_SECONDS}s")
    print(f"Duplicate rows: {len(dup_ids)} across {len(clusters)} sale(s)")
    print(f"Duplicated harvest value: {dup_value:,.0f}")
    print("\nOverpaid wages (already credited):")
    total = 0.0
    for (uid, ign), amount in sorted(overpaid.items(), key=lambda kv: -kv[1]):
        who = f"{ign} ({uid})" if uid else f"{ign} (not linked - nothing to claw back)"
        print(f"  {who:42} {amount:>12,.0f}")
        total += amount
    print(f"  {'TOTAL':42} {total:>12,.0f}")

    if not args.apply:
        print("\nDRY RUN - nothing changed. Re-run with --apply to delete the duplicate "
              "rows, and add --claw-back to also deduct the overpaid wages.")
        return 0

    conn.execute("BEGIN")
    placeholders = ",".join("?" * len(dup_ids))
    conn.execute(f"DELETE FROM hive_harvests WHERE id IN ({placeholders})", dup_ids)
    print(f"\nDeleted {len(dup_ids)} duplicate row(s).")

    if args.claw_back:
        clawed = 0.0
        for (uid, ign), amount in overpaid.items():
            if not uid:
                print(f"  skipped {ign}: no linked Discord account")
                continue
            amt = int(round(amount))
            conn.execute("UPDATE balances SET coins = coins - ? WHERE user_id = ?",
                         (amt, str(uid)))
            print(f"  clawed back {amt:,} from {ign} ({uid})")
            clawed += amt
        print(f"Total clawed back: {clawed:,.0f}")
    else:
        print("Wages were NOT clawed back (no --claw-back). The duplicate rows are gone, "
              "so the totals stop double-counting from here on.")

    conn.commit()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
