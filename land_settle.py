"""land_settle.py — closing, settling, the listing fee, cancel and rent, on escrow.

WHAT THIS OWNS. Everything that happens to money AFTER a hold exists:

    close    capture the winning hold into `treasury:estates`, release every
             losing hold, one row at a time, each marked before the next call
    settle   pay the seller out of the treasury, net of commission, under one
             caller-minted key, with its own progress marker so a half-finished
             settle resumes exactly where it stopped
    fee      the listing fee, which the audit found was deducted from the seller
             and credited to NOBODY — `_credit_platform_balance` was never called
             for it and no cancel path returned it. It now moves seller ->
             treasury under `land:listing:<id>:fee` or the listing never opens
    cancel   release the standing hold and close the lot, with three independent
             things stopping a double release
    rent     a scheduled tenant -> owner transfer keyed by PERIOD, so a retry
             cannot charge two months

`land_escrow.py` owns the bid ROW state machine and the ledger adapter; this
module owns the ORDER those calls happen in and the listing-level progress. The
split is deliberate: a row can be captured or released without anybody deciding
a lot has closed, and a lot closing is a decision about six rows and a listing.

WHY THE ORDER IS THE DESIGN (LAND_ESCROW_PLAN §2.1)
--------------------------------------------------
  1. CLAIM the listing (`active -> settling`) in one atomic UPDATE. Money moves
     only if that UPDATE won the row. This single statement is what stopped the
     8.5M-per-minute mint: `auction_sweep_loop` re-runs the same candidate set
     every 60 seconds, and one `database is locked` on the old final UPDATE
     re-paid the seller a minute later, forever.
  2. CAPTURE the winning hold into the treasury, and mark the row `captured`
     before anything else happens.
  3. RELEASE every other open hold on the lot, one at a time, marking each row
     `released` before the next call. There should be at most zero of these
     under the outbid-releases model; the loop exists because "should be" is not
     a guarantee.
  4. PAY the seller from the treasury, and refuse to unless the winning row is
     `captured` AND its reserved integer equals the price being settled. That
     guard is ported from `estates_db.build_auction_settle_run`: a crash
     mid-capture once let a runner-up be promoted while the real winner's coins
     sat in limbo, and the seller was paid out of a hammer nobody had paid.
  5. FLIP `status='sold'` last, and only after 4 succeeded.

Steps 2-4 each write a progress marker before and after, so a process death
anywhere leaves a row saying exactly which step was in flight. A resume re-sends
the IDENTICAL key for that step; core replays its own stored answer instead of
moving coins again. That is the whole recovery story, and it is why the keys are
minted from the `land_bids` row id at row-creation time rather than computed at
the moment of the call.

THE COIN-SUPPLY CHANGE THE OWNER MUST AGREE TO (§2.1, and it is not optional)
----------------------------------------------------------------------------
Today the commission is DESTROYED: `_credit_platform_balance` writes a scalar
store and a YAML mirror, not a `balances` row, so commission coins leave the
buyer and enter no wallet. Under escrow the full price is captured into
`treasury:estates` and only `net` leaves it, so the commission becomes REAL COINS
IN A REAL ACCOUNT and the money supply stops shrinking on every sale.
`_credit_platform_balance` is still called, for REPORTING only, and the two
figures are expected to agree. If the old burn is wanted back it needs
`wallet.mint`, which `estates` deliberately does not have and should not get.

WHAT THIS MODULE MUST NEVER DO. Call `Restocker_main.add_coins` or
`deduct_coins`. Both wrap their SQLite path in `except Exception` and fall
through to a whole-table YAML rewrite that bypasses SQLite entirely — and
therefore bypasses `ledger_balances_respect_holds`, the trigger installed to
constrain them. Escrow enforced by a trigger that the error handler of the
constrained function can walk past is not enforced. Land's money paths do not
reach those two functions any more; `Restocker_main` narrows the handler as well
(LAND_ESCROW_PLAN P0 item 3), because land is not their only caller.
"""
from __future__ import annotations

import logging
import math
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import Restocker_db as _db
import land_escrow as esc

log = logging.getLogger("restocker.land_settle")

#: Transient claim state on `land_listings.status`. Not `active`, so NEITHER of
#: the minute loop's two selects returns it — `get_expired_active_listings()` for
#: lots past their deadline, `get_part_settled_active_listings()` for lots whose
#: escrow says somebody has already paid — and the sweep cannot enter a
#: settlement somebody else is already inside by either route.
SETTLING = "settling"

#: A lot whose escrow could not be captured. Terminal, and deliberately NOT
#: `sold`: awarding a lot to someone who cannot pay and then paying the seller
#: out of a treasury that never received the money is the exact failure
#: `estates_db.build_auction_settle_run` was rewritten to prevent. The seller
#: relists; nobody is paid out of nothing.
FAILED_ESCROW = "failed_escrow"

#: A settlement is synchronous and sub-second, so a claim older than this is a
#: dead process, not a slow one. Re-arming is safe because every step inside the
#: claim is separately keyed: the retry replays whatever already applied.
STALE_CLAIM_MINUTES = 10

#: The settle ladder, in order. Progress is a POSITION in this list, which is why
#: it is a tuple and not a set — "have we got past the capture?" has to be
#: answerable without knowing which step is running.
STAGES = ("claimed", "captured", "losers_released", "paying_seller",
          "seller_paid", "done")


def _stage_index(stage: Optional[str]) -> int:
    """-1 for a listing that has not started settling, else its position."""
    s = str(stage or "")
    return STAGES.index(s) if s in STAGES else -1


def _reached(stage: Optional[str], name: str) -> bool:
    return _stage_index(stage) >= STAGES.index(name)


def _core() -> Any:
    """`Restocker_main`, or None under test. Used ONLY for reporting side effects.

    Nothing this module needs for correctness lives there. It is fetched from
    `sys.modules` rather than imported so that importing this file does not drag
    in the bot, and so a test can exercise the whole settlement without one.
    """
    return sys.modules.get("Restocker_main")


def _utcnow_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _policy():
    """The cog's config helpers, imported at CALL time to break the import cycle.

    `cogs.land_exchange` imports this module at module scope; this module needs
    the cog's `DEF` defaults, `_gd` and the loyalty tier table. Importing them
    here rather than duplicating them is the point — a second copy of the
    commission defaults is a second answer to "what does this listing cost", and
    the audit found three of those already.
    """
    import cogs.land_exchange as _lx
    return _lx


def _coin_amount(v) -> Optional[int]:
    """A FINITE, POSITIVE WHOLE number of coins, or None when the value is not money.

    The settle twin of `cogs.land_exchange._coin_amount`, kept local because that
    module imports this one — `_policy()`'s cycle break exists for exactly this
    reason and a money guard must not depend on the cog being importable.

    NaN reaches here from the same place it reaches the bid path: `json.loads`
    accepts a bare `NaN` token off the satellite relay and every comparison
    against NaN is False, so `price_i <= 0` did not reject it either. What did
    reject it was `int(round(float(price)))` RAISING — `ValueError` on NaN,
    `OverflowError` on `+inf` — one line past the guard, inside the claim, which
    is a crash where the hotfix (H3h) promised a refusal. Callers turn a None
    from here into a normal refusal instead.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return int(round(f))


# ══════════════════════════════════════════════════════════════════════════
# The listing claim — one atomic UPDATE, and the recovery that goes with it
# ══════════════════════════════════════════════════════════════════════════

def claim_listing_for_settlement(listing_id: int) -> bool:
    """`active -> settling` in one statement. True only if WE won the row.

    `update_land_listing` has no WHERE clause beyond the id, so the conditional
    claim is written here against the same `db()` helper it uses — same
    connection, same pragmas, no new plumbing.
    """
    with _db.db() as conn:
        cur = conn.execute(
            "UPDATE land_listings SET status=?, settling_at=datetime('now'), "
            "updated_at=datetime('now') WHERE id=? AND status='active'",
            (SETTLING, int(listing_id)))
        return cur.rowcount == 1


#: Outcome word for "the claim was refused because the lot is already over".
#: Distinct from `already_settling`, which means somebody is driving it RIGHT NOW
#: and it will finish. Both are `{"ok": True}` and neither is an error — but a
#: manager told "a settlement is already in flight" about a lot that was rolled
#: back an hour ago waits for something that is never coming.
#:
#: ANY CALLER THAT SPECIAL-CASES `already_settling` MUST ALSO CASE THIS ONE.
#: There are two today and both do: `Restocker_web.py:4655` (the partner-channel
#: notice, which would otherwise announce "🔨 closed") and
#: `cogs/land_exchange.py:1412`.
ALREADY_CLOSED = "already_closed"


def _claim_refused(listing_id: int) -> dict:
    """Why `claim_listing_for_settlement` said no, in the RIGHT word.

    The claim is `WHERE status='active'`, so it refuses on `settling` AND on
    every terminal status alike, and every call site reported all of them as
    `already_settling`. A rolled-back lot came back "a settlement is already in
    flight" — a safe outcome described as a transient one, which is the version
    of wrong that makes an operator wait instead of act.

    Re-reading is a second read and therefore racy: the status can move again
    between the failed UPDATE and this SELECT. It fails in the safe direction —
    a lot that has gone back to `active` reads as `already_settling`, i.e. "try
    again", which is what you want, and a lot cannot go from terminal back to
    live (`_settle_gate`).
    """
    now = _db.get_land_listing(int(listing_id)) or {}
    st = str(now.get("status") or "")
    if st in esc.TERMINAL_LISTING_STATUSES:
        return {"ok": True, "outcome": ALREADY_CLOSED, "listing_id": int(listing_id),
                "status": st}
    return {"ok": True, "outcome": "already_settling", "listing_id": int(listing_id),
            "status": st}


def release_listing_claim(listing_id: int) -> bool:
    """Put a claimed-but-unfinished listing back to `active` so the sweep retries.

    This is what makes a failed settlement RECOVERABLE rather than silently
    unpaid. It is safe only because `settle_stage` survives the release: the
    retry re-reads how far the last attempt got and re-sends the same keys for
    the steps it cannot prove landed, so nothing is paid twice and nothing is
    skipped.
    """
    try:
        with _db.db() as conn:
            cur = conn.execute(
                "UPDATE land_listings SET status='active', updated_at=datetime('now') "
                "WHERE id=? AND status=?", (int(listing_id), SETTLING))
            return cur.rowcount == 1
    except Exception as e:  # noqa: BLE001
        # Worst case the row stays `settling`. Nobody is double-paid — that was
        # the point — but nobody is paid either until it is re-armed, which
        # `rearm_stale_claims` does on the next sweep. This line is how a human
        # forces it if the sweep is not running.
        log.error("[land_settle] STUCK SETTLEMENT CLAIM on #%s — release failed: %s. "
                  "No coins were double-paid. It re-arms itself within %s minutes; "
                  "to force it: UPDATE land_listings SET status='active' WHERE id=%s "
                  "AND status='settling';", listing_id, e, STALE_CLAIM_MINUTES, listing_id)
        return False


def rearm_stale_claims() -> int:
    """Re-arm settlement claims that were never released. The recovery half.

    A claimed listing is invisible to BOTH of the sweep's selects — it is not
    `active` — so a process death between the claim and the release would strand
    it forever, on either side of the deadline.
    Re-entry is safe because every money step inside the claim is keyed and every
    row carries its own status.
    """
    try:
        with _db.db() as conn:
            cur = conn.execute(
                "UPDATE land_listings SET status='active', updated_at=datetime('now') "
                "WHERE status=? AND settling_at IS NOT NULL "
                "AND settling_at <= datetime('now', ?)",
                (SETTLING, f"-{int(STALE_CLAIM_MINUTES)} minutes"))
            n = int(cur.rowcount or 0)
        if n:
            log.warning("[land_settle] re-armed %s stale settlement claim(s)", n)
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("[land_settle] stale-claim re-arm failed: %s", e)
        return 0


# ══════════════════════════════════════════════════════════════════════════
# Releasing holds — the shared body of "outbid", "cancelled" and "lost the lot"
# ══════════════════════════════════════════════════════════════════════════

def release_row(row: dict, reason: str) -> str:
    """Release ONE hold and return the status the row landed in.

    A thin ordering wrapper over `land_escrow.release`, which owns the claim, the
    ledger call and the marker. What is added here is the one thing the row state
    machine cannot decide for itself: what to do when core answers
    `hold_not_open`.

    WHAT STOPS A DOUBLE RELEASE — three independent mechanisms, because a cancel
    can race the minute sweep and a manager can race both:

      1. `land_escrow.release` claims the row (`held -> releasing`) in one
         conditional UPDATE. The loser gets `claim_lost` and never calls core.
      2. `release_hold`'s own UPDATE carries `AND state='open'`, so a second
         release matches no row at core and raises `hold_not_open` — the hold's
         state machine is once-only regardless of what any caller believes.
      3. The release key is ONE key per bid, not one per reason. Outbid-release,
         cancel-release and expiry-release of bid 7 are the same money event, so
         a cancel racing the sweeper REPLAYS rather than conflicting. Distinct
         keys per reason would have been a double-release bug wearing a
         bookkeeping costume.

    `hold_not_open` is not treated as an error: it means core has already
    terminated this hold, possibly by CAPTURING it, and only core can say which —
    so the row is reconciled from `get_hold`, never guessed.
    """
    row_id = int(row["id"])
    hold_id = str(row.get("hold_id") or "")
    if str(row.get("status") or "") == "releasing":
        # A previous attempt already owns the claim — either this process died
        # mid-release, or `promote_top_bid` marked it in the same transaction
        # that recorded the bid which displaced it. Re-claiming would fail and
        # strand a hold whose owner has already been outbid on the board, so
        # re-drive the identical key instead: core replays or performs it.
        pass
    elif esc.claim_release(row_id, reason) is None:
        # Lost the race, or the row is already terminal. Both are fine and
        # neither is ours to fix: report what it actually is.
        return str((esc.bid_row(row_id) or {}).get("status") or "missing")
    if not hold_id:
        # A releasing row with no hold id never reserved anything, so marking it
        # released would be recording a release that did not happen.
        return esc.unclaim_release(row_id, "no hold_id on a releasable row",
                                   outcome_known=True)
    key = esc.release_key(row["listing_id"], row.get("kind") or "bid", row_id)
    try:
        esc.ledger().release(hold_id, key, reason=reason)
    except Exception as e:  # noqa: BLE001
        code = esc.ledger().error_code(e)
        if code == "hold_not_open":
            esc.unclaim_release(row_id, f"hold_not_open: {e}", outcome_known=False)
            return _reconcile_from_core(row_id, hold_id, e)
        return esc.unclaim_release(row_id, f"{code or type(e).__name__}: {e}",
                                   outcome_known=esc.outcome_known_for(code))
    esc.mark_released(row_id)
    return "released"


def _reconcile_from_core(row_id: int, hold_id: str, detail) -> str:
    """Core says the hold is not open. Ask what it IS and write that down.

    The one thing that must not happen here is a guess. A hold that is
    `captured` and a hold that is `released` demand opposite handling — one means
    the coins are in the treasury and the lot can settle, the other means the
    bidder has them back and the lot cannot. `get_hold` is a read; asking is
    cheap and being wrong is not.

    Nothing here forces a status: `land_escrow.release` / `.capture` have already
    parked the row in `*_unknown`, which is the correct resting place for "core
    knows and we do not". This either upgrades that to the real answer or leaves
    it exactly where it is for the reconcile sweep to try again.
    """
    if not hold_id:
        return str((esc.bid_row(row_id) or {}).get("status") or "unknown")
    try:
        state = str(esc.ledger().get(hold_id).get("state") or "")
    except Exception as e:  # noqa: BLE001
        log.error("[land_settle] row %s: hold %s is not open and get_hold failed "
                  "(%s). Leaving it in doubt for the reconcile sweep rather than "
                  "guessing. Original: %s", row_id, hold_id, e, detail)
        return str((esc.bid_row(row_id) or {}).get("status") or "unknown")
    try:
        return esc.reconcile_hold(row_id, state)
    except ValueError as e:
        log.error("[land_settle] row %s: core says hold %s is %r and the row cannot "
                  "record it (%s). Original: %s", row_id, hold_id, state, e, detail)
        return str((esc.bid_row(row_id) or {}).get("status") or "unknown")


def release_all_holds(listing_id: int, reason: str) -> dict:
    """Release every open hold on a listing, PER ROW, marking each before the next.

    Rule 2. The loop deliberately re-reads nothing in bulk: each row is claimed,
    called and marked on its own, so an interruption anywhere leaves every row
    either fully released or untouched, and the next pass picks up exactly the
    ones that are left.

    Returns `released` / `problems` / `deferred`. `deferred` is the rows this
    function is not ALLOWED to touch — see the note at the bottom — and a caller
    that treats an empty `problems` as "this lot has no escrow left" is wrong
    unless `deferred` is empty too.

    WHO READS `deferred`, because for a while nobody did and that is how a field
    becomes decoration. It is carried by `cancel_listing` and `expire_unsold` in
    their return values, by `land_escrow.retire_listing_escrow` (which also logs
    it — a row still deferred after that sweep's placement pass is one whose
    placement is younger than the age guard), by `cancel_listing_core`, and it
    ends at a human: `/realestate cancel` tells the manager that N reservations
    could not be ended on that click and the sweep will end them. Before that
    chain existed the manager was told `released: []` on a lot that still had a
    row reserving somebody's coins.
    """
    done, problems = [], []
    # `releasing` rows first: they are a previous attempt's in-flight work and
    # re-sending their identical key is a replay, whereas a `held` row still has
    # to be claimed. Doing them in this order means a resumed sweep finishes what
    # it started before it starts anything new.
    pending = [r for r in esc.rows_in_doubt(listing_id) if str(r["status"]) == "releasing"]
    pending += esc.held_rows(listing_id)
    for row in pending:
        landed = release_row(row, reason)
        (done if landed == "released" else problems).append(
            {"row": int(row["id"]), "status": landed})
    # `placing` / `place_unknown` rows CANNOT be released here, and used to be
    # skipped in silence — which is half of F3. A placement in doubt has no hold
    # id, so there is nothing to name in a release; establishing whether the hold
    # exists means re-sending the identical `place_hold` key, and that call may
    # RESERVE the bidder's coins. Two reasons that must not happen on this path:
    #
    #   * this function runs inside a cancel, a close and an expiry — user-facing
    #     latency, holding the listing's settlement claim — and a fresh ledger
    #     write there is the wrong place for it;
    #   * safety needs an age guard nobody here has. A `placing` row may belong to
    #     a worker that is alive right now, and two callers re-sending one key at
    #     once is exactly the double-placement `LedgerV2InProcess` exists to stop.
    #
    # So they are DEFERRED, named in the return value and said out loud, and
    # `land_escrow.sweep_terminal_listing_holds` (§3.6 check 2) retires them
    # within the minute: it re-reads the listing, resolves the placement with the
    # 15-minute guard, and then releases. Deferred is not skipped — but it is only
    # not-skipped because that sweep exists, which is why the two ship together.
    deferred = [{"row": int(r["id"]), "status": str(r["status"])}
                for r in esc.rows_in(listing_id, esc.PLACEMENT_IN_DOUBT_STATUSES)]
    if deferred:
        log.warning("[land_settle] #%s: %s escrow row(s) with an unresolved placement "
                    "(%s) cannot be released here — they have no hold id yet. Their "
                    "coins may be reserved. land_escrow.sweep_terminal_listing_holds() "
                    "resolves and retires them once this lot reads terminal.",
                    listing_id, len(deferred),
                    ", ".join(f"row {d['row']} ({d['status']})" for d in deferred))
    return {"released": done, "problems": problems, "deferred": deferred}


# ══════════════════════════════════════════════════════════════════════════
# Commission — integer basis points, floored, seller keeps the crumb
# ══════════════════════════════════════════════════════════════════════════

def commission_split(price: int, listing: dict) -> dict:
    """Split an integer hammer price into (commission, net). Ported from §6.3.

    `(price * bps) // 10000` with the fee FLOORED, so the rounding crumb goes to
    the seller and `commission + net == price` by construction rather than by two
    `round()` calls agreeing. The audit's honest finding was that the current
    float arithmetic does not leak today; this port is about not depending on
    that continuing to be true through a column migration.

    Loyalty discounts the seller's commission by their tier, never below
    `loyalty_min_commission`.
    """
    lx = _policy()
    price_i = int(price)
    base_pct = float(listing.get("commission_pct") or lx.DEF["commission_pct"])
    try:
        loy = _db.get_loyalty(str(listing["seller_id"])) or {}
    except Exception:  # noqa: BLE001 — a missing loyalty row is not a settlement failure
        loy = {}
    disc_pct = lx._loyalty_discount_pct(_db, loy.get("total_earned", 0))
    min_pct = lx._gd(_db, "loyalty_min_commission", lx.DEF["loyalty_min_commission"])
    eff_pct = max(float(min_pct), base_pct - disc_pct) if disc_pct else base_pct
    bps = max(0, min(10000, int(round(eff_pct * 100))))
    commission = (price_i * bps) // 10000
    return {"commission": commission, "net": price_i - commission,
            "commission_pct": eff_pct, "commission_bps": bps,
            "loyalty_discount_pct": disc_pct}


# ══════════════════════════════════════════════════════════════════════════
# Close + settle
# ══════════════════════════════════════════════════════════════════════════

class SettleBlocked(Exception):
    """The lot cannot settle and it is not a transient failure.

    Carries the outcome the listing should be driven to. Raised rather than
    returned so that no caller can accidentally continue past it into a payout —
    the whole class of bug here is a settlement that kept going after the step
    that was supposed to stop it.
    """

    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.message = message


def settle_listing(listing_id: int, *, buyer_id, price, win_row_id: Optional[int] = None,
                   note_reason: str = "auction close") -> dict:
    """Close and settle one lot: capture, release, pay, mark sold. Resumable.

    The single settlement core. `/realestate close`, the minute sweep,
    `close_listing_core` and `_instant_buy_core` all end up here, which is what
    makes the guarantee in this docstring worth reading — there were three
    implementations of this and they had already diverged (one guarded
    `current_bid or 0`, the other raised `TypeError` on a NULL bid).

    RESUMABILITY, precisely. Every return is a committed state. If the process
    dies at any point, the listing is left either `settling` (re-armed within
    `STALE_CLAIM_MINUTES` and re-entered) or `active`, and `settle_stage` says
    which of the five steps was in flight. The resumed pass re-sends the same key
    for that step and core replays its stored answer. The seller cannot be paid
    twice because the transfer key is `land:listing:<id>:settle:seller` and it is
    claimed in the same transaction as the coins move.

    "Picked up by the next sweep" IS NOT FREE, and this docstring used to claim it
    was. Being re-entered is only half of it: the sweep also has to work out that
    the lot is a part-settled sale, and it cannot do that from `land_listings`
    alone, because an instant buy writes nothing to `current_bid`/`current_bidder`
    and a lot with neither reads as "ended with no bids". A verified round of this
    ended with the buyer's 2,000,000 in `treasury:estates`, the seller paid
    nothing and the lot marked `expired`. What makes the resume real is
    `resume_row()` — the escrow, not the board, decides whether a lot has a buyer
    — and `_resolve_winning_row` binding a resumed settle to the row it already
    captured. Callers that decide "sold or expired" must go through `resume_row`.

    Returns `{"ok": True, "outcome": …}`; `outcome` is one of `sold`,
    `already_settling` (somebody is driving it right now), `already_closed`
    (the lot is over — sold, expired, cancelled, rolled back — and nothing is
    pending), `in_doubt`, or raises `SettleBlocked` for `failed_escrow`.
    `already_settling` and `already_closed` are both `ok: True` and both mean
    "we did nothing"; they differ in whether the reader should wait.
    """
    listing = _db.get_land_listing(listing_id)
    if not listing:
        return {"ok": False, "error": "That listing doesn't exist."}
    if not esc.escrow_available():
        return {"ok": False, "error": paused_sentence()}
    # Validate the price BEFORE the claim. A NaN/inf/zero price is a caller bug,
    # not a broken lot: refusing here leaves the listing `active` and untouched,
    # where refusing one line later (inside the claim) either RAISED or drove a
    # perfectly good lot to terminal `failed_escrow`.
    if _coin_amount(price) is None:
        return {"ok": False, "error": "That sale price isn't a usable coin amount."}
    if not claim_listing_for_settlement(listing_id):
        # We are not the settler. Say so as a success: a lot that is already
        # being settled is not an error, and reporting it as one is what makes a
        # caller retry into the settlement it just lost the race to. `already_
        # closed` when the lot is over rather than in flight — same non-error,
        # opposite advice to the human reading it.
        return _claim_refused(listing_id)
    try:
        return _settle_claimed(listing_id, buyer_id, price, win_row_id, note_reason)
    except SettleBlocked as blocked:
        # CONDITIONAL, the third and last terminal write in this module — the same
        # claim `_flip_terminal` and the `sold` flip make, against the same hazard.
        # This was the one that was left unconditional, and it is the one that runs
        # on the failure path, so it is the one most likely to race: a staff
        # `rolled_back` written while we held the claim was silently overwritten
        # with `failed_escrow` and its `closed_at` replaced. NO COINS ARE INVOLVED
        # either way — both are terminal and `_settle_claimed` has already released
        # every hold on both paths — but the row then says a lot failed its escrow
        # when what actually happened is that a human ended it. That is audit
        # truth, and audit truth is the whole reason the other two are conditional.
        won = _db.update_land_listing(listing_id, _if_status=SETTLING,
                                      status=blocked.outcome,
                                      closed_at=_utcnow_sql()) == 1
        log.error("[land_settle] #%s -> %s: %s", listing_id, blocked.outcome, blocked.message)
        if not won:
            # Same rule as `_flip_terminal`: losing the row is NOT retried and NOT
            # an error. Whoever wrote the status that is there now also held the
            # claim, and the holds are released either way. Reported, loudly, and
            # left alone; `_claim_refused` reads the real status off the row and
            # picks the word from it, so the caller is told the lot is closed
            # rather than that its escrow failed.
            now = _db.get_land_listing(listing_id) or {}
            log.error("[land_settle] lot #%s: the escrow block above is real, but the "
                      "row left `%s` underneath us and now reads `%s` — NOT overwriting "
                      "it as `%s`. No coins are owed; the holds are released on both "
                      "paths and whoever wrote that status also held the claim.",
                      listing_id, SETTLING, now.get("status"), blocked.outcome)
            return _claim_refused(listing_id)
        return {"ok": False, "outcome": blocked.outcome, "listing_id": int(listing_id),
                "error": blocked.message}
    except Exception:
        # Hand the row back so the sweep retries it. Safe because of the keys:
        # whatever already applied replays instead of applying again.
        release_listing_claim(listing_id)
        raise


def _settle_claimed(listing_id: int, buyer_id, price, win_row_id, note_reason: str) -> dict:
    """The body of `settle_listing`, with the listing claim already held."""
    listing = _db.get_land_listing(listing_id)
    price_i = _coin_amount(price) or 0
    if price_i <= 0:
        raise SettleBlocked(FAILED_ESCROW,
                            f"Lot #{listing_id} has no valid hammer price to settle.")

    # A row whose coin location core has not confirmed makes every later step a
    # guess: its coins may already be in the treasury, or may be about to be
    # released underneath the sale. Stop, hand the lot back, let the reconciler
    # ask core. This is a WAIT, not a failure.
    #
    # ASK FIRST, so the wait is usually zero. `reconcile_holds` answers the same
    # question in the bulk sweep, but only for rows older than its 15-minute
    # window; a lot that reaches settlement inside that window would wait a
    # quarter of an hour with a buyer's coins already in the treasury. One
    # `get_hold` per doubtful row here turns that into the same tick. Failures
    # fall through to the block below — asking and not getting an answer is
    # exactly the state `in_doubt` describes.
    try:
        esc.reconcile_listing_doubt(int(listing_id))
    except Exception as e:  # noqa: BLE001
        log.warning("[land_settle] #%s: could not resolve escrow doubt before "
                    "settling: %s", listing_id, e)
    doubtful = [r for r in esc.rows_in_doubt(listing_id)
                if str(r["status"]) in esc.UNKNOWN_STATUSES]
    if doubtful:
        release_listing_claim(listing_id)
        return {"ok": True, "outcome": "in_doubt", "listing_id": int(listing_id),
                "rows": [int(r["id"]) for r in doubtful],
                "error": ("This lot has escrow whose outcome core has not confirmed "
                          "yet. It settles as soon as that is resolved — nothing is "
                          "lost and nobody has been charged twice.")}

    stage = listing.get("settle_stage")
    if _stage_index(stage) < 0:
        _db.claim_listing_stage(listing_id, None, "claimed")
        stage = "claimed"

    # ── 2. capture the winner ────────────────────────────────────────────
    win = _resolve_winning_row(listing, buyer_id, price_i, win_row_id, stage)
    if not _reached(stage, "captured"):
        _capture_winner(listing_id, win, price_i, note_reason)
        _db.claim_listing_stage(listing_id, stage, "captured")
        stage = "captured"
    win = esc.bid_row(int(win["id"])) or win

    # ── 3. release every loser, per row ──────────────────────────────────
    if not _reached(stage, "losers_released"):
        outcome = release_all_holds(listing_id, reason=f"lot #{listing_id} closed")
        for prob in outcome["problems"]:
            if int(prob["row"]) == int(win["id"]):
                continue
            log.warning("[land_settle] #%s: losing hold row %s ended %s, not released",
                        listing_id, prob["row"], prob["status"])
        _db.claim_listing_stage(listing_id, stage, "losers_released")
        stage = "losers_released"
    else:
        # RESUMED PAST THIS RUNG, and something is still reserving coins. The
        # marker records that the losers ALIVE AT THE TIME were released; it says
        # nothing about a row created afterwards, and one is created every time a
        # buyer clicks Buy again after an interrupted settle told them to try
        # later. Without this the second click's hold is released by nobody: the
        # settle skips this step, the instant-buy path only releases on failure,
        # and the lot goes `sold` with the buyer's coins reserved until the TTL.
        # Per row, marked before the next call, and never the winner — the row
        # that backs this sale is captured, not held, and releasing it here would
        # be the double-release the ladder exists to prevent.
        for row in esc.held_rows(listing_id, exclude_row_id=int(win["id"])):
            landed = release_row(row, reason=f"lot #{listing_id} closed")
            if landed != "released":
                log.warning("[land_settle] #%s: late hold row %s ended %s, not released",
                            listing_id, row["id"], landed)

    # ── 4. pay the seller ────────────────────────────────────────────────
    split = commission_split(price_i, listing)
    if not _reached(stage, "seller_paid"):
        # THE GUARD (§2.1, ported from estates_db.build_auction_settle_run:3054).
        # The seller is paid out of coins the treasury actually received. A
        # winning row that is not `captured`, or whose reserved integer is not
        # the price being settled, means the money is not there — and paying
        # anyway is how a promoted runner-up once had a seller paid out of a
        # hammer nobody had paid.
        if str(win.get("status")) != "captured":
            raise SettleBlocked(FAILED_ESCROW,
                                f"Lot #{listing_id} cannot pay its seller: the winning "
                                f"bid's escrow is '{win.get('status')}', not captured.")
        held_int = int(win.get("hold_amount") or 0)
        if held_int != price_i:
            raise SettleBlocked(FAILED_ESCROW,
                                f"Lot #{listing_id} would settle at {price_i:,} but the "
                                f"captured escrow is {held_int:,}. Refusing to pay a "
                                f"hammer nobody paid.")
        # CLAIM BEFORE THE TRANSFER, MARK AFTER IT. The marker before is what
        # leaves evidence that a payment was attempted if the process dies inside
        # `pay_seller`; the marker after is what stops the next pass from even
        # trying. Between the two, the KEY is the guarantee: a resume re-sends
        # `land:listing:<id>:settle:seller`, and core replays its own stored
        # answer rather than moving coins again. That is the "interrupted after
        # the seller is paid does not pay again" property, and it holds without
        # this module knowing whether the first attempt got through.
        _db.claim_listing_stage(listing_id, stage, "paying_seller")
        stage = "paying_seller"
        if split["net"] > 0:
            try:
                esc.ledger().transfer(esc.TREASURY, str(listing["seller_id"]),
                                      split["net"], esc.seller_key(listing_id),
                                      reason=f"realestate:sale:{listing_id}")
            except Exception as e:
                # The hammer price is in the treasury and the seller is not paid.
                # Leave the stage at `paying_seller` and hand the listing back:
                # the retry re-sends the same key, which is the fix rather than
                # the risk. Not an `add_coins` fallback — that would mint, and it
                # is exactly what `estates` has no scope for.
                log.error("[land_settle] lot #%s: seller %s was NOT paid %s (%s). "
                          "The %s is in %s and the transfer key is %s — re-sending "
                          "the identical key is safe and is the fix.",
                          listing_id, listing["seller_id"], split["net"], e,
                          price_i, esc.TREASURY, esc.seller_key(listing_id))
                raise
        # ROUTE THE COMMISSION BEFORE THE STAGE MARKER, NOT AFTER IT. These are
        # REAL COINS sitting in `treasury:estates` (the capture put the whole
        # price there and only `net` left), and the marker is what stops the next
        # pass entering this block at all. With the call below the marker there
        # was a two-line window — marker written, process dies, call never made —
        # in which the split was never even ATTEMPTED for this lot: no run row was
        # minted, so `resume_pending()` had nothing to resume and `stuck_runs()`
        # named nothing. The commission simply stayed in the house account
        # forever, unrouted and unreported.
        #
        # Above the marker, the worst case is the opposite one: the split runs,
        # the process dies before the marker, and a resumed settle calls it again
        # — which is a REPLAY, and a replay is the case this design actually
        # handles (`split_rules.run_split` is idempotent by the trigger, across
        # rule edits as of F1). An at-least-once call into an idempotent function
        # beats an at-most-once call into a mechanism with no marker of its own.
        commission_split_run(listing_id, int(split["commission"]))
        # The split's own run row is now the durable evidence that the commission
        # was routed, so the stage marker can be written. Its ROWCOUNT IS READ:
        # `claim_listing_stage` is a conditional UPDATE and its answer was being
        # discarded here while `stage` was set locally regardless — a settle that
        # lost the row to somebody else carried on believing it owned the rung.
        # Losing it is not a money failure (the seller is paid, the commission is
        # routed, both by key) so this reports rather than raises; step 5's
        # conditional flip is what refuses to overwrite another writer's status.
        if not _db.claim_listing_stage(listing_id, "paying_seller", "seller_paid"):
            log.error("[land_settle] lot #%s: seller paid and commission routed, but "
                      "the settle_stage claim 'paying_seller' -> 'seller_paid' was "
                      "LOST — the row reads '%s'. No coins are owed (every leg is "
                      "keyed and replays); a human should reconcile this listing.",
                      listing_id,
                      (_db.get_land_listing(listing_id) or {}).get("settle_stage"))
        stage = "seller_paid"
        # Reporting only, and last: this is a scalar MIRROR of a real balance
        # rather than the only record of coins that were destroyed (§3.6 check 4
        # asserts the two agree; it does not correct them). It stays BELOW the
        # marker because it is the one call here that is not idempotent — running
        # it twice would over-report platform revenue — and a lost report is a
        # lost report, not a lost coin.
        _credit_platform_report(listing, split["commission"], listing_id)

    # ── 5. flip the listing, last ────────────────────────────────────────
    # CONDITIONAL, like every other transition in this module. `update_land_listing`
    # has no WHERE beyond the id, so this wrote `sold` over WHATEVER the row said —
    # including a `rolled_back` or `cancelled` written by somebody else while we held
    # the claim, which is a lot resurrected as sold by the settler that lost it. We
    # only own this row while it still says `settling`; if it does not, the write is
    # not ours to make. The coins are already where they belong (capture and seller
    # payment are both keyed and both replay), so this is reported, loudly, and NOT
    # retried into a second settlement.
    won = _db.update_land_listing(listing_id, _if_status=SETTLING, status="sold",
                                  sold_price=float(price_i), sold_to=str(buyer_id),
                                  closed_at=_utcnow_sql(), settle_stage="done") == 1
    if not won:
        now = _db.get_land_listing(listing_id) or {}
        log.error("[land_settle] lot #%s: settlement COMPLETED (captured %s, seller paid) "
                  "but the row left `%s` underneath us and now reads `%s` — not "
                  "overwriting it as sold. No coins are owed; the keys make a replay "
                  "a no-op. A human should reconcile this listing.",
                  listing_id, price_i, SETTLING, now.get("status"))
        return {"ok": True, "outcome": "already_settling", "listing_id": int(listing_id),
                "status": now.get("status")}
    return {"ok": True, "outcome": "sold", "listing_id": int(listing_id),
            "price": float(price_i), "net": split["net"],
            "commission": split["commission"],
            "commission_pct": split["commission_pct"],
            "loyalty_discount_pct": split["loyalty_discount_pct"],
            "seller_id": listing["seller_id"], "market_id": listing.get("market_id"),
            "win_row": int(win["id"])}


def _resolve_winning_row(listing: dict, buyer_id, price_i: int,
                         win_row_id: Optional[int], stage: Optional[str] = None) -> dict:
    """The `land_bids` row whose hold backs this sale, or refuse to settle.

    A settlement that cannot NAME the row holding the money must not pay anybody
    out of it. The two ways to get here without a row are both real and both mean
    "stop": a pre-escrow bid recorded under the old debit model (which P4's
    conversion turns into a hold, and which must never be settled as though it
    were one), and a hold that expired mid-auction because the lot was extended
    past it (§5) — in which case the runner-up's hold is long released and their
    coins are spent elsewhere, so falling through to them is not a rescue.

    A RESUMED SETTLE IS BOUND TO THE ROW IT ALREADY CAPTURED, not to the row the
    caller names. Once `settle_stage` is past `captured`, the coins are in
    `treasury:estates` under one specific row's key, and the only sale that can
    complete from here is that one. The caller may well name a different row and
    be honest about it — the buyer whose first attempt died between the capture
    and the seller transfer is invited to try again, and their second click
    creates a second row with a fresh hold. Settling THAT row would walk into
    §2.1's guard (`held`, not `captured`) and drive a lot whose money is already
    at the treasury to terminal `failed_escrow` with the seller unpaid. Binding
    to the captured row instead is what makes the retry finish the sale.

    The same read is the fallback when nothing is named at all: `winning_row`
    matches on `(bidder, exact reserved integer)` taken from
    `land_listings.current_bid`/`current_bidder`, and an INSTANT BUY never writes
    either column — so on that shape the captured row is the only thing that can
    say who bought the lot.
    """
    row = None
    if win_row_id is not None:
        row = esc.bid_row(int(win_row_id))
    settled = esc.captured_row(int(listing["id"]))
    if settled is not None and (row is None or int(row["id"]) != int(settled["id"])):
        if _reached(stage, "captured") or row is None:
            row = settled
    if row is None:
        row = esc.winning_row(int(listing["id"]), buyer_id, price_i)
    if row is None:
        raise SettleBlocked(
            FAILED_ESCROW,
            f"Lot #{listing['id']} has no escrow row holding {price_i:,} coins for "
            f"<@{buyer_id}>. Nothing is captured and the seller is NOT paid. If this "
            f"lot pre-dates escrow it must be converted (LAND_ESCROW_PLAN P4) before "
            f"it can settle; if its hold expired, the lot should be relisted.")
    if int(row.get("hold_amount") or 0) != price_i:
        raise SettleBlocked(
            FAILED_ESCROW,
            f"Lot #{listing['id']}: the escrow row named for this sale reserves "
            f"{int(row.get('hold_amount') or 0):,}, not the {price_i:,} being settled.")
    return row


def _capture_winner(listing_id: int, row: dict, price_i: int, note_reason: str) -> None:
    """Capture the winning hold into the treasury, claim-first, replay-safe.

    Three entry states, all reachable and all handled:

      `held`      the ordinary path — claim it, call core, mark it.
      `capturing` a previous attempt died between the claim and the answer.
                  Re-send the IDENTICAL key: core either replays the capture it
                  already did or performs it now. This is the resume, and it is
                  the reason the key was written at row creation.
      `captured`  already done. Return, and do not call core: the guard in
                  `_settle_claimed` reads the row, not this function's opinion.
    """
    row_id = int(row["id"])
    hold_id = str(row.get("hold_id") or "")
    if str(row.get("status") or "") == "capturing":
        # A previous attempt died between the claim and the answer. `capture()`
        # only starts from `held`, so re-drive the identical key by hand: core
        # either replays the capture it already did or performs it now. This is
        # the resume, and it is the reason `capture_key` is written at row
        # creation rather than computed here.
        key = str(row.get("capture_key")
                  or esc.capture_key(listing_id, row.get("kind") or "bid", row_id))
        try:
            esc.ledger().capture(hold_id, amount=price_i, to_user=esc.TREASURY,
                                 key=key,
                                 reason=f"realestate:{note_reason}:{listing_id}")
        except Exception as e:  # noqa: BLE001
            if esc.ledger().error_code(e) == "hold_not_open":
                landed = _reconcile_from_core(row_id, hold_id, e)
                if landed == "captured":
                    return
                raise SettleBlocked(FAILED_ESCROW,
                                    _expired_escrow_message(listing_id, landed))
            raise
        esc.reconcile_hold(row_id, "captured")
        return

    if str(row.get("status") or "") == "captured":
        return                       # already done; do not call core again
    if esc.claim_capture(row_id) is None:
        fresh = esc.bid_row(row_id) or {}
        if str(fresh.get("status")) == "captured":
            return
        raise SettleBlocked(FAILED_ESCROW,
                            f"Lot #{listing_id}: the winning escrow row went to "
                            f"'{fresh.get('status')}' before it could be captured.")
    key = str(row.get("capture_key")
              or esc.capture_key(listing_id, row.get("kind") or "bid", row_id))
    try:
        esc.ledger().capture(hold_id, price_i, esc.TREASURY, key,
                             reason=f"realestate:{note_reason}:{listing_id}")
    except Exception as e:  # noqa: BLE001
        code = esc.ledger().error_code(e)
        # Park the row FIRST — `capture_unknown` on a lost answer, back to `held`
        # on a definite refusal — so that whatever this function raises next, the
        # row already says what is known about the coins.
        esc.unclaim_capture(row_id, f"{code or type(e).__name__}: {e}",
                            outcome_known=esc.outcome_known_for(code))
        if code == "hold_not_open":
            landed = _reconcile_from_core(row_id, hold_id, e)
            if landed == "captured":
                return
            raise SettleBlocked(FAILED_ESCROW,
                                _expired_escrow_message(listing_id, landed))
        # Not a decision — a failure. Raising releases the listing claim so the
        # sweep re-enters and re-sends the same key, which is the retry.
        raise
    esc.mark_captured(row_id)


def _expired_escrow_message(listing_id: int, landed: str) -> str:
    """§5 part 3, in words a seller can read.

    The lot does NOT silently fall through to the runner-up. Under the
    outbid-releases model the runner-up's hold was released when they were
    outbid and their coins are spent elsewhere by now, so promoting them awards
    the lot to someone who cannot pay — and then pays the seller out of a
    treasury that never received the money.
    """
    return (f"Lot #{listing_id}: the winning bidder's escrow is '{landed}' at core — "
            f"their coins are not reserved any more, so there is nothing to capture "
            f"and the seller must not be paid. The lot does not fall through to the "
            f"runner-up (their reservation ended when they were outbid); relist it.")


#: The account the commission is actually sitting in after a capture. The whole
#: hammer price lands in `treasury:estates` and only `net` leaves for the seller,
#: so the commission is REAL COINS in this account with nobody's name on them.
#: A standing rule on it routes them; with no rule configured, nothing changes.
COMMISSION_SOURCE = esc.TREASURY


def commission_split_run(listing_id: int, commission: int) -> dict:
    """Route this lot's commission out of `treasury:estates` per the standing rules.

    WHY THIS EXISTS. `_credit_platform_report` below writes a SCALAR — it is a
    report and its own docstring says so. The coins themselves have been sitting
    in `treasury:estates` since the capture, credited to nobody, and the only way
    to get them to V Tech or to a partner market's owner has been a manual
    transfer. This turns "V Tech takes 70% of estates commission, the market owner
    30%" into two declarative rows and one audited run per sale.

    IDEMPOTENT BY THE LOT, not by the call, and not by the ruleset either. The
    run is keyed on ('land_commission', listing_id, `treasury:estates`) — all
    durable — so a resumed settlement that reaches this point twice replays the
    stored answer and moves nothing. It holds ACROSS A RULE EDIT between the two
    calls: `run_split` looks the trigger up at every version before it mints, and
    the plan pinned by the first offer is the one that executes. Until that
    lookup existed, one `set_short_policy` between two settles of one lot paid the
    same commission twice out of other lots' money in the same house account.

    That is why this needs no `settle_stage` rung of its own — the split's own run
    row IS the progress marker. For that claim to mean anything the row has to
    exist, so this is called ABOVE the `seller_paid` marker, not below it: see the
    comment at the call site.

    NEVER RAISES INTO THE SETTLE LADDER. The seller is already paid and the lot is
    already sold by the time this runs; a routing failure is money still sitting
    safely in the house account, which the resume sweep will move. Failing the
    settlement over it would be strictly worse.
    """
    if int(commission) <= 0:
        return {"outcome": "refused", "reason": "no_commission"}
    try:
        import split_rules
    except Exception as e:  # noqa: BLE001 — module not deployed yet
        log.debug("[land_settle] split_rules unavailable (%s); commission stays in %s",
                  e, COMMISSION_SOURCE)
        return {"outcome": "refused", "reason": f"unavailable: {e}"}
    try:
        res = split_rules.run_split("land_commission", int(listing_id),
                                    COMMISSION_SOURCE, int(commission),
                                    service="estates",
                                    reason=f"realestate:commission:{listing_id}")
    except split_rules.SplitError as e:
        # A DEFINITE REFUSAL, and it must not be dressed up as UNKNOWN. Every
        # `SplitError` is raised either before a run row exists (a ruleset that
        # does not add up, reaching the table by some route other than
        # `add_rule`) or inside the money transaction, which `_tx()` then rolls
        # back — the class's own contract is "never raised past a money move".
        # Nothing moved, and reporting "unknown" here would tell the operator to
        # wait for a sweep to resolve an ambiguity that does not exist. Three
        # outcomes, and this is the middle one.
        #
        # `retryable` is answered from the durable rows rather than assumed: if a
        # run row exists the sweep really will come back to it; if none was ever
        # minted, the sweep has nothing to find and only a human editing the
        # rules changes the answer. The old message promised a retry in both
        # cases, and in the second case there was no row to retry.
        row = None
        try:
            row = split_rules.find_run("land_commission", int(listing_id),
                                       COMMISSION_SOURCE)
        except Exception:  # noqa: BLE001 — reporting must not raise here
            pass
        live = bool(row) and row["state"] not in ("applied", "refused")
        log.error("[land_settle] #%s: commission routing REFUSED (%s). The %s coins "
                  "are still in %s — nothing moved. %s",
                  listing_id, e, commission, COMMISSION_SOURCE,
                  (f"Run {row['run_id']} is parked in '{row['state']}' and the split "
                   f"resume sweep will retry it." if live else
                   "NO run row was minted, so the resume sweep has nothing to retry: "
                   "the split rules on this account need fixing before this "
                   "commission can be routed."))
        return {"outcome": "refused", "retryable": live, "reason": str(e),
                "run_id": (row or {}).get("run_id", ""),
                "state": (row or {}).get("state", "none")}
    except Exception as e:  # noqa: BLE001
        # Genuinely ambiguous: something that is not this module's own refusal
        # came out of a money path. UNKNOWN is the honest answer and the sweep
        # resolves it from ledger evidence.
        log.error("[land_settle] #%s: commission routing FAILED (%s). The %s coins "
                  "are still in %s — nothing is lost and the split resume sweep "
                  "will retry.", listing_id, e, commission, COMMISSION_SOURCE)
        return {"outcome": "unknown", "reason": str(e)}
    if res["outcome"] == "applied":
        log.info("[land_settle] #%s: commission %s routed out of %s across %d leg(s)",
                 listing_id, res["allocated"], COMMISSION_SOURCE, len(res["legs"]))
    elif res.get("reason") == "no_rules":
        # The normal state on day one, and it must be quiet. Behaviour with no
        # rules configured is exactly what it was before this function existed.
        log.debug("[land_settle] #%s: no commission split rules on %s — the %s "
                  "stays there, as it always did.", listing_id, COMMISSION_SOURCE,
                  commission)
    else:
        # `retryable` means "the SWEEP will come back to it", and for a refused
        # run the answer is no — but that is not the same as "nothing to do". A
        # refused run moved no coins and this lot's commission is still unrouted,
        # so it is a job for a human and `/splits runs` lists it as one. Saying
        # only `retryable=False` here read as "settled, ignore".
        log.warning("[land_settle] #%s: commission split ended '%s' (%s). The coins "
                    "are in %s; sweep-retryable=%s.%s", listing_id, res["outcome"],
                    res.get("reason"), COMMISSION_SOURCE, res.get("retryable"),
                    (" Nothing moved and the commission is UNROUTED — it is listed "
                     "by `/splits runs`, and the next offer of this lot re-plans it."
                     if res.get("state") == "refused" else ""))
    return res


def _credit_platform_report(listing: dict, commission: int, listing_id: int) -> None:
    """Mirror the commission into the platform scalar store. Reporting only.

    Best-effort by design and wrapped accordingly: the coins are already in
    `treasury:estates` by the time this runs, so a failure here loses a REPORT,
    not money. Before escrow this call WAS the commission — it wrote a scalar and
    a YAML mirror and no `balances` row, which is why the commission was being
    destroyed on every sale.
    """
    if commission <= 0:
        return
    core = _core()
    if core is None or not hasattr(core, "_credit_platform_balance"):
        return
    try:
        core._credit_platform_balance(commission, market_id=listing.get("market_id") or "",
                                      note=f"realestate:commission:{listing_id}")
    except Exception as e:  # noqa: BLE001
        log.warning("[land_settle] #%s: commission is in %s but the platform report "
                    "failed: %s", listing_id, esc.TREASURY, e)


def _settled_money_blocked(listing_id: int, verb: str) -> Optional[dict]:
    """`None` if this lot may be closed WITHOUT paying anybody, else the refusal.

    `expire_unsold` and `cancel_listing` are the two endings that move no money
    and mark a lot terminal. Both are wrong for a lot whose settlement is
    part-done: the price is in `treasury:estates`, and recording "no bids" or
    "cancelled" over it leaves the coins there with nothing in the system
    pointing at the player who paid them.

    THREE independent reasons, checked separately because any can be true without
    the others: the ladder got past `claimed` (a settlement is part-done); a row
    is `captured`/`capturing` (coins have actually moved); and a row is
    `capture_unknown`/`release_unknown` (NOBODY KNOWS whether the coins moved).
    The stage catches a settle interrupted before it captured; the captured row
    catches a lot whose stage marker was never written because the process died
    between the two; the unknown row catches the case those two were blind to.

    THE THIRD ONE IS NOT "TREAT IT AS CAPTURED". `capture_unknown` means core may
    or may not have moved the coins, and neither guess is safe: assume captured
    and a lot with no buyer never ends; assume not, and this function returns
    `None`, the sweep writes `expired`, and a real player's price sits in
    `treasury:estates` under a listing that reads "ended with no bids". So the
    rule is the one `_settle_claimed` already applies at the settle end: while
    the question is OPEN the lot may not reach a terminal status, and something
    must go and ASK. `esc.reconcile_listing_doubt` is that ask — one `get_hold`
    per doubtful row, no age guard, right here at the decision point rather than
    up to 15 minutes later in the bulk sweep. Whatever it resolves stops blocking
    on this very call; whatever it cannot resolve blocks, loudly, and the lot
    stays live and settleable.

    FOR THE PERSON READING THIS AT 02:00 BECAUSE A LOT WILL NOT CLOSE: this is
    the block doing it, it is deliberate, and there is NO OPERATOR OVERRIDE — by
    design, because an override is a human asserting where the coins are and the
    only party that knows is the ledger. The lot has no ending while core cannot
    answer, and it converges on its own within one sweep of core answering. Fix
    core; do not force the status by hand. Full procedure, including what a hand
    `UPDATE` costs and why the freeze does not help:
    **LAND_MIGRATION_RUNBOOK.md §10, "Live operations — a lot stuck in
    `capture_unknown`".**
    """
    # ASK BEFORE DECIDING. Without this the refusal below is correct but inert:
    # the lot would sit un-expirable until `reconcile_holds` aged in, and the
    # manager unwind would refuse a lot nobody could unblock.
    try:
        esc.reconcile_listing_doubt(int(listing_id))
    except Exception as e:  # noqa: BLE001
        # Best-effort: failing to ASK is not licence to ASSUME. The unresolved
        # rows are still on the table below and still block.
        log.warning("[land_settle] #%s: could not resolve escrow doubt before %s: %s",
                    listing_id, verb, e)
    listing = _db.get_land_listing(listing_id) or {}
    stage = listing.get("settle_stage")
    row = esc.captured_row(int(listing_id))
    doubtful = esc.rows_in(int(listing_id), esc.UNKNOWN_STATUSES)
    if doubtful:
        log.error("[land_settle] REFUSING to %s lot #%s: escrow row(s) %s are in "
                  "%s — core may already have moved the coins to %s and this process "
                  "never heard the answer. Recording '%s' over that would strand a "
                  "real payment under a lot that reads as having no buyer. The lot "
                  "stays live; `reconcile_holds` resolves it and the settle sweep "
                  "then finishes or frees it.",
                  verb, listing_id, [int(r["id"]) for r in doubtful],
                  [str(r["status"]) for r in doubtful], esc.TREASURY, verb)
        return {"ok": False, "outcome": "in_doubt", "listing_id": int(listing_id),
                "settle_stage": stage,
                "rows": [int(r["id"]) for r in doubtful],
                "error": ("This lot has escrow whose outcome core has not confirmed "
                          "yet, so it can't be closed as unsold or cancelled — "
                          "somebody may already have paid for it. It resolves by "
                          "itself within a few minutes; nothing is lost and nobody "
                          "has been charged twice.")}
    if row is None and not _stage_index(stage) > STAGES.index("claimed"):
        return None
    log.error("[land_settle] REFUSING to %s lot #%s: settle_stage=%r, escrow row %s "
              "is %r. This lot has a buyer and its coins are at %s — closing it "
              "without paying would record an ending that never happened over a sale "
              "that is part-settled, and leave the payment owed to nobody. The settle "
              "sweep resumes it under the same keys.",
              verb, listing_id, stage, (row or {}).get("id"), (row or {}).get("status"),
              esc.TREASURY)
    return {"ok": False, "outcome": "settle_pending", "listing_id": int(listing_id),
            "settle_stage": stage,
            "error": ("This lot's sale is part-settled — its escrow has already been "
                      "captured, so it can't be closed as unsold or cancelled. It "
                      "finishes on the next settlement sweep.")}


def part_settled_row(listing_id: int) -> Optional[dict]:
    """The row on this lot whose coins HAVE moved, or MAY have. `None` if none has.

    "This lot already has a paid-for buyer" — the question `_instant_buy_core`
    has to answer before it writes a second buyer a row and reserves their coins
    against a purchase that cannot arrive. It is the first two thirds of
    `resume_row`, factored out rather than copied so the two cannot drift: a lot
    is part-settled if some row is `captured`/`capturing` (`captured_row`) or
    `capture_unknown` (coins that may already be in `treasury:estates`).

    NOT `open_buy_row`. A merely `held` buy row is a RESERVATION and nothing more
    — no coins have moved, the hold expires on its own, and treating one as a
    part settlement would let a single stuck click make a live lot unbuyable by
    anyone else until its hold aged out. `resume_row` still consults it, because
    "is this lot a sale rather than an unsold auction" is a wider question than
    "has somebody already paid".

    Resolves doubt first, for the reason `resume_row`'s docstring gives at
    length: one `get_hold` collapses `capture_unknown` into `captured` or
    `released`. If core cannot be reached the row stays `capture_unknown` and is
    returned anyway — a lot whose escrow may hold a buyer's coins is spoken for
    until core says otherwise.
    """
    try:
        esc.reconcile_listing_doubt(int(listing_id))
    except Exception as e:  # noqa: BLE001
        log.warning("[land_settle] #%s: could not resolve escrow doubt before "
                    "deciding whether the lot is part-settled: %s", listing_id, e)
    row = esc.captured_row(int(listing_id))
    if row is not None:
        return row
    unknown = [r for r in esc.rows_in(int(listing_id), esc.UNKNOWN_STATUSES)
               if str(r["status"]) == "capture_unknown"]
    return unknown[-1] if unknown else None


def resume_row(listing_id: int) -> Optional[dict]:
    """The escrow row that makes this lot a SALE rather than an unsold auction.

    Every caller that decides "sold or expired" reads `current_bid` and
    `current_bidder`. An INSTANT BUY writes neither: it puts a `kind='buy'` row
    on `land_bids`, holds against it and settles immediately, so a lot whose
    instant buy was interrupted looks — on `land_listings` alone — exactly like
    an auction nobody bid on. It is not one. Its buyer's coins are reserved, or
    already captured into `treasury:estates`, and expiring it strands them with
    the lot recorded as "ended with no bids".

    So the decision is made from the ESCROW, which is where the money actually
    is, and this is the one read both `_settle_expired` and `close_listing_core`
    use so the two cannot drift apart again. `None` means no row on this lot is
    holding or has moved coins, which is the only state in which "unsold" is true.

    `capture_unknown` IS SUCH A ROW, and `captured_row` cannot see it — it covers
    `captured`/`capturing` only. A capture whose answer was lost has coins that
    may already be in `treasury:estates`; returning `None` for it hands the lot
    to the "no bids" branch and expires it over a buyer who has paid. So this
    RESOLVES FIRST and then decides: one `get_hold` per doubtful row collapses
    `capture_unknown` into `captured` (the lot is a sale, and the row below finds
    it) or `released` (nothing moved, and "unsold" is true after all). Resolving
    before reading is what keeps a row that turns out to be `released` from being
    handed to `_capture_winner` as the winner of a sale that never happened.

    If core cannot be reached the row stays `capture_unknown` and is returned
    anyway — deliberately. The lot is then driven through `settle_listing`, whose
    `in_doubt` guard hands it straight back, ACTIVE and unsettled. That is the
    third outcome: not sold, not expired, still open, retried next minute. It is
    the only answer that does not require guessing where the coins are.

    `release_unknown` is NOT consulted, and that is a money judgement rather than
    an oversight: a release in doubt means the bidder's coins are either back with
    them or still reserved, and in neither case is anything sitting in the
    treasury or owed to a seller. It cannot make a lot a sale. It still blocks a
    terminal close — see `_settled_money_blocked` — because an open hold on a dead
    lot is its own problem; it just is not THIS one.

    The first two thirds of this — resolve the doubt, then `captured_row` or a
    `capture_unknown` row — is `part_settled_row()`, which `_instant_buy_core`
    also asks before it lets a SECOND player reserve coins on the lot. It is
    CALLED rather than copied: two readings of "has somebody already paid for
    this lot" that could disagree is the defect class this module keeps closing.
    """
    row = part_settled_row(int(listing_id))
    if row is not None:
        return row
    return esc.open_buy_row(int(listing_id))


def _flip_terminal(listing_id: int, status: str) -> Optional[dict]:
    """Write a lot's terminal status UNDER THE CLAIM. `None` if we lost the row.

    The same conditional write `_settle_claimed`'s `sold` flip uses, for the same
    reason and against the same hazard: `update_land_listing` has no WHERE beyond
    the id, so an unconditional `status='expired'` overwrites whatever somebody
    else wrote while we held the claim — a staff `rolled_back`, a manager
    `cancelled` — and resurrects a lot as expired that its owner has already
    ended. We only own this row while it still says `settling`.

    Losing it is NOT retried and NOT an error. `release_all_holds` has already
    run, so no coin is left reserved either way, and the status now on the row was
    written by somebody who also held the claim. Reported, loudly, and left alone.
    """
    if _db.update_land_listing(listing_id, _if_status=SETTLING, status=status,
                               closed_at=_utcnow_sql(), settle_stage="done") == 1:
        return None
    now = _db.get_land_listing(listing_id) or {}
    log.error("[land_settle] lot #%s: holds are released but the row left `%s` "
              "underneath us and now reads `%s` — NOT overwriting it as `%s`. No "
              "coins are owed; whoever wrote that status also held the claim.",
              listing_id, SETTLING, now.get("status"), status)
    # NOT `already_settling`: the lot we lost the row to is, in every observed
    # case, one somebody drove to a TERMINAL status while we held the claim
    # (a staff `rolled_back`, a manager `cancelled`). `_claim_refused` reads the
    # status that is actually on the row and picks the word from it.
    return _claim_refused(listing_id)


def expire_unsold(listing_id: int) -> dict:
    """An auction that ended with no bids. Release anything held, mark expired.

    The release loop runs even though "no bids" implies no holds, for the same
    reason the settle path has one: a lot with `current_bidder IS NULL` and a
    live hold is a bug, and the cheap way to survive it is to release rather than
    to assert it cannot happen and leak the bidder's coins for 24 hours.

    IT REFUSES A LOT WHOSE ESCROW HAS ALREADY MOVED. `settle_stage` past
    `claimed`, or a row in `captured`/`capturing`, means the hammer price is in
    `treasury:estates` and this lot has a buyer. Marking it `expired` there does
    not merely lose the sale — the coins stay in the treasury, the seller is
    never paid, and the listing records "no bids", so nothing in the system is
    left pointing at the player who is out the money. The refusal is loud and the
    lot stays settleable: the next sweep resumes it through `settle_listing`,
    which re-sends the same keys.

    OUTCOMES: `expired`, `already_settling`, `already_closed`, or whatever
    `_settled_money_blocked` returns. A lot that is already `rolled_back` comes
    back `already_closed`, not `already_settling` — it used to say the latter,
    which is a safe outcome wearing a transient word, and it told a manager to
    wait for a settlement that was never coming.
    """
    blocked = _settled_money_blocked(listing_id, "expire")
    if blocked is not None:
        return blocked
    if not claim_listing_for_settlement(listing_id):
        return _claim_refused(listing_id)
    # Re-read under the claim. Between the check above and this line an instant
    # buy can have captured; now that nobody else can settle this lot, the answer
    # is stable, and handing the claim back is what lets the resume have it.
    blocked = _settled_money_blocked(listing_id, "expire")
    if blocked is not None:
        release_listing_claim(listing_id)
        return blocked
    try:
        outcome = release_all_holds(listing_id, reason=f"lot #{listing_id} expired unsold")
        lost = _flip_terminal(listing_id, "expired")
        if lost is not None:
            return lost
        return {"ok": True, "outcome": "expired", "listing_id": int(listing_id),
                "released": outcome["released"], "problems": outcome["problems"],
                # `deferred` travels with the other two, always — see the note on
                # `release_all_holds`. A caller that reads `problems: []` as "no
                # escrow is left on this lot" is wrong unless `deferred` is empty
                # too, and it can only know that if we hand it over.
                "deferred": outcome["deferred"]}
    except Exception:
        release_listing_claim(listing_id)
        raise


def cancel_listing(listing_id: int, *, reason: str = "cancelled by seller") -> dict:
    """Cancel a lot: claim, release every open hold per row, mark cancelled.

    THE LISTING FEE IS NOT REFUNDED, and the refusal says so up front rather than
    leaving the seller to discover it. A listing fee that comes back on cancel is
    a free option on the auction: list high, watch the bidding, cancel for
    nothing. The fix for the audit's finding was never a refund path — it was
    that the fee must actually REACH `treasury:estates` instead of evaporating,
    which `charge_listing_fee` does.

    The "no bids" restriction is NOT enforced here. Under escrow, unwinding a bid
    is a release rather than a refund computation, so it is now a POLICY choice
    rather than a technical limit — and the policy lives with the callers, who
    know whether the requester is the seller or a manager. `cogs.land_exchange`
    keeps refusing a seller-cancel on a lot with a standing bid, for
    market-integrity reasons (a seller who can cancel after seeing a bid can shop
    the price around), and says so in those words.
    """
    if not esc.escrow_available():
        return {"ok": False, "error": paused_sentence()}
    # The same refusal `expire_unsold` makes, for the same reason: a lot whose
    # escrow is already captured has a buyer who has paid, and "cancelled" is not
    # an ending that can be written over that. A manager reaching for the unwind
    # button after an interrupted instant buy is exactly how this gets hit.
    blocked = _settled_money_blocked(listing_id, "cancel")
    if blocked is not None:
        return blocked
    if not claim_listing_for_settlement(listing_id):
        return _claim_refused(listing_id)
    blocked = _settled_money_blocked(listing_id, "cancel")
    if blocked is not None:
        release_listing_claim(listing_id)
        return blocked
    try:
        outcome = release_all_holds(listing_id, reason=reason)
        lost = _flip_terminal(listing_id, "cancelled")
        if lost is not None:
            return lost
        return {"ok": True, "outcome": "cancelled", "listing_id": int(listing_id),
                "released": outcome["released"], "problems": outcome["problems"],
                "deferred": outcome["deferred"], "fee_refunded": False}
    except Exception:
        release_listing_claim(listing_id)
        raise


# ══════════════════════════════════════════════════════════════════════════
# The listing fee — the audit's "deducted and credited to nobody"
# ══════════════════════════════════════════════════════════════════════════

def fee_key(listing_id: Any) -> str:
    """`land:listing:<id>:fee`. One listing, one fee, one key.

    The live reason string was `realestate:listing_fee` — no listing id in it at
    all, so the charge was unattributable AND un-idempotent: two listings by the
    same seller produced the same string, and nothing could tell a retry from a
    second listing.
    """
    return f"land:listing:{int(listing_id)}:fee"


def charge_listing_fee(listing_id: int, seller_id, fee) -> dict:
    """Move the listing fee seller -> treasury, or refuse the listing.

    Claim-first on `fee_stage`, so a crash between the transfer and the marker
    resumes by re-sending the same key and getting core's stored answer back
    instead of charging again.

    A fee of 0 is valid and is the default (`DEF["listing_fee"] = 0.0`), so this
    changes nothing for anyone until the owner turns it on — which is exactly why
    it is fixed now rather than on the day he turns it on and discovers the coins
    have been going nowhere.
    """
    fee_i = int(round(float(fee or 0)))
    if fee_i <= 0:
        return {"ok": True, "charged": 0}
    if not esc.escrow_available():
        return {"ok": False, "error": paused_sentence()}
    listing = _db.get_land_listing(listing_id) or {}
    if str(listing.get("fee_stage") or "") == "paid":
        return {"ok": True, "charged": fee_i, "replayed": True}
    if not _db.claim_listing_fee_stage(listing_id, None, "paying"):
        # Either a concurrent attempt owns it or a previous one died mid-flight.
        # Re-sending the identical key is safe in both cases: the winner's claim
        # is at core, not here, and this transfer replays rather than repeats.
        if str((_db.get_land_listing(listing_id) or {}).get("fee_stage") or "") != "paying":
            return {"ok": True, "charged": fee_i, "replayed": True}
    try:
        esc.ledger().transfer(str(seller_id), esc.TREASURY, fee_i, fee_key(listing_id),
                              reason=f"realestate:listing_fee:{listing_id}")
    except Exception as e:  # noqa: BLE001
        code = esc.ledger().error_code(e)
        if code == "insufficient":
            _db.update_land_listing(listing_id, fee_stage="refused")
            return {"ok": False, "error_code": code,
                    "error": (f"Listing fee is {fee_i:,} 🪙 and you don't have it "
                              f"available. " + available_sentence(seller_id, fee_i))}
        _db.update_land_listing(listing_id, fee_stage="unknown")
        raise
    _db.update_land_listing(listing_id, fee_stage="paid", fee_paid=float(fee_i))
    return {"ok": True, "charged": fee_i}


# ══════════════════════════════════════════════════════════════════════════
# Rent on leased parcels
# ══════════════════════════════════════════════════════════════════════════
#
# LAND_ESCROW_PLAN §6 item 12 says DROP the parcel/rent domain, because
# `cogs/lands.py` owns land ownership and building a second owner is how two
# systems come to disagree about who owns a plot. That reasoning is right and it
# is why what follows charges rent and records NOTHING about ownership: a lease
# row is an AGREEMENT (who pays whom, how much, how often), `parcel_id` is an
# opaque string belonging to whatever owns parcels, and no code path here reads
# or writes a parcel's owner. If the lease and the parcel ever disagree, the
# parcel is right and the lease is stale — so the sweep refuses a lease it cannot
# resolve rather than paying the figure it happens to be holding.
#
# It is OFF until `realestate:rent_enabled` is set, and with no leases the sweep
# is one indexed SELECT that returns nothing.

def rent_period(lease: dict, at: Optional[datetime] = None) -> str:
    """The billing period this lease is currently in. THE key's variable half.

    Monthly leases get `YYYY-MM`, which is what makes "a retry cannot charge two
    months" true by construction rather than by the caller being careful: the key
    for February is the same key in every attempt, on every worker, after every
    restart. Non-monthly leases get the ISO date of the period start, which has
    the same property for a 7-day or 90-day cycle.
    """
    now = at or datetime.now(timezone.utc)
    days = int(lease.get("period_days") or 30)
    if days == 30:
        return now.strftime("%Y-%m")
    due = str(lease.get("next_due_at") or "")
    if due:
        return due[:10]
    return now.strftime("%Y-%m-%d")


def rent_key(parcel_id: Any, period: str) -> str:
    """`land:parcel:<parcel_id>:rent:<period>` — keyed by PERIOD, not by attempt."""
    return f"land:parcel:{parcel_id}:rent:{period}"


def charge_rent(lease: dict, *, at: Optional[datetime] = None) -> dict:
    """Charge one lease for the period it is in. At most once per period, ever.

    THREE INDEPENDENT MECHANISMS stop a retry charging twice, because rent is a
    scheduled job and scheduled jobs are retried by cron entries, supervisors and
    people, none of whom coordinate:

      1. `land_rent_charges` is UNIQUE on `(parcel_id, period)`. A second charge
         for February cannot be WRITTEN DOWN, whatever the sweep believes about
         its own progress.
      2. `claim_rent_charge` is `pending -> claimed` in one atomic UPDATE, so of
         two workers holding the same row exactly one calls core.
      3. The transfer carries `land:parcel:<id>:rent:<period>`, so even if 1 and
         2 were both defeated, core replays its own stored answer rather than
         moving coins a second time.

    Any one of the three is sufficient. All three are here because the failure
    they prevent is a tenant charged twice for one month, which is the kind of
    bug that is discovered by the tenant.
    """
    if not esc.escrow_available():
        return {"ok": False, "error": paused_sentence()}
    period = rent_period(lease, at)
    key = rent_key(lease["parcel_id"], period)

    charge_id = _db.open_rent_charge(lease, period, key)
    if charge_id is None:
        existing = _db.find_rent_charge(str(lease["parcel_id"]), period) or {}
        if str(existing.get("status")) == "paid":
            return {"ok": True, "outcome": "already_paid", "period": period,
                    "charge_id": existing.get("id")}
        charge_id = existing.get("id")
        if charge_id is None:
            return {"ok": False, "error": "rent charge row vanished mid-claim"}

    claimed = _db.claim_rent_charge(int(charge_id))
    if claimed is None:
        fresh = _db.get_rent_charge(int(charge_id)) or {}
        return {"ok": True, "outcome": f"not_ours:{fresh.get('status')}",
                "period": period, "charge_id": int(charge_id)}

    amount = int(claimed["amount"])
    try:
        res = esc.ledger().transfer(str(claimed["tenant_id"]), str(claimed["owner_id"]),
                                    amount, key,
                                    reason=f"land:rent:{claimed['parcel_id']}:{period}")
    except Exception as e:  # noqa: BLE001
        code = esc.ledger().error_code(e)
        if esc.outcome_known_for(code):
            # Core looked and refused: no coins moved. Hand the row back so the
            # next sweep tries again — an empty wallet today is not an empty
            # wallet tomorrow — and park it after MAX attempts so a permanently
            # broke tenant does not generate a ledger call a minute forever.
            _db.release_rent_charge(int(charge_id), f"{code}: {e}",
                                    permanent=(code in ("bad_accounts", "bad_amount")))
            return {"ok": False, "outcome": "refused", "error_code": code,
                    "period": period, "charge_id": int(charge_id), "error": str(e)}
        # Unknown: the transfer may have applied. The row does NOT go back to
        # `pending`, because a pending row is one the sweep will charge.
        _db.park_rent_charge_unknown(int(charge_id), f"{code or type(e).__name__}: {e}")
        log.error("[land_settle] rent charge %s (%s %s) outcome UNKNOWN: %s. The key "
                  "is %s — re-sending it replays; do NOT create a new charge row.",
                  charge_id, claimed["parcel_id"], period, e, key)
        return {"ok": False, "outcome": "unknown", "period": period,
                "charge_id": int(charge_id), "error": str(e)}

    _db.settle_rent_charge(int(charge_id), ledger_ref=key,
                           replayed=bool(res.get("replayed")))
    return {"ok": True, "outcome": "paid", "period": period, "amount": amount,
            "charge_id": int(charge_id), "replayed": bool(res.get("replayed"))}


def sweep_rent(limit: int = 50, *, at: Optional[datetime] = None) -> dict:
    """Charge every lease that is due. Resumes from ROW STATE, never a cursor.

    Off unless `realestate:rent_enabled` is truthy. That gate is not timidity:
    `cogs/lands.py` owns parcels and is not staged here, so until the lease rows
    can be checked against real parcels this sweep has no way to notice a lease
    whose landlord sold the plot last week. Charging rent to the wrong landlord
    is worse than charging none.
    """
    if not _rent_enabled():
        return {"enabled": False, "charged": 0}
    if not esc.escrow_available():
        return {"enabled": True, "charged": 0, "error": "escrow unavailable"}
    charged, results = 0, []
    for lease in _db.land_leases_due(limit=limit):
        try:
            out = charge_rent(lease, at=at)
        except Exception as e:  # noqa: BLE001 — one bad lease must not stop the rest
            log.warning("[land_settle] rent sweep failed for lease %s: %s", lease.get("id"), e)
            continue
        results.append(out)
        if out.get("outcome") == "paid":
            charged += 1
    return {"enabled": True, "charged": charged, "results": results}


def _rent_enabled() -> bool:
    """Is the rent sweep switched on? Reads `bot_config`, not the environment.

    It parses the same truthy words as `Restocker_main._env_bool` and does NOT
    call it, because they answer different questions from different stores: that
    one reads an env var set at deploy time, this one reads a config row a
    manager can flip without a restart. Sharing the parser would couple a runtime
    switch to a deploy-time one; the duplication is four words in a tuple and it
    is the cheaper of the two mistakes.
    """
    try:
        return str(_db.get_config("realestate:rent_enabled") or "").strip().lower() in (
            "1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False


# ══════════════════════════════════════════════════════════════════════════
# Sentences
# ══════════════════════════════════════════════════════════════════════════

def paused_sentence() -> str:
    """What a user sees when the ledger is not there. Deliberately not an error id.

    Also the refusal `cogs.land_exchange._instant_buy_core` and `_place_bid_core`
    hand back while `realestate:bidding_frozen` is on, so it is read by the same
    person the freeze banner is read by — including the one whose coins are
    already in `treasury:estates` from an interrupted capture.

    It used to open "no coins have moved", flatly. See the long note on
    `cogs.land_exchange.freeze_notice` for why that was false for exactly the
    reader with money at stake, and why the answer is to separate RESERVED from
    IN FLIGHT rather than to drop the reassurance. Both sentences must keep
    saying the same true thing; `probe_copy_r5` K3c checks them together.
    """
    return ("The exchange is briefly paused while its money system is being "
            "upgraded — nothing you've bid is lost. Coins reserved against a bid "
            "are still yours; if the exchange had already taken coins for a buy "
            "of yours, they're sitting in escrow and that purchase finishes on "
            "its own once the money system answers. Try again in a few minutes.")


def available_sentence(user_id, needed: int) -> str:
    """A refusal with FIGURES (rule 4), including what the held coins are held for."""
    try:
        w = esc.ledger().balance(str(user_id))
    except Exception:  # noqa: BLE001 — a courtesy sentence never fails a refusal
        return ""
    bal, held, avail = (int(w.get("balance") or 0), int(w.get("held") or 0),
                        int(w.get("available") or 0))
    if held > 0:
        return (f"You have {bal:,} 🪙, but {held:,} is reserved by bids you're "
                f"already holding, which leaves {avail:,} available and you need "
                f"{int(needed):,}.")
    return f"You have {avail:,} 🪙 available and you need {int(needed):,}."
