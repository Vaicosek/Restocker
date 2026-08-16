"""Restocker Land Exchange — /realestate: list, bid on, and buy land.

Built to beat two competitor Discords the owner is moving into this space against
(see HANDOFF_REALESTATE.md): one runs land sales purely through support tickets with
manual staff-typed "SOLD"/"still up" pings (no public bid history, no timers, a 4%
fee, $10k min bid step, bids can't be withdrawn); the other has a real forum-based
auction bot (live current-bid, scheduled start/end, quick-bid + custom-bid buttons,
region/status tags) — the harder benchmark. Neither ties land into anything else.

V Tech's edge (the actual moat, not just "an auction bot too"): every listing gets a
DEFENSIBLE reserve price from the existing AI valuation engine (chunks x rate,
quality-multiplied, folded with real comps — see cogs/valuation.value_plot), and a
sold plot can immediately back a listed company (65% rule) via the SAME
`valuate:land_claim:<market_id>` config key gather_and_value() already reads — no
new plumbing needed on the stock side.

ESCROW MODEL — read this before touching a money path.
------------------------------------------------------
A bid is a HOLD, not a debit. `land_escrow` reserves the amount against the bidder's
AVAILABLE balance (`available = balance - held`); the coins stay in their wallet and
simply cannot be spent twice. Being outbid RELEASES that reservation — nothing is
recomputed and nothing is refunded, because the same reservation is retired. Winning
CAPTURES it into `treasury:estates`, and `land_settle` then pays the seller out of the
treasury by a keyed transfer, net of commission.

WHICH MODULE OWNS WHAT, so a future change lands in one place instead of three:
`land_escrow` owns the bid ROW state machine and the ledger adapter; `land_settle`
owns the ORDER a close happens in, the listing fee, cancel and rent; this file owns
Discord, the listing state machine and the policy questions ("may this person cancel",
"is this bid high enough"). No money moves in this file.

This paragraph used to say the opposite, and it was the reason for the audit: it
claimed "the bidder's own balance row IS the hold — there is no separate escrow ledger
to reconcile", which described a debit with no hold row. Under that model a refund was
`int(round(current_bid))` recomputed at refund time from a float column, three
independent derivations of one number; a retried settlement paid the seller twice; and
`deduct_coins`' YAML fallback could move the coins with SQLite — and therefore the
escrow trigger — entirely bypassed. None of those failure modes has an equivalent here:
a release names a hold id, a capture is claimed before it debits, and this file no
longer imports `add_coins` or `deduct_coins` at all.

Every money call is keyed from the `land_bids` row id that `add_land_bid` has always
returned (`land:listing:<lid>:bid:<row id>[:capture|:release]`), written to disk BEFORE
the call, so a retry re-sends the identical key and core replays its own answer instead
of moving coins a second time. See `land_escrow.py` for why an in-process caller has to
go through `ledger_v2._idempotent` to get that, and LAND_ESCROW_PLAN §1.3 for the keys.

Commands live under `/realestate` rather than `/land` — `/land` is already a Group
owned by cogs.lands.LandsCog (treasury/feed ingestion) and discord.py cannot share
one app_commands.Group across two Cogs safely: a duplicate top-level Group name
raises CommandAlreadyRegistered, and even routing a second cog's subcommands into an
imported Group instance leaves them bound to the WRONG cog instance at runtime
(verified — the callback's `self` resolves to whichever cog registered the Group
first, not the cog that defined the subcommand). `/realestate` keeps this cog fully
self-contained — no edits to lands.py or main.py beyond the one load_extension line.
"""
import math
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks

import json

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
_market_autocomplete = core._market_autocomplete
_get_market = core._get_market
log = core.log
bot = core.bot

# THE TWO LEGACY MONEY IMPORTS THAT USED TO BE HERE ARE GONE, and this comment is
# the note that says so rather than a docstring claiming it.
#
# `add_coins` / `deduct_coins` both wrap their SQLite path in `except Exception` and
# fall through to a whole-table YAML rewrite (`Restocker_main:2326`) that writes
# `_save_balances(data)` directly — bypassing SQLite and therefore bypassing
# `ledger_balances_respect_holds`, the trigger installed to constrain them. An escrow
# guard that the error handler of the guarded function can walk past is not a guard.
# `Restocker_main` now re-raises `sqlite3.IntegrityError` instead of swallowing it
# (LAND_ESCROW_PLAN P0 item 3), which closes that hole for every caller in the bot —
# but the right answer for LAND was never to reach for these at all.
#
# `_place_bid_core` was the last caller and it now places a HOLD, so the two names
# are not bound in this module any more. That is deliberate and it is load-bearing:
# an unbound name is a NameError at the first line that reaches for one, which is a
# far better failure than a debit that silently bypasses the escrow trigger. Nothing
# in this file may re-add them — a bid, a refund and a settlement all have a keyed
# escrow call now, and `land_escrow` / `land_settle` own every one of them.

import cogs.valuation as _valuation  # value_plot() — the AI reserve-price helper
import action_log                    # the audit row + its reverse ops (see _record_* below)
import land_escrow as _esc           # bid-row state machine + the ledger v2 adapter
import land_settle as _settle        # close / settle / fee / cancel / rent, on escrow

# A shared URL stitches several embeds into ONE image gallery in Discord — that's how a
# single listing message shows multiple photos. Cosmetic; points at the exchange page.
_GALLERY_URL = "https://dashboard.vaicosmarket.com/lands"

DEF = dict(
    commission_pct=5.0,          # house cut on every completed sale
    listing_fee=0.0,             # flat fee charged up front to list (0 = off by default)
    min_increment_pct=5.0,       # minimum raise over the current bid, as a %
    min_increment_floor=1000.0,  # ...but never less than this many coins
    anti_snipe_minutes=5.0,      # a bid inside this window of the end extends it
    default_auction_days=7.0,    # matches the harder competitor's 7-day window
    max_auction_days=14.0,       # HARD deadline from starts_at — anti-snipe can never pass it
    # ── Loyalty (feeds the existing V Tech loyalty table; the moat the competitors lack) ──
    loyalty_flat=10.0,           # flat V Tech points to BOTH buyer & seller per completed sale
    loyalty_rate=0.0001,         # + this many points per coin of sale price (8.5M -> ~850 pts)
    loyalty_min_commission=1.0,  # loyalty discount never drops commission below this %
    # ── The kill switch (LAND_ESCROW_PLAN §P0.5). 0 = open, anything else = frozen ──
    # It lives in DEF because `set_exchange_config` only writes keys that are IN DEF:
    # a switch you cannot throw without a deploy is not a kill switch. See the
    # `bidding_frozen()` block below for what it does and, more importantly, what
    # it deliberately does NOT do.
    bidding_frozen=0.0,
)

# Loyalty tiers: total_earned points >= threshold → that many %-POINTS off the seller's
# commission, automatically at settlement. The more you use the house, the cheaper it gets
# to sell — a real "discount at V Tech" with no coupon to redeem. Override via config key
# realestate:loyalty_tiers (JSON list of [threshold, pct_off]).
_LOYALTY_TIERS = [(20000, 2.5), (10000, 1.5), (4000, 1.0), (1000, 0.5)]


def _loyalty_discount_pct(_db, total_earned) -> float:
    tiers = None
    try:
        raw = _db.get_config("realestate:loyalty_tiers")
        tiers = json.loads(raw) if raw else None
    except Exception:
        tiers = None
    tiers = tiers or _LOYALTY_TIERS
    te = float(total_earned or 0)
    for thr, pct in sorted(tiers, key=lambda t: -float(t[0])):
        if te >= float(thr):
            return float(pct)
    return 0.0


def _loyalty_award_points(_db, price) -> float:
    flat = _gd(_db, "loyalty_flat", DEF["loyalty_flat"])
    rate = _gd(_db, "loyalty_rate", DEF["loyalty_rate"])
    return round(flat + float(price or 0) * rate, 2)

_QUALITY = Literal["raw", "modest", "developed", "premium", "flagship"]
_MODE = Literal["fixed", "auction"]

# Category pick-list for /sell (a clickable dropdown — no typing, no typos). The two
# land categories also flip the listing to land-kind, so the Land notify role is pinged
# and the AI valuation applies. Add/rename here to change the menu.
_CATEGORIES = [
    app_commands.Choice(name="Land", value="Land"),
    app_commands.Choice(name="Artificial Land", value="Artificial Land"),
    app_commands.Choice(name="Weapons", value="Weapons"),
    app_commands.Choice(name="Artifacts", value="Artifacts"),
    app_commands.Choice(name="Other", value="Other"),
]
_LAND_CATEGORIES = {"Land", "Artificial Land"}


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _gd(_db, key, fallback):
    v = _db.get_config(f"realestate:{key}")
    return _num(v, fallback) if v not in (None, "") else fallback


def _fmt(n) -> str:
    try:
        return f"{float(n):,.0f}"
    except (TypeError, ValueError):
        return "0"


def _sql_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _sql_now_plus(*, days: float = 0, minutes: float = 0) -> str:
    return _sql_ts(datetime.now(timezone.utc) + timedelta(days=days, minutes=minutes))


def _epoch(sql_ts: str) -> int:
    try:
        dt = datetime.strptime(sql_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return int(datetime.now(timezone.utc).timestamp())


def _coin_amount(v):
    """A FINITE, POSITIVE coin amount, or None when the value cannot be money.

    NaN slips past every ordinary money guard in this file because every
    comparison against NaN is False: `amt < min_bid` does not reject it and
    `bal < amt` does not reject it. `json.loads` accepts a bare `NaN` token, so a
    partner server can push one through the satellite relay, and the only thing
    that stopped it was `int(float('nan'))` raising — one line PAST two guards
    that both failed open. Callers turn a None from here into a normal refusal.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def _min_next_bid(listing: dict) -> Optional[int]:
    """The smallest WHOLE number of coins a new bid must meet. None if unusable.

    An integer, and that is now a correctness requirement rather than a style
    choice. A hold amount is an integer by contract (`ledger_v2._coins` rejects
    anything else with `bad_amount`), so a listing whose `reserve` is `1000.6`
    used to make the FIRST bid on it unplaceable — `LAND_FLOAT_EXPOSURE.md` shows
    exactly that: fractions are born at bid #1 (`float(listing["reserve"])`) and
    extinguished at bid #2 (`round(cur + step)`).

    Rounding is UP, deliberately. A minimum rounded down would let a bid of 1000
    clear a 1000.6 reserve, which is a price cut nobody asked for; rounded up, the
    seller's floor is always met. `land_listings.reserve` stays REAL — this round
    does not convert the money columns (`land_money_migrate.py` owns that) — but
    from here on nothing derived from it reaches the ledger as a fraction.

    None means the listing's own price data is not a number (a non-finite reserve
    or current_bid). Every comparison against such a value is False, i.e. the
    listing would otherwise accept ANY amount from anyone; the callers refuse the
    listing instead.
    """
    cur = listing.get("current_bid")
    if cur is None:
        base = _coin_amount(listing.get("reserve"))
        return int(math.ceil(base)) if base is not None else None
    cur = _coin_amount(cur)
    if cur is None:
        return None
    pct = _num(listing.get("min_increment_pct"), DEF["min_increment_pct"])
    step = max(cur * pct / 100.0, DEF["min_increment_floor"])
    nxt = cur + step
    return int(math.ceil(nxt)) if math.isfinite(nxt) else None


def _photos_of(listing: dict) -> list:
    """Photo URLs for a listing (JSON `photos`, falling back to legacy `image_url`)."""
    raw = listing.get("photos")
    urls = []
    if raw:
        try:
            urls = [str(u) for u in json.loads(raw)]
        except Exception:
            urls = []
    if not urls and listing.get("image_url"):
        urls = [str(listing["image_url"])]
    return [u for u in urls if u.lower().startswith(("http://", "https://"))][:4]


def _listing_title(listing: dict) -> str:
    return (listing.get("title") or listing.get("land")
            or (listing.get("description") or "").strip()[:60] or f"Listing #{listing['id']}")


# ── THE KILL SWITCH: `realestate:bidding_frozen` ───────────────────────────────
#
# WHAT IT IS. One config key that stops NEW money entering the land exchange, in
# seconds, without a deploy. LAND_ESCROW_PLAN §P0 item 5 calls it "the universal
# rollback primitive", and it is the only rollback primitive that does not need a
# `git push` to a container that auto-pulls — the mechanism that crash-looped the
# server once already.
#
# WHERE IT IS CHECKED, AND WHY ONLY THERE. Exactly two places: the top of
# `_place_bid_core` and the top of `_instant_buy_core` — the only two paths by
# which a player's coins are newly RESERVED against a listing. Both checks sit
# above `create_bid_row`, so a frozen exchange writes no `land_bids` row and
# places no hold: a freeze that refuses at the Discord layer while the core still
# reserves coins is not a freeze.
#
# WHERE IT IS DELIBERATELY *NOT* CHECKED. Not in `_settle_gate`. That gate is
# shared by all seven money paths, and freezing settlement is the opposite of
# what an operator wants: money already in flight must still be able to LAND.
# A lot whose auction ends during a freeze still settles, the winner still gets
# the lot, the seller still gets paid, holds still release and the sweep still
# runs. The freeze stops money *entering*; it never stops money *completing*.
# Freezing settlement instead would strand every open hold until the TTL expired
# and turn a five-second precaution into the incident.
#
# Nor does it touch `create_listing_core` or `cancel`. Listing costs nothing to
# nobody but the seller's listing fee, and cancel/close are how an operator drains
# the board while frozen. Taking those away would leave staff unable to clean up.
#
# FAIL-CLOSED. An unreadable or non-numeric flag reads as FROZEN, not as open.
# The default (key absent, or empty) is open — that is the un-set state and the
# state the exchange ships in. Only an explicit off value opens a set flag.
FREEZE_KEY = "bidding_frozen"
_FREEZE_BY_KEY = "bidding_frozen_by"
_FREEZE_AT_KEY = "bidding_frozen_at"
_FREEZE_REASON_KEY = "bidding_frozen_reason"

#: Values that mean "open" once the key has been written at all.
_FREEZE_OFF_TOKENS = ("0", "0.0", "false", "no", "off", "none")


def _cfg_get(_db, key):
    """Raw string value of a `realestate:<key>` row. May raise — the callers below
    each decide what an unreadable config row means, and they do not agree."""
    return _db.get_config(f"realestate:{key}")


def bidding_frozen(_db=None) -> bool:
    """Is new bidding/buying frozen right now? One config read, no cache.

    NOT CACHED, on purpose. The whole value of this switch is that it takes
    effect on the next click, not on the next process restart, and a cache with a
    TTL is a switch with a delay measured in whatever the TTL is.
    """
    if _db is None:
        import Restocker_db as _db
    try:
        raw = _cfg_get(_db, FREEZE_KEY)
    except Exception:  # noqa: BLE001
        # Can't read the switch -> assume it is ON. The safe direction for a
        # thing whose only job is to stop money is to stop money.
        return True
    if raw in (None, ""):
        return False                       # never set == open, the shipped state
    s = str(raw).strip().lower()
    if s in _FREEZE_OFF_TOKENS:
        return False
    try:
        f = float(s)
    except ValueError:
        return True                        # somebody wrote words in it -> frozen
    if not math.isfinite(f):
        return True                        # NaN is not "off" (see _coin_amount)
    return f != 0.0


def freeze_state(_db=None) -> dict:
    """`{frozen, by, at, reason}` — everything the person who threw it needs.

    An operator flipping this at 02:00 has to be able to see it is ON, who turned
    it on and when, without reading the config table by hand. Every field is
    best-effort: a missing `by`/`at` never hides the `frozen` bit.
    """
    if _db is None:
        import Restocker_db as _db
    frozen = bidding_frozen(_db)
    out = {"frozen": frozen, "by": "", "at": "", "reason": ""}
    for name, key in (("by", _FREEZE_BY_KEY), ("at", _FREEZE_AT_KEY),
                      ("reason", _FREEZE_REASON_KEY)):
        try:
            out[name] = str(_cfg_get(_db, key) or "")
        except Exception:  # noqa: BLE001
            out[name] = ""
    return out


def set_bidding_frozen(frozen: bool, by: str = "", reason: str = "", _db=None) -> dict:
    """Throw or clear the switch, recording WHO and WHEN. Returns `freeze_state()`.

    Un-freezing is exactly as cheap as freezing — one call, same function, same
    command — because a kill switch nobody can confidently turn off is one nobody
    turns on.
    """
    if _db is None:
        import Restocker_db as _db
    frozen = bool(frozen)
    _db.set_config(f"realestate:{FREEZE_KEY}", "1.0" if frozen else "0.0")
    _db.set_config(f"realestate:{_FREEZE_BY_KEY}", str(by or "unknown")[:100])
    _db.set_config(f"realestate:{_FREEZE_AT_KEY}",
                   _sql_ts(datetime.now(timezone.utc)) if frozen else "")
    if frozen:
        _db.set_config(f"realestate:{_FREEZE_REASON_KEY}", str(reason or "")[:200])
    elif reason:
        _db.set_config(f"realestate:{_FREEZE_REASON_KEY}", str(reason)[:200])
    else:
        _db.set_config(f"realestate:{_FREEZE_REASON_KEY}", "")
    # One line in the ops log per flip. Rare by construction, and the operator
    # wants proof in the container log that the switch actually moved.
    print(f"{'🧊 LAND EXCHANGE FROZEN' if frozen else '✅ land exchange un-frozen'} "
          f"by {by or 'unknown'}{(' — ' + reason) if (frozen and reason) else ''}",
          flush=True)
    return freeze_state(_db)


def freeze_notice(state: Optional[dict] = None, _db=None) -> str:
    """One player-facing sentence for embeds/boards, or "" when the board is open.

    Deliberately reassuring: a player who sees this next to a lot they were about
    to bid on needs to know the pause is a precaution, not a loss.

    IT USED TO SAY "no coins have moved", FLATLY, AND THAT WAS FALSE FOR THE ONE
    READER IT MATTERED TO. A buyer whose instant-buy was interrupted after the
    capture landed has their coins in `treasury:estates` — not in their wallet,
    not reserved by a hold. The sentence was right for 99% of readers and wrong
    for exactly the reader with money at stake, and a ledger incident is both why
    the switch gets thrown and why captures get interrupted, so the two co-occur
    by construction rather than by coincidence.

    The fix is NOT to delete the reassurance — a sentence made true by being made
    frightening is a worse product, and `probe_copy_r5` K3b asserts the 99% still
    get told nothing is lost. It is to stop flattening the three states the rest
    of this system keeps carefully apart:

      AVAILABLE  in the wallet, spendable
      RESERVED   in the wallet, held against a bid, not spendable — no coins moved
      IN FLIGHT  out of the wallet, in the exchange's escrow, mid-purchase

    Only the third one moved, and it is the third one this sentence had nothing
    to say to. What it now promises that reader — that the purchase completes on
    its own — is true and is not a hope: the resume sweep picks up a part-settled
    lot regardless of its deadline and completes it EVEN WHILE FROZEN
    (`probe_gate_final` G1.3, `probe_ungate` U5.5, U6.2). If that sweep is ever
    re-gated, this sentence becomes a lie again and must change with it.
    """
    st = state if state is not None else freeze_state(_db)
    if not st.get("frozen"):
        return ""
    tail = f" ({st['reason']})" if st.get("reason") else ""
    return ("New bids and instant-buys are paused while staff check something"
            + tail + " — nothing you've already bid is lost. Coins reserved "
            "against a bid are still yours and go back to spendable when that "
            "lot closes; if the exchange had already taken coins for a buy of "
            "yours, they're sitting in escrow and that purchase still finishes "
            "on its own. Lots that are already closing will finish normally.")


FREEZE_HEADING = "🧊 Bidding paused"


# ── The listing state machine, and the ONE gate every money path passes ────────
#
# `active` is the only status in which a listing may take money. Everything else
# is terminal. This used to be seven copies of `if listing["status"] != "active"`
# spread over six functions and a SQL WHERE clause, which is fine right up until
# something puts a listing BACK into `active` — and then every one of those seven
# copies waves it through.
#
# That is exactly what happened. `_sale_reverse_ops` restored `status='active'`
# with the pre-settlement bidder still on the row, `auction_sweep_loop` polls
# `status='active' AND ends_at <= now` once a minute, and a rolled-back auction
# win was re-settled within 60 seconds — the seller paid `net` and the house
# `commission` a second time, out of nothing, against the SAME audit key, so no
# second button and no second embed ever appeared. Measured +40,000 coins.
#
# The fix is this vocabulary, not a check in the sweep: a rollback drives the
# listing to `rolled_back`, which is terminal, so every path below refuses it for
# the same reason and there is no path left to special-case.
LIVE_STATUS = "active"

#: Terminal statuses → what to tell the person who tried anyway. Real words, and
#: `rolled_back` says what to do next rather than just refusing.
CLOSED_STATUS_REASON = {
    "sold": "That listing has already sold.",
    "expired": "That auction has already ended.",
    "cancelled": "That listing was cancelled.",
    "rolled_back": ("A staff member rolled that sale back, so the listing is closed — "
                    "it can't be bid on, bought or settled again. The seller can put it "
                    "back up with `/sell`, which starts a fresh auction window."),
    # Not terminal — a transient claim. It is here because the honest sentence is
    # "wait", not "that isn't active": a bidder who sees this a second before the
    # hammer falls has not done anything wrong and the lot is not dead.
    "settling": ("That lot is being settled right now — give it a few seconds. "
                 "No new bids or buys can land while it closes."),
    "failed_escrow": ("That lot couldn't be settled: the winning bidder's coins were "
                      "no longer reserved when it closed, so nobody was charged and the "
                      "seller was not paid. Staff have been told. The seller can relist "
                      "it with `/sell`."),
    # A listing exists but is not on the board yet: created, fee not yet paid.
    # A draft that is never paid for stays a draft and never takes a bid.
    "draft": "That listing hasn't opened yet.",
}


def _settle_gate(listing: Optional[dict]) -> Optional[str]:
    """None if `listing` may still move coins; otherwise the refusal, in words.

    EVERY PATH THAT SETTLES, REFUNDS OR ESCROWS AGAINST A LISTING PASSES HERE.
    Enumerated, because "a listing that has been rolled back must not be
    re-settleable by ANY path" is only true if the list is exhaustive:

      1. `_place_bid_core`      — /realestate bid, the 💰 Bid button, and the
                                  satellite's POST /api/network/land/bid
                                  (`_record_network_land_bid` calls this core).
      2. `_instant_buy_core`    — /realestate buy, the 🛒 Buy now button, and
                                  POST /api/network/land/buy.
      3. `_finalize_sale_core`  — the settlement itself, reached from 2, from 4,
                                  and from 5.
      4. `close_listing_core`   — manager force-settle / unwind, headless twin of
                                  /realestate close, and POST /api/network/land/close.
      5. `LandExchangeCog._settle_expired` — the once-a-minute `auction_sweep_loop`.
      6. `cancel_listing_core`  — seller/manager cancel, and POST .../land/cancel.
      7. the `/realestate close` and `/realestate cancel` command handlers, which
         read the row themselves before delegating.

    `get_expired_active_listings()` also pre-filters on `status='active'` in SQL,
    so the sweep never even fetches a closed row. The gate re-checks anyway: the
    row can be rolled back between that SELECT and the settlement.

    The satellite (`RestockerLightWeight`) and the web layer add NO status logic
    of their own — `Restocker_main._record_network_land_*` are thin wrappers over
    the cores above, and the board they render comes from
    `get_active_land_listings()`, which is `status='active'` in SQL. A rolled-back
    lot simply is not on it.
    """
    if not listing:
        return "That listing doesn't exist."
    status = str(listing.get("status") or "")
    if status == LIVE_STATUS:
        return None
    return CLOSED_STATUS_REASON.get(status, "That listing isn't active.")


def _listing_embed(listing: dict, bids: Optional[list] = None) -> discord.Embed:
    status = listing["status"]
    color = {"active": 0x2ECC71, "sold": 0xF1C40F, "expired": 0x95A5A6,
             "cancelled": 0xE74C3C, "rolled_back": 0xE74C3C}.get(status, 0x3498DB)
    is_land = (listing.get("kind") or "item") == "land"
    icon = "🏡" if is_land else "📦"
    title = f"{icon} {_listing_title(listing)} · #{listing['id']}"
    embed = discord.Embed(title=title, description=(listing.get("description") or "")[:2000], color=color)
    # A frozen exchange says so WHERE THE PLAYER IS LOOKING, not only in the refusal
    # of an action they already tried. First field, so it is above the Bid button.
    if status == LIVE_STATUS:
        try:
            _notice = freeze_notice()
        except Exception:  # noqa: BLE001 — a banner never breaks a listing embed
            _notice = ""
        if _notice:
            embed.add_field(name=FREEZE_HEADING, value=_notice, inline=False)
    embed.add_field(name="Seller", value=f"<@{listing['seller_id']}>", inline=True)
    if listing.get("category"):
        embed.add_field(name="Category", value=f"`{listing['category']}`", inline=True)
    if is_land and listing.get("chunks"):
        embed.add_field(name="Chunks", value=f"`{_fmt(listing['chunks'])}`", inline=True)
    if is_land and listing.get("coords"):
        embed.add_field(name="Coords", value=f"`{listing['coords']}`", inline=True)
    if listing["mode"] == "auction":
        rlabel = "Starting price" if not is_land else "Starting / AI reserve"
        embed.add_field(name=rlabel, value=f"`{_fmt(listing['reserve'])}` 🪙", inline=True)
        cur = listing.get("current_bid")
        embed.add_field(
            name="Current bid",
            value=(f"`{_fmt(cur)}` 🪙 · <@{listing['current_bidder']}>" if cur else "*no bids yet — be first*"),
            inline=True)
        if listing.get("buy_now"):
            embed.add_field(name="Buy It Now", value=f"`{_fmt(listing['buy_now'])}` 🪙", inline=True)
        if status == "active" and listing.get("ends_at"):
            embed.add_field(name="Ends", value=f"<t:{_epoch(listing['ends_at'])}:R>", inline=True)
    else:
        embed.add_field(name="Price", value=f"`{_fmt(listing['buy_now'])}` 🪙", inline=True)
    if is_land and listing.get("market_id"):
        embed.add_field(name="Backs company", value=f"`{listing['market_id']}`", inline=True)
    if status == "sold":
        embed.add_field(name="🎉 Sold", value=f"`{_fmt(listing['sold_price'])}` 🪙 to <@{listing['sold_to']}>",
                        inline=False)
    elif status == "expired":
        embed.add_field(name="Result", value="⌛ Ended — no winning bid.", inline=False)
    elif status == "cancelled":
        embed.add_field(name="Result", value="🚫 Cancelled by the seller.", inline=False)
    elif status == "rolled_back":
        # This lot is closed and nobody can bid on it — say that on the listing
        # itself, so nobody reads a closed lot with no bid on it as a bug.
        #
        # WHAT THIS DOES NOT SAY ANY MORE: "everyone involved got their coins
        # back". `status` is one value covering two very different money states —
        # a manager unwind, where nothing moved and the reservation was released,
        # and a SALE rollback, where the price is sitting in `treasury:estates`
        # and comes back only when staff run the compensating transfers by hand.
        # One sentence cannot be true of both, and the one that was here was
        # false for the second and read by the person it was false about. This
        # listing has no way to tell them apart, so it claims neither.
        embed.add_field(
            name="Result",
            value=("↩️ This sale was reversed by staff. The lot is closed and cannot "
                   "be bid on; the seller can put it up again with `/sell`. If coins "
                   "were involved, staff will confirm where they ended up — ask them "
                   "rather than assuming."), inline=False)
    if bids:
        lines = [f"`{_fmt(b['amount'])}` — <@{b['bidder_id']}>" for b in bids[:5]]
        embed.add_field(name="Recent bids", value="\n".join(lines), inline=False)
    tail = "Escrow auto-settles on close — no 'DM the owner to finalize'."
    if is_land:
        tail = "AI-valued · " + tail
    embed.set_footer(text=tail)
    return embed


def _gallery_embeds(listing: dict, bids: Optional[list] = None,
                    attachment_names: Optional[list] = None) -> list:
    """The listing as a list of embeds — the first carries all the detail, and extra
    embeds sharing _GALLERY_URL stitch additional photos into one gallery. When
    `attachment_names` is given (files live ON the message), images are referenced with
    attachment://<name> so they NEVER expire; otherwise the stored photo URLs are used
    (fine for the remote satellite board, though Discord CDN URLs can expire)."""
    main = _listing_embed(listing, bids)
    imgs = ([f"attachment://{n}" for n in attachment_names] if attachment_names
            else _photos_of(listing))
    if not imgs:
        return [main]
    main.url = _GALLERY_URL
    main.set_image(url=imgs[0])
    out = [main]
    for u in imgs[1:4]:
        e = discord.Embed(url=_GALLERY_URL)
        e.set_image(url=u)
        out.append(e)
    return out


async def _listing_autocomplete(interaction: discord.Interaction, current: str):
    import Restocker_db as _db
    current = (current or "").strip().lower()
    out = []
    for r in _db.get_active_land_listings():
        label = f"#{r['id']} " + (r.get("land") or (r.get("description") or "Land")[:30])
        if current and current not in label.lower() and current != str(r["id"]):
            continue
        price = r.get("current_bid") or r.get("reserve") or r.get("buy_now") or 0
        out.append(app_commands.Choice(name=f"{label} — {r['mode']} · {_fmt(price)}c"[:100], value=r["id"]))
        if len(out) >= 25:
            break
    return out



async def _land_autocomplete(interaction: discord.Interaction, current: str):
    """Land claims the bot has actually seen — from balances, fee history and bindings.

    The land name on a listing is passed straight through to create_land_listing with
    NO validation, so a typo produces a listing tied to nothing while looking perfectly
    correct. The sibling market_id on this same command has had a picker all along,
    which made the inconsistency visible without fixing it.
    """
    names = {}
    try:
        import Restocker_db as _db
        with _db.db() as conn:
            for q in ("SELECT land FROM land_balances",
                      "SELECT DISTINCT land FROM land_fees",
                      "SELECT DISTINCT land FROM land_ledger"):
                try:
                    for r in conn.execute(q).fetchall():
                        nm = str(r[0] or "").strip()
                        if nm:
                            names.setdefault(nm.lower(), nm)
                except Exception:
                    continue
        # Bound lands too: land_map:<lowername> -> market id. A land can be bound before
        # its first balance ever arrives, and that is exactly when someone lists it.
        try:
            for k in (_db.get_config_prefix("land_map:") or {}):
                nm = str(k).split(":", 1)[1].strip()
                if nm:
                    names.setdefault(nm.lower(), nm)
        except Exception:
            pass
    except Exception:
        return []
    cur = (current or "").strip().lower()
    out = []
    for key in sorted(names):
        if cur and cur not in key:
            continue
        out.append(app_commands.Choice(name=names[key][:100], value=names[key]))
        if len(out) >= 25:
            break
    return out


# ── Headless core (NO Discord I/O) — the single code path for both slash commands and
#    the /api/network/land/* endpoints the satellite calls. Money moves here; the callers
#    only handle presentation (a slash reply, or the satellite's board + the home embed
#    refresh). Keeping the escrow/settlement math in ONE place is the whole point — a
#    forked copy in the network layer is exactly the bug we don't want. ─────────────────
def _listing_for_network(l: dict, frozen: Optional[bool] = None,
                         notice: Optional[str] = None) -> dict:
    """Compact, JSON-safe summary of a listing for the satellite board.

    `bidding_frozen` / `bidding_frozen_notice` ride on every row so a partner
    board can show the freeze. The SATELLITE HOLDS NO DB AND MAKES NO MONEY
    DECISION and that does not change here — it renders a string this side
    computed, exactly like `min_next_bid`. It stays a relay. The deployed
    satellite ignores unknown keys, so this is additive and needs no satellite
    deploy to be safe; it needs one to be *visible* (see the report).
    """
    if frozen is None:
        try:
            st = freeze_state()
            frozen, notice = st["frozen"], freeze_notice(st)
        except Exception:  # noqa: BLE001 — never fail a board render on a flag read
            frozen, notice = False, ""
    price = l.get("current_bid") or l.get("reserve") or l.get("buy_now") or 0
    photos = _photos_of(l)
    return {
        "bidding_frozen": bool(frozen),
        "bidding_frozen_notice": (notice or "") if frozen else "",
        "id": l["id"],
        "kind": l.get("kind") or "item",
        "title": _listing_title(l),
        "category": l.get("category"),
        "mode": l["mode"],
        "chunks": l.get("chunks"),
        "quality": l.get("quality"),
        "reserve": l.get("reserve"),
        "current_bid": l.get("current_bid"),
        "current_bidder": l.get("current_bidder"),
        "buy_now": l.get("buy_now"),
        "min_next_bid": (_min_next_bid(l) if l["mode"] == "auction" else None),
        "price": price,
        "commission_pct": l.get("commission_pct"),
        "ends_at_epoch": (_epoch(l["ends_at"]) if l.get("ends_at") else None),
        "market_id": l.get("market_id"),
        "coords": l.get("coords"),
        "description": l.get("description"),
        "photos": photos,
        "image_url": (photos[0] if photos else l.get("image_url")),
    }


def network_land_listings(limit: int = 25) -> list:
    """Active listings as plain dicts for the satellite / network API. Headless."""
    import Restocker_db as _db
    rows = _db.get_active_land_listings()
    # ONE flag read for the whole board, not one per row.
    try:
        st = freeze_state(_db)
        frozen, notice = st["frozen"], freeze_notice(st)
    except Exception:  # noqa: BLE001
        frozen, notice = False, ""
    return [_listing_for_network(r, frozen, notice) for r in rows[:max(1, int(limit))]]


# ── Audit rows with an undo (action_log / the ↩ Rollback button) ────────────────
# WHICH ACTIONS GET ONE, AND WHY THESE.
#
# Not every action. An audit row costs an embed in the ops channel and a button
# somebody might press at 02:00, so a log that fires on every bid is a log nobody
# reads — and a log that fires on nothing is what this bot shipped before. The
# rule applied here is: an action earns a row when it (a) moves coins between
# people or destroys a record, AND (b) has no user-facing undo of its own, AND
# (c) its exact reverse is computable at the moment it runs.
#
# IN, from this file:
#   * `_finalize_sale_core` — settlement. Coins leave a buyer and arrive split
#     between a seller and the house, a pre-empted bidder is refunded, the listing
#     is marked sold and (when it backs a company) the share-price input moves.
#     Every path into a sale goes through it: /realestate buy, the Buy button, an
#     auction ending, and a manager's force-settle. One producer covers four.
#   * the manager unwind (`/realestate close refund_bidder:true` and
#     `close_listing_core(refund_bidder=True)`) — staff, destructive, and it hands
#     a bidder back coins while killing the listing. If it was the wrong listing,
#     nothing else in the bot remembers what it looked like.
#
# OUT, deliberately:
#   * `_place_bid_core` — highest-frequency money movement in the cog, and it
#     already has a user-facing undo built in: being outbid refunds you in full.
#     A row per bid is the noise that makes the channel unreadable.
#   * `create_listing_core` / `cancel_listing_core` — cancel is refused outright
#     once a bid is held (:555), so it destroys nothing anyone paid for.
#   * expiry with no bids — no coins moved, and the listing row is still there.
def _sale_reverse_ops(listing: dict, buyer_id, price: float, res: dict) -> list[dict]:
    """The reverse of the settlement `res` describes, as action_log ops.

    Built from the listing as it was BEFORE the update and from the figures the
    settlement actually computed — not recomputed later from the sold row, where
    the commission percentage and the pre-empted bidder are already gone.

    WHAT THIS OP LIST MOVES: no coins. Under escrow the money legs are a staff
    task carrying the exact compensating transfers, because the automatic version
    would MINT — see the comment on the ops themselves. What the button still
    does automatically is void the listing and back the commission out of the
    reporting scalar. Do not restore the coin ops without building the
    compensating ledger run first.

    WHAT THE LISTING BECOMES, AND WHY IT IS NOT `active`
    ---------------------------------------------------
    `rolled_back`, which is terminal (see `_settle_gate`). This function used to
    restore `status='active'` together with the pre-settlement
    `current_bid`/`current_bidder`, and that combination is a mint:

      * On an AUCTION WIN the buyer IS the standing bidder, and the first op
        above has just returned that exact escrow to them. Restoring the bidder
        leaves the row claiming a 40,000-coin hold that no longer exists —
        `auction_sweep_loop` sees `active` past `ends_at` within 60 seconds and
        settles it again, paying the seller `net` and the house `commission` out
        of nothing. `sale_action_key` is unchanged, so `action_log.record`
        returns the EXISTING row: same audit line, no second button, nothing in
        the ops channel. Measured +40,000. `close_listing_core(refund_bidder=
        True)` on the same restored row paid the winner their bid a second time.

      * On an INSTANT BUY over a live auction the pre-empted bidder is real, and
        the old op list clawed their refund back to re-escrow them. But
        `adjust_balance_tx` floors a clawback at 0 (money review §6) — a bidder
        who has spent the refund is clawed back to zero, the op still reports
        `done` with a shortfall task, and the setfields beside it still restores
        `active` with their name on it. The sweep then settles an auction backed
        by an escrow that is only partly there. Same mint, one player-behaviour
        precondition away.

    So: a rollback NEVER re-opens an auction and NEVER claws a refund back out of
    a player's balance. It voids the listing. The escrow question resolves the
    only way that cannot mint — everyone who was holding coins at settlement time
    keeps what settlement gave them until a human moves them:

        op 0  `manual`     the WHOLE money reversal, as a staff task: treasury ->
                           buyer `price`, seller -> treasury `net`, with the keys.
                           It declares `coins=price`, which is what puts the real
                           figure on the confirm dialog instead of the
                           commission. NOTHING here moves those coins.
        op 1  `platform`   -commission, and this is a REPORT, not money. The
                           commission itself is real and sits in `treasury:
                           estates`; this op corrects the scalar the dashboards
                           read. Backing it out does not give anyone coins.
        op 2  `setfields`  the listing goes `rolled_back`, terminal, with no bid.
        then               a `manual` per person who needs telling.

        buyer            is STILL OUT `price` until op 0 is done by a human
        seller           STILL HAS `net` until op 0 is done by a human
        pre-empted bidder keeps the refund settlement already paid them
        the listing       holds no bid and takes no money ever again

    That is why the button is not called ↩ Rollback. `action_log.undo_kind()`
    reads the declared `coins` on op 0 and the surface renders
    `↩ Reverse status · coins by hand`, with the by-hand figure above the table
    on the confirm dialog. Owner's decision, 15 Aug: rename it rather than
    half-build the executor. The compensating run is deferred, not rejected;
    when it is built it needs its own idempotency and audit treatment, and op 0
    becomes real ops again.

    Re-listing is the seller's decision, not staff's: `/sell` mints a NEW listing
    with a fresh `ends_at`. That is the answer to "is a rolled-back listing
    re-listable, by whom, and with what deadline" — never in place, because a
    listing restored to `active` past its own `ends_at` is settled unattended by
    the sweep before anyone reads the ops channel.
    """
    lid = int(listing["id"])
    price_i = int(round(float(price)))
    net_i = int(res.get("net") or 0)
    comm_i = int(res.get("commission") or 0)
    # ── THE THREE COIN LEGS ARE NOW A STAFF TASK, AND THAT IS NOT A COP-OUT ──
    #
    # They used to be `{"t": "coins", …}` ops: credit the buyer `price`, debit the
    # seller `net`, debit the platform scalar `commission`. Under the old debit
    # model that roughly conserved, because the buyer's coins had genuinely been
    # destroyed out of their wallet and the commission had been destroyed too.
    #
    # Under escrow it MINTS. The buyer's `price` did not vanish — it was captured
    # into `treasury:estates`, which then paid `net` out and KEPT `commission` as
    # real coins. `{"t": "coins"}` is `adjust_balance`, which credits from nothing
    # and never touches the treasury: crediting the buyer `price` while the
    # treasury still holds `commission` puts coins into the economy that no
    # account gave up. `estates` was chosen precisely because it cannot mint, and
    # a rollback that mints through the side door is worse than one that asks.
    #
    # The correct reversal is a COMPENSATING LEDGER RUN — treasury -> buyer,
    # seller -> treasury, each under its own `…:reverse:` key, failing loudly if
    # the seller has already spent the coins rather than clawing them back
    # through a floor-at-zero debit. LAND_ESCROW_PLAN §6 item 9 says build that
    # when the owner needs it and build it as a run, not as a deletion. Until it
    # exists, this op prints the exact three calls with the exact figures, which
    # is honest and takes two minutes, instead of a green button that inflates
    # the economy by `commission` every time it is pressed.
    ops: list[dict] = [
        {"t": "manual",
         # DECLARED EXPOSURE. Without this the confirm dialog's headline is
         # `money_total()`, whose only contributor here is the -commission
         # reporting mirror: a 40,000-coin sale rendered "Coins this will move:
         # 2,000". The figure a human confirms has to be the figure the action
         # is about, and under escrow that number lives on this op or nowhere.
         "coins": price_i,
         "what": f"Reverse the money on land sale #{lid} ({price_i:,} coins) by hand",
         "hint": (f"Escrow settled this lot, so the coins are REAL and in known "
                  f"accounts — do not use a coin adjustment, it would mint. "
                  f"Run, in this order:\n"
                  f"  1. transfer {_esc.TREASURY} -> <@{buyer_id}> {price_i:,} "
                  f"(key land:listing:{lid}:reverse:buyer)\n"
                  f"  2. transfer <@{res['seller_id']}> -> {_esc.TREASURY} "
                  f"{net_i:,} (key land:listing:{lid}:reverse:seller)\n"
                  f"If step 2 refuses with `insufficient`, the seller has already "
                  f"spent it: STOP, do not force it, and decide whether the house "
                  f"eats the difference — {_esc.TREASURY} is currently holding the "
                  f"{comm_i:,} commission from this sale and can absorb it, and it "
                  f"is allowed to go negative and say so rather than silently "
                  f"failing.")},
    ]
    if comm_i > 0:
        # Reporting mirror only — the scalar store is not where the commission
        # lives any more, so backing it out here corrects the REPORT, not the money.
        ops.append({"t": "platform", "amount": -comm_i,
                    "month": "", "market_id": str(listing.get("market_id") or "")})
    # The listing goes terminal, with NO bid on it. `closed_at` is deliberately
    # not in `fields`: the listing did close, at settlement time, and clearing it
    # would make a dead row read as an open one to anything reading dates.
    ops.append({"t": "setfields", "table": "land_listings", "where": {"id": lid},
                "fields": {"status": "rolled_back", "sold_price": None,
                           "sold_to": None, "current_bid": None,
                           "current_bidder": None}})
    prev_bidder = listing.get("current_bidder")
    if prev_bidder and str(prev_bidder) != str(buyer_id):
        # An instant buy that pre-empted a live auction. Settlement already
        # refunded this bidder in full and they keep it — see the docstring for
        # why the alternative (claw it back, re-open the auction) mints. Their
        # auction disappearing without a word is a person-shaped problem, so it
        # opens a staff task with the figure and a name, not an id.
        prev_bid = int(round(float(listing.get("current_bid") or 0)))
        ops.append({"t": "manual",
                    "what": f"Tell <@{prev_bidder}> their bid on “{_listing_title(listing)}” "
                            f"is not coming back",
                    "hint": f"They held the top bid at {prev_bid:,} coins when the buyer "
                            f"took it with Buy-Now. Settlement refunded that {prev_bid:,} "
                            f"in full and this rollback has NOT taken it back — they are "
                            f"whole. What they lost is the auction: lot #{lid} is now "
                            f"`rolled_back` and cannot be bid on again. If it should go "
                            f"back up, the seller re-lists it with `/sell` and this bidder "
                            f"bids again."})
    pts = float(res.get("loyalty_points") or 0)
    if pts:
        ops.append({"t": "loyalty", "user_id": str(res["seller_id"]),
                    "market_id": None, "points": -pts})
        if str(buyer_id) != str(res["seller_id"]):
            ops.append({"t": "loyalty", "user_id": str(buyer_id),
                        "market_id": None, "points": -pts})
    if listing.get("market_id"):
        # `valuate:land_claim:<mid>` is a bot_config key, which is not on
        # action_log's table allowlist and has no previous value stored anywhere.
        # Say so rather than pretend: this becomes a staff task with the figure.
        ops.append({"t": "manual",
                    "what": f"Land backing for {listing['market_id']} still reads "
                            f"{price_i:,} coins",
                    "hint": f"Sale #{lid} set config `valuate:land_claim:"
                            f"{listing['market_id']}` to {float(price_i)}. That figure "
                            f"feeds the 65% land haircut in gather_and_value(), so the "
                            f"share price is still pricing in a plot that is no longer "
                            f"sold. Clear or restore the key by hand."})
    return ops


def sale_action_key(listing_id) -> str:
    """Caller-minted, derived from the domain event, stable across re-reads.

    A listing settles at most once, so the listing id IS the event. Re-recording
    returns the existing row instead of a second Rollback button pointing at the
    same money.

    "At most once" is load-bearing here and it is not free. It holds because
    `status` leaves `active` inside `_finalize_sale_core` and NOTHING puts it
    back: `_sale_reverse_ops` drives a rolled-back listing to the terminal
    `rolled_back`, and `_settle_gate` refuses every settle path on it. When the
    rollback DID restore `active`, this key was silently wrong — the second
    settlement found the first row already there, `record()` returned it
    unchanged, and a real 40,000-coin payout went into the ops channel as
    nothing at all. If a future change ever makes a settled listing settleable
    again, this key has to grow an attempt counter with it.
    """
    return f"land:sale:{int(listing_id)}"


def unwind_action_key(listing_id) -> str:
    return f"land:unwind:{int(listing_id)}"


def _record_unwind(listing: dict, actor_id=None, actor_name: str = "") -> str:
    """Audit row for a manager force-unwind. Returns the action key to post.

    Called from BOTH unwind sites — the `/realestate close` handler and the
    headless `close_listing_core` — with the same key, so whichever one runs, the
    row exists exactly once.

    WHAT STOPS A SECOND REFUND. Three things, and only the third is new:

      1. Pressing ↩ twice on this row cannot re-run an op: each one claims
         `rb:<action>#<index>` in `sys_action_op_effects` inside its own commit
         (`action_log._apply_op`), so the second press reads "already applied".
      2. Unwinding twice cannot happen either — but NOT because of anything in
         this function. The first unwind takes the listing out of `active`, and
         the two callers refuse it on the way in: `close_listing_core` and the
         `/realestate close` handler each run `_settle_gate` on the row before
         they reach this recorder. This function is handed a listing that has
         already passed that gate; it does not re-check one.
      3. Neither of those covered the path that actually paid twice: the SALE
         rollback restored `active` with the winner still on the row, and a
         manager `/realestate close refund_bidder:true` on that restored listing
         handed them their 40,000 back a second time (500,000 → 540,000). Fixed
         at the state level — a rolled-back listing is `rolled_back`, which fails
         rule 2 above, so the refund branch is unreachable on it.

    AND WHY THIS REVERSE DOES NOT RE-ESCROW. It used to claw the refund back and
    set the lot to `active` again, in two separate ops. Both halves are unsafe on
    their own:

      * The clawback floors at 0 (`adjust_balance_tx`) when the bidder has spent
        the refund. The op still reports `done` with a shortfall task beside it,
        and the setfields still runs — leaving a LIVE auction whose escrow is
        partly imaginary. The sweep settles it and the seller is paid from
        nothing.
      * `closed_at=None` with the original `ends_at` restores a listing that is
        already past its own deadline, so `auction_sweep_loop` settles it inside
        a minute — unattended, on a row a manager touched seconds earlier.

    So the reverse voids the listing (`rolled_back`, terminal) and reports the
    released reservation as a note for a human, with the figure and the name —
    a note, not a task with an exposure, because under escrow nothing moved and
    there is nothing for anyone to move back. A manager who
    unwound the wrong lot recovers by having the seller re-list it with `/sell`;
    the bidder keeps their coins throughout and bids again. Nobody is ever short,
    and there is no automatic path that takes coins back out of a player's
    balance for a listing.
    """
    lid = int(listing["id"])
    ops: list[dict] = []
    ops.append({"t": "setfields", "table": "land_listings", "where": {"id": lid},
                "fields": {"status": "rolled_back"}})
    if listing.get("current_bidder"):
        _bid = int(round(float(listing.get("current_bid") or 0)))
        # NO `coins` KEY, deliberately: this task moves nothing and asks nobody
        # to move anything. Declaring an exposure here would put {_bid:,} on the
        # confirm dialog under "coins a human must move", which is the same
        # class of lie in the other direction.
        ops.append({"t": "manual",
                    "what": f"<@{listing['current_bidder']}>'s {_bid:,}-coin reservation "
                            f"on “{_listing_title(listing)}” stays released",
                    "hint": f"Lot #{lid} is now `rolled_back` and cannot be settled by "
                            f"any path. Under escrow their {_bid:,} coins were never "
                            f"taken — the unwind ended the RESERVATION, so their balance "
                            f"is untouched and their `available` went back up. This "
                            f"rollback deliberately does NOT re-reserve it, because "
                            f"re-placing a hold against a balance they may have since "
                            f"spent would leave the lot live with an escrow that is only "
                            f"partly there. To put the lot back on the market, the seller "
                            f"re-lists with `/sell` (fresh auction window) and this bidder "
                            f"bids again."})
    who = _listing_title(listing)
    bid = int(round(float(listing.get("current_bid") or 0)))
    action_log.record(
        "land_unwind",
        # "refunded" is the word this row used to use and it was never true under
        # escrow: no coins moved, the standing bidder's RESERVATION was released.
        # `close_listing_core`'s docstring promises the audit row says it in
        # those words — this is the row that has to keep that promise.
        (f"Unwound auction lot “{who}” — the standing bidder's {bid:,}-coin "
         f"reservation released (no coins moved)"
         if bid else f"Closed auction lot “{who}” with no bid held"),
        ops, actor_id=actor_id, actor_name=actor_name,
        action_key=unwind_action_key(lid))
    return unwind_action_key(lid)


def _finalize_sale_core(listing_id: int, buyer_id, price: float, *,
                        win_row_id=None, note_reason: str = "sale") -> dict:
    """Settle a lot onto escrow, then do the non-money side effects. Headless.

    THE MONEY IS NOT HERE ANY MORE. `land_settle.settle_listing` claims the
    listing, captures the winner's hold into `treasury:estates`, releases every
    other hold one row at a time, pays the seller out of the treasury under
    `land:listing:<id>:settle:seller`, and flips the row to `sold` — in that
    order, each step marked before and after, so an interrupted settle resumes
    exactly where it stopped and cannot pay anybody twice.

    What is left here is everything that is NOT money: the company-backing config
    write, loyalty points, and the audit row with its ↩ Rollback button. They run
    only when a sale actually happened (`outcome == "sold"`), never on a replay
    of one that already had.

    HONEST LIMITS, because this docstring is the one a future round will trust:

      * These side effects are NOT idempotent and are NOT in the key scheme. A
        settle that resumes past step 4 will not double-pay anybody, but it can
        award loyalty points a second time. Low value, non-zero annoyance,
        named rather than hidden (LAND_ESCROW_PLAN §8 item 7).
      * The pre-empted-bidder refund that used to be step 1 is GONE, not moved.
        Under escrow the pre-empted bidder's coins were never taken, so there is
        nothing to refund — `release_all_holds` retires their reservation inside
        the settlement instead, and their balance is untouched throughout.
    """
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    gate = _settle_gate(listing)          # settle path 3 — reached from 2, 4 and 5
    if gate:
        return {"ok": False, "error": gate}
    res = _settle.settle_listing(listing_id, buyer_id=buyer_id, price=price,
                                 win_row_id=win_row_id, note_reason=note_reason)
    if not res.get("ok") or res.get("outcome") != "sold":
        return res

    price_f = float(res.get("price") or price)
    seller_id = res["seller_id"]
    if listing.get("market_id"):
        # Pillar 5 — the plot immediately backs the company: gather_and_value() already
        # reads this exact config key at the land haircut (65% rule). No new plumbing.
        _db.set_config(f"valuate:land_claim:{listing['market_id']}", str(price_f))
    # Award V Tech loyalty points to BOTH sides of a real sale (can't be farmed — coins
    # actually moved). Feeds the existing loyalty table, so the loyalty hub sees it.
    pts = _loyalty_award_points(_db, price_f)
    try:
        _db.add_loyalty_points(str(seller_id), pts)
        if str(buyer_id) != str(seller_id):
            _db.add_loyalty_points(str(buyer_id), pts)
    except Exception as e:
        log.warning("[realestate] loyalty award failed for #%s: %s", listing_id, e)
    res["loyalty_points"] = pts

    # THE AUDIT ROW, written LAST and on every settlement path — including the
    # headless satellite one, which has no interaction and no channel. Order is
    # deliberate: an undo button for money that never moved is worse than no
    # button, so this runs after the coins and the listing row are already
    # committed. `listing` is the pre-settlement snapshot read at the top of this
    # function, which is the only place the pre-empted bidder and the pre-sale
    # commission still exist.
    #
    # It must never be able to fail a settlement that has already happened.
    try:
        action_log.record(
            "land_sale",
            f"Sold “{_listing_title(listing)}” for {int(round(price_f)):,} coins",
            _sale_reverse_ops(listing, buyer_id, price_f, res),
            actor_id=buyer_id, actor_name=str(_db.get_ign(str(buyer_id)) or ""),
            action_key=sale_action_key(listing_id))
        res["action_key"] = sale_action_key(listing_id)
    except Exception as e:  # noqa: BLE001
        log.warning("[realestate] sale #%s settled but its audit row failed: %s",
                    listing_id, e)
    return res


def _capped_duration(_db, days: float) -> float:
    """A listing's length, capped at `max_auction_days`. Applied at CREATION.

    `max_auction_days` already stops anti-snipe extending a lot forever, but
    nothing capped the length a seller asks for on day one — and under escrow
    that is no longer only an integrity question, it is a money one. A bid's hold
    lives to `ends_at + 24h` (`_bid_hold_ttl`), and `ledger_v2.MAX_HOLD_SECONDS`
    is 400 days. A lot created with `duration_days=500` therefore asks for a TTL
    core refuses, and EVERY bid on it comes back `bad_expiry` — an opaque refusal
    the bidder reads as "my money is no good", on a lot that looks perfectly
    normal on the board.

    Capping at creation is the fix that keeps the guarantee rather than papering
    over it: clamping the TTL instead would hand out a hold that expires before
    its own lot closes, which is the §5 failure — the lot closes on a winner
    whose escrow the sweeper has already released. With the cap, `max_auction_days`
    (default 14) + 24h is two orders of magnitude below the ceiling and
    `bad_expiry` is unreachable from this path.
    """
    cap = _num(_gd(_db, "max_auction_days", DEF["max_auction_days"]),
               DEF["max_auction_days"])
    if not (cap > 0) or not math.isfinite(cap):
        cap = DEF["max_auction_days"]
    want = _num(days, DEF["default_auction_days"])
    if not (want > 0) or not math.isfinite(want):
        want = DEF["default_auction_days"]
    return min(want, cap)


def _bid_hold_ttl(listing: dict, now_ts: int) -> int:
    """Seconds a bid's hold must live: to the lot's close, plus 24h.

    LEDGER_API_v2.md §5 — "Auctions set expiry to lot-close + 24h" — so a lot that
    closes exactly on its deadline still has live escrow for the settle sweep to
    capture. A fixed-price lot with no `ends_at` gets the grace period alone.

    The floor exists because `ledger_v2.MIN_HOLD_SECONDS` is 60 and a bid landing
    in the last seconds of a lot would otherwise ask for a TTL core refuses with
    `bad_expiry` — a refusal the bidder would read as "your money is no good".
    """
    end_ts = _epoch(listing["ends_at"]) if listing.get("ends_at") else now_ts
    return max(300, end_ts + _esc.HOLD_GRACE_SECONDS - now_ts)


def _place_bid_core(listing_id: int, bidder_id, amount=None) -> dict:
    """Headless: validate a bid and RESERVE it. No coins move. No Discord I/O.

    A bid is a HOLD against the bidder's AVAILABLE balance, and being outbid is a
    RELEASE of that same reservation. It used to be `deduct_coins` + a recomputed
    `add_coins` refund, which is the finding LAND_EXCHANGE_AUDIT.md was written
    about; `land_escrow.py`'s header records what that cost.

    ORDER, AND THE ORDER IS THE DESIGN (LAND_ESCROW_PLAN §2.2):

      1. Validate. **No balance pre-read.** `place_hold` carries the availability
         test inside its own `INSERT … WHERE available >= amt`, so the check IS
         the write. The old `bal < amt` read was a second, weaker authority in
         front of the real one, and the gap between it and `deduct_coins` is the
         audit's §2: a wallet drained in between produced a bid recorded at full
         amount, backed by nothing, and refunded in full.
      2. `create_bid_row` — the row, with both keys, BEFORE any money call. A
         crash here leaks a `pending` row and nothing else.
      3. `claim_placement` — one conditional UPDATE; only the winner calls core.
      4. `ledger().hold(...)` with the key minted in step 2.
      5. `mark_held` — the hold id and expiry written back, per row.
      6. `promote_top_bid` — the new high bid and the CLAIM on the hold it
         displaces, committed together. Only after step 5, never before: a crash
         between 5 and 6 leaves two open holds, which is a temporary
         over-reservation for the previous bidder; the reverse order would leave
         a window with NO hold on the lot at all.
      7. release the displaced hold, then anti-snipe (which extends the hold with
         the lot, §5) and return.
    """
    import Restocker_db as _db
    # KILL SWITCH, first statement in the function and ABOVE `create_bid_row`, so a
    # frozen exchange writes no `land_bids` row and places no hold. See FREEZE_KEY.
    if bidding_frozen(_db):
        return {"ok": False, "error_code": "bidding_frozen",
                "error": _settle.paused_sentence()}
    now_ts = int(datetime.now(timezone.utc).timestamp())
    listing = _db.get_land_listing(listing_id)
    gate = _settle_gate(listing)          # settle path 1 — /bid, Bid button, satellite
    if gate:
        return {"ok": False, "error": gate}
    if listing["mode"] != "auction":
        return {"ok": False, "error": "That's a fixed-price listing — buy it instead."}
    if listing.get("ends_at") and _epoch(listing["ends_at"]) <= now_ts:
        return {"ok": False, "error": "That auction has already ended."}
    if str(bidder_id) == str(listing["seller_id"]):
        return {"ok": False, "error": "You can't bid on your own listing."}
    if str(listing.get("current_bidder")) == str(bidder_id):
        return {"ok": False, "error": "You already hold the top bid — raise it with a higher amount."}
    min_bid = _min_next_bid(listing)
    if min_bid is None:
        return {"ok": False, "error": "This listing's price data is invalid — ask a "
                                      "manager to check it."}
    if amount in (None, ""):
        amt = min_bid
    else:
        raw = _coin_amount(amount)
        if raw is None:
            return {"ok": False, "error": "A bid has to be a positive number of coins."}
        # Bids are whole coins. Rounded UP, so a rounded bid never lands below the
        # figure the bidder typed or below the minimum they were quoted.
        amt = int(math.ceil(raw))
    if amt < min_bid:
        return {"ok": False, "error": f"Minimum bid is {_fmt(min_bid)} coins."}
    if not _esc.escrow_available():
        return {"ok": False, "error": _settle.paused_sentence()}

    prev_bidder, prev_amount = listing.get("current_bidder"), listing.get("current_bid")
    row = _esc.create_bid_row(listing_id, bidder_id, float(amt), amt, kind="bid")
    row_id = int(row["id"])
    if _esc.claim_placement(row_id) is None:
        return {"ok": False, "error": "That bid is already being placed — give it a moment."}
    try:
        held = _esc.ledger().hold(str(bidder_id), amt,
                                  f"realestate:bid:{listing_id}",
                                  _bid_hold_ttl(listing, now_ts),
                                  _esc.hold_key(listing_id, "bid", row_id))
    except Exception as e:  # noqa: BLE001
        code = _esc.ledger().error_code(e)
        known = _esc.outcome_known_for(code)
        _esc.fail_placement(row_id, f"{code or type(e).__name__}: {e}", outcome_known=known)
        if code == "insufficient":
            # Rule 4 — figures, and what the reserved coins are reserved FOR.
            return {"ok": False, "error_code": code,
                    "error": (f"Your bid on #{listing_id} is {amt:,} 🪙. "
                              + _settle.available_sentence(bidder_id, amt))}
        if not known:
            # NOT `failed`: the hold may exist. Never tell them nothing was taken.
            return {"ok": False, "error_code": code or "unknown",
                    "error": ("We couldn't confirm that bid. Nothing has been taken from "
                              "you that isn't reserved, and it resolves within a minute — "
                              "check the listing before bidding again.")}
        return {"ok": False, "error_code": code, "error": str(e)}
    _esc.mark_held(row_id, str(held.get("hold_id") or ""), held.get("expires_at"))

    promoted = _esc.promote_top_bid(listing_id, row_id, bidder_id, amt)
    if not promoted["ok"]:
        # Somebody else's bid took the board first. OUR hold is ours to undo —
        # nothing else knows about it — and it is a release, not a refund.
        _settle.release_row(_esc.bid_row(row_id) or row,
                            f"bid on #{listing_id} was beaten to the board")
        return {"ok": False, "error_code": "outbid_in_flight", "error": promoted["error"]}
    # WHAT THE RELEASE ACTUALLY DID, kept rather than discarded. `release_row`
    # answers `released` / `releasing` / `release_unknown` / `release_refused`,
    # and only the first means the displaced bidder's coins are spendable again.
    # This loop used to throw that answer away and `prev_bidder` came back
    # regardless, so both outbid surfaces asserted the good case unconditionally:
    # the DM said "those coins never left your balance and are spendable again"
    # and the PUBLIC channel note said "their `N` 🪙 reservation is released" —
    # over a row reading `release_unknown` with the coins still held. Measured in
    # `adv/probe_copy_r5.py` §K6.
    #
    # It is reported, not repaired, here: a release that did not land is
    # `reconcile_holds`' question, and re-sending it on a bidder-facing click is
    # the double-release the escrow design exists to prevent. Exactly the same
    # split as `deferred` on the cancel/close paths — say the true thing, let the
    # component that owns the question fix it.
    prev_released = True
    for displaced in promoted["displaced"]:
        if _settle.release_row(displaced, f"outbid on #{listing_id}") != "released":
            prev_released = False

    anti_snipe_extended = False
    if listing.get("ends_at"):
        end_ts = _epoch(listing["ends_at"])
        anti_snipe = _num(listing.get("anti_snipe_minutes"), DEF["anti_snipe_minutes"])
        if (end_ts - now_ts) < anti_snipe * 60.0:
            # The extension is BOUNDED. Uncapped, `ends_at = now + N` let two
            # colluding accounts hold a lot open forever at zero net cost (only
            # the top bidder's coins are reserved, and they alternate). Under
            # escrow the ceiling also protects the money: `MAX_HOLD_SECONDS` is
            # 400 days, so an unbounded lot eventually cannot have its hold
            # extended at all and becomes unsettleable.
            want_ts = now_ts + int(round(anti_snipe * 60.0))
            if listing.get("starts_at"):
                max_days = _gd(_db, "max_auction_days", DEF["max_auction_days"])
                max_days = max_days if (math.isfinite(max_days) and max_days > 0) \
                    else DEF["max_auction_days"]
                want_ts = min(want_ts, _epoch(listing["starts_at"]) + int(max_days * 86400))
            # Never SHORTEN a lot: at the wall the extension simply stops happening.
            if want_ts > end_ts:
                _db.update_land_listing(
                    listing_id,
                    ends_at=_sql_ts(datetime.fromtimestamp(want_ts, timezone.utc)))
                anti_snipe_extended = True
                end_ts = want_ts
        # Extend the HOLD with the lot, always — not only when this call moved
        # `ends_at`. A hold placed before an earlier extension was computed from
        # an older deadline, and `extend_hold` sets an absolute expiry from a
        # relative TTL, so re-asserting it is convergent and costs one call.
        # Without this the lot outlives its escrow, core's sweeper releases the
        # winner's coins mid-auction, and the close pays a seller out of a
        # capture that fails (LAND_ESCROW_PLAN §5).
        _esc.extend_for_listing(listing_id, end_ts, now_ts)

    return {"ok": True, "listing_id": listing_id, "amount": amt,
            "bid_row_id": row_id, "hold_id": held.get("hold_id"),
            "prev_bidder": prev_bidder, "prev_amount": prev_amount,
            # Travels beside `prev_bidder` because every surface that names the
            # displaced bidder also describes what happened to their coins.
            "prev_released": prev_released,
            "anti_snipe_extended": anti_snipe_extended,
            # The satellite RENDERS these and must not compute them (§4 item 3).
            "balance": held.get("balance"), "held": held.get("held"),
            "available": held.get("available"),
            "message": (f"Bid placed: {amt:,} coins reserved on listing #{listing_id}. "
                        f"They stay in your wallet and are released the moment "
                        f"you're outbid.")}


#: THE INSTANT-BUY RETURN CONTRACT, written down once because it is a CONTRACT
#: and not a missing check. `_instant_buy_core` returns exactly two shapes:
#:
#:    a COMPLETED PURCHASE — `ok: True` carrying a `price`, or
#:    a REFUSAL           — `ok: False` carrying an `error` sentence.
#:
#: It used to return `settle_listing`'s dict VERBATIM for every outcome that was
#: not `sold`, and three of those are `ok: True` with no `price` in them
#: (`in_doubt`, `already_settling`, `already_closed`). All three callers branch on
#: `ok` alone, so all three read a settlement result as a purchase: the 🛒 Buy
#: button and `/realestate buy` raised `KeyError: 'price'` with the interaction
#: already deferred — no reply at all, the click just died — and the satellite
#: announced "🏡 #N bought via the network for `0` 🪙" in a partner channel and
#: opened a deal room between the seller and somebody who had not bought anything.
#: Deadline guards close instances of that; naming the contract closes the class.
#:
#: EVERY `ok: True` outcome `settle_listing` can return has a key here except
#: `sold`. `tests/test_land_escrow.py` reads that set off `settle_listing`'s own
#: AST and fails if a new outcome is added upstream without a sentence here, so
#: this table cannot quietly fall out of date the way the pass-through did.
#:
#: The sentences differ because the ADVICE differs, which is the whole point of
#: `_claim_refused` picking the right word: `already_closed` means stop,
#: `already_settling` and `in_doubt` mean somebody's money is in flight and the
#: right move is to wait rather than to click again.
NON_SALE_OUTCOMES: dict = {
    "in_doubt": (
        "escrow_in_doubt",
        "We can't confirm this listing's escrow with the ledger yet, so the purchase "
        "hasn't gone through. Nothing has been taken from you that isn't reserved, and "
        "any reservation ends on its own — don't buy it again yet, it resolves as soon "
        "as the ledger answers."),
    "already_settling": (
        "settle_in_progress",
        "This listing is being settled right now. If that settlement is yours it "
        "finishes on its own within a minute — don't buy it again."),
    _settle.ALREADY_CLOSED: (
        "listing_closed",
        "That listing is already closed — it isn't for sale any more. Nothing has been "
        "taken from you."),
}

#: The fallback for an outcome nobody has taught this function yet. It fails
#: CLOSED — a refusal, never a purchase — so an unhandled upstream outcome costs
#: a buyer one confusing sentence instead of a dead button and a `0` 🪙 note.
#:
#: IT ASSERTS NO NEGATIVE ABOUT THE MONEY, and that is the point of a fail-closed
#: sentence. The old wording promised the reader that nothing had been taken and
#: that their coins were only reserved, then invited them to click again. This
#: entry is reached only for an outcome the table above does not name — so it is
#: the one branch that cannot know whether a capture happened, and it was the
#: branch making the most confident claim about it. Failing closed on the money
#: and open on the story is not failing closed. The wording below is true of both
#: states the row could be in (reserved, or already in escrow) and gives the
#: advice that is safe in both: wait.
NON_SALE_FALLBACK = (
    "settle_incomplete",
    "That purchase didn't complete. Anything reserved or in escrow for it resolves "
    "on its own within a few minutes and nothing is lost — don't buy it again yet.")


def _instant_buy_core(listing_id: int, buyer_id) -> dict:
    """Headless instant buy: reserve the price, then settle the lot immediately.

    THE RETURN CONTRACT, and it is a CONTRACT rather than a check: this returns a
    COMPLETED PURCHASE carrying a `price`, or a REFUSAL carrying an `error`. It
    never hands `settle_listing`'s result back untranslated, because three of
    that function's outcomes are `ok: True` with no `price` in them and all three
    callers here branch on `ok` alone and then read `res["price"]`. The table is
    `NON_SALE_OUTCOMES` above, `tests/test_land_escrow.py` binds it to
    `settle_listing`'s own AST, and the two guards below are what make a refusal
    FREE — nothing written, nothing reserved.

    An instant buy is a ONE-BID AUCTION THAT CLOSES AT ONCE. It writes a
    `land_bids` row with `kind='buy'`, places a hold against it, and hands the
    row id to the same `settle_listing` every other close uses — which captures
    that hold, releases any standing bid's hold, and pays the seller.

    THE COMPENSATING REFUND IS DELETED, NOT FIXED. The old code debited the buyer
    and then relied on `add_coins(...buy_refund...)` if settlement returned
    `{"ok": False}` — which `_finalize_sale_core` only did BEFORE any money moved,
    so any raise inside it skipped the refund entirely. The live consequence was
    the audit's worst user-visible outcome: buyer debited 8.5M, seller paid,
    exception propagates, `_record_network_land_buy` catches it and tells the
    buyer "try again shortly", and nothing stops the retry.

    That whole shape is gone. The coins are a RESERVATION until the capture, so
    an aborted settlement releases rather than compensates — and a release cannot
    fail in a way that keeps the coins, because the hold expires on its own if
    everything else dies.

    THE COMPENSATING PATH BECAME A RESUME PATH, which is a different thing and
    not a free one. Once the capture lands the coins are no longer a reservation:
    they are in `treasury:estates` and the only correct ending is to finish the
    sale. That resume is `land_settle`'s, not this function's, and it is reached
    two ways — the minute sweep via `resume_row()`, or the buyer clicking Buy
    again. The second click deliberately still places its own row and hold; the
    settle binds to the row that was already captured (`_resolve_winning_row`)
    and releases the new one as a late loser, so a retry finishes the sale
    instead of creating a second one. The release below is only for the case
    where nothing was captured at all.

    THE SECOND CLICK IS BOUNDED BY THE DEADLINE, since the guard below landed,
    and that removes no resume route. The defence used to be a PARTITION — past
    `ends_at` is exactly what `get_expired_active_listings()` selects, so the
    sweep owned the refused set and the click owned the rest. IT IS NO LONGER A
    PARTITION, because the sweep's input set changed (owner decision, 15 Aug: the
    resume sweep is un-gated from the deadline). It is now a COVER with an
    overlap, which is a weaker shape and a safer one:

      past `ends_at`     the click is refused, and `get_expired_active_listings()`
                         selects the lot — `_settle_expired` finishes it.
      before `ends_at`   the click is the route AND
                         `get_part_settled_active_listings()` selects the lot the
                         moment somebody's coins have moved on it —
                         `_resume_part_settled` finishes it.

    So every lot holding a buyer's money is reachable from at least one route on
    both sides of the deadline, where the pre-deadline case previously had the
    click alone and nothing at all while the exchange was frozen. The overlap is
    safe because both routes bind to the SAME row (`resume_row`) and every money
    step is keyed: whichever arrives second finds the sale already made. That is
    `probe_ungate.py` U1/U2, and the acceptance is `probe_gate_final` G1.3/G1c.1.

    What the guard does remove is a SECOND player buying a lot that is past its
    deadline and already has a paid-for buyer — see the comment on the guard
    itself; below the deadline the `part_settled_row` check further down refuses
    that same stranger, which is why un-gating the sweep did not open one.
    """
    import Restocker_db as _db
    # KILL SWITCH, first statement and ABOVE `create_bid_row` — same reason as
    # `_place_bid_core`: no row, no hold, no coins reserved while frozen.
    if bidding_frozen(_db):
        return {"ok": False, "error_code": "bidding_frozen",
                "error": _settle.paused_sentence()}
    listing = _db.get_land_listing(listing_id)
    gate = _settle_gate(listing)          # settle path 2 — /buy, Buy button, satellite
    if gate:
        return {"ok": False, "error": gate}
    # THE DEADLINE, the guard `_place_bid_core:1195` has always carried and this
    # path never did. It used to be bounded by the 60-second sweep — a lot sat
    # past `ends_at` and `active` for at most a minute, and buying it in that
    # window merely raced the close. `capture_unknown` removed the bound: a lot
    # whose escrow core cannot answer for is deliberately held `active` past its
    # deadline for as long as core stays dark, which is unbounded. A SECOND player
    # clicking Buy there is told `ok: True` on a lot that already has a paid-for
    # buyer, and their coins are reserved for the length of the wedge. Nothing is
    # lost — the settle binds to the row that was already captured and releases
    # theirs as a late loser — but `ok: True` on a purchase that will never arrive
    # is a lie, and an unbounded reservation is not a small one.
    # `ends_at`-conditional, like the bid guard: a fixed-price lot with no
    # deadline (`:2370`) is never past one.
    if listing.get("ends_at") and _epoch(listing["ends_at"]) <= int(
            datetime.now(timezone.utc).timestamp()):
        return {"ok": False, "error_code": "listing_ended",
                "error": "That listing has already ended — it's closing now."}
    price = listing.get("buy_now")
    if not price:
        return {"ok": False, "error": "No instant-buy price on this listing — place a bid instead."}
    # Once bidding has met/passed the Buy-Now, instant-buy would let someone take it BELOW
    # the standing high bid and short the seller — force them to out-bid instead.
    if listing.get("current_bid") and float(listing["current_bid"]) >= float(price):
        return {"ok": False, "error": "Bidding has reached the Buy-Now price — place a higher bid instead."}
    if str(buyer_id) == str(listing["seller_id"]):
        return {"ok": False, "error": "You can't buy your own listing."}
    if not _esc.escrow_available():
        return {"ok": False, "error": _settle.paused_sentence()}
    price_i = int(round(float(price)))
    if price_i <= 0:
        return {"ok": False, "error": "That listing's price isn't a valid coin amount."}

    # THIS LOT ALREADY HAS A PAID-FOR BUYER, and it is not you. Refused ABOVE
    # `create_bid_row`, like the deadline guard and for the same reason: a guard
    # below the hold returns `ok: False` and has still reserved the coins, which
    # is half the finding. `part_settled_row` is `resume_row`'s own reading —
    # `captured`, `capturing` or `capture_unknown`, i.e. coins that have moved or
    # may have — so this cannot disagree with what the settle will bind to.
    #
    # A merely `held` row is deliberately NOT part-settled: that is a reservation,
    # it expires by itself, and blocking on one would let a single stuck click
    # make a live lot unbuyable for an hour.
    #
    # Only a STRANGER is refused. The lot's own buyer clicking Buy again is the
    # documented resume route below the deadline (`i3`, `G1b`) and it still binds
    # to the row it already captured, so it must go through.
    spoken_for = _settle.part_settled_row(listing_id)
    if spoken_for is not None and str(spoken_for.get("bidder_id")) != str(buyer_id):
        return {"ok": False, "error_code": "already_bought",
                "error": ("Someone has already bought this listing and their payment is "
                          "still completing — it isn't for sale any more. Nothing has "
                          "been taken from you.")}

    # Row first, keys minted from its id, THEN the money. A crash before the hold
    # leaks a `pending` row and nothing else.
    row = _esc.create_bid_row(listing_id, buyer_id, float(price), price_i, kind="buy")
    row_id = int(row["id"])
    if _esc.claim_placement(row_id) is None:
        return {"ok": False, "error": "That purchase is already being processed."}
    try:
        held = _esc.ledger().hold(str(buyer_id), price_i,
                                  f"realestate:buy:{listing_id}",
                                  _esc.BUY_HOLD_SECONDS,
                                  _esc.hold_key(listing_id, "buy", row_id))
    except Exception as e:  # noqa: BLE001
        code = _esc.ledger().error_code(e)
        known = _esc.outcome_known_for(code)
        _esc.fail_placement(row_id, f"{code or type(e).__name__}: {e}", outcome_known=known)
        if code == "insufficient":
            return {"ok": False, "error_code": code,
                    "error": (f"Buy-Now on #{listing_id} is {price_i:,} 🪙. "
                              + _settle.available_sentence(buyer_id, price_i))}
        if not known:
            return {"ok": False, "error_code": code or "unknown",
                    "error": ("We couldn't confirm that purchase. Nothing has been taken "
                              "from you that isn't reserved, and it resolves within a "
                              "minute — do NOT buy it again yet.")}
        return {"ok": False, "error_code": code, "error": str(e)}
    _esc.mark_held(row_id, str(held.get("hold_id") or ""), held.get("expires_at"))

    settle_raised = False
    try:
        res = _finalize_sale_core(listing_id, buyer_id, price_i, win_row_id=row_id,
                                  note_reason="buy")
    except Exception as e:  # noqa: BLE001
        # The settlement raised. The listing claim has already been handed back by
        # `settle_listing`, so the sweep will re-enter — but the buyer should not
        # wait an hour for their reservation if it is provably still just a
        # reservation. Fall through to the same release rule as a refusal.
        #
        # THE SENTENCE IS CHOSEN BELOW, FROM THE ROW, not written here. It used
        # to be a fixed string: "Nothing has been taken from you — your coins
        # were only reserved, and the reservation ends automatically. Try again
        # in a moment." The capture and the seller transfer are two separate
        # calls, so this `except` also catches the case where the capture LANDED
        # and the transfer died — a `captured` row, the buyer's coins in
        # `treasury:estates`, `held` back to zero and no reservation left in
        # existence. Every clause of that sentence was false in exactly the state
        # this file spends the most words on, and its last clause told the buyer
        # to do the one thing the eleven lines below exist to stop.
        settle_raised = True
        log.warning("[realestate] instant-buy settle raised for #%s (buyer %s): %s",
                    listing_id, buyer_id, e)
        res = {"ok": False, "listing_id": listing_id}
    else:
        if res.get("ok") and res.get("outcome") == "sold":
            res["message"] = f"Bought listing #{listing_id} for {_fmt(price_i)} coins."
            res["price"] = float(price_i)
            return res
    # The sale did not complete. Give the buyer their reservation back NOW rather
    # than leaving it to the hour-long TTL — but ONLY if the row is still `held`.
    # `already_settling` and `in_doubt` mean another settler owns these rows, and a
    # row that is `capturing` or `capture_unknown` may already have moved the
    # coins: releasing either would be the double-release this design exists to
    # make impossible. `held` is the one state that says, from core's own row,
    # that nothing has happened to the money.
    # `already_closed` is listed beside `already_settling` deliberately, and NOT
    # because another settler owns the row — nobody does; the lot is over. It is
    # here to keep this branch's behaviour byte-identical to what it was when
    # both cases came back under the one word. A `held` row on a terminal lot is
    # collected by `sweep_terminal_listing_holds` within the minute (F3), so the
    # reservation is not stranded — it is released by the component that owns
    # that question rather than opportunistically here. If you want it released
    # sooner, that is a behaviour change and needs its own probe.
    row_status, released_now = "", False
    if res.get("outcome") not in ("already_settling", _settle.ALREADY_CLOSED, "in_doubt"):
        fresh = _esc.bid_row(row_id) or {}
        row_status = str(fresh.get("status") or "")
        if row_status == "held":
            released_now = _settle.release_row(
                fresh, reason=f"instant buy on #{listing_id} did not complete") == "released"
    if settle_raised:
        # THE SENTENCE, NOW ANSWERED BY THE ROW. Three states, three different
        # true things to say, and the advice differs because the state does —
        # the same distinction `NON_SALE_OUTCOMES` above exists to make.
        if released_now:
            res["error_code"] = "settle_failed_released"
            res["error"] = ("That purchase didn't complete, and the coins that were "
                            "reserved for it have been released — nothing was taken "
                            "from you and they're spendable again. You can try again.")
        elif row_status in ("held", "placing", "place_unknown", ""):
            # Still (or possibly still) a reservation: no capture is known to have
            # happened, but this process could not end the hold either.
            res["error_code"] = "settle_failed_reserved"
            res["error"] = ("That purchase didn't complete. Nothing has been taken "
                            "from you — your coins are only reserved, and the "
                            "reservation ends on its own if the sale doesn't finish. "
                            "Give it a minute before trying again.")
        else:
            # `capturing` / `captured` / `capture_unknown`: the coins are OUT of
            # the wallet and in `treasury:estates`. There is no reservation left,
            # nothing here may release one, and the resume sweep finishes this lot
            # regardless of its deadline (`probe_ungate` U6.2, `probe_gate_final`
            # G1.3) — including while the exchange is frozen. Telling this buyer
            # to try again is how a second lot of their coins gets reserved
            # against a purchase they have already paid for.
            res["error_code"] = "settle_failed_in_escrow"
            res["error"] = ("That purchase didn't finish cleanly. Your coins have "
                            "already been taken for it and are sitting in the "
                            "exchange's escrow — nothing is lost, and the sale "
                            "completes on its own within a few minutes. Don't buy "
                            "it again.")
    if not res.get("ok"):
        return res
    # THE CONTRACT, applied. Everything from here down is `ok: True` and NOT
    # `sold` — a settlement result, which is not a purchase and has no `price`.
    # Translating it into a refusal here is what stops it reaching three callers
    # that each dereference `res["price"]`; see NON_SALE_OUTCOMES above.
    code, sentence = NON_SALE_OUTCOMES.get(str(res.get("outcome")), NON_SALE_FALLBACK)
    log.info("[realestate] instant-buy on #%s (buyer %s) did not complete: settle "
             "returned `%s` -> refusing as `%s`", listing_id, buyer_id,
             res.get("outcome"), code)
    return {"ok": False, "listing_id": listing_id, "error_code": code,
            "error": sentence, "settle_outcome": res.get("outcome")}


# ── Headless management cores — the /sell, /cancel, /close, /config commands live on the
#    satellite bot now; these are the data-only functions its relay endpoints call. No
#    Discord I/O (the satellite renders; the web layer runs writes on the bot loop). ──────
def create_listing_core(seller_id, title, starting_price, buy_now=None, details=None,
                        category=None, chunks=None, backs_company=None,
                        duration_days=None) -> dict:
    """Create a listing from the satellite's /sell. Auction mode, seller-set starting
    price, optional Buy-It-Now. Category (Land/Artificial Land) or chunks/backs_company
    make it land-kind (AI valuation + 65% backing). Returns {ok, listing, ai_note}."""
    import Restocker_db as _db
    try:
        starting_price = float(starting_price)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Starting price must be a number."}
    if starting_price <= 0:
        return {"ok": False, "error": "Starting price must be > 0."}
    bn = None
    if buy_now not in (None, ""):
        try:
            bn = float(buy_now)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Buy-Now price must be a number."}
        if bn <= starting_price:
            return {"ok": False, "error": "Buy-Now must be higher than the starting price."}
    cat = (str(category).strip() or None) if category else None
    if backs_company and not _get_market(backs_company):
        return {"ok": False, "error": f"Company `{backs_company}` not found."}
    ch = None
    if chunks not in (None, ""):
        try:
            ch = float(chunks)
        except (TypeError, ValueError):
            ch = None
    is_land = bool(ch or backs_company or (cat in _LAND_CATEGORIES))
    kind = "land" if is_land else "item"

    ai_note = None
    if is_land and ch:
        try:
            ai = _valuation.value_plot(ch, "premium" if backs_company else "raw")
            ai_note = (f"AI valuation for reference: {_fmt(ai['assessed_value'])} coins "
                       f"({_fmt(ch)} chunks). The starting price stands as set.")
        except Exception:
            ai_note = None

    anti_snipe = _gd(_db, "anti_snipe_minutes", DEF["anti_snipe_minutes"])
    commission_pct = _gd(_db, "commission_pct", DEF["commission_pct"])
    incr_pct = _gd(_db, "min_increment_pct", DEF["min_increment_pct"])
    try:
        dur = float(duration_days) if duration_days else _gd(_db, "default_auction_days", DEF["default_auction_days"])
    except (TypeError, ValueError):
        dur = _gd(_db, "default_auction_days", DEF["default_auction_days"])
    ends_at = _sql_now_plus(days=_capped_duration(_db, dur))

    # The fee is read from config here rather than hard-coded to 0, which is what
    # `create_listing_core` and the satellite's /sell both did — so the three
    # listing surfaces had three different money behaviours for the same action.
    listing_fee = _gd(_db, "listing_fee", DEF["listing_fee"])

    # DRAFT FIRST. The row exists so the fee has an id to key on
    # (`land:listing:<id>:fee`), and `draft` is not `active`, so it is not on the
    # board and cannot take a bid before it has been paid for. The live code
    # deducted the fee BEFORE the listing existed, under the reason string
    # `realestate:listing_fee` — no listing id in it at all, so the charge was
    # unattributable and two listings by one seller were indistinguishable.
    listing_id = _db.create_land_listing(
        seller_id=str(seller_id), kind=kind, title=str(title).strip()[:120], category=cat,
        chunks=(ch or 0), market_id=(backs_company or None),
        land=(str(title).strip()[:120] if is_land else None),
        description=(str(details).strip()[:1500] if details else None), mode="auction",
        reserve=round(starting_price, 2), buy_now=(round(bn, 2) if bn else None),
        min_increment_pct=incr_pct, commission_pct=commission_pct,
        listing_fee=listing_fee,
        ends_at=ends_at, anti_snipe_minutes=int(anti_snipe), status="draft")
    fee_res = _settle.charge_listing_fee(listing_id, seller_id, listing_fee)
    if not fee_res.get("ok"):
        # The draft never opens. It is marked `cancelled` rather than deleted so
        # the refused fee attempt keeps its listing id — the key it claimed at
        # core points at a row that still exists, which is what makes a later
        # "why was I charged for #412" answerable.
        _db.update_land_listing(listing_id, status="cancelled",
                                closed_at=core.utcnow_iso())
        return {"ok": False, "error": fee_res.get("error", "Listing fee could not be charged."),
                "error_code": fee_res.get("error_code"), "listing_id": listing_id}
    _db.update_land_listing(listing_id, status="active")
    listing = _db.get_land_listing(listing_id)
    return {"ok": True, "listing": _listing_for_network(listing), "kind": kind,
            "ai_note": ai_note, "listing_fee": int(fee_res.get("charged") or 0)}


def set_listing_photos(listing_id: int, photo_urls: list) -> None:
    """Store the satellite-hosted photo URLs for a listing (called after /sell uploads)."""
    import Restocker_db as _db
    urls = [u for u in (photo_urls or []) if str(u).lower().startswith(("http://", "https://"))][:4]
    if urls:
        _db.update_land_listing(listing_id, photos=json.dumps(urls),
                                image_url=urls[0])


def cancel_listing_core(listing_id: int, requester_id, is_mgr: bool = False) -> dict:
    """Seller (or manager) cancels a listing. THE ONE cancel implementation.

    WHAT HAPPENS TO A HELD BID. `land_settle.cancel_listing` claims the listing,
    then releases every open hold on it one row at a time, marking each row
    `released` before it touches the next. No coins move — the bidder's
    `available` rises by exactly what it fell by, because it is the same
    reservation being retired rather than a refund recomputed from a float
    column.

    WHAT STOPS A DOUBLE RELEASE. Three things, and any one of them is enough:
    the row claim (`held -> releasing` in one UPDATE, so a cancel racing the
    minute sweep loses cleanly), core's own `AND state='open'` on the release,
    and the one-key-per-bid release key, which makes a racing cancel a REPLAY
    rather than a conflict. See `land_settle.release_row`.

    THE STANDING-BID RESTRICTION IS NOW A POLICY, NOT A LIMIT — and it stays.
    Technically a seller could now cancel a lot with a live bid, because
    unwinding it is a release. It is still refused, because a seller who can
    cancel after seeing a bid can shop the price around, and the refusal says
    that rather than pretending the money is the obstacle.

    THE LISTING FEE IS NOT REFUNDED. A fee that comes back on cancel is a free
    option on the auction. The audit's finding was not "the fee should be
    refunded", it was that the fee reached NOBODY — deducted from the seller,
    `_credit_platform_balance` never called for it, no path returning it. It now
    reaches `treasury:estates` and stays there.
    """
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    gate = _settle_gate(listing)          # settle path 6 — seller/manager cancel
    if gate:
        return {"ok": False, "error": gate}
    if str(requester_id) != str(listing["seller_id"]) and not is_mgr:
        return {"ok": False, "error": "Only the seller (or a manager) can cancel this."}
    if listing.get("current_bid") and not is_mgr:
        return {"ok": False, "error": (
            "Someone has already bid on this, and their coins are reserved against it. "
            "Cancelling a lot after a bid has landed isn't allowed — it would let a "
            "seller shop the price around. A manager can `/realestate close` it, which "
            "releases the bidder's reservation in full.")}
    res = _settle.cancel_listing(listing_id, reason=(
        f"listing #{listing_id} cancelled by "
        f"{'a manager' if is_mgr else 'the seller'}"))
    if res.get("ok"):
        res.setdefault("listing_id", listing_id)
        res["fee_note"] = ("The listing fee isn't refunded on cancel — a refundable "
                           "listing fee would be a free option on the auction.")
        # `deferred` travels to the reply EXPLICITLY, not by pass-through. It
        # already arrives in `res` and this line changes nothing today — it is
        # here so the key is visible at this hop, because the next person to
        # rebuild this dict (which `close_listing_core` below already does) would
        # otherwise drop it and re-create the defect it was added to fix: a
        # manager told `released: []` on a lot that still has a row reserving a
        # bidder's coins. See `land_settle.release_all_holds`.
        res["deferred"] = res.get("deferred") or []
    return res


def close_listing_core(listing_id: int, refund_bidder: bool = False) -> dict:
    """Manager force-settle/unwind (money only; the caller handles Discord effects).

    `refund_bidder=True` releases the standing bid's reservation and closes the
    lot; otherwise the top bid wins and the lot settles, or it expires if there
    are none. Both branches go through `land_settle`, which is the same code the
    minute sweep and the satellite's close run — the point of this round is that
    there is one close, not three that have already diverged.

    "Refund" is the word the command has always used and it is now flatly
    wrong: nothing is refunded, because nothing was taken. The bidder's
    reservation ends and their `available` goes back up.

    The three places a human reads this say `released`, not `refunded`, and each
    one is a real string you can go and check:
      * the audit row  — `_record_unwind`'s summary: "the standing bidder's
        <n>-coin reservation released (no coins moved)"
      * the channel notice — `/realestate close`: "the standing bidder's coins
        are no longer reserved"
      * the manager's reply — "Closed #<id> and released the standing bid."
    The row said "refunded" until 15 Aug and this docstring claimed otherwise;
    if you change one of those strings, change this list in the same commit.

    ALL THREE ARE CONDITIONAL ON `deferred` BEING EMPTY, and the last two now say
    so. `released: []` is not "this lot has no escrow left": a row whose
    placement core never confirmed cannot be released on this path and comes back
    in `deferred` instead. The close handler branches on it and posts the
    still-reserved sentence when it is non-empty — otherwise the channel notice
    above is an affirmatively false public statement about somebody's money.
    That is why `deferred` is in the return dict below rather than dropped when
    this function rebuilds it: dropping it re-creates the defect in silence.

    What is NOT renamed: the `refund_bidder` parameter, and the
    `cancelled_refunded` outcome string on the wire. Both are read by the
    deployed satellite and by `/api/network/land/*` callers, so renaming them
    is a compatibility break for a vocabulary fix, and it would have to happen
    in all four surfaces in one commit. The words a PLAYER sees are fixed; the
    words a PROGRAM sends are not.
    """
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    gate = _settle_gate(listing)          # settle path 4 — manager force-settle/unwind
    if gate:
        return {"ok": False, "error": gate}
    if refund_bidder:
        res = _settle.cancel_listing(listing_id, reason=(
            f"listing #{listing_id} unwound by a manager"))
        if not res.get("ok") or res.get("outcome") != "cancelled":
            # `ok: True` is not "it was cancelled". `already_settling` means
            # somebody else owns this lot right now and nothing was unwound;
            # `in_doubt` means the escrow's outcome is unconfirmed and the lot is
            # deliberately still live. Reporting either as `cancelled_refunded`
            # tells a manager the lot is closed and the bidder released when
            # neither happened — and writes an unwind audit row for a state
            # change that did not occur. Pass the real outcome through.
            return res
        try:
            key = _record_unwind(listing)
        except Exception as e:  # noqa: BLE001
            log.warning("[realestate] unwind #%s has no audit row: %s", listing_id, e)
            key = None
        return {"ok": True, "outcome": "cancelled_refunded", "action_key": key,
                "released": res.get("released"), "problems": res.get("problems"),
                # Carried to the reply, which is the only place a human sees it.
                # Dropping it here is what made `released: []` read as "this lot
                # has no escrow left" on a lot that still had a row reserving
                # coins — see `land_settle.release_all_holds`.
                "deferred": res.get("deferred")}
    # THE ESCROW DECIDES WHETHER THIS LOT HAS A BUYER, not the board. An instant
    # buy writes no `current_bid`/`current_bidder`, so an interrupted one reads
    # here as an auction that ended with no bids — and closing it as unsold
    # records exactly that over a lot whose price is already in the treasury.
    # Checked BEFORE the board because the two can disagree: an instant buy over a
    # live auction leaves a standing bid on the listing and a captured `buy` row
    # on `land_bids`, and it is the captured row that has been paid for.
    resume = _settle.resume_row(listing_id)
    if resume is not None:
        res = _finalize_sale_core(listing_id, resume["bidder_id"],
                                  int(resume["hold_amount"] or 0),
                                  win_row_id=int(resume["id"]),
                                  note_reason="manager_close")
        if res.get("ok") and res.get("outcome") == "sold":
            res["sold_to_buyer"] = str(resume["bidder_id"])
        return res
    if listing.get("current_bid") and listing.get("current_bidder"):
        res = _finalize_sale_core(listing_id, listing["current_bidder"],
                                  float(listing["current_bid"]),
                                  note_reason="manager_close")
        if res.get("ok") and res.get("outcome") == "sold":
            res["sold_to_buyer"] = str(listing["current_bidder"])
        return res
    return _settle.expire_unsold(listing_id)


def get_exchange_config() -> dict:
    """Current Land Exchange config knobs (commission, fees, auction defaults)."""
    import Restocker_db as _db
    return {k: _gd(_db, k, DEF[k]) for k in DEF}


def set_exchange_config(**kwargs) -> dict:
    """Set any of the config knobs (values that are None are ignored). Returns the result.

    `bidding_frozen` is routed through `set_bidding_frozen` rather than written
    raw, so a flip made over the network API still records who and when.

    The "who" here is the ENDPOINT, not a name off the wire. The caller is
    identity-gated (`Restocker_web._land_require_manager`), but the acting id is
    not passed down, and a `by` label read out of the request body would be a
    forgeable audit field on the one row an operator reads under pressure. A
    label that is honestly vague beats one that is confidently wrong.
    """
    import Restocker_db as _db
    for key in DEF:
        val = kwargs.get(key)
        if val is None:
            continue
        if key == FREEZE_KEY:
            try:
                set_bidding_frozen(float(val) != 0.0,
                                   by="the network config API",
                                   reason=str(kwargs.get("freeze_reason") or "")[:200],
                                   _db=_db)
            except (TypeError, ValueError):
                pass
            continue
        try:
            _db.set_config(f"realestate:{key}", str(float(val)))
        except (TypeError, ValueError):
            pass
    return get_exchange_config()


# ── Buttons: anyone participates by CLICKING, no command to learn. Per-listing custom
#    ids (rex:bid:<id> / rex:buy:<id>) are made restart-safe with discord.py DynamicItem
#    (>=2.4) — registered once, they keep working on listing messages after a reboot. ──
def _cog(interaction):
    return interaction.client.get_cog("LandExchangeCog")


class BidModal(discord.ui.Modal, title="Place a bid"):
    def __init__(self, listing_id: int):
        super().__init__(timeout=300)
        self.listing_id = listing_id
        import Restocker_db as _db
        l = _db.get_land_listing(listing_id) or {}
        hint = _min_next_bid(l) if l.get("mode") == "auction" else None
        self.amount = discord.ui.TextInput(
            label="Your bid (coins)",
            placeholder=(f"Minimum {_fmt(hint)} — leave blank to bid the minimum" if hint else "Amount in coins"),
            required=False, max_length=15)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.amount.value or "").strip().replace(",", "").replace(" ", "")
        amount = None
        if raw:
            try:
                amount = float(raw)
            except ValueError:
                return await interaction.response.send_message("❌ That's not a number.", ephemeral=True)
        res = _place_bid_core(self.listing_id, interaction.user.id, amount)
        if not res.get("ok"):
            return await interaction.response.send_message(f"❌ {res['error']}", ephemeral=True)
        # Acknowledge the modal FIRST (it has its own ~3s deadline), then run the slow
        # after-effects (listing refresh + outbid DM) which can exceed it.
        # NOT "you'll be refunded if outbid" — under escrow the coins were never
        # taken, so there is nothing to refund. `_bid_note` was corrected to
        # `reservation is released` and this line was left behind saying the
        # opposite to the same person about the same coins.
        reply = (f"✅ Bid placed: `{_fmt(res['amount'])}` 🪙 reserved. If you're outbid "
                 f"the reservation is released automatically — the coins never leave "
                 f"your balance.")
        await interaction.response.send_message(reply, ephemeral=True)
        cog = _cog(interaction)
        if cog is not None:
            await cog._post_bid(self.listing_id, res, _bid_note(self.listing_id, res, interaction.user.id))


class BidButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rex:bid:(?P<lid>\d+)"):
    def __init__(self, listing_id: int):
        self.listing_id = listing_id
        super().__init__(discord.ui.Button(
            label="💰 Bid", style=discord.ButtonStyle.primary, custom_id=f"rex:bid:{listing_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["lid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(BidModal(self.listing_id))


class BuyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rex:buy:(?P<lid>\d+)"):
    def __init__(self, listing_id: int, price=None):
        self.listing_id = listing_id
        label = f"🛒 Buy now ({_fmt(price)})" if price else "🛒 Buy now"
        super().__init__(discord.ui.Button(
            label=label[:80], style=discord.ButtonStyle.success, custom_id=f"rex:buy:{listing_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["lid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        res = _instant_buy_core(self.listing_id, interaction.user.id)
        if not res.get("ok"):
            return await interaction.followup.send(f"❌ {res['error']}", ephemeral=True)
        cog = _cog(interaction)
        if cog is not None:
            await cog._post_sale(
                self.listing_id, interaction.user.id, res["price"],
                note=f"🛒 **#{self.listing_id}** bought by <@{interaction.user.id}> for `{_fmt(res['price'])}` 🪙.")
        await interaction.followup.send(
            f"✅ Bought for `{_fmt(res['price'])}` 🪙 — coins moved automatically via escrow. "
            f"You've been added to a private transfer room with the seller.", ephemeral=True)


def _bid_note(listing_id: int, res: dict, actor_id) -> str:
    # "refunded" was the right word under the old debit model and is the wrong one
    # now: the previous bidder was never charged, so nothing is being given back.
    # Their RESERVATION ends — same coins, same wallet, spendable again. This note
    # is public in the listing channel, so it is where most people will learn what
    # the exchange actually does with their money.
    #
    # AND THE RELEASE IS REPORTED, NOT ASSUMED. `prev_released` comes from
    # `release_row`'s own answer in `_place_bid_core`. When the release did not
    # land, the coins really are still reserved, and this note is the most-read
    # sentence the exchange writes — announcing a release that did not happen
    # sends the displaced bidder to spend coins they do not have available.
    note = f"💰 New bid on **#{listing_id}**: `{_fmt(res['amount'])}` 🪙 from <@{actor_id}>"
    if res.get("prev_bidder"):
        if res.get("prev_released") is False:
            note += (f" (outbidding <@{res['prev_bidder']}> — their `"
                     f"{_fmt(res['prev_amount'])}` 🪙 is still reserved for a moment "
                     f"while the ledger confirms the release)")
        else:
            note += (f" (outbidding <@{res['prev_bidder']}> — their `"
                     f"{_fmt(res['prev_amount'])}` 🪙 reservation is released)")
    if res.get("anti_snipe_extended"):
        note += " · ⏱️ anti-snipe extended the end time"
    return note


def _listing_view(listing: dict) -> discord.ui.View:
    """The Bid / Buy buttons for a listing message (empty once it's closed)."""
    v = discord.ui.View(timeout=None)
    if listing.get("status") == "active":
        if listing.get("mode") == "auction":
            v.add_item(BidButton(int(listing["id"])))
        if listing.get("buy_now"):
            v.add_item(BuyButton(int(listing["id"]), price=listing.get("buy_now")))
    return v


# ── Opt-in notify roles: interested people click a button to give themselves a "ping me
#    on new Land / Items" role. When /sell posts a listing, that role is mentioned so
#    only opted-in members are notified (reliable, unlike mass DMs which rate-limit and
#    bounce off closed DMs). Managers set the role per kind and post the panel. ─────────
_NOTIFY_LABEL = {"land": "🏡 Land", "item": "📦 Items"}


class NotifyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rex:notify:(?P<kind>land|item)"):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(discord.ui.Button(
            label=f"🔔 Notify me: {_NOTIFY_LABEL.get(kind, kind)}",
            style=discord.ButtonStyle.secondary, custom_id=f"rex:notify:{kind}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["kind"])

    async def callback(self, interaction: discord.Interaction):
        import Restocker_db as _db
        rid = _db.get_config(f"realestate:notify_role:{self.kind}")
        role = interaction.guild.get_role(int(rid)) if (rid and interaction.guild) else None
        if not role:
            return await interaction.response.send_message(
                "That notify role isn't set up here yet — ask a manager.", ephemeral=True)
        member = interaction.user
        try:
            if role in getattr(member, "roles", []):
                await member.remove_roles(role, reason="realestate: opted out of listing pings")
                await interaction.response.send_message(
                    f"🔕 Removed **{role.name}** — you won't be pinged for new {_NOTIFY_LABEL.get(self.kind)} listings.",
                    ephemeral=True)
            else:
                await member.add_roles(role, reason="realestate: opted in to listing pings")
                await interaction.response.send_message(
                    f"🔔 You've got **{role.name}** — you'll be pinged when a new "
                    f"{_NOTIFY_LABEL.get(self.kind)} listing goes up. Click again to opt out.",
                    ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ I don't have permission to manage that role — a manager needs to move my role above it.",
                ephemeral=True)


def _notify_panel_view(kinds) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for k in kinds:
        v.add_item(NotifyButton(k))
    return v


class LandExchangeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # The player/manager-facing slash commands are hosted on the branded
        # "V Tech Lands & Auctions" satellite bot (as thin relays to /api/network/land/*),
        # so we DO NOT register this cog's app commands on the main Restocker bot — its
        # command list stays clean. Everything else (the settlement loop, escrow cores,
        # network API helpers, restart-safe buttons) still runs here on the main bot.
        # The command methods below stay in the file as the reference implementation; this
        # line just keeps them off main's command tree.
        self.__cog_app_commands__ = []

    realestate = app_commands.Group(
        name="realestate",
        description="Restocker Land Exchange — list, bid on, and buy land",
    )

    # ── settlement (shared by /buy, manual /close, and the auto-close loop) ──────
    async def _finalize_sale(self, listing_id: int, buyer_id, price: float, *, note: str,
                             win_row_id=None):
        """Settle a sale via the shared headless core (money + DB + company backing),
        then do the Discord side (refresh, winner DM, transfer room).

        `win_row_id` names the escrow row that backs the sale. The sweep passes it
        when it is resuming an interrupted instant buy, where the board says
        nothing and the `land_bids` row says everything.
        """
        res = _finalize_sale_core(listing_id, buyer_id, price, win_row_id=win_row_id)
        # `ok` IS NOT `sold` — the same contract NON_SALE_OUTCOMES names on the
        # headless side. `settle_listing` returns `ok: True` for `in_doubt`,
        # `already_settling` and `already_closed`, none of which is a completed
        # sale, and `_post_sale` posts the audit row, DMs a "winner", opens a deal
        # room and announces the hammer price in the listing channel. Gated on
        # `ok` alone it did all four for a settlement that had not happened.
        if not res.get("ok") or res.get("outcome") != "sold":
            log.warning("[realestate] finalize #%s did not sell: outcome=%s %s",
                        listing_id, res.get("outcome"), res.get("error") or "")
            return res
        await self._post_sale(listing_id, buyer_id, price, note)
        return res

    async def _post_sale(self, listing_id: int, buyer_id, price: float, note: str):
        """Everything that happens once a listing is SOLD, regardless of path (auction
        end, instant buy, manager close): post the audit row with its ↩ Rollback button,
        refresh the message, DM the winner, and open a private transfer room with the
        seller + winner to coordinate the in-game handover.

        This is the ONE async funnel every sale path reaches (`/realestate buy`, the
        Buy button, `_settle_expired`, `/realestate close`), which is why the audit
        row is posted from here rather than from four call sites.
        """
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        await self._post_audit_row(sale_action_key(listing_id))
        await self._refresh_message(listing_id, extra=note)
        if listing:
            await self._dm_winner(listing, buyer_id, price)
            await self._open_deal_room(listing, buyer_id, price)

    async def _post_audit_row(self, action_key: str):
        """Put the ops-log embed + ↩ Rollback button up for a row a core recorded.

        Best-effort by design: the money is already committed by the time we get
        here, so a missing ops channel must not turn a settled sale into an error.
        The row itself is already durable either way — this only decides whether
        anyone sees a button for it today.
        """
        rb = sys.modules.get("cogs.rollback")
        if rb is None:
            return
        try:
            await rb.post_by_key(self.bot, action_key)
        except Exception as e:  # noqa: BLE001
            log.warning("[realestate] audit row %s not posted: %s", action_key, e)

    async def _dm_winner(self, listing: dict, buyer_id, price: float):
        """DM the auction winner their receipt + the seller's handover message (coords,
        'come collect', etc.). Best-effort — a closed DM never blocks settlement."""
        try:
            user = self.bot.get_user(int(buyer_id)) or await self.bot.fetch_user(int(buyer_id))
            what = _listing_title(listing)
            body = (f"🎉 You won **{what}** on the V Tech Auction House for "
                    f"`{_fmt(price)}` 🪙 — the coins already moved from escrow, no manual payment "
                    f"needed. A private transfer room has been opened for you and the seller.")
            if listing.get("coords"):
                body += f"\nCoords: `{listing['coords']}`"
            if listing.get("winner_message"):
                body += f"\n\n**From the seller:** {listing['winner_message']}"
            try:
                import Restocker_db as _db
                pts = _loyalty_award_points(_db, price)
                body += f"\n\n🎟️ You earned **{_fmt(pts)}** V Tech loyalty points — see `/loyalty`."
            except Exception:
                pass
            await user.send(body[:1900])
        except Exception as e:
            log.warning("[realestate] winner DM failed for #%s: %s", listing.get("id"), e)

    async def _dm_outbid(self, prev_bidder, listing_id: int, new_amount, released=True):
        """DM the bidder who was just outbid — here's the new top bid, and what
        actually happened to the coins that were reserved for their old one.

        "Refunded" is wrong and was wrong here for the same reason `_bid_note`
        names: under escrow the coins never left their balance, so there is
        nothing to refund. A player told they were "refunded" goes looking for a
        credit in their transaction history that does not exist.

        `released` is `_place_bid_core`'s `prev_released` — `release_row`'s own
        answer, not an assumption. This DM used to say "those coins never left
        your balance and are spendable again" unconditionally, which is false
        over a `release_unknown` row: the coins are still held, and a bidder who
        believes that sentence comes back to bid and is refused for funds they
        were told they had. The release still is not retried here — that is
        `reconcile_holds`' job — but it is no longer described as done.
        """
        try:
            import Restocker_db as _db
            listing = _db.get_land_listing(listing_id) or {}
            user = self.bot.get_user(int(prev_bidder)) or await self.bot.fetch_user(int(prev_bidder))
            what = _listing_title(listing) if listing else f"#{listing_id}"
            if released:
                tail = ("Your reservation has been released — those coins never left "
                        "your balance and are spendable again. Bid again on the "
                        "listing to stay in the running.")
            else:
                tail = ("The coins reserved for your bid are still reserved for a "
                        "moment — the ledger hasn't confirmed the release yet. "
                        "Nothing has been taken from you and they free up on their "
                        "own within a few minutes.")
            body = (f"⚠️ You've been outbid on **{what}** (#{listing_id}) — the top bid is now "
                    f"`{_fmt(new_amount)}` 🪙. {tail}")
            await user.send(body[:1900])
        except Exception as e:
            log.warning("[realestate] outbid DM failed for #%s: %s", listing_id, e)

    async def _post_bid(self, listing_id: int, res: dict, note: str):
        """Shared after-a-bid effects: refresh the listing message and DM the person who
        was just outbid. Used by the slash command, the Bid button, and the network relay."""
        await self._refresh_message(listing_id, extra=note)
        if res.get("prev_bidder"):
            # `is not False` rather than truthiness: a caller that predates
            # `prev_released` (or a relay that drops it) gets the old sentence,
            # and only a MEASURED failure changes it.
            await self._dm_outbid(res["prev_bidder"], listing_id, res.get("amount"),
                                  released=res.get("prev_released") is not False)

    async def _open_deal_room(self, listing: dict, buyer_id, price: float):
        """Open a private thread with the seller + winner to coordinate the transfer.
        Created in the configured deals channel, else the listing's own channel. Adding a
        member who isn't in this server just fails quietly (network winners get the DM)."""
        try:
            import Restocker_db as _db
            chan_id = _db.get_config("realestate:deals_channel") or listing.get("channel_id")
            if not chan_id:
                return
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
            if channel is None or not hasattr(channel, "create_thread"):
                return
            what = _listing_title(listing)
            try:
                thread = await channel.create_thread(
                    name=f"🤝 #{listing['id']} {what}"[:100],
                    type=discord.ChannelType.private_thread, invitable=False)
            except Exception:
                # server may not allow private threads — fall back to a public one
                thread = await channel.create_thread(
                    name=f"🤝 #{listing['id']} {what}"[:100], type=discord.ChannelType.public_thread)
            guild = getattr(channel, "guild", None)
            for uid in (listing["seller_id"], str(buyer_id)):
                try:
                    member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                    await thread.add_user(member)
                except Exception:
                    pass
            body = (f"🤝 **Transfer room** — **{what}** (#{listing['id']})\n"
                    f"Seller <@{listing['seller_id']}> · Winner <@{buyer_id}> · "
                    f"sold for `{_fmt(price)}` 🪙 (already settled via escrow — no payment here).\n")
            if listing.get("coords"):
                body += f"Coords: `{listing['coords']}`\n"
            if listing.get("winner_message"):
                body += f"Seller's note: {listing['winner_message']}\n"
            body += "Sort out the in-game handover here. Ping a manager if you need help."
            await thread.send(body[:1900])
        except Exception as e:
            log.warning("[realestate] deal room failed for #%s: %s", listing.get("id"), e)

    async def _ping_notify_role(self, channel, kind: str, listing_id: int, title: str):
        """Mention the opt-in notify role for this listing kind, if one is configured."""
        try:
            import Restocker_db as _db
            rid = _db.get_config(f"realestate:notify_role:{kind}")
            if not rid or channel is None:
                return
            await channel.send(
                f"<@&{int(rid)}> 🆕 New {_NOTIFY_LABEL.get(kind, kind)} listing — **{title}** (#{listing_id})",
                # `title` is the seller's own text (a /realestate auction parameter).
                # Spelled out in full: verified against discord.py 2.7.1,
                # `AllowedMentions(roles=True).to_dict()` is
                # {'replied_user': True, 'parse': ['everyone', 'users', 'roles']} —
                # the UNSET `everyone` field is the truthy `default` sentinel, not
                # False. A listing titled "@everyone" pinged the whole guild from
                # the auction channel.
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=True, users=False))
        except Exception as e:
            log.warning("[realestate] notify-role ping failed for #%s: %s", listing_id, e)

    async def _refresh_message(self, listing_id: int, extra: str = ""):
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        if not listing or not listing.get("channel_id") or not listing.get("message_id"):
            return
        try:
            channel = self.bot.get_channel(int(listing["channel_id"])) or await self.bot.fetch_channel(int(listing["channel_id"]))
            msg = await channel.fetch_message(int(listing["message_id"]))
            bids = _db.get_land_bids(listing_id, limit=5)
            # Photos were uploaded AS attachments on the listing message, so reference them
            # by attachment://<name> — those never expire (Discord CDN URLs do). Omitting the
            # `attachments` kwarg on edit keeps the existing files on the message.
            names = [a.filename for a in msg.attachments] or None
            await msg.edit(embeds=_gallery_embeds(listing, bids, attachment_names=names),
                           view=_listing_view(listing))
            if extra:
                await channel.send(extra)
        except Exception as e:
            log.warning("[realestate] refresh_message failed for #%s: %s", listing_id, e)

    async def _settle_expired(self, listing_id: int):
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        # Settle path 5. `get_expired_active_listings()` already filtered on
        # `status='active'` in SQL, but a staff rollback can land between that
        # SELECT and this line — which is the whole 60-second window the mint
        # lived in. Re-read, re-gate, and say nothing: a lot that went terminal
        # while the sweep was walking is not an error, it is the point.
        if _settle_gate(listing):
            return
        # An interrupted instant buy is a SALE waiting to be finished, and it is
        # invisible on `land_listings`: it writes no `current_bid`/`current_bidder`,
        # so the branch below reads it as "no bids" and expires a lot whose price
        # is already in `treasury:estates`. `resume_row` asks the escrow instead,
        # and it is checked first because a buy over a live auction leaves BOTH a
        # standing bid on the board and a captured `buy` row — the captured row is
        # the one somebody has actually paid for.
        resume = _settle.resume_row(listing_id)
        if resume is not None:
            price_i = int(resume["hold_amount"] or 0)
            await self._finalize_sale(
                listing_id, resume["bidder_id"], price_i, win_row_id=int(resume["id"]),
                note=(f"🔨 Auction **#{listing_id}** ended — sold to <@{resume['bidder_id']}> "
                      f"for `{_fmt(price_i)}` 🪙."))
        elif listing.get("current_bid") and listing.get("current_bidder"):
            await self._finalize_sale(
                listing_id, int(listing["current_bidder"]), float(listing["current_bid"]),
                note=(f"🔨 Auction **#{listing_id}** ended — sold to <@{listing['current_bidder']}> "
                      f"for `{_fmt(listing['current_bid'])}` 🪙."))
        else:
            # Not a bare status flip: `expire_unsold` claims the row and releases
            # anything still held on it first. A lot with no `current_bidder` and
            # a live hold is a bug, and the cheap way to survive it is to release
            # rather than to assert it cannot happen and leak a bidder's coins.
            _settle.expire_unsold(listing_id)
            await self._refresh_message(listing_id, extra=f"⌛ Auction **#{listing_id}** ended with no bids.")

    async def _resume_part_settled(self, listing_id: int):
        """Finish an interrupted sale on a lot that has NOT reached its deadline.

        `_settle_expired`'s narrow sibling. That one owns "this auction is over —
        sold, or unsold": it may expire a lot, or sell it to the standing bidder.
        NEITHER IS LEGAL HERE. This lot is still live; its auction has not ended
        and nobody has out-waited anybody. The one fact that is true about it is
        that some row on it holds coins that have already moved, and the only
        correct ending for those coins is the sale they were captured for.

        The row is `resume_row`'s, which is the read `_settle_expired` and
        `close_listing_core` already share, so the three cannot drift into three
        answers to "does this lot have a buyer". It covers the `held` BUY row as
        well as the captured ones, and that case is deliberate rather than
        incidental: an instant buy is a one-bid auction that closes at once, so a
        `held` `kind='buy'` row is a purchase whose own settlement did not finish
        — the buyer has asked for the lot at Buy-Now and their coins are set aside
        for it. `_instant_buy_core` releases such a row itself when it can; one
        that is still sitting there is one where the release did not happen.
        A standing AUCTION bid can never be picked up here: `open_buy_row` filters
        on `kind='buy'`, so a live auction cannot be closed early by this sweep.

        AN EXPIRED HOLD IS NOT RESUMED, and this is the sharp edge of running
        before the deadline. A hold outlives its row by `BUY_HOLD_SECONDS`; past
        that, core has released the coins on its own and the capture would fail —
        `settle_listing` turns a failed capture into terminal `failed_escrow`,
        which on a lot that has NOT ended would destroy a live auction over a
        click somebody abandoned an hour ago. Past `ends_at` that is the right
        answer (the lot is over either way) and `_settle_expired` still gives it;
        here it is not, so a stale reservation is left alone and the lot keeps
        trading.

        The price comes from the row's `hold_amount`, not the board: an instant
        buy writes nothing to `current_bid`, so the escrow is the only record of
        what was actually paid.
        """
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        # Re-read and re-gate, exactly as `_settle_expired` does: a staff rollback
        # or a manager close can land between the SELECT and this line.
        if _settle_gate(listing):
            return
        resume = _settle.resume_row(listing_id)
        if resume is None:
            return                      # the doubt resolved to `released` — nothing was paid
        if str(resume.get("status")) == "held":
            # CANDIDATE FIX (R5V V2): a merely `held` row is a RESERVATION —
            # no coins have moved. Completing it is money ENTERING the exchange,
            # which is exactly what the kill switch is thrown to stop. The
            # captured/capture_unknown branches below are money COMPLETING and
            # still run while frozen, which is the un-gating's whole point.
            if bidding_frozen(_db):
                log.info("[realestate] lot #%s: buy row %s is only `held` and the "
                         "exchange is frozen — not starting a capture.",
                         listing_id, resume.get("id"))
                return
            exp = resume.get("hold_expires_at")
            # `_esc._sqlish` first: `ledger_v2` writes ISO-8601 with a `T`, and
            # `_epoch` parses SQLite's space form and returns NOW for anything it
            # cannot read — so handing it the raw value would call every live hold
            # expired and disable this whole sweep silently.
            if exp and _epoch(_esc._sqlish(exp)) <= int(
                    datetime.now(timezone.utc).timestamp()):
                log.warning("[realestate] lot #%s: buy row %s is still `held` but its "
                            "hold expired at %s — NOT settling a live lot on a "
                            "reservation core has already let go.",
                            listing_id, resume.get("id"), exp)
                return
        price_i = int(resume["hold_amount"] or 0)
        if price_i <= 0:
            log.error("[realestate] lot #%s has a part-settled row %s with no usable "
                      "hold amount (%r) — a human must reconcile it; NOT settling.",
                      listing_id, resume.get("id"), resume.get("hold_amount"))
            return
        await self._finalize_sale(
            listing_id, resume["bidder_id"], price_i, win_row_id=int(resume["id"]),
            note=(f"🛒 **#{listing_id}** — the interrupted purchase by "
                  f"<@{resume['bidder_id']}> for `{_fmt(price_i)}` 🪙 has now completed."))

    @tasks.loop(minutes=1)
    async def auction_sweep_loop(self):
        """The minute loop: recover, reconcile, settle, then charge rent.

        ORDER MATTERS AND IT IS NOT ALPHABETICAL.

          1. `rearm_stale_claims` first. A listing stuck in `settling` from a dead
             process is invisible to `get_expired_active_listings()`, so if this
             ran last the lot would wait a full extra minute after every crash —
             and if it never ran the lot would wait forever.
          2. `reconcile_holds` / `replay_placements` next, because a lot with a
             row in `capture_unknown` REFUSES to settle (its coins may already be
             in the treasury). Resolving doubt before settling is what turns a
             lost response into a completed sale rather than a stuck one.
          2b. The two §3.6 invariant checks — every live lot's escrow outlives its
             own close, and no lot that is OVER still has escrow open. Both run
             after the doubt is resolved and before settlement, so a lot repaired
             here can still settle on this same pass.
          3. Settlement of lots past `ends_at`.
          3b. RESUME of part-settled lots that are NOT past `ends_at`. Un-gated
             from the deadline on purpose (owner decision, 15 Aug) and strictly
             narrower than step 3: it may only finish a sale whose coins have
             already moved, never expire a lot and never sell one to a bidder.
          4. Rent last, and only if it is switched on. It is the newest and least
             proven thing in this loop; if it throws, everything above it has
             already run.

        Every step is separately try/excepted for that reason: this loop is the
        only thing that finishes an interrupted settlement, so one failing step
        must never stop the next.
        """
        try:
            import Restocker_db as _db
            try:
                _settle.rearm_stale_claims()
            except Exception as e:  # noqa: BLE001
                log.warning("[realestate] stale-claim re-arm failed: %s", e)
            if _esc.escrow_available():
                for name, fn in (("hold reconcile", _esc.reconcile_holds),
                                 ("placement replay", _esc.replay_placements)):
                    try:
                        fn()
                    except Exception as e:  # noqa: BLE001
                        log.warning("[realestate] %s sweep failed: %s", name, e)
                # 2b. The anti-snipe guard. `_place_bid_core` extends the winner's
                # hold in the same call that extends the lot, and that is not
                # sufficient on its own: a crash between the `ends_at` write and
                # the `extend_hold` leaves a lot that outlives its escrow, core's
                # expiry sweeper releases the winner's coins mid-auction, and the
                # close then pays a seller out of a capture that fails. This is
                # check 1 of LAND_ESCROW_PLAN §3.6 and costs one indexed read.
                # It runs BEFORE settlement so a lot repaired here can still be
                # settled on this same pass.
                try:
                    _esc.sweep_hold_extensions(_db.get_active_land_listings("auction"),
                                               _epoch)
                except Exception as e:  # noqa: BLE001
                    log.warning("[realestate] hold-extension sweep failed: %s", e)
                # 2b'. LAND_ESCROW_PLAN §3.6 CHECK 2: no open hold references a
                # listing that is sold, cancelled or expired. The plan wrote this
                # down and nothing built it, and F3 is what that cost — a bid whose
                # `place_hold` answer was lost, replayed onto a lot that had been
                # cancelled in the meantime, with cancel/settle/expire all having
                # run their release loops before the row had a hold id to release.
                # Nothing came back for it and the bidder's coins stayed reserved
                # for 24 hours with nobody told.
                #
                # It is an INVARIANT rather than a path fix: it does not care how
                # the hold got there, which is the only reason it would have caught
                # F3 before anyone knew F3 existed. It runs AFTER the replay above
                # (so a placement resolved this pass is retired on this pass) and
                # BEFORE settlement (so it cannot be mistaken for part of one).
                # Normal cost: one indexed join returning nothing.
                try:
                    _esc.sweep_terminal_listing_holds()
                except Exception as e:  # noqa: BLE001
                    log.warning("[realestate] terminal-listing hold sweep failed: %s", e)
                # 2c. Parked rows. Core has refused the same capture or release
                # MAX_HOLD_REFUSALS times, so retrying is pointless and the sweeps
                # above deliberately leave them alone. Parked is not lost — the
                # hold is still open and the coins are still the bidder's — but a
                # parked row nobody is told about IS lost, so it says so every
                # pass rather than sitting silently in a status column.
                try:
                    parked = _esc.refused_rows(limit=20)
                    if parked:
                        log.error("[realestate] %s escrow row(s) parked for a human: %s. "
                                  "Core refuses these every time; the bidders' coins are "
                                  "still reserved under open holds. Check last_error on "
                                  "each and release or capture by hand.",
                                  len(parked),
                                  ", ".join(f"#{r['listing_id']}/row {r['id']} "
                                            f"({r['status']})" for r in parked))
                except Exception as e:  # noqa: BLE001
                    log.warning("[realestate] parked-row report failed: %s", e)
            done = set()
            for listing in _db.get_expired_active_listings():
                done.add(int(listing["id"]))
                try:
                    await self._settle_expired(listing["id"])
                except Exception as e:
                    log.warning("[realestate] auto-settle failed for #%s: %s", listing["id"], e)
            # 3b. THE RESUME SWEEP, UN-GATED FROM THE DEADLINE (owner decision,
            # 15 Aug). Step 3 above selects on `ends_at`, so an instant buy
            # interrupted mid-capture on a 7-day lot was never looked at by
            # anything: the buyer's coins sat in `treasury:estates`, the seller was
            # unpaid, and the only escape was the buyer clicking Buy again — which
            # `realestate:bidding_frozen` closes, and a ledger incident is both why
            # the switch is thrown and why captures get interrupted. A part-settled
            # lot is part-settled regardless of when it was due.
            #
            # RESUME ONLY. This runs `_resume_part_settled`, not `_settle_expired`:
            # those lots have NOT reached their deadline, so expiring one or selling
            # it to a standing bidder would end an auction early. The only thing it
            # is allowed to do is finish the sale somebody has already paid for.
            for listing in _db.get_part_settled_active_listings():
                if int(listing["id"]) in done:
                    continue
                try:
                    await self._resume_part_settled(int(listing["id"]))
                except Exception as e:  # noqa: BLE001
                    log.warning("[realestate] resume failed for #%s: %s", listing["id"], e)
            try:
                _settle.sweep_rent()
            except Exception as e:  # noqa: BLE001
                log.warning("[realestate] rent sweep failed: %s", e)
        except Exception as e:
            log.warning("[realestate] auction_sweep_loop error: %s", e)

    @auction_sweep_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    def _register_buttons(self):
        # Restart-safe buttons: register the DynamicItem classes once so the Bid/Buy
        # buttons on listing messages AND the notify-role panel keep working after a reboot.
        for cls in (BidButton, BuyButton, NotifyButton):
            try:
                self.bot.add_dynamic_items(cls)
            except Exception as e:
                log.warning("[realestate] dynamic button register failed: %s", e)

    async def cog_load(self):
        self._register_buttons()
        if self.bot.is_ready() and not self.auction_sweep_loop.is_running():
            self.auction_sweep_loop.start()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.auction_sweep_loop.is_running():
            self.auction_sweep_loop.start()

    def cog_unload(self):
        self.auction_sweep_loop.cancel()

    # ── /sell — the ONE command. Title + price, drag in photos, done. Land or item. ──
    @app_commands.command(
        name="sell",
        description="List anything for auction — one command: name it, set a price, drag in photos")
    @app_commands.describe(
        title="What you're selling (item or land name)",
        starting_price="Opening bid",
        buy_now="(Optional) Buy-It-Now price for an instant sale",
        photo="(Optional) drag a photo straight in",
        photo2="(Optional) a second photo",
        photo3="(Optional) a third photo",
        details="(Optional) description / condition / what's included",
        category="Pick a category — Land & Artificial Land list as land, the rest as items",
        chunks="(Land only) plot size in chunks — turns on AI valuation + company backing",
        backs_company="(Land only) a company this plot will back (65% rule) once sold",
        duration_days="(Optional) auction length in days — default from config",
    )
    @app_commands.choices(category=_CATEGORIES)
    # `title` becomes the listing headline AND the notify-role ping text, so a typo here
    # produces a listing that matches nothing anyone searches for. Autocomplete suggests
    # real catalog/stock items but does not constrain: a one-off land title still types
    # through fine, which is why this is safe on a field that is not always an item.
    @app_commands.autocomplete(backs_company=_market_autocomplete,
                               title=core.any_item_autocomplete)
    async def sell(self, interaction: discord.Interaction,
                   title: str, starting_price: float,
                   buy_now: Optional[float] = None,
                   photo: Optional[discord.Attachment] = None,
                   photo2: Optional[discord.Attachment] = None,
                   photo3: Optional[discord.Attachment] = None,
                   details: Optional[str] = None,
                   category: Optional[app_commands.Choice[str]] = None,
                   chunks: Optional[float] = None, backs_company: Optional[str] = None,
                   duration_days: Optional[int] = None):
        if starting_price <= 0:
            return await interaction.response.send_message("❌ `starting_price` must be > 0.", ephemeral=True)
        if buy_now is not None and buy_now <= starting_price:
            return await interaction.response.send_message(
                "❌ `buy_now` must be higher than the starting price.", ephemeral=True)
        if backs_company and not _get_market(backs_company):
            return await interaction.response.send_message(f"❌ Company `{backs_company}` not found.", ephemeral=True)

        import Restocker_db as _db
        # gather dragged-in photos → real image attachments
        atts = [a for a in (photo, photo2, photo3) if a is not None]
        for a in atts:
            if a.content_type and not a.content_type.startswith("image/"):
                return await interaction.response.send_message(
                    f"❌ `{a.filename}` isn't an image.", ephemeral=True)
        cat_name = category.value if category else None
        # Category decides land-vs-item (Land / Artificial Land = land), unless the seller
        # explicitly gave land data, which also forces land.
        is_land = bool(chunks or backs_company or (cat_name in _LAND_CATEGORIES))
        kind = "land" if is_land else "item"

        # AI-suggested reserve for land (annotates; never overrides the seller's price)
        ai_note = None
        if is_land and chunks:
            try:
                ai = _valuation.value_plot(chunks, "premium" if (backs_company) else "raw")
                ai_note = (f"🤖 AI valuation for reference: `{_fmt(ai['assessed_value'])}` 🪙 "
                           f"({_fmt(chunks)} chunks). Your starting price stands as set.")
            except Exception:
                ai_note = None

        anti_snipe = _gd(_db, "anti_snipe_minutes", DEF["anti_snipe_minutes"])
        commission_pct = _gd(_db, "commission_pct", DEF["commission_pct"])
        incr_pct = _gd(_db, "min_increment_pct", DEF["min_increment_pct"])
        ends_at = _sql_now_plus(days=_capped_duration(
            _db, duration_days or _gd(_db, "default_auction_days", DEF["default_auction_days"])))

        await interaction.response.defer(thinking=True)
        # Re-upload the photos AS attachments on the listing message so they never expire.
        files = []
        for i, a in enumerate(atts):
            try:
                files.append(await a.to_file())
            except Exception as e:
                log.warning("[realestate] photo fetch failed: %s", e)

        listing_id = _db.create_land_listing(
            seller_id=str(interaction.user.id), kind=kind, title=title.strip()[:120],
            category=cat_name, chunks=(chunks or 0),
            market_id=(backs_company or None), land=(title.strip()[:120] if is_land else None),
            description=(details or "").strip() or None, mode="auction",
            reserve=round(starting_price, 2), buy_now=(round(buy_now, 2) if buy_now else None),
            min_increment_pct=incr_pct, commission_pct=commission_pct, listing_fee=0,
            ends_at=ends_at, anti_snipe_minutes=int(anti_snipe), status="active")

        listing = _db.get_land_listing(listing_id)
        names = [f.filename for f in files] or None
        embeds = _gallery_embeds(listing, None, attachment_names=names)
        content = ai_note if ai_note else None
        # Edit the deferred placeholder into the listing (unambiguous — one message, and the
        # returned InteractionMessage carries the uploaded attachments' final URLs).
        msg = await interaction.edit_original_response(
            content=content, embeds=embeds, attachments=files, view=_listing_view(listing))
        # store persistent photo URLs (for the satellite) + the message location
        photo_urls = [a.url for a in getattr(msg, "attachments", [])]
        _db.update_land_listing(listing_id, channel_id=str(msg.channel.id), message_id=str(msg.id),
                                photos=(json.dumps(photo_urls) if photo_urls else None))
        # ping the opt-in notify role for this kind, if one is set
        await self._ping_notify_role(interaction.channel, kind, listing_id, title.strip()[:120])

    # ── /realestate list ──────────────────────────────────────────────────────────
    @realestate.command(name="list", description="List a plot for sale — fixed price or a timed auction")
    @app_commands.describe(
        chunks="Size of the plot in chunks",
        mode="Fixed price (buy_now) or a timed auction",
        quality="Build/farm/market quality — feeds the AI reserve price if you don't set one",
        reserve="(Auction) Starting/reserve price — omit to auto-value from chunks x quality",
        buy_now="Fixed-price sale price, or an optional instant-buy price on an auction",
        comps="(Optional) comma-separated recent comparable sale prices to fold into the AI reserve",
        land="(Optional) tracked land name — ties this listing to the land bound in /my market",
        market_id="(Optional) a company this plot will back (65% rule) once sold",
        coords="(Optional) plot coordinates — your choice whether to disclose",
        description="Short description of the plot / build",
        image="(Optional) image URL of the plot — shown on the listing (land sells on looks)",
        winner_message="(Optional) note DM'd to the winner on close (coords, 'come collect', etc.)",
        duration_days="(Auction) how many days it runs — default is set by /realestate config",
        min_increment_pct="(Auction) override the minimum bid raise, as a % of the current bid",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete, land=_land_autocomplete)
    async def list_(self, interaction: discord.Interaction, chunks: float, mode: _MODE,
                    quality: _QUALITY = "raw", reserve: Optional[float] = None,
                    buy_now: Optional[float] = None, comps: Optional[str] = None,
                    land: Optional[str] = None, market_id: Optional[str] = None,
                    coords: Optional[str] = None, description: Optional[str] = None,
                    image: Optional[str] = None, winner_message: Optional[str] = None,
                    duration_days: Optional[int] = None, min_increment_pct: Optional[float] = None):
        if chunks <= 0:
            return await interaction.response.send_message("❌ `chunks` must be > 0.", ephemeral=True)
        if market_id and not _get_market(market_id):
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)

        import Restocker_db as _db
        comp_list = [float(c) for c in (comps or "").split(",") if c.strip().replace(".", "", 1).isdigit()]
        ai = _valuation.value_plot(chunks, quality, comp_list)

        if mode == "fixed":
            if not buy_now or buy_now <= 0:
                return await interaction.response.send_message(
                    "❌ Fixed-price listings need `buy_now` (the sale price).", ephemeral=True)
            reserve_final = reserve if reserve and reserve > 0 else buy_now
            ends_at = None
        else:
            reserve_final = reserve if reserve and reserve > 0 else ai["assessed_value"]
            if buy_now and buy_now <= reserve_final:
                return await interaction.response.send_message(
                    "❌ `buy_now` must be higher than the reserve price.", ephemeral=True)
            ends_at = _sql_now_plus(days=_capped_duration(
            _db, duration_days or _gd(_db, "default_auction_days", DEF["default_auction_days"])))

        commission_pct = _gd(_db, "commission_pct", DEF["commission_pct"])
        listing_fee = _gd(_db, "listing_fee", DEF["listing_fee"])
        incr_pct = min_increment_pct if min_increment_pct and min_increment_pct > 0 else _gd(
            _db, "min_increment_pct", DEF["min_increment_pct"])
        anti_snipe = _gd(_db, "anti_snipe_minutes", DEF["anti_snipe_minutes"])

        seller_id = interaction.user.id
        img = (image or "").strip() or None
        if img and not img.lower().startswith(("http://", "https://")):
            return await interaction.response.send_message(
                "❌ `image` must be a full http(s) URL.", ephemeral=True)
        # DRAFT -> charge the fee -> ACTIVE. Same order, same key and the same
        # `charge_listing_fee` the satellite's /sell and `create_listing_core`
        # use, so the three listing surfaces stop having three different money
        # behaviours for one action. The balance pre-check is gone: the transfer
        # carries its own availability test, and a read-then-check is exactly the
        # race a hold exists to remove. The refusal below still shows figures —
        # it just gets them from the ledger's answer rather than from a guess.
        listing_id = _db.create_land_listing(
            seller_id=str(seller_id), kind="land", title=(land or "Land plot"),
            market_id=market_id, land=land, chunks=chunks,
            coords=coords, description=description, image_url=img,
            winner_message=((winner_message or "").strip() or None), mode=mode, quality=quality,
            reserve=round(reserve_final, 2), buy_now=(round(buy_now, 2) if buy_now else None),
            min_increment_pct=incr_pct, commission_pct=commission_pct, listing_fee=listing_fee,
            ends_at=ends_at, anti_snipe_minutes=int(anti_snipe), status="draft",
        )
        fee_res = _settle.charge_listing_fee(listing_id, seller_id, listing_fee)
        if not fee_res.get("ok"):
            _db.update_land_listing(listing_id, status="cancelled",
                                    closed_at=core.utcnow_iso())
            return await interaction.response.send_message(
                f"❌ {fee_res.get('error', 'The listing fee could not be charged.')}",
                ephemeral=True)
        _db.update_land_listing(listing_id, status="active")
        listing = _db.get_land_listing(listing_id)
        ai_note = (f"🤖 AI-suggested reserve: `{_fmt(ai['assessed_value'])}` 🪙 "
                   f"({_fmt(chunks)} chunks × `{_fmt(ai['rate_per_chunk'])}`/chunk × {ai['quality_multiplier']}x "
                   f"{quality}" + (f", folded with {len(comp_list)} comp(s)" if comp_list else "") + ")")
        await interaction.response.send_message(content=ai_note if mode == "auction" else None,
                                                embeds=_gallery_embeds(listing), view=_listing_view(listing))
        msg = await interaction.original_response()
        _db.update_land_listing(listing_id, channel_id=str(msg.channel.id), message_id=str(msg.id))
        await self._ping_notify_role(interaction.channel, "land", listing_id, (land or "Land plot"))

    # ── /realestate listings ──────────────────────────────────────────────────────
    @realestate.command(name="listings", description="Browse active land listings, soonest-ending first")
    async def listings(self, interaction: discord.Interaction):
        import Restocker_db as _db
        rows = _db.get_active_land_listings()
        if not rows:
            return await interaction.response.send_message("No active listings right now.", ephemeral=True)
        embed = discord.Embed(title="🏡 Restocker Land Exchange — active listings", color=0x2ECC71)
        for r in rows[:20]:
            price = (f"bid `{_fmt(r['current_bid'])}`" if r.get("current_bid")
                     else f"reserve `{_fmt(r['reserve'])}`") if r["mode"] == "auction" else f"`{_fmt(r['buy_now'])}`"
            ends = f" · ends <t:{_epoch(r['ends_at'])}:R>" if r.get("ends_at") else ""
            name = f"#{r['id']} " + (r.get("land") or (r.get("description") or "Land")[:40])
            embed.add_field(name=name[:256],
                            value=f"{'🔨' if r['mode']=='auction' else '🏷️'} {price} 🪙 · {_fmt(r['chunks'])} chunks{ends}",
                            inline=False)
        if len(rows) > 20:
            embed.set_footer(text=f"+{len(rows) - 20} more active — use /realestate info for a specific listing")
        await interaction.response.send_message(embed=embed)

    # ── /realestate info ────────────────────────────────────────────────────────────
    @realestate.command(name="info", description="Full detail + bid history for one listing")
    @app_commands.describe(listing_id="Listing to view")
    @app_commands.autocomplete(listing_id=_listing_autocomplete)
    async def info(self, interaction: discord.Interaction, listing_id: int):
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        if not listing:
            return await interaction.response.send_message(f"❌ No listing `#{listing_id}`.", ephemeral=True)
        bids = _db.get_land_bids(listing_id, limit=5)
        await interaction.response.send_message(embed=_listing_embed(listing, bids))

    # ── /realestate bid ─────────────────────────────────────────────────────────────
    @realestate.command(name="bid", description="Bid on an auction listing")
    @app_commands.describe(listing_id="Listing to bid on", amount="Bid amount — omit to bid the minimum allowed")
    @app_commands.autocomplete(listing_id=_listing_autocomplete)
    async def bid(self, interaction: discord.Interaction, listing_id: int, amount: Optional[float] = None):
        res = _place_bid_core(listing_id, interaction.user.id, amount)
        if not res.get("ok"):
            return await interaction.response.send_message(f"❌ {res['error']}", ephemeral=True)
        # Acknowledge FIRST (3s deadline), then run the slow after-effects (refresh + outbid DM).
        await interaction.response.send_message(
            f"✅ Bid placed: `{_fmt(res['amount'])}` 🪙 on `#{listing_id}`.", ephemeral=True)
        await self._post_bid(listing_id, res, _bid_note(listing_id, res, interaction.user.id))

    # ── /realestate buy ─────────────────────────────────────────────────────────────
    @realestate.command(name="buy", description="Buy a listing instantly at its fixed/instant-buy price")
    @app_commands.describe(listing_id="Listing to buy")
    @app_commands.autocomplete(listing_id=_listing_autocomplete)
    async def buy(self, interaction: discord.Interaction, listing_id: int):
        # Defer up front — settlement opens a transfer room (several HTTP calls) that would
        # blow the 3s response deadline if we waited to acknowledge until after it.
        await interaction.response.defer(ephemeral=True, thinking=True)
        res = _instant_buy_core(listing_id, interaction.user.id)
        if not res.get("ok"):
            return await interaction.followup.send(f"❌ {res['error']}", ephemeral=True)
        await self._post_sale(
            listing_id, interaction.user.id, res["price"],
            note=f"🛒 **#{listing_id}** bought instantly by <@{interaction.user.id}> for `{_fmt(res['price'])}` 🪙.")
        await interaction.followup.send(
            f"✅ Bought `#{listing_id}` for `{_fmt(res['price'])}` 🪙 — you're in a transfer room with the seller.",
            ephemeral=True)

    # ── /realestate cancel ──────────────────────────────────────────────────────────
    @realestate.command(name="cancel", description="Cancel your own listing (only if no bid has been placed yet)")
    @app_commands.describe(listing_id="Listing to cancel")
    @app_commands.autocomplete(listing_id=_listing_autocomplete)
    async def cancel(self, interaction: discord.Interaction, listing_id: int):
        # A THIN WRAPPER OVER THE CORE, deliberately. This body used to be a
        # SECOND cancel implementation beside `cancel_listing_core`, and the two
        # had already diverged from their close-path twins (one guarded
        # `current_bid or 0`, the other raised TypeError on a NULL bid). Every
        # money and permission decision below now happens in exactly one place;
        # what is left here is the Discord reply.
        res = cancel_listing_core(listing_id, interaction.user.id,
                                  is_mgr=is_manager(interaction))
        if not res.get("ok"):
            return await interaction.response.send_message(f"❌ {res['error']}", ephemeral=True)
        await self._refresh_message(listing_id, extra=f"🚫 Listing **#{listing_id}** was cancelled.")
        released = len(res.get("released") or [])
        extra = (f" {released} reserved bid(s) released — those bidders' coins are "
                 f"free again." if released else "")
        # THE `deferred` READER. `released: []` is NOT "this lot has no escrow
        # left": a row whose placement core has not confirmed cannot be released
        # on this path (it has no hold id yet, and re-sending the placement key
        # here could reserve a bidder's coins on a user-facing click), so it is
        # deferred to `land_escrow.sweep_terminal_listing_holds`. That is correct
        # and it is also invisible — a manager was told the lot was clean while a
        # row was still holding somebody's coins. Say it instead.
        deferred = len(res.get("deferred") or [])
        if deferred:
            extra += (f" ⏳ {deferred} more reservation(s) can't be ended on this "
                      f"click — their placement hasn't been confirmed by the "
                      f"ledger yet. Those coins are still reserved; the escrow "
                      f"sweep ends them within a few minutes. Nothing is lost and "
                      f"nobody has been charged.")
        await interaction.response.send_message(
            f"✅ Cancelled `#{listing_id}`.{extra}", ephemeral=True)

    # ── /realestate close (manager dispute-resolution / force-settle) ─────────────
    @realestate.command(name="close", description="(Managers) Force-settle or unwind a listing right now")
    @app_commands.describe(listing_id="Listing to close", refund_bidder="Cancel and refund the standing bid instead of selling")
    @app_commands.autocomplete(listing_id=_listing_autocomplete)
    async def close(self, interaction: discord.Interaction, listing_id: int, refund_bidder: bool = False):
        """(Managers) Force-settle or unwind. A THIN WRAPPER over `close_listing_core`.

        This body was the second of two close implementations and they had
        already diverged — the core guarded `listing["current_bid"] or 0`, this
        one did not, so a manager closing a lot with a NULL bid raised
        `TypeError` here and paid 0 there. There is now one close: this decides
        who may run it and what the reply says, and nothing else.
        """
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        gate = _settle_gate(listing)          # settle path 7b — /realestate close
        if gate:
            return await interaction.response.send_message(f"❌ {gate}", ephemeral=True)
        # Settlement can take several ledger transactions; acknowledge first.
        await interaction.response.defer(ephemeral=True, thinking=True)
        res = close_listing_core(listing_id, refund_bidder=refund_bidder)
        if not res.get("ok"):
            return await interaction.followup.send(
                f"❌ {res.get('error', 'That listing could not be closed.')}", ephemeral=True)

        outcome = res.get("outcome")
        if outcome == "cancelled_refunded":
            # The audit row is recorded inside the core with the same key the
            # headless twin uses, so whichever path runs, the row exists once.
            # Posting it is this layer's job because only this layer has a bot.
            if res.get("action_key"):
                await self._post_audit_row(res["action_key"])
            # THE `deferred` READER, CLOSE PATH — the twin of the one in
            # `/realestate cancel` above, and it was missing until 15 Aug.
            # Mutation proved the asymmetry: deleting the cancel reader failed a
            # test, deleting the producer here failed nothing, because nothing
            # consumed it. The consequence was worse on this path than on that
            # one: cancel was merely SILENT about a still-open reservation, while
            # close posted an affirmatively false statement into a public
            # channel — "the standing bidder's coins are no longer reserved" over
            # a lot whose `place_unknown` row may be reserving them right now.
            #
            # `released: []` is not "this lot is clean". A row whose placement
            # core never confirmed has no hold id to name, and re-sending its
            # placement key on a user-facing click could RESERVE a bidder's
            # coins — so `land_settle.release_all_holds` is not allowed to touch
            # it and returns it as `deferred` instead. `land_escrow.
            # sweep_terminal_listing_holds` retires it within the minute.
            #
            # The condition is the fix, NOT the deletion of the reassuring
            # sentence: on a clean close the reservation really is over and
            # saying so is the truthful, useful thing (probe_copy_r5 K1b).
            deferred = len(res.get("deferred") or [])
            if deferred:
                note = (f"🚫 Listing **#{listing_id}** was closed by a manager. "
                        f"⏳ {deferred} reservation(s) on it are still reserved for "
                        f"now — the ledger hasn't confirmed them yet, so they can't "
                        f"be ended on this click. The escrow sweep ends them within "
                        f"a few minutes. Nobody has been charged.")
                reply = (f"✅ Closed `#{listing_id}`. ⏳ {deferred} reservation(s) "
                         f"can't be ended on this click — their placement hasn't "
                         f"been confirmed by the ledger yet. Those coins are still "
                         f"reserved; the escrow sweep ends them within a few "
                         f"minutes. Nothing is lost and nobody has been charged.")
            else:
                note = (f"🚫 Listing **#{listing_id}** was closed by a manager — the "
                        f"standing bidder's coins are no longer reserved.")
                reply = f"✅ Closed `#{listing_id}` and released the standing bid."
            await self._refresh_message(listing_id, extra=note)
            return await interaction.followup.send(reply, ephemeral=True)
        if outcome == "sold":
            await self._post_sale(
                listing_id, res.get("sold_to_buyer"), res.get("price"),
                note=(f"🔨 Listing **#{listing_id}** closed by a manager — sold to "
                      f"<@{res.get('sold_to_buyer')}> for `{_fmt(res.get('price'))}` 🪙."))
            return await interaction.followup.send(f"✅ Settled `#{listing_id}` as sold.", ephemeral=True)
        if outcome == "expired":
            await self._refresh_message(
                listing_id, extra=f"⌛ Listing **#{listing_id}** was closed by a manager with no bids.")
            return await interaction.followup.send(
                f"✅ Closed `#{listing_id}` — no bids to settle.", ephemeral=True)
        # `already_settling` / `in_doubt` — a real answer, not a failure.
        await interaction.followup.send(
            f"ℹ️ `#{listing_id}`: {res.get('error') or outcome}", ephemeral=True)

    # ── /realestate notify_role + notifypanel (opt-in listing pings) ───────────────
    @realestate.command(name="notify_role",
                        description="(Managers) Set the opt-in role pinged when a new Land/Item listing goes up")
    @app_commands.describe(kind="Which listings this role is for", role="The role to ping (and let members self-assign)")
    @app_commands.choices(kind=[app_commands.Choice(name="Land", value="land"),
                                app_commands.Choice(name="Items", value="item")])
    async def notify_role(self, interaction: discord.Interaction,
                          kind: app_commands.Choice[str], role: discord.Role):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        _db.set_config(f"realestate:notify_role:{kind.value}", str(role.id))
        await interaction.response.send_message(
            f"✅ New **{_NOTIFY_LABEL.get(kind.value)}** listings will now ping {role.mention}. "
            f"Post the self-assign panel with `/realestate notifypanel`. "
            f"⚠️ Make sure my role is **above** {role.mention} so I can assign it.", ephemeral=True)

    @realestate.command(name="notifypanel",
                        description="(Managers) Post the button panel where members opt in to listing pings")
    async def notifypanel(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        kinds = [k for k in ("land", "item") if _db.get_config(f"realestate:notify_role:{k}")]
        if not kinds:
            return await interaction.response.send_message(
                "❌ Set a role first with `/realestate notify_role`.", ephemeral=True)
        embed = discord.Embed(
            title="🔔 Get notified about new listings",
            description="Click a button to toggle a ping role for yourself. You'll be @mentioned "
                        "when a new listing of that type goes up — click again any time to opt out.",
            color=0x2ECC71)
        await interaction.channel.send(embed=embed, view=_notify_panel_view(kinds))
        await interaction.response.send_message("✅ Notify panel posted.", ephemeral=True)

    # ── /realestate config (managers) ──────────────────────────────────────────────
    @realestate.command(name="config", description="(Managers) View/set Land Exchange commission, fees & auction defaults")
    @app_commands.describe(
        commission_pct="House commission % on every completed sale",
        listing_fee="Flat coin fee charged up front to create a listing",
        min_increment_pct="Default minimum bid raise, as a % of the current bid",
        anti_snipe_minutes="A bid inside this many minutes of the end extends it",
        default_auction_days="Default auction length in days",
        bidding_frozen="🧊 KILL SWITCH — True stops ALL new bids/buys instantly. False re-opens.",
        freeze_reason="Shown to players on every listing while frozen (e.g. 'checking a duplicate sale')",
    )
    async def config(self, interaction: discord.Interaction, commission_pct: Optional[float] = None,
                     listing_fee: Optional[float] = None, min_increment_pct: Optional[float] = None,
                     anti_snipe_minutes: Optional[float] = None, default_auction_days: Optional[float] = None,
                     bidding_frozen: Optional[bool] = None, freeze_reason: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        for key, val in (("commission_pct", commission_pct), ("listing_fee", listing_fee),
                         ("min_increment_pct", min_increment_pct), ("anti_snipe_minutes", anti_snipe_minutes),
                         ("default_auction_days", default_auction_days)):
            if val is not None:
                _db.set_config(f"realestate:{key}", str(float(val)))
        # The switch, from the surface an operator already has open. Un-freezing is
        # the SAME command with `bidding_frozen: False` — one field, one click.
        if bidding_frozen is not None:
            set_bidding_frozen(bidding_frozen,
                               by=f"<@{interaction.user.id}>",
                               reason=(freeze_reason or ""), _db=_db)
        state = freeze_state(_db)
        lines = []
        if state["frozen"]:
            # Top of the embed, before the knobs: it is the only line that matters
            # while it is on, and it carries who + when + how to undo it.
            lines.append(f"{FREEZE_HEADING} — **NEW BIDS AND BUYS ARE BLOCKED**")
            lines.append(f"· set by {state['by'] or 'unknown'}"
                         + (f" at `{state['at']}` UTC" if state["at"] else ""))
            if state["reason"]:
                lines.append(f"· reason: {state['reason']}")
            lines.append("· in-flight lots STILL settle — this stops money entering, not completing.")
            lines.append("· re-open with `/realestate config bidding_frozen:False`")
            lines.append("")
        lines += [f"**{k}** — `{_gd(_db, k, DEF[k])}`" for k in DEF]
        embed = discord.Embed(title="⚙️ Land Exchange configuration", description="\n".join(lines),
                              color=(0x5DADE2 if state["frozen"] else 0x22FF7A))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(LandExchangeCog(bot))
