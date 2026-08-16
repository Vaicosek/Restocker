"""land_escrow.py — real escrow for Land Exchange bids: hold, release, capture.

WHAT THIS REPLACES. Until now a bid in `cogs/land_exchange.py` was a DEBIT:
`deduct_coins(bidder, amount)` at bid time, `add_coins(prev_bidder, amount)` on
outbid. LAND_EXCHANGE_AUDIT.md put it plainly — "the bidder's own balance row IS
the hold — there is no separate escrow ledger to reconcile" — and the file's own
header said the same. Three consequences, all of them live:

  * the refund was a RECOMPUTATION (`int(round(current_bid))`) rather than the
    reversal of a reservation, so one bid had three independent derivations of
    its own amount and a column migration could silently desynchronise them;
  * the outbid credit's reason (`realestate:outbid_refund:<listing_id>`) named
    neither the bidder nor the sequence, so two outbids on one listing produced
    the SAME string — no idempotency check could ever have told them apart;
  * a wallet drained between the balance read and the debit produced a bid
    recorded at full amount, backed by zero coins, and refunded in full. One coin
    backing several bids.

Under this module a bid is a HOLD (LEDGER_API_v2.md §5). Coins do not move. The
bidder's `available` falls and their `balance` does not; being outbid RELEASES
the same reservation instead of crediting a recomputed figure. One coin cannot
back two bids because `ledger_v2.place_hold` carries the availability test inside
its own `INSERT … WHERE available >= amt` — the check IS the write, so the race
that made the old pre-read useless cannot exist.

THE THREE LAYERS, AND WHY THIS ONE IS IN THE MIDDLE.

  Restocker_db.py       the table and the escrow columns (LAND_ESCROW_PLAN §1.2),
                        plus `add_land_bid(..., kind=, hold_amount=, status=)`,
                        which returns the row id every key here is minted from.
  land_escrow.py        THIS FILE. Owns the `land_bids` STATE MACHINE — every
                        transition is one `UPDATE … WHERE status = <the state I
                        believe I am in>` that reports whether it won — mints the
                        keys, talks to `ledger_v2`, and decides what a failure
                        MEANS: a refusal core acted on, or an outcome nobody knows.
  cogs/land_exchange.py the policy: which listing, what minimum, who may bid,
                        when the lot closes. It cannot be imported without
                        discord.py, which is exactly why none of the money rules
                        below live in it — and why every one of them is testable
                        against a temp SQLite file and a fake ledger.

WHAT IT IS MODELLED ON, BY NAME. The status judgement is a PORT from
`estates_db.py`, not a second answer to the same question:

  estates_db                             land_escrow
  ────────────────────────────────────   ──────────────────────────────────────
  DEFINITE_REFUSAL_CODES                 DEFINITE_REFUSAL_CODES
  outcome_known_for()                    outcome_known_for()
  IN_DOUBT_STATUSES / UNKNOWN_STATUSES   IN_DOUBT_STATUSES / UNKNOWN_STATUSES
  PLACEMENT_IN_DOUBT_STATUSES            PLACEMENT_IN_DOUBT_STATUSES
  REFUSED_STATUSES / MAX_HOLD_REFUSALS   REFUSED_STATUSES / MAX_HOLD_REFUSALS
  RELEASABLE_STATUSES                    RELEASABLE_STATUSES
  HOLD_STATE_RESULT                      HOLD_STATE_RESULT
  create_stake / claim_stake / stake_held  create_bid_row / claim_placement /
                                         mark_held
  unclaim_stake_capture / _release       unclaim_capture / unclaim_release
  _reconcile_hold_row                    reconcile_hold()
  holds_needing_reconcile                reconcile_holds()  (sweep)
  placements_needing_replay              replay_placements() (sweep)

The rule underneath all of them: AN UNKNOWN OUTCOME IS NOT A REFUSAL. A
`place_hold` that timed out may well have placed the hold; a capture whose answer
was lost may already have moved the coins into `treasury:estates`. Those rows go
to `place_unknown` / `capture_unknown` / `release_unknown`, never to `failed` and
never back to `held`, and the only way out is to ask core (`get_hold`) and write
the answer down. `outcome_known_for()` is the ONE place that judgement lives.

WHAT IT DELIBERATELY DOES NOT DO. It never holds, caches or reconciles a balance
— core owns coins. It never decides whether a bid is high enough or an auction
has ended. And it does not touch the float money columns: `land_bids.amount` and
`land_listings.current_bid` stay REAL, because converting them is
`land_money_migrate.py`'s separate migration with its own dry run, and folding
two risks into one commit makes neither testable.

DEPLOYMENT ORDER, WHICH IS NOT OPTIONAL. `ledger_migrate.py` must have run on
`restocker.db` before this module is asked for a hold — it is what creates
`ledger_holds`, `ledger_idempotency`, the treasuries and the escrow triggers. If
it has not, `available()` is False and every money path refuses with a human
sentence. There is deliberately NO fallback to `deduct_coins`: a per-call
fallback is how one `_place_bid_core` ends up with two money models and a
reconciliation problem the fallback was meant to avoid (LAND_ESCROW_PLAN.md §3).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Iterable, Optional

import Restocker_db as _db

log = logging.getLogger("restocker.land_escrow")

#: LAND_ESCROW_PLAN.md §1.1. `estates` has NO `wallet.mint`
#: (`ledger_v2.SERVICE_SCOPES`), so a land bug can misallocate coins and can
#: never create them: `treasury:estates` goes negative and screams instead. Two
#: of the three findings in LAND_EXCHANGE_AUDIT.md are *minting* bugs, which
#: makes this the single most valuable property in the migration — and the
#: reason a fresh `land` service id was not invented for it.
SERVICE = "estates"
TREASURY = "treasury:estates"

#: A bid's hold outlives its lot by this much, so a lot that closes exactly on
#: its deadline still has live escrow to capture (LEDGER_API_v2.md §5: "Auctions
#: set expiry to lot-close + 24h").
HOLD_GRACE_SECONDS = 24 * 3600

#: An instant buy settles inside the call that places it, so its hold only has to
#: outlive one settlement. A short TTL means a crashed buy frees the buyer's
#: coins within the hour instead of a day. `ledger_v2.MIN_HOLD_SECONDS` is 60.
BUY_HOLD_SECONDS = 3600

#: The re-extension guard treats a hold as too short if it does not outlive its
#: lot by at least this much. One hour, not one second: the settle sweep runs
#: once a minute and a hold that expires *during* the settlement is the same bug.
EXTENSION_FLOOR_SECONDS = 3600

#: How many DEFINITE refusals of the same capture or release before the row parks
#: for a human instead of retrying forever. Ported from
#: `estates_db.MAX_HOLD_REFUSALS` (its N6): a permanently-refused capture
#: otherwise runs one ledger call per row per sweep tick for the life of the lot.
MAX_HOLD_REFUSALS = int(os.getenv("LAND_MAX_HOLD_REFUSALS", "3"))


# ── The status vocabulary ─────────────────────────────────────────────────────
#
#   pending → placing → held → releasing → released
#                    ↘ failed        ↘ capturing → captured
#                    ↘ place_unknown  ↘ capture_unknown → (reconcile)
#                                      ↘ release_unknown → (reconcile)
#                                      ↘ *_refused        → (a human)
#
# The SETS below are this module's, because "which of these means the coins may
# already have moved" is a judgement about the ledger, not about the table.
#
# A pre-escrow bid row carries the column's DEFAULT, `legacy`, which appears in
# none of these sets and never will. It
# was settled under the old debit model: there is no hold to release and nothing
# to capture, and a sweep that picked one up would invent history.

#: "Core may or may not have applied our call." Neither retryable nor terminal.
UNKNOWN_STATUSES: tuple[str, ...] = ("capture_unknown", "release_unknown")

#: Everything handed to core and not definitively refused. `capturing` /
#: `releasing` is the in-flight version of the same doubt — the worker that sent
#: the call may be gone and the coins may already be in the treasury. Treating an
#: in-flight row as "nothing has happened yet" is `estates_db`'s N5 finding, and
#: it cost a punter a stake that was invisible to the pool it had paid into.
IN_DOUBT_STATUSES: tuple[str, ...] = UNKNOWN_STATUSES + ("capturing", "releasing")

#: A `place_hold` whose outcome is unresolved. Deliberately NOT part of
#: IN_DOUBT_STATUSES: only a capture moves coin and only a `held` row can be
#: captured, so a placement in doubt cannot have reached the treasury and cannot
#: change who won. Blocking a whole lot on one bidder's timed-out hold would
#: freeze the auction for everybody; the worst case here is a bid that does not
#: count, with the coins still the bidder's.
PLACEMENT_IN_DOUBT_STATUSES: tuple[str, ...] = ("placing", "place_unknown")

#: Where a row lands once core has DEFINITIVELY refused the same call
#: MAX_HOLD_REFUSALS times. Not in doubt — core proved it moved nothing, so the
#: hold is still open and the coins are still the bidder's — but not retryable
#: either, because the next attempt gets the same refusal.
REFUSED_STATUSES: tuple[str, ...] = ("capture_refused", "release_refused")

#: Statuses a release may be CLAIMED from. A row in doubt is absent: only the
#: reconciler may move that. `capture_refused` IS here, because core proved it
#: moved nothing and the hold is still open, so handing the coins back is the
#: only correct ending — without it a parked row has no exit and the bidder's
#: coins stay reserved until the hold expires. `release_refused` is NOT here:
#: re-claiming it restarts the exact loop that parking exists to stop.
RELEASABLE_STATUSES: tuple[str, ...] = ("held", "capture_refused")

#: unknown status -> where a repeatedly-refused row parks instead.
_REFUSED_FOR = {"capture_unknown": "capture_refused",
                "release_unknown": "release_refused"}

#: Ledger error codes that mean "core evaluated this and refused it, and provably
#: moved nothing". Ported from `estates_db.DEFINITE_REFUSAL_CODES` so two
#: services cannot disagree about the same error string.
#:
#: Deliberately ABSENT, each absence load-bearing:
#:   hold_not_open            core already terminated the hold — captured or
#:                            released, and only core knows which.
#:   idempotency_in_progress  another attempt may be applying it right now.
#:   idempotency_unresolved   core itself cannot say whether it applied.
#:   internal_error / rate_limited / disabled / any timeout
#:                            no answer is not an answer.
DEFINITE_REFUSAL_CODES: frozenset[str] = frozenset({
    "insufficient", "treasury_insolvent", "frozen", "escrow_shortfall",
    "gambling_blocked",
    "forbidden_hold", "forbidden_scope", "forbidden_source", "unauthorized",
    "hold_not_found", "bad_flag", "missing_idempotency_key",
    "bad_idempotency_key", "idempotency_conflict",
})


def outcome_known_for(error_code: Any) -> bool:
    """True iff `error_code` is a refusal core provably acted on and declined.

    Pass `getattr(exc, "code", "")` from a `ledger_v2.LedgerError`. Anything
    unrecognised is treated as UNKNOWN — the safe direction is always "ask core",
    never "assume nothing happened". Every `bad_*` validation refusal
    (`bad_amount`, `bad_expiry`, `bad_accounts`, …) is matched by prefix, so a new
    one does not silently become an unknown.
    """
    code = str(error_code or "").strip().lower()
    if not code:
        return False
    return code in DEFINITE_REFUSAL_CODES or code.startswith("bad_")


#: core hold state -> the row status it implies. The ONE place this mapping
#: lives, so no two hold-bearing rows can disagree about what core said.
#: `expired` is core's sweeper having released the hold for us: the bidder has
#: their coins back, which is the safe direction, and the row is done.
HOLD_STATE_RESULT: dict[str, str] = {
    "captured": "captured",
    "released": "released",
    "expired": "released",
    "open": "held",
}

#: Statuses a row may be reconciled FROM. Anything else is terminal already or
#: never had a hold, and reconciling it would invent history. A parked
#: (`*_refused`) row is included because a human who asks core explicitly must be
#: able to write the answer down; it is not in the automatic sweep's input, so
#: recording `open` on one cannot restart the refusal loop by itself.
RECONCILABLE_STATUSES: tuple[str, ...] = (
    "held", "capturing", "releasing",
    "capture_unknown", "release_unknown", "capture_refused", "release_refused",
)

#: Row statuses that may be reserving a bidder's coins at core RIGHT NOW. `held`
#: provably is. `releasing` is a release whose answer nobody saw, so the hold may
#: still be open. `placing` / `place_unknown` may be: the `place_hold` was sent
#: and no answer came back, which is exactly the case `replay_placements` exists
#: to settle.
#:
#: `capture_refused` / `release_refused` are deliberately ABSENT even though their
#: holds are open: they are parked for a human by `_park`, the automatic sweeps
#: leave them alone by design, and `refused_rows()` puts them on a screen every
#: minute. `capture_unknown` / `release_unknown` are absent because `reconcile_holds`
#: owns them and only core can say where those coins are.
OPEN_HOLD_STATUSES: tuple[str, ...] = ("held", "releasing") + PLACEMENT_IN_DOUBT_STATUSES

#: A `land_listings.status` from which no further money may move — the lot is over.
#:
#: LAND_ESCROW_PLAN §3.6 check 2 ("no open hold references a listing that is sold,
#: cancelled or expired") is stated in terms of this set, and `sweep_terminal_listing_holds`
#: is that check. It mirrors `cogs.land_exchange.CLOSED_STATUS_REASON` minus that
#: dict's two NON-terminal entries — `settling` is a transient claim a lot comes
#: back from, and `draft` has not opened yet — and it lives HERE rather than in the
#: cog because this module must stay importable without discord.py, which is what
#: makes every rule in it testable against a temp SQLite file.
#:
#: Enumerated POSITIVELY rather than as "anything that is not `active`", and that
#: direction is the whole safety argument: a status this set does not know is
#: treated as LIVE, which costs one wasted sweep pass. The inverse — treating an
#: unrecognised status as terminal — releases the escrow behind a bid that is still
#: running and hands the board to a bidder with no coins behind them.
TERMINAL_LISTING_STATUSES: frozenset[str] = frozenset({
    "sold", "expired", "cancelled", "rolled_back", "failed_escrow",
})


class EscrowUnavailable(RuntimeError):
    """`ledger_v2` is absent, or its tables have not been migrated in.

    Raised rather than falling back to `deduct_coins`. A fallback would put two
    money models behind one `_place_bid_core`, which is the mixed state
    LAND_ESCROW_PLAN.md §3 orders the whole migration to prevent. That fallback
    is GONE, not merely unused — `cogs/land_exchange.py` no longer imports
    `add_coins` or `deduct_coins` at all (see its module docstring), so there is
    no second money model left for this class to stand in front of.

    WHERE IT IS RAISED, and how a player ever sees it. Exactly one site:
    `LedgerV2InProcess._mod()`, when `import ledger_v2` fails. NOBODY CATCHES IT
    BY NAME, and that is the design rather than an omission — the docstring used
    to say "the callers turn it into 'the exchange is briefly paused'", which
    read as if there were an `except EscrowUnavailable` somewhere, and there is
    not. The real chain is:

        _mod() raises  ->  available() catches it and returns False
                       ->  escrow_available() is False
                       ->  every money path refuses ABOVE any row or hold with
                           `paused_sentence()`.

    The money paths are named, not numbered, and the count is asserted rather
    than asserted-in-prose. Six functions hold that gate: `_place_bid_core` and
    `_instant_buy_core` in `cogs/land_exchange.py`, and `settle_listing`,
    `cancel_listing`, `charge_listing_fee` and `charge_rent` in `land_settle.py`.
    This paragraph used to carry line numbers (`_place_bid_core:1216`,
    `_instant_buy_core:1378`) and a count that said 5; by the next round the
    lines had drifted by ~30 and the count was one too few, which is the exact
    defect class this project keeps re-finding — the code changed and the thing
    describing it did not. `tests/test_land_escrow.py` §8 now counts the gates
    off the AST, so this list cannot rot in silence again.

    So the class is a hard stop for `_call`, which must never proceed against a
    ledger it could not import, and `available()` is the thing that turns it into
    a sentence. Both halves are pinned by `tests/test_land_escrow.py` §8 — a
    class raised by one line and asserted by nothing is how the last four dead
    mechanisms in this project started.

    An `EscrowUnavailable` escaping `_call` mid-flight carries no `code`, so
    `outcome_known_for('')` reads it as UNKNOWN. That is pessimistic — `_mod()`
    raises before any call leaves this process, so nothing can have happened —
    and pessimistic is the correct direction here: it parks the row for the
    reconciler instead of asserting a negative about core.
    """


# ══════════════════════════════════════════════════════════════════════════
# Keys — minted from the domain, written to disk BEFORE the money call
# ══════════════════════════════════════════════════════════════════════════
#
# LAND_ESCROW_PLAN.md §1.3:
#
#     land:listing:<listing_id>:<action>[:<bid_row_id>][:<suffix>]
#
# `<bid_row_id>` is the `land_bids` id that `Restocker_db.add_land_bid` has always
# returned and `_place_bid_core:428` always threw away. It is a domain sequence
# number already committed to disk when the key is minted, so there is no new
# counter and no `MAX(seq)+1` race.
#
# Three properties the old `reason=` strings did not have: the key identifies the
# bidder implicitly through the row id; it is minted from a row that is already on
# disk; and `:release` is ONE key per bid rather than one per reason — an outbid
# release, a cancel release and an expiry release of bid 7 are the same money
# event ("hold 7 ends without capture"), and `/hold/release` fingerprints on
# `hold_id` alone, so a cancel racing the sweeper replays instead of conflicting.
# Distinct keys there would have been a double-release bug in a bookkeeping
# costume.


def hold_key(listing_id: Any, kind: str, row_id: Any) -> str:
    """The key this row's `place_hold` carries. `kind` is `bid` or `buy`."""
    return f"land:listing:{int(listing_id)}:{_action(kind)}:{int(row_id)}"


def capture_key(listing_id: Any, kind: str, row_id: Any) -> str:
    """Minted at row creation, not at capture time (rule 3).

    A settlement that resumes an hour or a month later re-sends the key the first
    attempt used, rather than a fresh one derived from whatever the row looks like
    by then. That is the difference between a replay and a second capture.
    """
    return f"{hold_key(listing_id, kind, row_id)}:capture"


def release_key(listing_id: Any, kind: str, row_id: Any) -> str:
    """One key per bid, deliberately not one per reason — see the note above."""
    return f"{hold_key(listing_id, kind, row_id)}:release"


def seller_key(listing_id: Any) -> str:
    """The seller's proceeds transfer. A listing settles at most once."""
    return f"land:listing:{int(listing_id)}:settle:seller"


def _action(kind: str) -> str:
    k = str(kind or "bid").strip().lower()
    if k not in ("bid", "buy", "fee"):
        raise ValueError(f"unknown land_bids kind {kind!r}")
    return k


def row_coins(row: dict) -> int:
    """The exact integer this row reserved at core. Never a recomputation.

    `hold_amount` is written before the `place_hold` call and read back by every
    capture, so the integer the ledger sees is stored ONCE. `amount` stays REAL
    and stays the display figure — this round does not convert the float money
    columns, because `land_money_migrate.py` owns that migration and has its own
    dry run, and folding two risks into one commit makes neither testable.

    That separation is the point of §2.3. The old path derived one bid's amount
    three times — `int(round(amt))` at the debit, the raw float in the column,
    `int(round(current_bid))` at the refund — so a later `ROUND()` migration could
    silently desynchronise a refund from its own debit. Here a release names a
    hold id and a capture names `hold_amount`. There is nothing left to re-derive.

    A row with no `hold_amount` is one this module did not write (a pre-escrow
    `legacy` row, or a hand-inserted one). Refuse it rather than inventing an
    integer at capture time.
    """
    raw = row.get("hold_amount")
    if raw is None:
        raise ValueError(
            f"land_bids row {row.get('id')} has no hold_amount, so nothing here "
            f"knows what it reserved. A hold amount is an integer by contract "
            f"(ledger_v2._coins); refusing to derive one from the float column.")
    amount = int(raw)
    if amount <= 0:
        raise ValueError(f"land_bids row {row.get('id')} reserved {amount}, "
                         f"which is not a positive number of coins.")
    return amount


# ══════════════════════════════════════════════════════════════════════════
# The ledger adapter
# ══════════════════════════════════════════════════════════════════════════


class LedgerV2InProcess:
    """Calls `ledger_v2` as Python functions, with real caller-minted idempotency.

    THE PART THAT IS NOT OBVIOUS, AND THE REASON THIS CLASS EXISTS.
    LAND_ESCROW_PLAN.md §1.1 says land calls `ledger_v2.place_hold` /
    `capture_hold` / `release_hold` directly rather than over HTTP, and §2.2 says
    a retry "re-sends the identical key and lets core replay the answer". Both
    are right and they do not compose on their own: `place_hold(…, key=…)` only
    STAMPS the key onto the hold row. The claim, the conflict check and the
    replay all live in `ledger_v2._idempotent`, which wraps the HTTP handlers,
    and `ledger_holds.idempotency_key` carries no UNIQUE index. A direct
    in-process retry with the identical key would therefore place a SECOND hold
    and reserve the bidder's coins twice — the exact opposite of what the key is
    for, and it would look like a success.

    So this class is the in-process equivalent of that wrapper: claim → run, with
    the money function completing the claim inside its own transaction, so
    `in_progress` still provably means "the coins did not move". It uses six
    private names from `ledger_v2` (`_claim_idempotency`, `_Idem`, `_fingerprint`,
    `_subject`, `_release_idempotency`, `_ok`) and one private exception
    (`Replay`). That coupling is deliberate and it is the narrow kind: it is
    confined to this class, it fails LOUDLY with an AttributeError on the first
    call if ledger v2 renames anything, and the alternative — re-implementing the
    claim semantics locally — is how two implementations of one rule drift apart.

    THE RIGHT LONG-TERM HOME is `ledger_v2` itself, as a public
    `call_in_process(service, endpoint, …)` that `_idempotent` also uses. That is
    a change to a file this round does not own; until it lands, this class is the
    only in-process caller and this paragraph is the note that says why.

    `extend` and `get` carry no key. `POST /hold/extend` takes none either
    (LEDGER_API_v2.md §6) because it sets an ABSOLUTE expiry from a relative TTL,
    so running it twice converges rather than compounding.
    """

    #: endpoint name -> the money function. The names are ledger_v2's own, from
    #: `IN_BAND_ENDPOINTS`: `_finalize_idempotency` refuses to complete a claim
    #: whose endpoint is not declared in-band there, so a typo here fails on the
    #: first call rather than quietly arming the stale-claim takeover.
    _ENDPOINT = {"hold": "place_hold", "hold.capture": "capture_hold",
                 "hold.release": "release_hold", "transfer": "transfer"}

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service

    def _mod(self):
        try:
            import ledger_v2
        except Exception as e:
            raise EscrowUnavailable(f"ledger_v2 is not importable: {e}") from e
        return ledger_v2

    def available(self) -> bool:
        """True iff a hold could be placed right now: module present, tables in.

        Both tables, not one: `ledger_holds` without `ledger_idempotency` is a
        ledger that can reserve coins and cannot replay a key, which is worse
        than no ledger at all — every retry would be a second reservation.
        """
        try:
            lv = self._mod()
            with _db.db() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' "
                    "AND name IN ('ledger_holds','ledger_idempotency')").fetchone()["n"]
            return int(n) == 2 and hasattr(lv, "place_hold")
        except Exception:
            return False

    def _call(self, endpoint: str, key: Optional[str], body: dict, **kwargs) -> dict:
        """Claim the key, run the money function, return the STORED response.

        A replay returns the bytes core stored for the FIRST attempt, marked
        `replayed: True`. That is what lets `replay_placements()` recover a hold
        id this process never saw: the answer was lost, the hold was not.
        """
        lv = self._mod()
        fn = getattr(lv, self._ENDPOINT[endpoint])
        if not key:
            return lv._ok(fn(self.service, **kwargs))
        try:
            claim_ts = lv._claim_idempotency(key, self.service, endpoint,
                                             lv._fingerprint(body, endpoint),
                                             lv._subject(body))
        except lv.Replay as replay:
            return dict(replay.body)
        idem = lv._Idem(key, claim_ts, endpoint)
        try:
            result = fn(self.service, key=key, idem=idem, **kwargs)
        except Exception:
            # Deletes nothing if the money transaction already marked it `done`.
            lv._release_idempotency(key, claim_ts)
            raise
        # `idem.body` is what was STORED, so this caller and a later replay get
        # the same bytes by construction rather than by two builders agreeing.
        return idem.body if idem.body is not None else lv._ok(result)

    def hold(self, user_id: str, amount: int, reason: str, expires_in: int,
             key: str) -> dict:
        return self._call("hold", key,
                          {"user_id": str(user_id), "amount": int(amount)},
                          user_id=str(user_id), amount=int(amount),
                          reason=reason, expires_in=int(expires_in))

    def capture(self, hold_id: str, amount: int, to_user: str, key: str,
                reason: str = "") -> dict:
        return self._call("hold.capture", key,
                          {"hold_id": str(hold_id), "amount": int(amount),
                           "to_user": str(to_user)},
                          hold_id=str(hold_id), amount=int(amount),
                          to_user=str(to_user), reason=reason)

    def release(self, hold_id: str, key: str, reason: str = "") -> dict:
        return self._call("hold.release", key, {"hold_id": str(hold_id)},
                          hold_id=str(hold_id), reason=reason)

    def transfer(self, from_user: str, to_user: str, amount: int, key: str,
                 reason: str = "") -> dict:
        # `acting_user` is in ledger_v2's fingerprint for /transfer but not in its
        # in-process signature: it is an HTTP-layer authorisation input, and land
        # runs inside core. Pinned to None so the fingerprint is stable across
        # attempts, which is the only property the key needs from it.
        return self._call("transfer", key,
                          {"from_user": str(from_user), "to_user": str(to_user),
                           "amount": int(amount), "acting_user": None},
                          from_user=str(from_user), to_user=str(to_user),
                          amount=int(amount), reason=reason)

    def extend(self, hold_id: str, expires_in: int) -> dict:
        return self._mod().extend_hold(self.service, str(hold_id), int(expires_in))

    def get(self, hold_id: str) -> dict:
        return self._mod().get_hold(self.service, str(hold_id))

    def balance(self, user_id: str) -> dict:
        return self._mod().get_balance(str(user_id))

    def error_code(self, exc: BaseException) -> str:
        """The machine-readable code on a `LedgerError`, or '' for anything else.

        On the adapter rather than at module level because a substitute ledger
        (a test's, or an HTTP client's) raises its OWN exception type, and the
        thing that raised is the only thing that can say how to read it. `''`
        means "unrecognised", which `outcome_known_for` turns into UNKNOWN — the
        safe direction.
        """
        return str(getattr(exc, "code", "") or "")


_LEDGER: Any = LedgerV2InProcess()


def ledger() -> Any:
    """The ledger implementation every money call below goes through."""
    return _LEDGER


def set_ledger(impl: Any) -> Any:
    """Swap the ledger implementation. Returns the previous one.

    A TEST SEAM, and the only one. `tests/test_land_escrow.py` drives the refusal
    and unknown-outcome paths through it, because a refused capture and a lost
    response cannot be produced on demand from a healthy ledger — and those are
    exactly the paths the old code never had to handle. Production never calls
    it: `_LEDGER` is a `LedgerV2InProcess` from import time.
    """
    global _LEDGER
    prev, _LEDGER = _LEDGER, impl
    return prev


def escrow_available() -> bool:
    """True iff a hold could be placed right now (ledger v2 present + migrated)."""
    try:
        return bool(_LEDGER.available())
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════
# The row state machine. Claim first; mark the row before the next call.
# ══════════════════════════════════════════════════════════════════════════
#
# Every transition is ONE conditional UPDATE that reports whether it won. The
# whole safety argument rests on the loser getting False: two sweeps racing one
# listing, a manager `/close` racing the minute loop, a resumed settle racing a
# fresh one — in every case exactly one caller may talk to core, and it is the one
# that got True here.


def _claim(row_id: int, expect: str, to: str) -> Optional[dict]:
    """`expect -> to` in one atomic UPDATE. The row iff THIS caller won it."""
    with _db.db() as conn:
        cur = conn.execute(
            "UPDATE land_bids SET status=?, claimed_at=datetime('now'), "
            "attempts=attempts+1 WHERE id=? AND status=?",
            (str(to), int(row_id), str(expect)))
        if cur.rowcount != 1:
            return None
        return _asdict(conn.execute("SELECT * FROM land_bids WHERE id=?",
                                    (int(row_id),)).fetchone())


def _mark(row_id: int, status: str, *, hold_id: Optional[str] = None,
          hold_expires_at: Optional[str] = None, error: Optional[str] = None,
          clear_error: bool = False, refusals: Optional[int] = None,
          settled: bool = False) -> None:
    """Record the outcome of a money call on one row. Unconditional by design.

    The conditional half already happened in `_claim`; this is the progress
    marker saying the call came back, and it is written BEFORE the next call is
    made (rule 2). It is unconditional so that a resume which re-sent the
    identical key and got a REPLAY can still write the terminal status onto a row
    it already owns.
    """
    sets = ["status=?"]
    args: list[Any] = [str(status)]
    if hold_id is not None:
        sets.append("hold_id=?"); args.append(str(hold_id))
    if hold_expires_at is not None:
        sets.append("hold_expires_at=?"); args.append(str(hold_expires_at))
    if error is not None:
        sets.append("last_error=?"); args.append(str(error)[:500])
    elif clear_error:
        sets.append("last_error=NULL")
    if refusals is not None:
        sets.append("refusals=?"); args.append(int(refusals))
    if settled:
        sets.append("settled_at=datetime('now')")
    args.append(int(row_id))
    with _db.db() as conn:
        conn.execute(f"UPDATE land_bids SET {', '.join(sets)} WHERE id=?", args)


def _asdict(row) -> Optional[dict]:
    return dict(row) if row is not None else None


def bid_row(row_id: int) -> Optional[dict]:
    with _db.db() as conn:
        return _asdict(conn.execute("SELECT * FROM land_bids WHERE id=?",
                                    (int(row_id),)).fetchone())


def rows_in(listing_id: int, statuses: Iterable[str]) -> list[dict]:
    """Escrow rows on one listing in any of `statuses`, oldest first.

    Oldest first so a resumed settle walks them in the same order every time:
    "resume where it stopped" only means something if the sequence is stable.
    """
    st = [str(s) for s in statuses]
    if not st:
        return []
    with _db.db() as conn:
        rows = conn.execute(
            f"SELECT * FROM land_bids WHERE listing_id=? AND status IN "
            f"({','.join('?' * len(st))}) ORDER BY id ASC",
            [int(listing_id), *st]).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
# The row lifecycle, one call per transition
# ══════════════════════════════════════════════════════════════════════════
#
# Deliberately FINE-GRAINED rather than four fat "do the whole operation"
# functions. `cogs/land_exchange._place_bid_core`, `_instant_buy_core` and
# `land_settle` each need to say something different to a person between the
# claim and the call — a bid quotes the minimum, an instant buy quotes the
# Buy-Now, a settle quotes nothing at all — and a wrapper that owned the whole
# sequence would either swallow that or grow a callback for it. What must NOT be
# duplicated is the STATE RULES, and those live here: which statuses a release may
# be claimed from, where an unknown outcome lands, when a refusal parks. A caller
# that skips a step gets a False from the next one rather than a silent double-call.


def create_bid_row(listing_id: int, bidder_id: Any, amount: float,
                   hold_amount: int, kind: str = "bid") -> dict:
    """Write the row FIRST, with both its keys, `status='pending'`.

    Rule 1 and rule 3 in one function. `add_land_bid` has always returned the row
    id and `_place_bid_core:428` has always thrown it away; that id is what every
    key here is minted from, so the keys are on disk before any money call and a
    retry re-reads them instead of minting new ones.

    A crash immediately after this leaves a `pending` row and nothing else: no
    hold, no coins, no listing change. That is the cheapest possible failure, and
    it is the whole reason the row comes first.

    `amount` keeps feeding the existing REAL column (the display figure, untouched
    by this round). `hold_amount` is the INTEGER that will be reserved — stored
    once, so no capture ever re-derives it from a float.
    """
    kind = _action(kind)
    hold_amount = int(hold_amount)
    if hold_amount <= 0:
        raise ValueError("hold_amount must be a positive whole number of coins")
    row_id = _db.add_land_bid(listing_id, str(bidder_id), float(amount),
                              kind=kind, hold_amount=hold_amount, status="pending")
    with _db.db() as conn:
        conn.execute("UPDATE land_bids SET idem_key=?, capture_key=? WHERE id=?",
                     (hold_key(listing_id, kind, row_id),
                      capture_key(listing_id, kind, row_id), int(row_id)))
    return bid_row(row_id)


def claim_placement(row_id: int) -> Optional[dict]:
    """CLAIM FIRST. `pending | place_unknown -> placing`. Row iff WE won it.

    `place_unknown` is claimable because that is what the replay pass re-sends;
    `failed` is not, because core evaluated it and said no, and re-sending a
    refused request is a different business decision that a human makes.
    """
    row = _claim(row_id, "pending", "placing")
    return row if row is not None else _claim(row_id, "place_unknown", "placing")


def mark_held(row_id: int, hold_id: str, expires_at: Optional[str] = None) -> bool:
    """`placing -> held`, writing back the hold id and its expiry. Per row.

    The progress marker that turns "we asked for a reservation" into "the
    reservation exists and here is its name". Nothing may capture or release a
    row that has not been through here, because nothing else knows the hold id.
    """
    if not hold_id:
        return False
    _mark(row_id, "held", hold_id=str(hold_id), hold_expires_at=expires_at,
          clear_error=True)
    return True


def fail_placement(row_id: int, error: str, *, outcome_known: bool = False) -> str:
    """The `place_hold` did not come back with a hold. Returns the landed status.

    `outcome_known=True` -> `failed`. Core evaluated the request and refused it:
    no hold exists, nothing is reserved, and "nothing was taken" is TRUE, so the
    caller may say so and quote figures.

    `outcome_known=False` -> `place_unknown`. A timeout or a 500 means the hold
    may be sitting at core right now. Filing that as `failed` would tell a bidder
    "nothing was taken" while their coins are unavailable until the hold expires,
    AND would put the row beyond the reconciler's reach — a reconcile needs a
    `hold_id` and a failed placement has none. `place_unknown` keeps the row's
    `idem_key`, and that key IS the recovery: `replay_placements()` re-sends it
    and core replays its original answer, hold id and all.

    Default False on purpose. An unrecognised failure must never claim the
    bidder's coins are untouched.
    """
    landed = "failed" if outcome_known else "place_unknown"
    _mark(row_id, landed, error=str(error))
    if landed == "place_unknown":
        log.error("[land_escrow] bid row %s: hold outcome UNKNOWN (%s). The hold MAY "
                  "exist at core. The row keeps its key and replay_placements() "
                  "re-sends it; do NOT tell the bidder nothing was taken.",
                  row_id, error)
    return landed


def claim_release(row_id: int, reason: str = "outbid") -> Optional[dict]:
    """CLAIM FIRST. `held | capture_refused -> releasing`. Row iff WE won it.

    A row in doubt is deliberately not claimable, so an outbid or a cancel cannot
    drag a `capture_unknown` row into a release loop core will refuse with
    `hold_not_open` forever. `capture_refused` IS claimable: core proved it moved
    nothing, the hold is still open, and handing the coins back is the only
    correct ending — without that a parked row has no exit at all.
    """
    for expect in RELEASABLE_STATUSES:
        row = _claim(row_id, expect, "releasing")
        if row is not None:
            _mark(row_id, "releasing", error=str(reason))
            return row
    return None


def mark_released(row_id: int) -> bool:
    """`releasing -> released`. The bidder's `available` is back where it was."""
    _mark(row_id, "released", settled=True, clear_error=True)
    return True


def unclaim_release(row_id: int, error: str, *, outcome_known: bool = False) -> str:
    """Hand a `releasing` row back. Returns the status it landed in.

    Default `release_unknown`. `hold_not_open` in particular is NOT a reason to go
    back to `held`: it means core has already terminated this hold — possibly by
    CAPTURING it — and only core can say which.
    """
    return _park(row_id, error, unknown_status="release_unknown",
                 outcome_known=outcome_known)


def claim_capture(row_id: int) -> Optional[dict]:
    """CLAIM FIRST. `held -> capturing`. The capture key was minted at creation."""
    return _claim(row_id, "held", "capturing")


def mark_captured(row_id: int) -> bool:
    """`capturing -> captured`. The coins are now REAL, in `treasury:estates`."""
    _mark(row_id, "captured", settled=True, clear_error=True)
    return True


def unclaim_capture(row_id: int, error: str, *, outcome_known: bool = False) -> str:
    """Hand a `capturing` row back. Returns the status it landed in.

    An unknown outcome lands in `capture_unknown`, never back in `held`. A capture
    that COMMITTED and lost its response, filed as `held`, is how a captured bid
    becomes unreleasable: every later release gets `409 hold_not_open` forever
    while the coins sit in the treasury and the settlement refuses to pay the
    seller against a row that claims to be merely held.
    """
    return _park(row_id, error, unknown_status="capture_unknown",
                 outcome_known=outcome_known)


def promote_top_bid(listing_id: int, row_id: int, bidder_id: Any, amount: int) -> dict:
    """Make this row the standing high bid, and claim the one it displaces — in
    ONE transaction. The half of LAND_ESCROW_PLAN §2.3 a ledger call cannot make
    atomic.

    `land_listings` and `ledger_holds` live in one file but on two connections
    (`ledger_v2._conn()` runs `isolation_level=None` so it can issue its own
    `BEGIN IMMEDIATE`), so "record the new high bid and release the old hold
    atomically" is not literally available. What IS available, and what this does,
    is commit the DECISION atomically:

        UPDATE land_listings SET current_bid=?, current_bidder=?
         WHERE id=? AND status='active' AND (current_bid IS NULL OR current_bid < ?)
        UPDATE land_bids  SET status='releasing' … WHERE listing_id=? AND status='held'
                                                     AND id <> <the new row>

    Both statements commit together. After that commit there is exactly one `held`
    row on the lot — the new one — and every displaced row is durably marked
    `releasing`, a state `reconcile_holds()` resolves if this process dies before
    the release call goes out. So the failure mode is a hold that stays open a few
    minutes too long (an over-reservation for somebody who has already been
    outbid), never a window with two standing high bids and never one with none.

    The `current_bid < ?` clause is the other half. Without it the listing update
    is last-writer-wins: two bidders racing one lot both get holds — correctly,
    `place_hold` serialises per WALLET, not per lot — and the loser's UPDATE can
    land second and put the LOWER bid on the board. The money was already safe;
    the display was not (LAND_ESCROW_PLAN §8 item 9). One clause, and it belongs
    here rather than in the cog because it must share a transaction with the
    displacement claim.

    Returns `{"ok": True, "displaced": [rows the caller must now release]}`, or
    `ok: False` when this bid did not win the row — in which case the CALLER must
    release the hold it just placed, because nothing else will.
    """
    with _db.db() as conn:
        cur = conn.execute(
            "UPDATE land_listings SET current_bid=?, current_bidder=?, "
            "updated_at=datetime('now') WHERE id=? AND status='active' "
            "AND (current_bid IS NULL OR current_bid < ?)",
            (float(amount), str(bidder_id), int(listing_id), float(amount)))
        if cur.rowcount != 1:
            return {"ok": False, "displaced": [],
                    "error": "Somebody else's bid landed first — yours was not the "
                             "highest by the time it reached the board."}
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM land_bids WHERE listing_id=? AND status='held' AND id<>? "
            "ORDER BY id", (int(listing_id), int(row_id))).fetchall()]
        for r in rows:
            conn.execute(
                "UPDATE land_bids SET status='releasing', claimed_at=datetime('now'), "
                "attempts=attempts+1, last_error=? WHERE id=? AND status='held'",
                ("outbid", int(r["id"])))
            r["status"] = "releasing"
    return {"ok": True, "displaced": rows}

def _park(row_id: int, error: Any, *, unknown_status: str,
          outcome_known: bool = False) -> str:
    """Hand an in-flight row back after a failed capture/release. Returns status.

    A DEFINITE refusal returns the row to `held`, where a retry is safe — until
    core has said no `MAX_HOLD_REFUSALS` times, at which point it parks in
    `capture_refused` / `release_refused` for a human. Parked is not lost: the
    hold is still open at core and the coins are still the bidder's, and
    `refused_rows()` lists them. Without the bound, a wallet that refuses every
    capture the same way forever gets one ledger call per sweep tick for the life
    of the listing, with the row flipping in and out of a state that makes the
    settle work or not depending on timing.

    Anything else lands in `*_unknown`, never back in `held`. A capture that
    COMMITTED and lost its response, filed as `held`, is how a captured bid
    becomes unreleasable: every later release gets `409 hold_not_open` forever
    while the coins sit in the treasury and the settlement refuses to pay the
    seller against a row that claims to be merely held.
    """
    refusals = int((bid_row(row_id) or {}).get("refusals") or 0)
    if outcome_known:
        refusals += 1
        landed = "held" if refusals < MAX_HOLD_REFUSALS else _REFUSED_FOR[unknown_status]
    else:
        landed = unknown_status
    _mark(row_id, landed, error=str(error), refusals=refusals)
    if landed != "held":
        log.error("[land_escrow] bid row %s -> %s (%s). %s", row_id, landed, error,
                  "core refuses this every time — a human must decide; the hold is "
                  "still open and still the bidder's" if landed in REFUSED_STATUSES
                  else "ask core (get_hold) and record the answer; do NOT guess")
    return landed


def _status_of(row_id: int) -> Optional[str]:
    row = bid_row(row_id)
    return str(row["status"]) if row and row.get("status") is not None else None


def reconcile_hold(row_id: int, hold_state: str) -> str:
    """Record what `get_hold` said. Returns the status the row landed in.

    Forward, always. A hold core calls `captured` makes the row `captured`, so a
    settlement can finally see the coin it already holds; `released` or `expired`
    makes it `released`, because the bidder already has the coins back. Only
    `open` — core saying plainly that nothing happened — returns the row to
    `held`, where a retry is safe.

    This is not "reconciling balances"; this module never holds one. It is asking
    the system that owns the hold what it did, and writing the answer down. Core
    is the authority, this row is the note-taker.
    """
    state = str(hold_state or "").strip().lower()
    target = HOLD_STATE_RESULT.get(state)
    if target is None:
        raise ValueError(f"core reported hold state {hold_state!r}, which this "
                         f"module does not know how to record. Ask core again "
                         f"rather than guessing.")
    row = bid_row(row_id)
    if not row:
        return "missing"
    current = str(row.get("status") or "")
    if current == target:
        return target                    # already recorded; calling twice is free
    if current not in RECONCILABLE_STATUSES:
        raise ValueError(f"land_bids row {row_id} is {current!r}; refusing to "
                         f"reconcile it to {target!r}. A terminal row is history, "
                         f"not a draft.")
    _mark(row_id, target, clear_error=True, refusals=0,
          settled=target in ("captured", "released"))
    log.info("[land_escrow] bid row %s reconciled %s -> %s from core hold state %r",
             row_id, current, target, state)
    return target


# ══════════════════════════════════════════════════════════════════════════
# Reads
# ══════════════════════════════════════════════════════════════════════════


def held_rows(listing_id: int, exclude_row_id: Optional[int] = None) -> list[dict]:
    """Every row on this listing that currently reserves coins.

    Under §2.3's model there is at most one, because each new top bid releases the
    previous one before it returns. The query returns a list anyway and the
    callers loop: "should be at most one" is not a guarantee, and a settlement
    releases whatever it actually finds rather than what it expects to find.
    """
    rows = rows_in(int(listing_id), ("held",))
    return [r for r in rows
            if exclude_row_id is None or int(r["id"]) != int(exclude_row_id)]


def rows_in_doubt(listing_id: int) -> list[dict]:
    """Rows on this lot whose coin location core has not confirmed.

    A settlement must not run while one of these belongs to the lot: its coins
    may already be in the treasury, or may be about to be released out from under
    the sale, and only core can say which. `place_unknown` is deliberately not
    here — see PLACEMENT_IN_DOUBT_STATUSES.
    """
    return rows_in(int(listing_id), IN_DOUBT_STATUSES)


def refused_rows(limit: int = 200) -> list[dict]:
    """Rows parked because core refused the same call MAX_HOLD_REFUSALS times.

    Not in doubt — core proved it moved nothing — so they block no settlement.
    They do need a human: the bidder's coins are still reserved under an open hold
    that this bot has stopped trying to release.
    """
    ph = ",".join("?" * len(REFUSED_STATUSES))
    with _db.db() as conn:
        rows = conn.execute(
            f"SELECT * FROM land_bids WHERE status IN ({ph}) ORDER BY id LIMIT ?",
            (*REFUSED_STATUSES, int(limit))).fetchall()
    return [dict(r) for r in rows]


def winning_row(listing_id: int, bidder_id: Any, amount: int) -> Optional[dict]:
    """The row whose hold backs `current_bid` — the one a settlement captures.

    Matched on (listing, bidder, exact reserved integer) and only in a state a
    capture can start from or has already finished in. Ported in spirit from
    `estates_db.build_auction_settle_run:3054`: a settlement that cannot name the
    row holding the money must not pay anybody out of it.
    """
    best = None
    for r in rows_in(int(listing_id), ("held", "capturing", "captured")):
        if str(r["bidder_id"]) != str(bidder_id):
            continue
        if int(r.get("hold_amount") or 0) != int(amount):
            continue
        best = r
    return best


def captured_row(listing_id: int) -> Optional[dict]:
    """The row on this lot whose coins core has ALREADY moved to the treasury.

    The question `winning_row` cannot answer. `winning_row` matches on
    `(bidder, exact reserved integer)` because that is what a caller who is
    starting a settlement knows; a caller RESUMING one knows something stronger
    and less negotiable — some row on this lot has been captured, so the hammer
    price is that row's `hold_amount` and the buyer is that row's bidder,
    whatever the board or the caller believes. An instant buy writes nothing to
    `land_listings.current_bid`, so on that shape this is the only record of who
    bought the lot and for how much.

    `capturing` is included because it is the same money question one answer
    short: the row is mid-capture and the settle path re-drives its identical key
    rather than starting a different sale. `captured` wins if both exist, since a
    confirmed capture is not a guess.
    """
    rows = rows_in(int(listing_id), ("captured", "capturing"))
    for r in rows:
        if str(r["status"]) == "captured":
            return r
    return rows[0] if rows else None


def open_buy_row(listing_id: int) -> Optional[dict]:
    """An instant-buy row still reserving its buyer's coins on this lot.

    "Somebody has pressed Buy and their money is set aside for it" — which is not
    "this auction ended with no bids", even though `current_bid` is NULL for both.
    Newest first: if two clicks each left a reservation, the later one is the row
    the buyer is waiting on, and the earlier is released as a loser by the settle.
    """
    rows = [r for r in rows_in(int(listing_id), ("held",))
            if str(r.get("kind") or "") == "buy"]
    return rows[-1] if rows else None


def needing_attention(older_than_minutes: int = 15, limit: int = 200) -> list[dict]:
    """Rows core has an answer for that this bot has not written down yet.

    Resumes from ROW STATE, never from a cursor — the candidate set is the status
    itself, so a row that resolves leaves the set and an interrupted sweep neither
    repeats work nor skips a row. `FINDINGS.md` records the cursor version of this
    being got wrong once already.

    15 minutes matches `ledger_v2.IDEMPOTENCY_STALE_SECONDS`, so a replay of a
    still-claimed key is taken over rather than refused forever.
    """
    statuses = tuple(IN_DOUBT_STATUSES) + ("place_unknown",)
    ph = ",".join("?" * len(statuses))
    with _db.db() as conn:
        rows = conn.execute(
            f"SELECT * FROM land_bids WHERE status IN ({ph}) "
            f"AND (claimed_at IS NULL OR claimed_at <= datetime('now', ?)) "
            f"ORDER BY id ASC LIMIT ?",
            (*statuses, f"-{int(older_than_minutes)} minutes", int(limit))).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
# The three sweeps. All resume from ROW STATE, never from a cursor.
# ══════════════════════════════════════════════════════════════════════════


def reconcile_holds(older_than_minutes: int = 15, limit: int = 50) -> int:
    """Ask core about every in-doubt hold and write the answer down. Per row.

    The ONLY way a row leaves `capture_unknown` / `release_unknown`. Without it
    those rows accumulate with nobody resolving them, and the coins they describe
    are invisible to the bidder and to the settlement alike.

    One bad row must not stop the sweep, so a failure is a `continue` and never a
    `break` — the R4-1 shape, where one frozen row stalled 199 others and nothing
    was parked, so nothing was on any screen.
    """
    done = 0
    for row in needing_attention(older_than_minutes, limit):
        if str(row.get("status")) == "place_unknown" or not row.get("hold_id"):
            continue                       # replay_placements() owns those
        try:
            reconcile_hold(int(row["id"]),
                      str(ledger().get(row["hold_id"]).get("state") or ""))
            done += 1
        except Exception as e:
            log.warning("[land_escrow] reconcile of bid row %s (hold %s) failed: %s",
                        row["id"], row["hold_id"], e)
    return done


def reconcile_listing_doubt(listing_id: int, limit: int = 20) -> dict:
    """Ask core about THIS lot's `*_unknown` rows RIGHT NOW. No age guard.

    `reconcile_holds` is the same question asked in bulk, once a minute, behind a
    15-minute age window. That window is the bug this function exists to close:
    a lot whose deadline falls inside it is expired by the settlement sweep
    BEFORE the reconciler is allowed to look, so `capture_unknown` — core moved
    the coins, this process never heard the answer — was decided by a caller that
    could not see it. The guard and the owner of the status could not see each
    other; this is the read that lets them.

    Dropping the age guard is safe HERE and would not be safe in `reconcile_holds`:

      * The call is `get(hold_id)` — a pure READ of core's hold state. It carries
        no idempotency key, applies nothing and replays nothing, so unlike the
        re-sent `place_hold` in `_resolve_placement` there is no
        `IDEMPOTENCY_STALE_SECONDS` window to respect. Asking early can only get
        an answer early or fail.
      * It is scoped to ONE listing and fires only at a decision point that is
        about to write a TERMINAL status or decide "this lot had no buyer". The
        bulk sweep walks every in-doubt row in the database every minute; the age
        guard there is what keeps that from becoming a per-row ledger call per
        tick (`MAX_HOLD_REFUSALS`' reasoning, one layer up).

    `capturing` / `releasing` are deliberately NOT touched: those are in-flight
    calls whose worker may still be alive, and re-deciding them from underneath it
    is exactly what the age guard protects. They are already refused by their own
    guards (`captured_row` covers `capturing`).

    THREE OUTCOMES, and the caller must honour all three. `resolved` are rows core
    answered for; `unresolved` are rows still in doubt because core could not be
    reached or gave an answer this module will not guess at. A non-empty
    `unresolved` means the question is STILL OPEN — the caller must refuse to
    write a terminal status, not fall back to "probably nothing happened".
    """
    resolved: list[dict] = []
    unresolved: list[dict] = []
    for row in rows_in(int(listing_id), UNKNOWN_STATUSES)[:int(limit)]:
        row_id = int(row["id"])
        if not row.get("hold_id"):
            # Nothing to ask core ABOUT. A row in `*_unknown` with no hold id
            # cannot have moved coins (the hold id is written before the capture
            # call), but this module does not act on that inference — it reports
            # the row as unresolved and lets a human see it.
            unresolved.append({"row": row_id, "status": str(row.get("status")),
                               "why": "no hold_id to ask core about"})
            continue
        try:
            landed = reconcile_hold(
                row_id, str(ledger().get(row["hold_id"]).get("state") or ""))
            resolved.append({"row": row_id, "was": str(row.get("status")),
                             "status": landed})
            log.info("[land_escrow] listing %s: inline reconcile of bid row %s "
                     "%s -> %s (a terminal decision was pending on it)",
                     listing_id, row_id, row.get("status"), landed)
        except Exception as e:  # noqa: BLE001
            unresolved.append({"row": row_id, "status": str(row.get("status")),
                               "why": f"{type(e).__name__}: {e}"})
            log.warning("[land_escrow] listing %s: inline reconcile of bid row %s "
                        "(hold %s) failed: %s — the lot must NOT be closed while "
                        "this is open", listing_id, row_id, row["hold_id"], e)
    return {"resolved": resolved, "unresolved": unresolved}


def _resolve_placement(row: dict) -> str:
    """Re-send ONE row's `place_hold` with the IDENTICAL key. Returns the status.

    The single-row half of `replay_placements`, factored out because the terminal-lot
    retirement needs exactly the same question answered — "does this hold exist?" —
    before it is allowed to release anything.

    Core either replays the original hold (it landed, and the replay carries the
    hold id this row never saw) or places it now (it did not). Both answers end the
    doubt; neither is a guess.

    `expires_in` is recomputed rather than stored, deliberately: it is NOT part of
    the idempotency fingerprint (LEDGER_API_v2.md §6), precisely so a resume minutes
    later replays instead of raising `idempotency_conflict` on a TTL that drifted by
    the time it took to notice.
    """
    row_id = int(row["id"])
    if not _claim(row_id, "place_unknown", "placing"):
        return str((bid_row(row_id) or {}).get("status") or "missing")
    ttl = BUY_HOLD_SECONDS if str(row.get("kind")) == "buy" else HOLD_GRACE_SECONDS
    try:
        out = ledger().hold(str(row["bidder_id"]), row_coins(row),
                            reason=f"realestate:{row.get('kind') or 'bid'}:"
                                   f"{row['listing_id']}",
                            expires_in=ttl, key=str(row["idem_key"]))
    except Exception as e:  # noqa: BLE001
        code = ledger().error_code(e)
        return fail_placement(row_id, f"{code or type(e).__name__}: {e}",
                              outcome_known=outcome_known_for(code))
    mark_held(row_id, out["hold_id"], out.get("expires_at"))
    log.warning("[land_escrow] bid row %s recovered: core %s hold %s",
                row_id, "replayed" if out.get("replayed") else "placed", out["hold_id"])
    return "held"


def rearm_stale_placements(older_than_minutes: int = 15, limit: int = 50) -> int:
    """`placing -> place_unknown` for rows whose worker never came back. Per row.

    A row is put in `placing` by `claim_placement` and leaves it the moment the
    `place_hold` answers, one way or the other. A process that dies in between
    leaves it there FOREVER: `needing_attention()` asks for `place_unknown` and the
    in-doubt pair, so nothing looks at `placing`, and the bidder's coins may be
    reserved under a hold whose id was never written down.

    The same shape as `land_settle.rearm_stale_claims` and for the same reason: an
    in-flight claim that outlives the age window is not in flight, it is abandoned.
    Re-arming it to `place_unknown` — where the row's own `idem_key` is the recovery
    — hands it to `_resolve_placement`, which re-sends that key and lets core say
    what actually happened. It does NOT assume the hold failed.

    Age-guarded, and that guard is load-bearing: without it this would drag a
    placement that is genuinely mid-flight out from under the worker that owns it,
    and two callers would be re-sending one key at once.

    IT DOES NOT USE `_claim`, and that is the one interesting line here. `_claim`
    stamps `claimed_at=datetime('now')`, which would reset the very clock the age
    guard reads: the row would be re-armed and then have to wait ANOTHER
    `older_than_minutes` before `needing_attention()` would look at it, so a
    re-arm would push the recovery out instead of pulling it in. The age that
    matters is how long ago the PLACEMENT was claimed, so that timestamp is left
    exactly where it is and the row is picked up on this same pass. The claim-first
    discipline is unchanged — this is still one conditional UPDATE carrying the
    believed state, and only the caller whose `rowcount` is 1 proceeds.
    """
    with _db.db() as conn:
        rows = conn.execute(
            "SELECT id FROM land_bids WHERE status='placing' AND idem_key IS NOT NULL "
            "AND (claimed_at IS NULL OR claimed_at <= datetime('now', ?)) "
            "ORDER BY id ASC LIMIT ?",
            (f"-{int(older_than_minutes)} minutes", int(limit))).fetchall()
    n = 0
    for r in rows:
        with _db.db() as conn:
            cur = conn.execute(
                "UPDATE land_bids SET status='place_unknown', attempts=attempts+1, "
                "last_error=? WHERE id=? AND status='placing' "
                "AND (claimed_at IS NULL OR claimed_at <= datetime('now', ?))",
                ("left `placing` by a worker that never came back",
                 int(r["id"]), f"-{int(older_than_minutes)} minutes"))
            won = cur.rowcount == 1
        if won:
            log.warning("[land_escrow] bid row %s was left `placing` by a worker that "
                        "never came back. Re-armed to place_unknown; its key will be "
                        "re-sent and core asked what it did. Its coins MAY have been "
                        "reserved this whole time.", r["id"])
            n += 1
    return n


def replay_placements(older_than_minutes: int = 15, limit: int = 50) -> int:
    """Resolve every unresolved `place_hold`, then honour the lot it belongs to.

    Two steps, and the ORDER IS THE WHOLE POINT.

      1. Re-send the row's identical key. This is the only thing that can tell
         whether the original hold landed — you cannot release a reservation you
         have not established exists, and "release instead of replay" would leave
         a hold sitting at core with no row naming it, which is strictly worse than
         the bug it was meant to fix.
      2. THEN re-read the listing. If the lot went terminal while this row was in
         doubt — cancelled, sold, expired, rolled back — the reservation this call
         just resolved must not stand: `cancel`, `settle` and `expire` all ran
         their release loops before this row had a hold id to release, so nothing
         else will ever come back for it. Hand the whole lot to
         `retire_listing_escrow`, which releases per row.

    Step 2 covers the lot rather than just this row on purpose. The row that lost
    its answer is not special: if this lot is terminal and still has escrow open,
    every open hold on it is stranded by the same mechanism, and retiring one while
    walking past its siblings is the F3 bug with a smaller radius.

    So the old promise in this docstring — "either way the row stops being a bid
    whose coins nobody can account for" — is now true rather than aspirational. It
    was not: `land_escrow` re-placed the hold, `release_all_holds` never collected
    `place_unknown`, and the coins stayed reserved until the 24h TTL with nobody
    told (LAND_ESCROW_VERIFY.md F3).
    """
    rearm_stale_placements(older_than_minutes, limit)
    done, terminal_lots = 0, []
    for row in needing_attention(older_than_minutes, limit):
        if str(row.get("status")) != "place_unknown" or not row.get("idem_key"):
            continue
        landed = _resolve_placement(row)
        if landed == "held":
            done += 1
        # Re-read the lot AFTER the answer is on disk. `failed` rows reserve
        # nothing, so they need no retirement; anything else may.
        if landed != "failed" and listing_is_terminal(row["listing_id"]):
            lid = int(row["listing_id"])
            if lid not in terminal_lots:
                terminal_lots.append(lid)
    for lid in terminal_lots:
        try:
            retire_listing_escrow(lid, reason=f"lot #{lid} is over",
                                  older_than_minutes=older_than_minutes)
        except Exception as e:  # noqa: BLE001
            log.error("[land_escrow] listing %s is terminal with escrow still open "
                      "and retiring it failed: %s. sweep_terminal_listing_holds() "
                      "retries every minute.", lid, e)
    return done


# ══════════════════════════════════════════════════════════════════════════
# LAND_ESCROW_PLAN §3.6 check 2 — no open hold on a lot that is over
# ══════════════════════════════════════════════════════════════════════════
#
# The plan wrote this check down and nothing built it, which is why F3 survived
# a settle path, a cancel path and an expiry path that each looked correct on
# their own. Every one of them releases what it can SEE at the moment it runs;
# none of them comes back for a hold that became visible afterwards. This is the
# assertion that does not care how the hold got there.


def _settle():
    """`land_settle`, imported late. It imports THIS module at module scope, so a
    top-level import here is a cycle. Late is also honest: the release rules are
    `land_settle.release_row`'s and there must not be a second copy of them here.
    """
    import land_settle
    return land_settle


def listing_status(listing_id: Any) -> Optional[str]:
    """This lot's status right now, or None if it cannot be read.

    None is NOT "terminal". A read that fails must never be the reason a live
    auction's escrow is released, so every caller treats None as "leave it alone"
    and the next sweep pass asks again.
    """
    try:
        row = _db.get_land_listing(int(listing_id))
    except Exception as e:  # noqa: BLE001
        log.warning("[land_escrow] could not read listing %s (%s); treating it as "
                    "LIVE and leaving its escrow alone.", listing_id, e)
        return None
    if not row:
        return None
    st = row.get("status") if isinstance(row, dict) else row["status"]
    return str(st) if st is not None else None


def listing_is_terminal(listing_id: Any) -> bool:
    """True only for a status this module recognises as over. See the set."""
    return (listing_status(listing_id) or "") in TERMINAL_LISTING_STATUSES


def rows_holding_on_terminal_listings(limit: int = 50) -> list[dict]:
    """Every escrow row that may still reserve coins on a lot that is over.

    Resumes from ROW STATE joined to LISTING STATE, never from a cursor: a row
    that is retired leaves the candidate set because its status changes, so an
    interrupted sweep repeats nothing and skips nothing. `needing_attention()`
    makes the same argument and `FINDINGS.md` records the cursor version of it
    being got wrong once already.

    Ordered by listing then row so a resumed pass walks them in a stable sequence
    — "resume where it stopped" only means something if the order is fixed.
    """
    statuses = tuple(OPEN_HOLD_STATUSES)
    terminal = tuple(sorted(TERMINAL_LISTING_STATUSES))
    with _db.db() as conn:
        rows = conn.execute(
            f"SELECT b.* FROM land_bids b JOIN land_listings l ON l.id = b.listing_id "
            f"WHERE b.status IN ({','.join('?' * len(statuses))}) "
            f"AND l.status IN ({','.join('?' * len(terminal))}) "
            f"ORDER BY b.listing_id ASC, b.id ASC LIMIT ?",
            (*statuses, *terminal, int(limit))).fetchall()
    return [dict(r) for r in rows]


def retire_listing_escrow(listing_id: int, reason: str,
                          older_than_minutes: int = 15) -> dict:
    """End every open reservation on a lot that is over. Per row, marked per row.

    Refuses outright unless the listing reads terminal AT THIS MOMENT — re-read
    here rather than trusted from the caller, because between the sweep's query and
    this call a `settling` lot can come back to `active`, and releasing a live
    bid's escrow is a worse bug than the one being fixed.

    Two passes, in this order:

      1. Placement doubt, age-guarded. A `place_unknown` row is re-sent (rule: ask
         core, never guess) so that pass 2 has a hold id to release. `placing` rows
         older than the window are re-armed into it first. The age guard is what
         keeps this out of the way of a placement that is genuinely in flight — and
         it is also why `land_settle.release_all_holds` DEFERS these rather than
         doing them inline: a cancel cannot know whether a `placing` row's worker is
         still alive, and it must not make a fresh `place_hold` on a user-facing
         path.
      2. The release, which is `land_settle.release_all_holds` — not a second
         implementation of it. One claim per row, one call, one marker, so an
         interruption anywhere leaves each row either fully released or untouched.

    A row that ends in `*_unknown` leaves `OPEN_HOLD_STATUSES` and becomes
    `reconcile_holds`'s problem, so this sweep cannot spin on it; a parked
    `*_refused` row was never in scope and is reported by `refused_rows()`.
    """
    st = listing_status(listing_id)
    if st not in TERMINAL_LISTING_STATUSES:
        return {"ok": False, "outcome": "listing_live", "listing_id": int(listing_id),
                "listing_status": st, "released": [], "problems": [], "resolved": [],
                "deferred": []}
    rearm_stale_placements(older_than_minutes)
    resolved = []
    for row in rows_in(int(listing_id), PLACEMENT_IN_DOUBT_STATUSES):
        if str(row.get("status")) != "place_unknown" or not row.get("idem_key"):
            continue                       # still in flight, or nothing to re-send
        if not _aged(row, older_than_minutes):
            continue
        landed = _resolve_placement(row)
        resolved.append({"row": int(row["id"]), "status": landed})
        log.warning("[land_escrow] listing %s is %s and bid row %s had an unresolved "
                    "placement. Resolved it to %s so the reservation can be ENDED "
                    "rather than left standing on a lot nobody can settle.",
                    listing_id, st, row["id"], landed)
    out = _settle().release_all_holds(int(listing_id), reason)
    if out["released"]:
        log.warning("[land_escrow] listing %s is %s: retired %s stranded reservation(s) "
                    "(LAND_ESCROW_PLAN §3.6 check 2). %s",
                    listing_id, st, len(out["released"]),
                    "The lot's own close path did not see these — a hold that becomes "
                    "visible after the close is exactly what this check is for.")
    if out["deferred"]:
        # THE SWEEP IS THE READER OF LAST RESORT. Pass 1 above resolves placement
        # doubt so pass 2 can release it; a row still deferred AFTER both passes
        # is one whose placement was too young for the age guard, and its coins
        # may be reserved right now. Saying so is the difference between "this
        # lot is clean" and "this lot is clean except for row 7" — and the next
        # pass, one minute later, is what actually retires it.
        log.warning("[land_escrow] listing %s is %s and %s escrow row(s) are still "
                    "DEFERRED after this pass (%s): their placement is younger than "
                    "the %s-minute guard, so they could not be resolved or released "
                    "yet. Their coins may be reserved. The next sweep retires them.",
                    listing_id, st, len(out["deferred"]),
                    ", ".join(f"row {d['row']} ({d['status']})" for d in out["deferred"]),
                    older_than_minutes)
    return {"ok": True, "outcome": "retired", "listing_id": int(listing_id),
            "listing_status": st, "resolved": resolved,
            "released": out["released"], "problems": out["problems"],
            "deferred": out["deferred"]}


def sweep_terminal_listing_holds(older_than_minutes: int = 15, limit: int = 50) -> int:
    """§3.6 CHECK 2, as a sweep: no open hold references a lot that is over.

    THE BACKSTOP. Everything else in this module fixes one path; this asserts an
    invariant and does not care which path broke it. It is what would have caught
    F3 without anyone knowing F3 existed — and, on the day the report was written,
    also the second hold an interrupted instant buy leaves behind (F2), because
    both end the same way: a `held` row on a lot whose status says the lot is over.

    Returns the number of reservations retired. One indexed join per pass and, in
    the normal case, zero ledger calls — the query returns nothing.

    One bad lot must not stop the sweep, so a failure is a `continue` and never a
    `break`: the R4-1 shape, where one frozen row stalled 199 others and nothing
    was parked, so nothing was on any screen.
    """
    seen: list[int] = []
    for row in rows_holding_on_terminal_listings(limit):
        lid = int(row["listing_id"])
        if lid not in seen:
            seen.append(lid)
    retired = 0
    for lid in seen:
        try:
            out = retire_listing_escrow(lid, reason=f"lot #{lid} is over",
                                        older_than_minutes=older_than_minutes)
        except Exception as e:  # noqa: BLE001
            log.error("[land_escrow] listing %s: retiring stranded escrow failed (%s). "
                      "The bidder's coins are still reserved; this retries next pass.",
                      lid, e)
            continue
        retired += len(out.get("released") or [])
    return retired


def _aged(row: dict, older_than_minutes: int) -> bool:
    """Has this row's claim outlived the window? A NULL `claimed_at` counts as yes.

    Asked in SQL rather than in Python clock arithmetic, so it uses the same
    `datetime('now')` the claim itself was written with and cannot disagree with
    `needing_attention()` about what "15 minutes old" means.
    """
    with _db.db() as conn:
        r = conn.execute(
            "SELECT 1 FROM land_bids WHERE id=? AND (claimed_at IS NULL "
            "OR claimed_at <= datetime('now', ?))",
            (int(row["id"]), f"-{int(older_than_minutes)} minutes")).fetchone()
    return r is not None


def extend_for_listing(listing_id: int, ends_at_epoch: int, now_epoch: int) -> Optional[dict]:
    """Push this lot's open hold out to `ends_at + 24h`. Idempotent, no key.

    THE ANTI-SNIPE HALF (LAND_ESCROW_PLAN §5). A bid inside the anti-snipe window
    moves `ends_at`, and the top bidder's hold was placed with an expiry computed
    from the OLD `ends_at`. Extend the lot far enough and the hold expires before
    the lot closes: core's sweeper releases it — correctly; that is what expiry is
    for — the coins go back, and the lot closes on a winner with no escrow behind
    them. The seller is then paid out of a capture that fails.

    `extend_hold` sets an ABSOLUTE expiry from a relative TTL, so recomputing the
    TTL from `ends_at` on every extension converges: running it twice is harmless,
    which is why this is the one operation here with no stored key. Only one hold
    is open per lot under §2.3's model, so this is normally one call.

    It cannot outrun the ceiling either. `max_auction_days` caps `ends_at` at
    `starts_at + N` (the H4 hotfix), so the TTL asked for here is at most
    `N days + 24h` — far below `ledger_v2.MAX_HOLD_SECONDS` (400 days), which is
    the `bad_expiry` wall an UNCAPPED auction eventually hits, at which point the
    hold can no longer be extended and the lot becomes unsettleable. The cap is
    what makes this call provably always legal, which is why the two ship together.
    """
    ttl = int(ends_at_epoch) + HOLD_GRACE_SECONDS - int(now_epoch)
    out = None
    for row in held_rows(listing_id):
        if not row.get("hold_id"):
            continue
        try:
            out = ledger().extend(str(row["hold_id"]), ttl)
        except Exception as e:
            log.error("[land_escrow] listing %s: hold %s could NOT be extended to "
                      "cover the new deadline (%s). The lot now outlives its escrow; "
                      "sweep_hold_extensions() repairs it within a minute.",
                      listing_id, row["hold_id"], e)
            continue
        _mark(int(row["id"]), "held", hold_expires_at=out.get("expires_at"))
    return out


def sweep_hold_extensions(listings: Iterable[dict], epoch_of: Callable[[str], int],
                          limit: int = 200) -> int:
    """Re-extend any live lot whose escrow expires too close to its own close.

    THE GUARD HALF, and it is required rather than belt-and-braces: extending
    inside the bid is not sufficient, because a crash between the `ends_at` write
    and the `extend` call leaves a lot outliving its hold and nothing else would
    ever notice. Once a minute, for every active auction with a standing bid,
    assert `hold_expires_at > ends_at + 1h` and re-extend the ones that fail. One
    indexed read and, in the normal case, zero ledger calls.

    `epoch_of` is `cogs.land_exchange._epoch`, passed in rather than imported:
    this module has to stay importable without discord.py, which is what makes
    every rule above testable against a temp SQLite file.
    """
    import time
    now = int(time.time())
    fixed = 0
    for listing in list(listings)[:int(limit)]:
        if not listing.get("ends_at") or not listing.get("current_bidder"):
            continue
        end_ts = int(epoch_of(str(listing["ends_at"])))
        floor = end_ts + EXTENSION_FLOOR_SECONDS
        for row in held_rows(int(listing["id"])):
            have = row.get("hold_expires_at")
            if have and int(epoch_of(_sqlish(have))) > floor:
                continue
            log.warning("[land_escrow] listing %s: hold %s expires %s, which does not "
                        "outlive its lot (closes %s) — re-extending.",
                        listing["id"], row.get("hold_id"), have, listing["ends_at"])
            if extend_for_listing(int(listing["id"]), end_ts, now):
                fixed += 1
    return fixed


def _sqlish(ts: Any) -> str:
    """`ledger_v2` writes ISO-8601 with a T; `_epoch` parses SQLite's space form."""
    return str(ts)[:19].replace("T", " ")
