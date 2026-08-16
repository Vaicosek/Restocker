"""Executable proof for the CSN content-signature store.

Run: python3 test_csn_ingest.py   (no pytest needed, no network, no Discord)

Each check is a claim from csn_sig's docstring or the csn_ingest schema comment,
turned into something that fails loudly if it stops being true.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import csn_sig                                                    # noqa: E402

_TMP = tempfile.mkdtemp(prefix="csn_ingest_test_")
import Restocker_db as db                                         # noqa: E402
db.DB_PATH = Path(_TMP) / "test.db"

_FAILED = []
_PASSED = 0


def check(label, got, want):
    global _PASSED
    if got == want:
        _PASSED += 1
        print(f"  ok   {label}")
    else:
        _FAILED.append(label)
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def row(actor="Steve", verb="bought", qty=64, item="Diamond", item_raw=None,
        coins="298.13", sale_date="2026-08-14", occ=None, sale_ts=None,
        seller="Vaicos"):
    return {
        "actor": actor, "seller": seller, "verb": verb, "qty": qty,
        "item": item, "item_raw": item_raw if item_raw is not None else item,
        "coins_str": coins, "coins": float(str(coins).replace(",", "")),
        "sale_date": sale_date, "occ": occ,
        "sale_ts": sale_ts or f"{sale_date}T12:00:00+00:00",
    }


db.init_db()
print()

# ── 1. the signature is exactly reproducible ────────────────────────────────
print("1. signature reproducibility")
a = csn_sig.sale_sig("Vaicos", "Steve", "bought", 64, "Diamond", "298.13", "2026-08-14", 1)
b = csn_sig.sale_sig("Vaicos", "Steve", "bought", 64, "Diamond", "298.13", "2026-08-14", 1)
check("same content -> byte-identical sig", a, b)
check("sig is 64 hex chars", (len(a), all(c in "0123456789abcdef" for c in a)), (64, True))

# Whitespace, colour codes and name case must not change the sig.
c = csn_sig.sale_sig("  vaicos ", "§aSteve", "BOUGHT", "64", "Diamond", "298.13",
                     "2026-08-14", "1")
check("noise-insensitive (case/§/space/str-vs-int)", c, a)

# The amount must go through Decimal, not float.
check("'298.13' and 298.13 agree",
      csn_sig.coins_to_centi("298.13"), 29813)
check("integer centi-coins, no float artefacts",
      [csn_sig.coins_to_centi(x) for x in ("-0.31", "0", "-0", "1,024.50", "+12")],
      [-31, 0, 0, 102450, 1200])

# The thing that was broken: time must NOT be an input.
t1 = csn_sig.sig_for_row(row(sale_ts="2026-08-14T12:00:21.317000+00:00", occ=1))
t2 = csn_sig.sig_for_row(row(sale_ts="2026-08-14T12:00:47.902000+00:00", occ=1))
check("B8: reconstruction drift does NOT change the sig", t1, t2)

# ── 2. insert-and-catch IS the dedup ────────────────────────────────────────
print("\n2. insert-and-catch dedup")
batch = [row(occ=1)]
r1 = db.csn_ingest_record("greyhames", batch)
check("first ingest is new", (r1["new"], r1["dup"], r1["bad"]), (1, 0, 0))
r2 = db.csn_ingest_record("greyhames", [row(occ=1)])
check("re-upload of same content is a duplicate", (r2["new"], r2["dup"]), (0, 1))
check("duplicate returns the SAME row id", r2["ids"], r1["ids"])
r3 = db.csn_ingest_record("greyhames", [row(occ=1)])
check("seen_count counts re-deliveries",
      db.csn_ingest_rows(r1["ids"])[0]["seen_count"], 3)

# A different market is a different claim -> its own row (scope is the index).
r4 = db.csn_ingest_record("viridianmarket", [row(occ=1)])
check("same sale, different market -> separate row", r4["new"], 1)

# ── 3. two genuinely identical sales are BOTH counted ───────────────────────
print("\n3. identical sales are not merged (the ±90s window's bug)")
twins = [row(actor="Bob", coins="50", occ=1, sale_ts="2026-08-14T09:00:10+00:00"),
         row(actor="Bob", coins="50", occ=2, sale_ts="2026-08-14T09:00:40+00:00")]
r5 = db.csn_ingest_record("greyhames", twins)
check("two identical sales 30s apart -> 2 rows", r5["new"], 2)
r6 = db.csn_ingest_record("greyhames", twins)
check("re-walking those two -> 0 new", r6["new"], 0)

# ── 4. occ assignment is order-independent ──────────────────────────────────
print("\n4. occ is order-independent (the multiset argument)")
raw = [row(actor="Ann", coins="7", sale_ts="2026-08-14T10:00:00+00:00"),
       row(actor="Ann", coins="7", sale_ts="2026-08-14T11:00:00+00:00"),
       row(actor="Ann", coins="7", sale_ts="2026-08-14T12:00:00+00:00")]
fwd = csn_sig.assign_occurrences([dict(x) for x in raw])
rev = csn_sig.assign_occurrences([dict(x) for x in reversed(raw)])
check("walk order does not change the sig MULTISET",
      sorted(csn_sig.sig_for_row(x) for x in fwd),
      sorted(csn_sig.sig_for_row(x) for x in rev))
check("group of 3 gets occ 1,2,3", sorted(x["occ"] for x in fwd), [1, 2, 3])

# A newly-appended identical sale must not renumber the existing ones.
grown = csn_sig.assign_occurrences(
    [dict(x) for x in raw] + [row(actor="Ann", coins="7",
                                  sale_ts="2026-08-14T13:00:00+00:00")])
grown_sigs = {csn_sig.sig_for_row(x) for x in grown}
check("a new 4th identical sale leaves the existing 3 sigs untouched",
      ({csn_sig.sig_for_row(x) for x in fwd} <= grown_sigs, len(grown_sigs)),
      (True, 4))

# ── 5. partial walk then full walk self-heals ───────────────────────────────
print("\n5. partial walk -> full walk is self-healing")
part = csn_sig.assign_occurrences([dict(x) for x in raw[:2]])
db.csn_ingest_record("selfheal", part)
full = csn_sig.assign_occurrences([dict(x) for x in raw])
r7 = db.csn_ingest_record("selfheal", full)
check("full walk adds only the unseen 3rd", (r7["new"], r7["dup"]), (1, 2))

# ── 6. midnight boundary probe ──────────────────────────────────────────────
print("\n6. midnight boundary")
check("normal row probes ONE date",
      csn_sig.boundary_dates("2026-08-14", "2026-08-14T12:00:00+00:00"),
      ["2026-08-14"])
check("row inside the 60s band probes TWO dates",
      csn_sig.boundary_dates("2026-08-14", "2026-08-14T00:00:31+00:00"),
      ["2026-08-14", "2026-08-13"])
mid = row(actor="Mid", coins="9", occ=1, sale_date="2026-08-13",
          sale_ts="2026-08-13T23:59:50+00:00")
db.csn_ingest_record("edge", [mid])
drifted = row(actor="Mid", coins="9", occ=1, sale_date="2026-08-14",
              sale_ts="2026-08-14T00:00:20+00:00")
r8 = db.csn_ingest_record("edge", [drifted])
check("a sale that drifted across midnight is NOT re-ingested",
      (r8["new"], r8["dup"]), (0, 1))

# ── 7. per-consumer flags are independent ───────────────────────────────────
print("\n7. per-consumer flags")
ids = db.csn_ingest_record("flags", [row(actor="Cara", coins="5", occ=1)])["ids"]
check("txn claim wins", db.csn_claim("txn", ids), ids)
check("txn claim is not re-winnable", db.csn_claim("txn", ids), [])
check("hive is untouched by the txn claim", db.csn_claim("hive", ids), ids)
db.csn_settle("txn", ids, "done")
db.csn_settle("hive", ids, "skip")
stored = db.csn_ingest_rows(ids)[0]
check("each consumer holds its own state",
      (stored["txn_state"], stored["hive_state"],
       stored["earn_state"], stored["feed_state"]),
      ("done", "skip", "pending", "pending"))
check("earn still sees it as pending work",
      [r["id"] for r in db.csn_pending("earn", "flags")], ids)
check("feed can still claim it after txn finished", db.csn_claim("feed", ids), ids)

# ── 8. claim-first survives a concurrent racer ──────────────────────────────
print("\n8. claim-first exclusivity")
ids2 = db.csn_ingest_record("race", [row(actor="Dan", coins="11", occ=1)])["ids"]
won_a = db.csn_claim("earn", ids2)
won_b = db.csn_claim("earn", ids2)
check("exactly one of two racers wins the row",
      (len(won_a), len(won_b)), (1, 0))

# A row abandoned in 'claimed' is surfaced, never auto-released.
ids3 = db.csn_ingest_record("stuck", [row(actor="Eve", coins="13", occ=1)])["ids"]
db.csn_claim("txn", ids3)
check("a crashed consumer leaves a visible stuck claim",
      [r["stuck_consumer"] for r in db.csn_stuck_claims(older_than_minutes=-1)
       if r["link_id"] == "stuck"],
      ["txn"])

# ── 9. end-to-end through the real ingest entry point ───────────────────────
print("\n9. add_csn_transactions_detailed (the real entry point)")
e2e = [row(actor="Frank", coins="120.50", occ=1),
       row(actor="Frank", coins="120.50", occ=2)]
n1, rows1 = db.add_csn_transactions_detailed("e2e", [dict(x) for x in e2e])
check("both identical sales book", n1, 2)
n2, rows2 = db.add_csn_transactions_detailed("e2e", [dict(x) for x in e2e])
check("re-uploading the same file books NOTHING", n2, 0)
with db.db() as conn:
    total = conn.execute(
        "SELECT COUNT(*), SUM(coins) FROM csn_transactions WHERE market_id='e2e'"
    ).fetchone()
check("ledger holds 2 rows totalling 241.00 coins",
      (total[0], round(total[1], 2)), (2, 241.00))

# The pre-v3 fleet must keep working: no occ column at all.
legacy = [{"actor": "Gus", "seller": "Vaicos", "verb": "bought", "qty": 1,
           "item": "Stone", "coins": 3.0, "coins_str": "3",
           "sale_ts": "2026-08-14T08:00:00+00:00"}]
n3, _ = db.add_csn_transactions_detailed("legacy", [dict(x) for x in legacy])
n4, _ = db.add_csn_transactions_detailed("legacy", [dict(x) for x in legacy])
check("pre-v3 row (no occ/sale_date) ingests then dedups", (n3, n4), (1, 0))


# ── 10. hive wages: the signature replaces the ±120s window ─────────────────
# The hive wage ledger is a real money path with two feeds into it, and they can
# identify a sale with very different confidence. These checks pin the split.
print("\n10. hive wage dedup (uq_hive_sig vs uq_hive_sale)")


def hive(ign, item, qty, ts, sig=None, market="greyhames", msg=None, line=0):
    return db.add_hive_harvest(market, ign, None, item, qty, 5.46875,
                               msg or f"t:{ign}:{item}:{qty}:{ts}:{sig}", line,
                               sale_ts=ts, wage_value=4.6875, sale_sig=sig)


# THE BUG THIS FIXES. Two genuinely separate harvests of 64 Honey Block by the same
# player inside the same minute. CSN reconstructs both to the SAME minute-granular
# timestamp, so the ±120s window — and uq_hive_sale, which keys on sale_ts — called
# the second one a duplicate and dropped it. The harvester was never paid for it and
# nothing anywhere said so.
sig_a = csn_sig.sale_sig("Vaicos", "Jesse", "sold", 64, "Honey Block", "-350", "2026-08-14", 1)
sig_b = csn_sig.sale_sig("Vaicos", "Jesse", "sold", 64, "Honey Block", "-350", "2026-08-14", 2)
check("two distinct harvests get two distinct signatures", sig_a != sig_b, True)
h1 = hive("Jesse", "Honey Block", 64, "2026-08-14T14:18:00+00:00", sig_a)
h2 = hive("Jesse", "Honey Block", 64, "2026-08-14T14:18:00+00:00", sig_b)
check("BOTH are recorded — the second harvest is paid too",
      (h1 is not None, h2 is not None), (True, True))

# …and re-walking still cannot double-pay: same signature, rejected.
h1_again = hive("Jesse", "Honey Block", 64, "2026-08-14T14:18:47+00:00", sig_a)
check("re-walked with a drifted timestamp -> NOT paid again", h1_again, None)

# Cross-market: the same physical sale exported under a second market id is the same
# wage. uq_hive_sig is deliberately not market-scoped, so the second market loses.
h1_other = hive("Jesse", "Honey Block", 64, "2026-08-14T14:18:00+00:00", sig_a,
                market="vtech")
check("the same sale under a second market -> paid once, not twice", h1_other, None)

# The webhook feed has no signature and MUST keep its old behaviour, window included.
f1 = hive("Guy", "Honeycomb Block", 32, "2026-08-14T15:00:00+00:00")
f2 = hive("Guy", "Honeycomb Block", 32, "2026-08-14T15:00:40+00:00")
check("unsigned feed rows keep the ±120s window (unchanged)",
      (f1 is not None, f2), (True, None))
f3 = hive("Guy", "Honeycomb Block", 32, "2026-08-14T15:30:00+00:00")
check("…and a genuinely later feed sale still records", f3 is not None, True)

with db.db() as conn:
    _n = conn.execute("SELECT COUNT(*) FROM hive_harvests WHERE ign='Jesse'").fetchone()[0]
check("Jesse's wage ledger holds exactly the 2 real harvests", _n, 2)


# ── 11. real names survive canonicalisation ─────────────────────────────────
# The signature folds case so the two languages can agree on it. That canonical form
# must never reach a surface a human reads: players identify by the capitalisation
# they chose, and a ledger listing "jessenapoleon" is a name nobody recognises.
print("\n11. canonical form for hashing, real names for humans")
_nm = [dict(row(actor="JesseNapoleon", seller="GreyHames", verb="sold",
                item="Honey Block", item_raw="Honey Block", qty=64,
                coins="-350", occ=1, sale_date="2026-08-13"))]
_n, _rows = db.add_csn_transactions_detailed("names", [dict(x) for x in _nm])
check("the row booked", _n, 1)
check("the returned row carries the name as CSN printed it",
      (_rows[0]["actor"], _rows[0]["seller"]), ("JesseNapoleon", "GreyHames"))
with db.db() as conn:
    _t = conn.execute("SELECT actor, seller FROM csn_transactions "
                      "WHERE market_id='names'").fetchone()
    _i = conn.execute("SELECT actor, actor_display FROM csn_ingest "
                      "WHERE link_id='names'").fetchone()
check("the ledger stores the real name", (_t[0], _t[1]), ("JesseNapoleon", "GreyHames"))
check("csn_ingest keeps BOTH: canonical for the hash, display for people",
      (_i["actor"], _i["actor_display"]), ("jessenapoleon", "JesseNapoleon"))

# Case must still not create a second row — that is what the canonical form is for.
_n2, _ = db.add_csn_transactions_detailed(
    "names", [dict(row(actor="jessenapoleon", seller="greyhames", verb="sold",
                       item="Honey Block", item_raw="Honey Block", qty=64,
                       coins="-350", occ=1, sale_date="2026-08-13"))])
check("the same sale spelled differently is still ONE sale", _n2, 0)

print(f"\n{'='*58}\n{_PASSED} passed, {len(_FAILED)} failed")
if _FAILED:
    for f in _FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
