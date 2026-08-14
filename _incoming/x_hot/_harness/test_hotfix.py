"""Proves H1-H4 against the REAL patched cogs/land_exchange.py.

Run:  python3 /home/claude/build/hotfix/_harness/test_hotfix.py
Also: python3 /home/claude/build/hotfix/_harness/test_hotfix.py --original
      (points at the untouched staged file, to show the bugs are real)
"""
import sys, os, sqlite3, asyncio, json, math, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
HOTFIX = os.path.dirname(HERE)
ORIGINAL = "/mnt/user-data/uploads/RestockerLocal"
USE_ORIGINAL = "--original" in sys.argv
sys.path.insert(0, HERE)
sys.path.insert(0, ORIGINAL if USE_ORIGINAL else HOTFIX)

import stubs  # noqa: E402  (installs discord / Restocker_db / Restocker_main)

stubs.cogs_pkg.__path__ = [os.path.join(ORIGINAL if USE_ORIGINAL else HOTFIX, "cogs")]
import cogs.land_exchange as LX  # noqa: E402

DB = "/tmp/land_hotfix_test/restocker.db"
os.makedirs(os.path.dirname(DB), exist_ok=True)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def sql_now_plus(**kw):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


def mk_auction(seller="S", *, reserve=1000.0, buy_now=None, ends_in_min=-1,
               bid=None, bidder=None, anti_snipe=5, starts_ago_days=0.0):
    return stubs.rdb.create_land_listing(
        seller_id=seller, kind="land", title="Plot", mode="auction", reserve=reserve,
        buy_now=buy_now, current_bid=bid, current_bidder=bidder,
        min_increment_pct=5.0, commission_pct=5.0, listing_fee=0,
        starts_at=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=starts_ago_days)
                   ).strftime("%Y-%m-%d %H:%M:%S"),
        ends_at=sql_now_plus(minutes=ends_in_min), anti_snipe_minutes=anti_snipe,
        status="active")


def credit(uid, coins):
    stubs.rdb.adjust_balance(uid, int(coins))


def run_sweep(cog):
    """Drive the REAL auction_sweep_loop body (including _rearm_stale_claims)."""
    asyncio.run(LX.LandExchangeCog.auction_sweep_loop.coro(cog))


def make_cog():
    cog = LX.LandExchangeCog(stubs.main.bot)

    async def _noop(*a, **kw):
        return None
    cog._post_sale = _noop
    cog._refresh_message = _noop
    cog._post_bid = _noop
    return cog


# ══════════════════════════════════════════════════════════════════════════════
print(f"\n=== TARGET: {'ORIGINAL (staged, unmodified)' if USE_ORIGINAL else 'HOTFIX'} ===")

# ── H1: the double-settle mint ────────────────────────────────────────────────
print("\nH1 — settle once even when the final UPDATE keeps failing")
stubs.fresh_db(DB)
PRICE = 8_500_000
lid = mk_auction("1001", bid=float(PRICE), bidder="2001", ends_in_min=-2)
credit("1001", 0)
credit("2001", 0)          # buyer's coins already collected at bid time (the hold)
supply_before = stubs.total_user_coins() + stubs.HOUSE["coins"]

# Simulate `database is locked` on exactly the write that marks the listing sold.
fail_state = {"n": 0}


def fault(where, **kw):
    if where == "update_land_listing" and kw.get("status") == "sold":
        fail_state["n"] += 1
        raise sqlite3.OperationalError("database is locked")


stubs.rdb.FAULT = fault
cog = make_cog()
for _ in range(3):                       # three 60s sweep passes, all failing to mark
    run_sweep(cog)
paid_after_failures = int(stubs.rdb.get_balance("1001")["coins"])
check("H1a  final UPDATE actually failed on every pass", fail_state["n"] >= 3,
      f"failures={fail_state['n']}")
check("H1b  seller paid AT MOST once across 3 failed sweeps",
      paid_after_failures <= PRICE, f"seller={paid_after_failures:,}")

stubs.rdb.FAULT = None                   # lock clears
run_sweep(cog)                           # the recovery pass
seller = int(stubs.rdb.get_balance("1001")["coins"])
house = stubs.HOUSE["coins"]
row = stubs.rdb.get_land_listing(lid)
n_sale = stubs.ledger_count("1001", f"realestate:sale:{lid}")
check("H1c  seller paid exactly the net, once", seller == PRICE - int(round(PRICE * 0.05)),
      f"seller={seller:,} expected={PRICE - int(round(PRICE*0.05)):,}")
check("H1d  exactly ONE realestate:sale ledger row", n_sale == 1, f"rows={n_sale}")
check("H1e  listing ends up 'sold'", row["status"] == "sold", f"status={row['status']}")
check("H1f  net + commission == int(round(price))", seller + house == int(round(PRICE)),
      f"{seller:,} + {house:,} = {seller + house:,}")
supply_after = stubs.total_user_coins() + stubs.HOUSE["coins"]
check("H1g  coins conserved: minted exactly the collected price, no more",
      supply_after - supply_before == int(round(PRICE)),
      f"delta={supply_after - supply_before:,} price={PRICE:,}")

# 20 more sweeps after the sale must be inert.
for _ in range(20):
    run_sweep(cog)
check("H1h  20 further sweeps mint nothing",
      int(stubs.rdb.get_balance("1001")["coins"]) == seller and stubs.HOUSE["coins"] == house,
      f"seller={int(stubs.rdb.get_balance('1001')['coins']):,}")

# ── H1 recovery: a claim whose owner died ─────────────────────────────────────
print("\nH1 — a listing stranded mid-claim is recovered, not silently unpaid")
stubs.fresh_db(DB)
lid = mk_auction("1002", bid=500_000.0, bidder="2002", ends_in_min=-2)
if not USE_ORIGINAL:
    with stubs.db() as c:                # emulate: claimed, then the process died
        c.execute("UPDATE land_listings SET status='settling', "
                  "updated_at=datetime('now','-30 minutes') WHERE id=?", (lid,))
    stranded = stubs.rdb.get_land_listing(lid)["status"]
    cog = make_cog()
    run_sweep(cog)
    after = stubs.rdb.get_land_listing(lid)
    check("H1i  stale claim re-armed and settled by the next sweep",
          stranded == "settling" and after["status"] == "sold",
          f"{stranded} -> {after['status']}")
    check("H1j  seller paid exactly once on recovery",
          stubs.ledger_count("1002", f"realestate:sale:{lid}") == 1)
else:
    print("  [SKIP] original has no claim state")

# ── H2: instant-buy idempotency ───────────────────────────────────────────────
print("\nH2 — instant-buy: a raise mid-settle must not let the retry buy it twice")
stubs.fresh_db(DB)
BN = 2_000_000
lid = mk_auction("1003", reserve=100.0, buy_now=float(BN), ends_in_min=60)
credit("2003", 5_000_000)
supply_before = stubs.total_user_coins() + stubs.HOUSE["coins"]

boom = {"armed": True}


def fault2(where, **kw):
    if boom["armed"] and where == "update_land_listing" and kw.get("status") == "sold":
        boom["armed"] = False            # fails once, like a transient lock
        raise sqlite3.OperationalError("database is locked")


stubs.rdb.FAULT = fault2
try:
    r1 = LX._instant_buy_core(lid, "2003")
except Exception as e:
    r1 = {"ok": False, "error": f"RAISED: {type(e).__name__}: {e}"}
check("H2a  first attempt does not raise out of the core", not str(r1.get("error", "")).startswith("RAISED"),
      str(r1.get("error"))[:90])
# The user is told "try again shortly" -> they retry.
r2 = LX._instant_buy_core(lid, "2003")
buyer_bal = int(stubs.rdb.get_balance("2003")["coins"])
n_charges = stubs.ledger_count("2003", f"realestate:buy:{lid}")
n_sales = stubs.ledger_count("1003", f"realestate:sale:{lid}")
n_refunds = stubs.ledger_count("2003", f"realestate:buy_refund:{lid}")
check("H2b  buyer debited exactly once", n_charges == 1, f"charge rows={n_charges}")
check("H2c  seller paid exactly once", n_sales == 1, f"sale rows={n_sales}")
check("H2d  buyer wallet reduced by exactly the price once",
      buyer_bal == 5_000_000 - BN, f"bal={buyer_bal:,}")
check("H2e  no phantom refund on top of a completed sale", n_refunds == 0, f"refunds={n_refunds}")
supply_after = stubs.total_user_coins() + stubs.HOUSE["coins"]
check("H2f  coins conserved across the failed+retried buy",
      supply_after == supply_before, f"delta={supply_after - supply_before:,}")

print("\nH2 — a THIRD click after success must not refund the buyer their money back")
r3 = LX._instant_buy_core(lid, "2003")
buyer_bal2 = int(stubs.rdb.get_balance("2003")["coins"])
check("H2g  repeat click does not hand the coins back",
      buyer_bal2 == buyer_bal, f"bal={buyer_bal2:,} (was {buyer_bal:,})")

print("\nH2 — a genuine refusal after collection still refunds, exactly once")
stubs.fresh_db(DB)
lid = mk_auction("1004", reserve=100.0, buy_now=1000.0, ends_in_min=60)
credit("2004", 10_000)
supply_before = stubs.total_user_coins() + stubs.HOUSE["coins"]


def fault3(where, **kw):
    # Sell the listing out from under the buyer between the debit and the settle.
    if where == "update_land_listing":
        return


stubs.rdb.FAULT = None
_orig_finalize = LX._finalize_sale_core
LX._finalize_sale_core = lambda *a, **kw: {"ok": False, "error": "That listing is no longer active."}
r = LX._instant_buy_core(lid, "2004")
r_again = LX._instant_buy_core(lid, "2004")
LX._finalize_sale_core = _orig_finalize
check("H2h  refused purchase is refunded", int(stubs.rdb.get_balance("2004")["coins"]) == 10_000,
      f"bal={int(stubs.rdb.get_balance('BUYER4')['coins']):,}")
check("H2i  refund happens once, not once per retry",
      stubs.ledger_count("2004", f"realestate:buy_refund:{lid}") == 1,
      f"refund rows={stubs.ledger_count('BUYER4', f'realestate:buy_refund:{lid}')}")
supply_after = stubs.total_user_coins() + stubs.HOUSE["coins"]
check("H2j  coins conserved across refuse+retry", supply_after == supply_before)

# ── H3: NaN ───────────────────────────────────────────────────────────────────
print("\nH3 — NaN / inf must be a refusal, not an exception, and must move no coins")
stubs.fresh_db(DB)
lid = mk_auction("1005", reserve=1000.0, ends_in_min=60)
credit("2005", 500)                    # deliberately LESS than the reserve
nan_from_json = json.loads('{"amount": NaN}')["amount"]   # the satellite hop, verbatim
check("H3a  json.loads really does yield NaN", nan_from_json != nan_from_json)
supply_before = stubs.total_user_coins()
for label, val in (("NaN", float("nan")), ("json NaN", nan_from_json),
                   ("+inf", float("inf")), ("-inf", float("-inf")),
                   ("negative", -5000.0), ("string nan", float("nan"))):
    try:
        res = LX._place_bid_core(lid, "2005", val)
        raised = None
    except Exception as e:
        res, raised = None, f"{type(e).__name__}: {e}"
    check(f"H3b  bid amount {label} -> clean refusal",
          raised is None and res is not None and res.get("ok") is False,
          raised or str(res.get("error"))[:70])
check("H3c  no coins moved on any NaN/inf bid", stubs.total_user_coins() == supply_before)
row = stubs.rdb.get_land_listing(lid)
check("H3d  current_bid never became NaN",
      row["current_bid"] is None or math.isfinite(float(row["current_bid"])),
      f"current_bid={row['current_bid']}")

print("\nH3 — a listing already poisoned with a non-finite price cannot be traded against")
# SQLite coerces a NaN REAL to NULL on write (measured), but stores +inf faithfully —
# so +inf is the reachable poison, and _min_next_bid raises OverflowError on it.
with stubs.db() as c:
    c.execute("UPDATE land_listings SET current_bid=? WHERE id=?", (float("inf"), lid))
    row_chk = c.execute("SELECT current_bid FROM land_listings WHERE id=?", (lid,)).fetchone()
check("H3e0 +inf really does persist in a REAL column",
      row_chk["current_bid"] == float("inf"), str(row_chk["current_bid"]))
credit("2006", 10_000_000)
supply_before = stubs.total_user_coins()
try:
    res = LX._place_bid_core(lid, "2006", 1_000_000.0)
    raised = None
except Exception as e:
    res, raised = None, f"{type(e).__name__}: {e}"
check("H3e  poisoned listing refuses new bids instead of accepting anything",
      raised is None and res is not None and res.get("ok") is False, raised or str(res)[:80])
check("H3f  no coins moved against the poisoned listing",
      stubs.total_user_coins() == supply_before)

print("\nH3 — NaN buy_now and NaN settle price")
stubs.fresh_db(DB)
lid = mk_auction("1007", reserve=100.0, ends_in_min=60)
with stubs.db() as c:
    c.execute("UPDATE land_listings SET buy_now=? WHERE id=?", (float("nan"), lid))
credit("2007", 10_000_000)
supply_before = stubs.total_user_coins()
try:
    res = LX._instant_buy_core(lid, "2007")
    raised = None
except Exception as e:
    res, raised = None, f"{type(e).__name__}: {e}"
check("H3g  NaN buy_now -> clean refusal", raised is None and res.get("ok") is False,
      raised or str(res.get("error"))[:70])
try:
    res = LX._finalize_sale_core(lid, "2007", float("nan"))
    raised = None
except Exception as e:
    res, raised = None, f"{type(e).__name__}: {e}"
check("H3h  NaN settle price -> clean refusal", raised is None and res.get("ok") is False,
      raised or str(res.get("error"))[:70])
check("H3i  no coins moved on either", stubs.total_user_coins() == supply_before)
check("H3j  listing left active by the refused settle",
      stubs.rdb.get_land_listing(lid)["status"] == "active",
      stubs.rdb.get_land_listing(lid)["status"])

try:
    res = LX.create_listing_core("1011", "Plot", float("nan"))
    _raised = None
except Exception as e:
    res, _raised = {}, f"{type(e).__name__}: {e}"
check("H3k  create_listing_core refuses a NaN starting price",
      _raised is None and res.get("ok") is False, _raised or str(res.get("error"))[:60])

# ── H4: anti-snipe cap ────────────────────────────────────────────────────────
print("\nH4 — anti-snipe extensions are bounded by a hard deadline")
stubs.fresh_db(DB)
stubs.rdb.set_config("realestate:max_auction_days", "0.01")   # 14.4 min cap, for a fast test
lid = mk_auction("1008", reserve=1000.0, ends_in_min=1, anti_snipe=5, starts_ago_days=0.0)
starts = LX._epoch(stubs.rdb.get_land_listing(lid)["starts_at"])
A, B = "3001", "3002"
credit(A, 100_000_000)
credit(B, 100_000_000)
ends_seen = []
for i in range(30):                       # 30 ping-pong extensions
    who = A if i % 2 == 0 else B
    r = LX._place_bid_core(lid, who, None)   # bid the minimum
    if not r.get("ok"):
        break
    row = stubs.rdb.get_land_listing(lid)
    ends_seen.append(LX._epoch(row["ends_at"]))
    with stubs.db() as c:                 # jump the clock to just inside the snipe window
        c.execute("UPDATE land_listings SET ends_at=datetime('now','+30 seconds') WHERE id=?", (lid,))
final_end = max(ends_seen) if ends_seen else 0
cap = starts + int(0.01 * 86400)
check("H4a  extensions happened at all (feature still works)", len(ends_seen) >= 2,
      f"extensions={len(ends_seen)}")
check("H4b  ends_at never pushed past starts_at + max_auction_days",
      final_end <= cap, f"final_end-cap = {final_end - cap}s over {len(ends_seen)} bids")

# The decisive control: an auction that has ALREADY run past the cap. The ping-pong loop
# above runs in zero wall-clock time, so it cannot separate capped from uncapped on its
# own — this one can, by ageing starts_at instead of waiting 14 days.
stubs.fresh_db(DB)
stubs.rdb.set_config("realestate:max_auction_days", "14.0")
lid = mk_auction("1012", reserve=1000.0, ends_in_min=1, anti_snipe=5, starts_ago_days=20.0)
before_end = LX._epoch(stubs.rdb.get_land_listing(lid)["ends_at"])
credit("3003", 100_000_000)
r = LX._place_bid_core(lid, "3003", None)
after_end = LX._epoch(stubs.rdb.get_land_listing(lid)["ends_at"])
check("H4d  bid on a past-the-cap auction is still ACCEPTED (business rule intact)",
      r.get("ok") is True, str(r.get("error"))[:70])
check("H4e  ...but it does NOT extend the end time past the hard deadline",
      after_end == before_end and not r.get("anti_snipe_extended"),
      f"moved {after_end - before_end}s, extended={r.get('anti_snipe_extended')}")

# a normal, non-colluding late bid still gets its extension
stubs.fresh_db(DB)
lid = mk_auction("1009", reserve=1000.0, ends_in_min=1, anti_snipe=5)
before_end = LX._epoch(stubs.rdb.get_land_listing(lid)["ends_at"])
credit("4001", 10_000_000)
r = LX._place_bid_core(lid, "4001", None)
after_end = LX._epoch(stubs.rdb.get_land_listing(lid)["ends_at"])
check("H4c  an honest last-minute bid still extends the auction",
      r.get("ok") and r.get("anti_snipe_extended") and after_end > before_end,
      f"+{after_end - before_end}s")

# ── conservation sanity on the ordinary happy path ────────────────────────────
print("\nRegression — ordinary bid / outbid / settle still conserves coins")
stubs.fresh_db(DB)
lid = mk_auction("1010", reserve=1000.0, ends_in_min=1)
credit("5001", 50_000)
credit("5002", 50_000)
supply_before = stubs.total_user_coins() + stubs.HOUSE["coins"]
r1 = LX._place_bid_core(lid, "5001", 10_000.0)
r2 = LX._place_bid_core(lid, "5002", 20_000.0)
check("Rg  both bids accepted", r1.get("ok") and r2.get("ok"), f"{r1.get('error')} {r2.get('error')}")
check("Rh  outbid B1 refunded in full", int(stubs.rdb.get_balance("5001")["coins"]) == 50_000)
with stubs.db() as c:
    c.execute("UPDATE land_listings SET ends_at=datetime('now','-1 minutes') WHERE id=?", (lid,))
cog = make_cog()
run_sweep(cog)
supply_after = stubs.total_user_coins() + stubs.HOUSE["coins"]
sold = stubs.rdb.get_land_listing(lid)
check("Ri  auction settled to the top bidder", sold["status"] == "sold" and sold["sold_to"] == "5002",
      f"{sold['status']} / {sold['sold_to']}")
check("Rj  coins conserved end-to-end (bid held, then paid out)",
      supply_after == supply_before, f"delta={supply_after - supply_before:,}")
check("Rk  seller net + house commission == price",
      int(stubs.rdb.get_balance("1010")["coins"]) + stubs.HOUSE["coins"] == 20_000,
      f"{int(stubs.rdb.get_balance('1010')['coins']):,} + {stubs.HOUSE['coins']:,}")

# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{'=' * 70}\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
