"""Proof by execution for LAND_ESCROW_PLAN §2.1/§2.5/§2.6 and rent.

Every test here drives the SHIPPED modules against a REAL `restocker.db` with
production pragmas and the REAL escrow triggers installed by `ledger_migrate.py`.
Nothing is mocked except Discord and the audit log; in particular `ledger_v2`,
`land_escrow`, `land_settle`, `Restocker_db` and `cogs/land_exchange.py` are the
files that deploy.

The five things this file exists to prove, in the order the task named them:

  1. A close captures the winner and releases every loser EXACTLY ONCE.
  2. A crash mid-close resumes and does not double-pay.
  3. A settle interrupted after the seller is paid does not pay again.
  4. Rent for one period charges once across three retries.
  5. `deduct_coins`' YAML fallback can no longer swallow the escrow trigger.

Assertions are against the DATABASE — `ledger_holds`, `balances`,
`ledger_idempotency`, `land_bids` — not against return values, because a return
value is what the buggy code was also producing.
"""
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import land_stubs  # noqa: E402

land_stubs.install()
land_stubs.install_core()

SELLER = "700000000000000001"
WINNER = "700000000000000002"
LOSER_A = "700000000000000003"
LOSER_B = "700000000000000004"
TENANT = "700000000000000005"
LANDLORD = "700000000000000006"


# ── harness ───────────────────────────────────────────────────────────────

class Ctx:
    """One test's world: a fresh db, the real modules, and helpers to build a lot."""

    def __init__(self, tmp):
        self.tmp = tmp
        self._cm = land_stubs.fresh_db(tmp)
        self.db = self._cm.__enter__()
        import ledger_v2
        import land_escrow
        import land_settle
        self.lv2 = ledger_v2
        self.esc = land_escrow
        self.st = land_settle
        # A fresh in-process adapter per test, so a swapped-in fault ledger from
        # one test cannot leak into the next.
        self.esc.set_ledger(self.esc.LedgerV2InProcess())

    def close(self):
        self._cm.__exit__(None, None, None)

    # -- world building ---------------------------------------------------
    def credit(self, uid, coins):
        """Put real coins in a wallet, the way the rest of the bot does."""
        with self.db.db() as conn:
            conn.execute("INSERT INTO balances (user_id, coins, principal, lp) "
                         "VALUES (?,?,?,0) ON CONFLICT(user_id) DO UPDATE SET "
                         "coins=excluded.coins, principal=excluded.principal",
                         (str(uid), float(coins), float(coins)))

    def coins(self, uid):
        with self.db.db() as conn:
            row = conn.execute("SELECT coins FROM balances WHERE user_id=?",
                               (str(uid),)).fetchone()
            return int(row["coins"]) if row else 0

    def listing(self, *, price=None, commission_pct=5.0, fee=0.0, ends_at=None):
        # `status='active'` with `ends_at` in the year 2000 is a state no live lot
        # is ever in: the sweep closes an active lot within 60 seconds of its
        # deadline. It went unnoticed because nothing in this file read `ends_at`
        # — `settle_listing` and `expire_unsold` are called directly here, and
        # neither checks it. `_instant_buy_core` now does (the guard
        # `_place_bid_core` has always carried), so the default has to be what it
        # always meant: OPEN. Tests that want an ended lot pass `ends_at=`.
        if ends_at is None:
            ends_at = (datetime.now(timezone.utc)
                       + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        return self.db.create_land_listing(
            seller_id=SELLER, kind="land", title="Test plot", mode="auction",
            reserve=1000.0, buy_now=(float(price) if price else None),
            commission_pct=commission_pct, listing_fee=fee,
            ends_at=ends_at, status="active")

    def bid(self, listing_id, bidder, amount, kind="bid"):
        """Place a real hold through the real row state machine. Returns the row."""
        row = self.esc.create_bid_row(listing_id, bidder, float(amount),
                                      int(amount), kind=kind)
        rid = int(row["id"])
        assert self.esc.claim_placement(rid) is not None
        out = self.esc.ledger().hold(str(bidder), int(amount),
                                     f"realestate:bid:{listing_id}",
                                     self.esc.HOLD_GRACE_SECONDS,
                                     self.esc.hold_key(listing_id, kind, rid))
        self.esc.mark_held(rid, out["hold_id"], out.get("expires_at"))
        return self.esc.bid_row(rid)

    def top_bid(self, listing_id, bidder, amount):
        row = self.bid(listing_id, bidder, amount)
        self.db.update_land_listing(listing_id, current_bid=float(amount),
                                    current_bidder=str(bidder))
        return row

    # -- introspection ----------------------------------------------------
    def holds(self, uid=None):
        sql = "SELECT * FROM ledger_holds"
        args = ()
        if uid:
            sql += " WHERE user_id=?"
            args = (str(uid),)
        with self.db.db() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def entries(self, action=None):
        sql = "SELECT * FROM ledger_entries"
        args = ()
        if action:
            sql += " WHERE action=?"
            args = (action,)
        with self.db.db() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def idem(self, key):
        with self.db.db() as conn:
            row = conn.execute("SELECT * FROM ledger_idempotency WHERE key=?",
                               (key,)).fetchone()
            return dict(row) if row else None


class FaultLedger:
    """The real adapter with one call rigged to blow up.

    A crash mid-settlement cannot be produced from a healthy ledger, and the
    whole point of the resume machinery is what happens after one. `after`
    controls whether the failure lands BEFORE the money moves (a refusal) or
    AFTER it has committed and only the answer was lost — the second is the
    dangerous one, and it is the one the resume tests use.
    """

    def __init__(self, real, fail_on, *, after=False, exc=None):
        self.real, self.fail_on, self.after = real, fail_on, after
        self.exc = exc or RuntimeError("connection died")
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.real, name)

    def _maybe(self, name, fn, *a, **kw):
        self.calls.append(name)
        if name != self.fail_on:
            return fn(*a, **kw)
        if self.after:
            fn(*a, **kw)                # the money REALLY moves...
            raise self.exc              # ...and the caller never learns
        raise self.exc

    def hold(self, *a, **kw):
        return self._maybe("hold", self.real.hold, *a, **kw)

    def capture(self, *a, **kw):
        return self._maybe("capture", self.real.capture, *a, **kw)

    def release(self, *a, **kw):
        return self._maybe("release", self.real.release, *a, **kw)

    def transfer(self, *a, **kw):
        return self._maybe("transfer", self.real.transfer, *a, **kw)

    def get(self, *a, **kw):
        # `get` is a pure READ of core's hold state and carries no idempotency
        # key, which is why the settle path is allowed to call it inline. It is
        # riggable here for the same reason the others are: "the settle asks"
        # is only a guarantee if there is a test for what happens when the ask
        # itself cannot be answered.
        return self._maybe("get", self.real.get, *a, **kw)

    def error_code(self, exc):
        return self.real.error_code(exc)


# ── the proofs ────────────────────────────────────────────────────────────

def test_close_captures_winner_and_releases_every_loser_once(ctx):
    """PROOF 1. One capture, two releases, each exactly once, coins conserved."""
    ctx.credit(WINNER, 50_000)
    ctx.credit(LOSER_A, 50_000)
    ctx.credit(LOSER_B, 50_000)
    ctx.credit(SELLER, 0)
    lid = ctx.listing()
    a = ctx.bid(lid, LOSER_A, 10_000)
    b = ctx.bid(lid, LOSER_B, 20_000)
    win = ctx.top_bid(lid, WINNER, 40_000)

    before = {u: ctx.coins(u) for u in (WINNER, LOSER_A, LOSER_B, SELLER)}
    assert before[WINNER] == 50_000, "a hold must not move coins"

    res = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    assert res["outcome"] == "sold", res

    # The winner paid exactly the hammer; the losers paid nothing at all.
    assert ctx.coins(WINNER) == 10_000
    assert ctx.coins(LOSER_A) == 50_000
    assert ctx.coins(LOSER_B) == 50_000
    # commission 5% of 40,000 = 2,000 -> seller nets 38,000
    assert ctx.coins(SELLER) == 38_000
    assert ctx.coins(ctx.esc.TREASURY) == 2_000, "the commission is REAL coins now"

    # Exactly one capture and two releases at the ledger, and every hold terminal.
    states = {h["hold_id"]: h["state"] for h in ctx.holds()}
    assert sorted(states.values()) == ["captured", "released", "released"]
    assert len(ctx.entries("capture")) == 1
    assert len(ctx.entries("release")) == 2
    # And the rows agree with the ledger, which is the property a sweep resumes on.
    assert ctx.esc.bid_row(int(win["id"]))["status"] == "captured"
    assert ctx.esc.bid_row(int(a["id"]))["status"] == "released"
    assert ctx.esc.bid_row(int(b["id"]))["status"] == "released"
    # Conservation: nothing was created or destroyed.
    total_before = sum(before.values()) + 0
    total_after = sum(ctx.coins(u) for u in (WINNER, LOSER_A, LOSER_B, SELLER)) \
        + ctx.coins(ctx.esc.TREASURY)
    assert total_after == total_before, (total_before, total_after)


def test_second_close_is_a_no_op_not_a_second_payout(ctx):
    """PROOF 1b. The 8.5M/minute mint: re-running the sweep pays nobody again."""
    ctx.credit(WINNER, 50_000)
    ctx.credit(SELLER, 0)
    lid = ctx.listing()
    ctx.top_bid(lid, WINNER, 40_000)
    ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    snapshot = (ctx.coins(WINNER), ctx.coins(SELLER), ctx.coins(ctx.esc.TREASURY))

    for _ in range(3):
        again = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
        # `sold` is not `active`, so the claim cannot be won at all.
        #
        # WORD CHANGED 15 Aug, ASSERTION STRENGTHENED. This used to accept
        # `already_settling`, which the claim refusal returned for EVERY status
        # it refused — including terminal ones. That told a caller "a settlement
        # is in flight, wait" about a lot that was already over. The refusal now
        # reads the row and says which: `already_closed` + the real status. The
        # no-op guarantee this test exists for is unchanged and still asserted
        # below (balances, one capture, one transfer_in); what is new is that the
        # word and the status are pinned too, so the two cannot silently merge
        # again.
        assert again.get("outcome") == ctx.st.ALREADY_CLOSED, again
        assert again.get("status") == "sold", again
        assert again.get("ok") is True, again
    assert (ctx.coins(WINNER), ctx.coins(SELLER), ctx.coins(ctx.esc.TREASURY)) == snapshot
    assert len(ctx.entries("capture")) == 1
    assert len([e for e in ctx.entries("transfer_in")]) == 1


def test_crash_mid_close_resumes_and_does_not_double_pay(ctx):
    """PROOF 2. Kill the process after the capture COMMITS but before it is
    recorded; the resume replays and the seller is paid once."""
    ctx.credit(WINNER, 50_000)
    ctx.credit(SELLER, 0)
    lid = ctx.listing()
    win = ctx.top_bid(lid, WINNER, 40_000)

    real = ctx.esc.ledger()
    fault = FaultLedger(real, "capture", after=True)   # money moves, answer lost
    ctx.esc.set_ledger(fault)
    try:
        ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
        raise AssertionError("the rigged capture should have raised")
    except RuntimeError:
        pass
    finally:
        ctx.esc.set_ledger(real)

    # The coins really did move; the row says "I do not know", not "held".
    assert ctx.coins(WINNER) == 10_000
    assert ctx.coins(ctx.esc.TREASURY) == 40_000
    assert ctx.esc.bid_row(int(win["id"]))["status"] == "capture_unknown"
    assert ctx.coins(SELLER) == 0, "the seller must NOT have been paid yet"
    # The listing was handed back so the sweep can re-enter.
    assert ctx.db.get_land_listing(lid)["status"] == "active"

    # The resume: reconcile from core, then settle again.
    ctx.esc.reconcile_holds(older_than_minutes=0)
    assert ctx.esc.bid_row(int(win["id"]))["status"] == "captured"
    res = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    assert res["outcome"] == "sold", res

    assert ctx.coins(WINNER) == 10_000, "the winner must not be charged twice"
    assert ctx.coins(SELLER) == 38_000
    assert ctx.coins(ctx.esc.TREASURY) == 2_000
    assert len(ctx.entries("capture")) == 1, "exactly one capture ever happened"


def test_settle_interrupted_after_seller_paid_does_not_pay_again(ctx):
    """PROOF 3. The transfer commits, the answer is lost, the retry replays."""
    ctx.credit(WINNER, 50_000)
    ctx.credit(SELLER, 0)
    lid = ctx.listing()
    ctx.top_bid(lid, WINNER, 40_000)

    real = ctx.esc.ledger()
    fault = FaultLedger(real, "transfer", after=True)
    ctx.esc.set_ledger(fault)
    try:
        ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
        raise AssertionError("the rigged transfer should have raised")
    except RuntimeError:
        pass
    finally:
        ctx.esc.set_ledger(real)

    assert ctx.coins(SELLER) == 38_000, "the seller WAS paid; the answer was lost"
    row = ctx.db.get_land_listing(lid)
    assert row["settle_stage"] == "paying_seller", row["settle_stage"]
    assert row["status"] == "active", "handed back for the sweep to resume"
    # The key is `done` at core with the response stored — that is what makes the
    # replay byte-identical rather than a second payment.
    claim = ctx.idem(ctx.esc.seller_key(lid))
    assert claim and claim["state"] == "done", claim

    res = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    assert res["outcome"] == "sold", res
    assert ctx.coins(SELLER) == 38_000, "PAID ONCE"
    assert ctx.coins(ctx.esc.TREASURY) == 2_000
    outs = [e for e in ctx.entries("transfer_out")]
    assert len(outs) == 1, f"one seller transfer, got {len(outs)}"


def test_settle_killed_between_the_split_and_the_stage_marker_routes_once(ctx):
    """PROOF 3b. The commission split has NO settle_stage rung of its own — its own
    run row is its progress marker. So the call has to happen BEFORE
    `settle_stage` reaches `seller_paid`, or a death in that window skips the
    split forever: the resumed settle sees the rung reached, never calls it, and
    since no run row was ever minted the resume sweep has nothing to find and
    `stuck_runs()` names nothing. The coins just sit in `treasury:estates`.

    This kills the process in exactly that window — the split has run, the marker
    has not been written — and proves the resume routes the commission ONCE."""
    import split_rules as sr
    ctx.credit(WINNER, 50_000)
    ctx.credit(SELLER, 0)
    ctx.credit("treasury:vtech", 0)
    with ctx.lv2._tx() as conn:
        sr.ensure_schema(conn)
    sr.add_rule(ctx.esc.TREASURY, "account", "treasury:vtech", 7000, seq=0)

    lid = ctx.listing()
    ctx.top_bid(lid, WINNER, 40_000)

    real_claim = ctx.db.claim_listing_stage

    def dying(listing_id, expect, to):
        if expect == "paying_seller" and to == "seller_paid":
            raise RuntimeError("process died between the split and the marker")
        return real_claim(listing_id, expect, to)

    ctx.db.claim_listing_stage = dying
    try:
        ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
        raise AssertionError("the rigged marker should have raised")
    except RuntimeError:
        pass
    finally:
        ctx.db.claim_listing_stage = real_claim

    # The window's own state: seller paid, commission ROUTED, marker not written.
    assert ctx.coins(SELLER) == 38_000
    assert ctx.coins("treasury:vtech") == 1_400, "70% of the 2,000 commission"
    assert ctx.db.get_land_listing(lid)["settle_stage"] == "paying_seller"
    runs = sr.stuck_runs(0)
    assert len(runs) == 0, f"the run is finished, not parked: {runs}"

    # The resume re-enters the block and replays every keyed leg exactly once.
    res = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    assert res["outcome"] == "sold", res
    assert ctx.coins(SELLER) == 38_000, "seller paid ONCE"
    assert ctx.coins("treasury:vtech") == 1_400, "commission routed ONCE"
    assert ctx.coins(ctx.esc.TREASURY) == 600, "the 30% nobody claimed stays put"
    with ctx.lv2._tx() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT state FROM split_runs WHERE trigger_row_id=?", (str(lid),))]
    assert [r["state"] for r in rows] == ["applied"], rows
    assert ctx.db.get_land_listing(lid)["settle_stage"] == "done"


def test_settle_refuses_to_pay_a_hammer_nobody_paid(ctx):
    """The §2.1 guard: no capture -> no payout, and the lot is not `sold`."""
    ctx.credit(WINNER, 50_000)
    ctx.credit(SELLER, 0)
    lid = ctx.listing()
    # A listing that claims a bid with no escrow row behind it — exactly what a
    # pre-escrow lot looks like after the code lands and before P4 converts it.
    ctx.db.update_land_listing(lid, current_bid=40_000.0, current_bidder=WINNER)

    res = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    assert res["ok"] is False and res["outcome"] == "failed_escrow", res
    assert ctx.coins(SELLER) == 0, "nobody is paid out of nothing"
    assert ctx.db.get_land_listing(lid)["status"] == "failed_escrow"


def test_cancel_releases_the_held_bid_and_cannot_double_release(ctx):
    """PROOF for cancel: `available` fully restored, one release, replay-safe."""
    ctx.credit(LOSER_A, 50_000)
    lid = ctx.listing()
    row = ctx.bid(lid, LOSER_A, 12_000)
    assert ctx.lv2.get_balance(LOSER_A)["available"] == 38_000

    res = ctx.st.cancel_listing(lid, reason="seller cancelled")
    assert res["outcome"] == "cancelled", res
    assert ctx.lv2.get_balance(LOSER_A)["available"] == 50_000
    assert ctx.coins(LOSER_A) == 50_000, "a release moves NO coins"
    assert ctx.holds(LOSER_A)[0]["state"] == "released", "released, not expired"
    assert res["fee_refunded"] is False

    # Three mechanisms, each on its own: the row claim refuses, core's own
    # `state='open'` refuses, and the release key would replay. Drive the row
    # helper directly to prove the second release cannot land.
    before = len(ctx.entries("release"))
    landed = ctx.st.release_row(ctx.esc.bid_row(int(row["id"])), "again")
    assert landed in ("released", "release_unknown"), landed
    assert len(ctx.entries("release")) == before, "no second release entry"
    assert ctx.lv2.get_balance(LOSER_A)["available"] == 50_000


def test_listing_fee_reaches_the_treasury_and_is_not_refunded(ctx):
    """PROOF for the fee: the audit's 'deducted and credited to nobody'."""
    ctx.credit(SELLER, 100_000)
    ctx.db.set_config("realestate:listing_fee", "2500")
    import cogs.land_exchange as lx
    out = lx.create_listing_core(SELLER, "Fee plot", 5000)
    assert out["ok"], out
    lid = int(out["listing"]["id"])
    assert ctx.coins(SELLER) == 97_500
    assert ctx.coins(ctx.esc.TREASURY) == 2_500, "the fee reached a real account"
    assert ctx.db.get_land_listing(lid)["fee_stage"] == "paid"
    assert ctx.db.get_land_listing(lid)["status"] == "active"

    # Cancel does NOT hand it back — a refundable listing fee is a free option.
    ctx.st.cancel_listing(lid)
    assert ctx.coins(SELLER) == 97_500
    assert ctx.coins(ctx.esc.TREASURY) == 2_500

    # A seller who cannot afford it never gets a listing on the board.
    ctx.credit(LOSER_B, 100)
    refused = lx.create_listing_core(LOSER_B, "Too poor", 5000)
    assert refused["ok"] is False and refused["error_code"] == "insufficient", refused
    assert "2,500" in refused["error"], "a refusal must carry FIGURES"
    assert ctx.db.get_land_listing(refused["listing_id"])["status"] == "cancelled"
    assert ctx.coins(LOSER_B) == 100


def test_rent_charges_once_across_three_retries(ctx):
    """PROOF 4. One period, three sweeps, one charge."""
    ctx.credit(TENANT, 100_000)
    ctx.credit(LANDLORD, 0)
    ctx.db.set_config("realestate:rent_enabled", "1")
    lease_id = ctx.db.create_land_lease("parcel-42", TENANT, LANDLORD, 7_500,
                                        next_due_at="2000-01-01 00:00:00")
    lease = ctx.db.get_land_lease(lease_id)
    period = ctx.st.rent_period(lease)
    assert ctx.st.rent_key("parcel-42", period) == f"land:parcel:parcel-42:rent:{period}"

    first = ctx.st.charge_rent(lease)
    assert first["outcome"] == "paid", first
    for _ in range(3):
        again = ctx.st.charge_rent(ctx.db.get_land_lease(lease_id))
        assert again["outcome"] in ("already_paid", "not_ours:paid"), again
    assert ctx.coins(TENANT) == 92_500, "charged exactly once"
    assert ctx.coins(LANDLORD) == 7_500
    assert len([e for e in ctx.entries("transfer_out")]) == 1

    # And the whole sweep, three times, is also once.
    for _ in range(3):
        ctx.st.sweep_rent()
    assert ctx.coins(TENANT) == 92_500

    # The three mechanisms, checked individually rather than assumed:
    with ctx.db.db() as conn:
        rows = conn.execute("SELECT COUNT(*) c FROM land_rent_charges "
                            "WHERE parcel_id='parcel-42'").fetchone()
    assert rows["c"] == 1, "the UNIQUE (parcel, period) index refuses a second row"
    assert ctx.idem(ctx.st.rent_key("parcel-42", period))["state"] == "done"


def test_rent_advances_the_period_so_next_month_is_a_different_key(ctx):
    """A charge that could not be repeated but also never advanced would be a
    tenant who pays once and lives free — assert the marker moves."""
    ctx.credit(TENANT, 100_000)
    ctx.db.set_config("realestate:rent_enabled", "1")
    lease_id = ctx.db.create_land_lease("parcel-7", TENANT, LANDLORD, 1_000,
                                        next_due_at="2000-01-01 00:00:00")
    ctx.st.charge_rent(ctx.db.get_land_lease(lease_id))
    lease = ctx.db.get_land_lease(lease_id)
    assert lease["last_period"], "the paid period is recorded on the lease"
    assert lease["next_due_at"] > "2000-01-01", lease["next_due_at"]


def test_yaml_fallback_cannot_swallow_the_escrow_trigger(ctx):
    """PROOF 5. The audit's finding: the guard defeated by its own error handler.

    A wallet with a live hold is asked, through the SHIPPED `deduct_coins`, to
    spend the reserved coins. The trigger aborts it. Before the narrowing, that
    `sqlite3.IntegrityError` landed in `except Exception` and the handler rewrote
    the whole balances table from YAML — moving the coins with SQLite, and
    therefore the trigger, bypassed. Now it propagates, and the marker that the
    fallback ran stays empty.
    """
    ctx.credit(LOSER_A, 10_000)
    lid = ctx.listing()
    ctx.bid(lid, LOSER_A, 10_000)          # available is now 0
    land_stubs.YAML_FALLBACK_HITS.clear()

    core = sys.modules["Restocker_main"]
    raised = None
    try:
        core.deduct_coins(int(LOSER_A), 10_000, reason="shop purchase")
    except sqlite3.IntegrityError as e:
        raised = e
    assert raised is not None, "the escrow trigger's abort must reach the caller"
    assert "hold" in str(raised).lower(), str(raised)
    assert land_stubs.YAML_FALLBACK_HITS == [], \
        "the YAML whole-table rewrite ran — the trigger was bypassed"
    assert ctx.coins(LOSER_A) == 10_000, "the balance is byte-identical after the abort"
    # A debit that stays ABOVE the hold floor is unaffected — the guard is not a
    # freeze on the wallet.
    ctx.credit(LOSER_B, 10_000)
    core.deduct_coins(int(LOSER_B), 3_000, reason="shop purchase")
    assert ctx.coins(LOSER_B) == 7_000


def test_commission_split_is_integer_and_conserves(ctx):
    """§6.3: floored basis points, the crumb goes to the seller, no leak."""
    lid = ctx.listing(commission_pct=5.0)
    listing = ctx.db.get_land_listing(lid)
    for price in (1, 7, 999, 1000, 8_500_000, 12_345_679):
        split = ctx.st.commission_split(price, listing)
        assert isinstance(split["commission"], int) and isinstance(split["net"], int)
        assert split["commission"] + split["net"] == price, price
        assert split["commission"] >= 0 and split["net"] >= 0


def test_expired_unsold_releases_a_stray_hold(ctx):
    ctx.credit(LOSER_A, 20_000)
    lid = ctx.listing()
    ctx.bid(lid, LOSER_A, 5_000)           # a hold with no current_bidder: a bug
    res = ctx.st.expire_unsold(lid)
    assert res["outcome"] == "expired", res
    assert ctx.lv2.get_balance(LOSER_A)["available"] == 20_000
    assert ctx.db.get_land_listing(lid)["status"] == "expired"


def test_settle_asks_rather_than_guessing_when_a_row_is_in_doubt(ctx):
    """A lot with a `capture_unknown` row is never settled on a GUESS.

    Two halves, and the invariant is only real if BOTH hold:

      B. core unreachable — the ask fails, the doubt stands, and the settle
         WAITS. `in_doubt`, seller unpaid, lot handed back `active`. This is
         this test's original assertion, kept verbatim, under the condition
         where it is still the right answer.
      A. core reachable — the settle ASKS (`get(hold_id)`, a pure read carrying
         no idempotency key) and proceeds on the ANSWER. Here core never
         captured the stray, so the doubt resolves to "not captured", the stray
         bidder is made whole and the sale completes.

    Renamed from `..._waits_rather_than_guessing`: waiting was the whole
    behaviour until 15 Aug, and it was inert. `reconcile_holds` owns
    `capture_unknown` but is age-guarded at 15 minutes, so a lot whose deadline
    fell inside that window was un-settleable AND un-unwindable for a quarter of
    an hour, with a manager's close refusing and nothing to unblock it. The
    guard is right for the bulk sweep (it batches a per-row ledger call across
    the whole DB) and wrong at a decision point. Asking early can only get an
    answer early — which is exactly why it is safe to ask here and NOT safe to
    replay a `place_hold` here.
    """
    ctx.credit(WINNER, 50_000)
    ctx.credit(LOSER_A, 50_000)
    ctx.credit(SELLER, 0)

    # ── B. the ask cannot be answered: WAIT, and be handed back ──────────
    lid = ctx.listing()
    stray = ctx.bid(lid, LOSER_A, 9_000)
    ctx.esc.claim_capture(int(stray["id"]))
    ctx.esc.unclaim_capture(int(stray["id"]), "lost the answer", outcome_known=False)
    ctx.top_bid(lid, WINNER, 40_000)
    assert ctx.esc.bid_row(int(stray["id"]))["status"] == "capture_unknown"

    real = ctx.esc.ledger()
    ctx.esc.set_ledger(FaultLedger(real, "get"))
    try:
        res = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    finally:
        ctx.esc.set_ledger(real)
    assert res["outcome"] == "in_doubt", res
    assert ctx.coins(SELLER) == 0
    assert ctx.db.get_land_listing(lid)["status"] == "active", "handed back, not failed"
    # Nothing was guessed in EITHER direction while the question was open.
    assert ctx.esc.bid_row(int(stray["id"]))["status"] == "capture_unknown", \
        "an unanswerable ask must leave the row in doubt, not resolve it optimistically"
    assert len(ctx.entries("capture")) == 0, "no capture on a lot still in doubt"

    # ── A. the ask IS answered: resolve, then settle on the answer ───────
    res2 = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    assert res2["outcome"] == "sold", res2
    assert ctx.esc.bid_row(int(stray["id"]))["status"] != "capture_unknown", \
        "the settle must RESOLVE the doubt by asking, not settle around it"
    # Core never captured the stray, so its owner is whole — the answer, not a
    # guess in the convenient direction.
    assert ctx.coins(LOSER_A) == 50_000, ctx.coins(LOSER_A)
    assert ctx.lv2.get_balance(LOSER_A)["available"] == 50_000
    assert ctx.coins(SELLER) == 38_000, ctx.coins(SELLER)
    assert ctx.coins(WINNER) == 10_000, ctx.coins(WINNER)
    assert len(ctx.entries("capture")) == 1, "exactly one capture, the winner's"


def test_instant_buy_reserves_then_settles_in_one_call(ctx):
    ctx.credit(WINNER, 100_000)
    ctx.credit(SELLER, 0)
    import cogs.land_exchange as lx
    lid = ctx.listing(price=30_000)
    res = lx._instant_buy_core(lid, WINNER)
    assert res.get("outcome") == "sold", res
    assert ctx.coins(WINNER) == 70_000
    assert ctx.coins(SELLER) == 28_500          # 5% of 30,000
    assert ctx.coins(ctx.esc.TREASURY) == 1_500
    assert ctx.db.get_land_listing(lid)["status"] == "sold"
    # A second click cannot buy it twice — the lot is terminal.
    again = lx._instant_buy_core(lid, WINNER)
    assert again["ok"] is False
    assert ctx.coins(WINNER) == 70_000


def test_instant_buy_that_cannot_settle_gives_the_reservation_back(ctx):
    """The deleted compensating refund: an aborted buy releases, never refunds."""
    ctx.credit(WINNER, 100_000)
    import cogs.land_exchange as lx
    lid = ctx.listing(price=30_000)
    real = ctx.esc.ledger()
    # A DEFINITE refusal of the capture: core looked and said no, so the row goes
    # back to `held` and nothing moved. That is the only shape in which releasing
    # the buyer's reservation is provably safe, and it is the shape the old
    # compensating `add_coins` refund could not tell apart from the others.
    refusal = ctx.lv2.LedgerError("insufficient", 409, "nope")
    ctx.esc.set_ledger(FaultLedger(real, "capture", exc=refusal))
    try:
        res = lx._instant_buy_core(lid, WINNER)
    finally:
        ctx.esc.set_ledger(real)
    assert res.get("ok") is not True, res
    assert ctx.coins(WINNER) == 100_000, "no coins ever left the buyer"
    assert ctx.lv2.get_balance(WINNER)["available"] == 100_000, \
        "and the reservation was handed back, not left to expire"
    assert ctx.coins(SELLER) == 0
    assert ctx.db.get_land_listing(lid)["status"] == "active", "handed back for retry"


def test_instant_buy_refuses_a_lot_past_its_deadline_and_reserves_nothing(ctx):
    """The guard `_place_bid_core` has always carried, now on the buy path too.

    Why it matters more than it looks: a lot past `ends_at` used to sit `active`
    for at most the 60 seconds until the sweep, so buying one merely raced the
    close. The `capture_unknown` block removed that bound — a lot whose escrow
    core cannot answer for is deliberately held `active` past its deadline for as
    long as core stays dark. A SECOND player clicking Buy there was told
    `ok: True` on a lot that already had a paid-for buyer, and their coins were
    reserved for the length of the wedge.

    Both halves are asserted: the refusal, and that it costs nothing. A guard
    placed after `create_bid_row`/`hold` would return `ok: False` and still have
    reserved the coins, which is the whole finding.
    """
    ctx.credit(WINNER, 100_000)
    import cogs.land_exchange as lx
    lid = ctx.listing(price=30_000, ends_at="2000-01-01 00:00:00")
    bid = lx._place_bid_core(lid, WINNER, 40_000)
    assert bid.get("ok") is False, bid
    res = lx._instant_buy_core(lid, WINNER)
    assert res.get("ok") is False, res
    assert res.get("error_code") == "listing_ended", res
    assert "ended" in str(res.get("error", "")).lower(), res
    # Nothing was written and nothing was reserved.
    assert ctx.coins(WINNER) == 100_000
    assert ctx.lv2.get_balance(WINNER)["available"] == 100_000, \
        "the refusal must happen ABOVE create_bid_row, not after the hold"
    assert ctx.esc.rows_in(lid, ctx.esc.OPEN_HOLD_STATUSES) == [], \
        "a refused buy leaves no escrow row behind"
    assert ctx.db.get_land_listing(lid)["status"] == "active"
    # And an open lot is still buyable — the guard is the deadline, not the path.
    live = ctx.listing(price=30_000)
    assert lx._instant_buy_core(live, WINNER).get("outcome") == "sold"


def test_a_blocked_settle_does_not_overwrite_a_staff_rollback(ctx):
    """The THIRD terminal write, under the claim like the other two.

    `_flip_terminal` and the `sold` flip both carry `_if_status=SETTLING`;
    `settle_listing`'s `SettleBlocked` handler wrote `failed_escrow`
    unconditionally, so a staff `rolled_back` written while the settle held the
    claim was silently replaced and its `closed_at` overwritten. No coins move
    either way — both statuses are terminal and the holds are released on both
    paths — which is exactly why it went unseen. It is audit truth: the row said
    a lot failed its escrow when what happened is that a human ended it.
    """
    ctx.credit(WINNER, 100_000)
    ctx.credit(SELLER, 0)
    lid = ctx.listing()
    ctx.top_bid(lid, WINNER, 40_000)
    before = ctx.coins(WINNER) + ctx.coins(SELLER) + ctx.coins(ctx.esc.TREASURY)
    orig = ctx.st._settle_claimed

    def blocked_under_a_rollback(listing_id, *a, **kw):
        # A staff rollback lands while we hold the claim, then the escrow blocks.
        ctx.st._settle_claimed = orig
        ctx.db.update_land_listing(listing_id, status="rolled_back",
                                   closed_at="2001-02-03 04:05:06")
        raise ctx.st.SettleBlocked(ctx.st.FAILED_ESCROW, "escrow went away")

    ctx.st._settle_claimed = blocked_under_a_rollback
    try:
        res = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    finally:
        ctx.st._settle_claimed = orig
    row = ctx.db.get_land_listing(lid)
    assert row["status"] == "rolled_back", \
        f"the staff rollback was overwritten with {row['status']!r}"
    assert row["closed_at"] == "2001-02-03 04:05:06", \
        "the staff rollback's closed_at was overwritten too"
    # The caller is told the lot is CLOSED, not that its escrow failed.
    assert res.get("outcome") != "failed_escrow", res
    assert res.get("outcome") == ctx.st.ALREADY_CLOSED, res
    # And it is still audit truth about money: nothing moved.
    assert ctx.coins(WINNER) + ctx.coins(SELLER) + ctx.coins(ctx.esc.TREASURY) == before
    assert ctx.coins(SELLER) == 0


def test_a_blocked_settle_still_writes_failed_escrow_when_it_owns_the_row(ctx):
    """The control for the test above: the claim must not refuse the normal case.

    A conditional write that never wins is a silent no-op, and this is the pair
    that tells the two apart — same blocked settle, nobody racing it, the lot
    must still end `failed_escrow` with the refusal reported as one.
    """
    ctx.credit(WINNER, 100_000)
    lid = ctx.listing()
    ctx.top_bid(lid, WINNER, 40_000)
    orig = ctx.st._settle_claimed

    def just_blocked(listing_id, *a, **kw):
        ctx.st._settle_claimed = orig
        raise ctx.st.SettleBlocked(ctx.st.FAILED_ESCROW, "escrow went away")

    ctx.st._settle_claimed = just_blocked
    try:
        res = ctx.st.settle_listing(lid, buyer_id=WINNER, price=40_000)
    finally:
        ctx.st._settle_claimed = orig
    assert ctx.db.get_land_listing(lid)["status"] == "failed_escrow", res
    assert res.get("ok") is False and res.get("outcome") == "failed_escrow", res



# ── runner ────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for fn in tests:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = Ctx(tmp)
            try:
                fn(ctx)
                print(f"  PASS  {fn.__name__}")
            except Exception as e:  # noqa: BLE001
                import traceback
                failures.append(fn.__name__)
                print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
                traceback.print_exc()
            finally:
                ctx.close()
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
