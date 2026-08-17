#!/usr/bin/env python3
"""fix_share_gifts.py — correct the record of the 5,001.15 GreyHames OTC transfer.

WHAT IS WRONG RIGHT NOW
───────────────────────
The acquisition leg is recorded as two hops through an account that was never part of
it, and its event date is missing entirely:

    gift:…1203738126850461738:776151361599438869   you  → 776…   (wrong recipient;
                                                                  note carries 09.08.26,
                                                                  which is the SALE date)
    fix:…776151361599438869->1059536577744863242   776… → Jesse  (wrong origin: the
                                                                  shares came from
                                                                  V Tech, not from 776…)

Both describe one event: **V Tech transferred 5,001.15 GreyHames shares to Jesse
Pinkman on 1 August 2026, with no coins paid** — an internal allocation. Jesse then
sold them on to thestablegenius123 on 9 August for 5,000,000c, which the `sale:` row
already records correctly.

WHAT THIS SCRIPT DOES
─────────────────────
Replaces those two rows with one, and does nothing else:

    otc:greyhames:1203738126850461738->1059536577744863242:2026-08-01
        V Tech → Jesse · 5,001.15 shares · value 0 (no coins paid)
        note states "1 Aug 2026" so the page reads the event date from the month NAME,
        which is unambiguous — unlike "01.08.26", which `history_web` can only read
        day-first and has to caveat as such on screen.

It also rewrites the `sale:` row's note to cite "9 Aug 2026" instead of "09.08.26",
for the same reason. Same date, same fact, a citation that needs no caveat. The preview
shows this; if you don't want it, pass --keep-sale-note.

WHAT IT DELIBERATELY DOES NOT TOUCH
───────────────────────────────────
**`stock_holdings`.** They are already right: thestablegenius123 holds the 5,001.15 and
Jesse holds none, which is the correct end state after the sale. V Tech → Jesse → Stable
nets out to exactly that, so replacing two *record* rows with one moves no shares. A
script that "tidied" the record and adjusted balances to match would be inventing a
share movement that never happened.

It writes to `share_gifts` and to no other table.

RUNNING IT
──────────
Preview is the default and writes nothing:

    python3 fix_share_gifts.py                 # show what would change
    python3 fix_share_gifts.py --apply         # do it (takes a backup first)

Run it where the bot's `restocker.db` is — the same folder the bot runs from — or point
it somewhere explicit with --db. It is idempotent: the new row is keyed, so a second
run reports "already correct" and changes nothing.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

MARKET = "greyhames"
VTECH_ACCOUNT = "1203738126850461738"      # markets.owner_id for 'vtech' (V Tech Hives)
JESSE = "1059536577744863242"
STABLE = "776151361599438869"
SHARES = 5001.15
BASIS = 27635.7

# The two rows that together mis-describe the acquisition.
STALE_KEYS = (
    f"gift:{MARKET}:{VTECH_ACCOUNT}:{STABLE}:{SHARES}",
    f"fix:{MARKET}:{STABLE}->{JESSE}",
)

NEW_KEY = f"otc:{MARKET}:{VTECH_ACCOUNT}->{JESSE}:2026-08-01"
NEW_NOTE = ("V Tech -> Jesse Pinkman on 1 Aug 2026: internal allocation of 5,001.15 "
            "GreyHames shares, no coins paid. Supersedes two earlier rows that routed "
            "this transfer through the wrong account and carried no event date. Jesse "
            "resold these shares to thestablegenius123 on 9 Aug 2026 for 5,000,000c.")

SALE_KEY = f"sale:{MARKET}:{JESSE}:{STABLE}:{SHARES}"
SALE_NOTE = ("Jesse Pinkman -> thestablegenius123: paid 5,000,000c on 9 Aug 2026, "
             "in-game payment, screenshot proof. Shares came to Jesse from V Tech on "
             "1 Aug 2026.")

DEFAULT_CANDIDATES = ("restocker.db", "data/restocker.db", "../restocker.db")


def find_db(explicit):
    if explicit:
        return explicit
    for c in DEFAULT_CANDIDATES:
        if os.path.exists(c):
            return c
    sys.exit("Could not find restocker.db here. Pass --db <path>.")


def fmt(n):
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="path to restocker.db (auto if omitted)")
    ap.add_argument("--apply", action="store_true", help="actually write (default: preview)")
    ap.add_argument("--keep-sale-note", action="store_true",
                    help="leave the sale row's note exactly as it is")
    a = ap.parse_args()

    path = find_db(a.db)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    have = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM share_gifts")}
    print("=" * 74)
    print(f"  {os.path.abspath(path)}")
    print(f"  share_gifts: {len(have)} row(s)")
    print("=" * 74)

    if NEW_KEY in have:
        print("\nAlready correct — the consolidated row is present. Nothing to do.")
        # Still report the stale rows if a previous run half-completed.
        left = [k for k in STALE_KEYS if k in have]
        if left:
            print("But these superseded rows are still here:")
            for k in left:
                print(f"  {k}")
            print("Re-run with --apply to remove them.")
        else:
            conn.close()
            return 0

    print("\nREMOVE")
    found_stale = False
    for k in STALE_KEYS:
        r = have.get(k)
        if not r:
            print(f"  (not present) {k}")
            continue
        found_stale = True
        print(f"  {k}")
        print(f"      {r['from_user']} -> {r['to_user']} · {fmt(r['shares'])} shares · "
              f"value {fmt(r['value_coins'] or 0)} c")
        print(f"      note: {r['note']}")

    if not found_stale and NEW_KEY not in have:
        print("  nothing — neither stale row is here. Check --db points at the live file.")

    print("\nADD")
    print(f"  {NEW_KEY}")
    print(f"      {VTECH_ACCOUNT} (V Tech) -> {JESSE} (Jesse Pinkman)")
    print(f"      {fmt(SHARES)} shares · {MARKET} · value 0 c — no coins paid")
    print(f"      event date 01 Aug 2026, read from the month name in the note")
    print(f"      note: {NEW_NOTE}")

    if not a.keep_sale_note and SALE_KEY in have:
        print("\nRE-CITE (same date, unambiguous source)")
        print(f"  {SALE_KEY}")
        print(f"      was: {have[SALE_KEY]['note']}")
        print(f"      now: {SALE_NOTE}")

    # Holdings are the end state and must not move. Report them so the preview proves it.
    print("\nHOLDINGS — untouched by this script, shown so you can see they already agree")
    for uid, who in ((JESSE, "Jesse Pinkman"), (STABLE, "thestablegenius123")):
        row = conn.execute("SELECT shares FROM stock_holdings WHERE user_id=? AND market_id=?",
                           (uid, MARKET)).fetchone()
        print(f"  {who:22} {fmt(row['shares']) if row else '0'} shares")

    if not a.apply:
        print("\n" + "=" * 74)
        print("  PREVIEW ONLY — nothing written. Re-run with --apply to commit.")
        print("=" * 74)
        conn.close()
        return 0

    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy(path, backup)

    with conn:
        conn.execute("DELETE FROM share_gifts WHERE key IN (?, ?)", STALE_KEYS)
        conn.execute(
            "INSERT OR REPLACE INTO share_gifts "
            "(key, market_id, from_user, to_user, shares, basis, value_coins, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (NEW_KEY, MARKET, VTECH_ACCOUNT, JESSE, SHARES, BASIS, 0, NEW_NOTE,
             have[STALE_KEYS[0]]["created_at"] if STALE_KEYS[0] in have
             else time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())),
        )
        if not a.keep_sale_note and SALE_KEY in have:
            conn.execute("UPDATE share_gifts SET note=? WHERE key=?", (SALE_NOTE, SALE_KEY))

    rows = list(conn.execute("SELECT key, from_user, to_user, shares, value_coins "
                             "FROM share_gifts ORDER BY created_at"))
    conn.close()
    print("\n" + "=" * 74)
    print(f"  WRITTEN. Backup: {backup}")
    print(f"  share_gifts now has {len(rows)} row(s):")
    for r in rows:
        print(f"    {r['from_user']} -> {r['to_user']} · {fmt(r['shares'])} sh · "
              f"{fmt(r['value_coins'] or 0)} c")
    print("  Reload /history as either party to see it.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
