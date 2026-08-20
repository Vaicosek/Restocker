"""
estates_web.py — Lands, Auctions and pari-mutuel Prediction Markets.

Mounted like `bank_api`: `register_estates_routes(app)`, called in the `try/except`
block in `Restocker_web.start_webserver` immediately before `web.AppRunner`.

WHAT IS HERE, AND WHERE ITS DATA ACTUALLY LIVES
-----------------------------------------------
Three surfaces, two databases, and the split is not arbitrary:

  * **Auctions** — `land_listings` / `land_bids` in `restocker.db` (`Restocker_db.py:743`).
    The lot board, the bid ladder and the hold state per bid row.
    `estates.db` has retired `auctions`/`bids` tables of its own; they are dead at
    SCHEMA_VERSION 5 and `ESTATES_DB_USAGE.md`'s appendix says so in one line —
    "If you want a lot, a bid, a hammer price or a seller payout, the answer is core's
    exchange. Do not add a second one." This module reads core's, and only core's.

  * **Parcels, leases and rent** — `parcels` / `rent_charges` in `estates.db`.

  * **Prediction markets** — `markets` / `outcomes` / `stakes` in `estates.db`,
    through the frozen `estates_db` interface. Pari-mutuel: players stake against each
    other and the house takes a rake off the pool, never a side. There is no
    house-banked game on this site and none may be added — the casino was scrapped.

  * **Money** — `ledger_v2`, in-process, as the service `estates`. Not over HTTP: the
    land exchange runs inside core (LAND_ESCROW_PLAN §1.1), so `place_hold`,
    `release_hold` and `get_balance` are ordinary function calls. `estates` holds
    `wallet.read`, `wallet.transfer` and `hold.*` and deliberately NOT `wallet.mint`:
    a bug here can misallocate coins and can never create them.

A STAKE IS A HOLD. A BID IS A HOLD. NEITHER IS A DEBIT.
------------------------------------------------------
This is the property the whole section is built to have, and it is the one the UI is
required to state rather than imply. `place_hold` reserves against AVAILABLE and moves
nothing: `balance` is untouched, `held` rises, `available` falls. The coins stay the
bidder's until the lot settles and a `capture` moves them. Being outbid is a `release` —
the same reservation retired, not a refund recomputed from a stored number. Every screen
that shows a bid or a stake says "held, not spent", and the confirm screen says it again
with the figures.

The bid path, in the order LAND_ESCROW_PLAN §2.2 requires:

  1. validate the lot — open, auction, not ended, not the seller, not already top,
     meets the minimum next bid. **No balance pre-read as an authority**: `place_hold`
     carries the availability test inside its own INSERT ... WHERE, so the check IS the
     write and the read-then-write race cannot exist.
  2. write the bid row FIRST, `status='pending'`, with `idem_key` and `capture_key`
     already minted from its own row id. Claim-first. A crash here leaks a pending row
     and nothing else.
  3. claim `pending -> placing`.
  4. `place_hold(... key='land:listing:<lid>:bid:<row>')`.
       * definite refusal  -> row `failed`, and "nothing was reserved" is TRUE.
       * timeout / unknown -> row `place_unknown`, NEVER `failed`, because a hold may
         exist; the row keeps its key so a replay makes core repeat its answer.
  5. write back `hold_id`, `hold_expires_at`, `status='held'`.
  6. THEN release the previous top bid's hold. After step 5, never before: a crash
     between them over-reserves one bidder for a while, which a sweep fixes; the
     reverse order leaves the lot with no hold on it at all.
  7. update `current_bid` / `current_bidder`.

ESCROW COLUMNS
--------------
`land_bids` needs `kind`, `idem_key`, `hold_id`, `hold_expires_at`, `status`,
`capture_key`, `attempts`, `last_error`, `claimed_at`, `settled_at` (LAND_ESCROW_PLAN
§1.2). They are added here, idempotently, in `_ensure_escrow_columns` — the same
"cheap to call repeatedly" shape as `bank_api._ensure_tables`. THIS IS A STOPGAP: the
plan puts them in `Restocker_db._migrate`'s ALTER list alongside the integer-money
migration, and when P3 ships this function becomes a no-op it can keep calling safely.
It does not convert `land_bids.amount` to INTEGER — that is P3's migration and it is not
this module's to run. Every amount this module writes is already an integer, and every
amount it reads is passed through `int(round(...))` before it is shown or held.

IDENTITY AND IDEMPOTENCY
------------------------
Identity is the session's Discord id, from the cookie, via `vt_web_shell.session_user`.
A user id in a request body is ignored and logged as an attack signal. Every money POST
carries a form key minted when the page rendered, verified and claimed single-use by
`vt_web_shell.money_post`; a replayed bid returns the first bid's result and does not
place a second hold. That key is separate from — and sits in front of — the ledger's own
domain key, and both are needed: the form key stops a double-click creating a second bid
ROW, the ledger key stops a retried hold reserving the coins twice.
"""

from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone
from typing import Optional

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp is a hard dep of the web server
    web = None  # type: ignore[assignment]

import vt_web_shell as shell

log = logging.getLogger("estates_web")

ESTATES_VERSION = "1.0"

#: LEDGER_API_v2 §5: auction holds expire at lot close + 24h, wagers at resolve + 7d.
#: The grace is what stops a settlement that is retrying at attempt 3 of 5 from losing
#: its escrow underneath itself.
BID_HOLD_GRACE_S = 24 * 3600
WAGER_HOLD_GRACE_S = 7 * 24 * 3600
SERVICE = "estates"


# ══════════════════════════════════════════════════════════════════════════
# Backing services — each optional, each absent by NAME rather than silently
# ══════════════════════════════════════════════════════════════════════════

def _ledger():
    import ledger_v2 as _L
    return _L


def _core_db():
    import Restocker_db as _db
    return _db


def _edb():
    """`estates_db`, or None. Parcels and prediction markets live there.

    Absent means those two panels are not rendered and say why. It does NOT mean they
    render empty: an empty parcel register and an unreachable one look identical on
    screen and mean opposite things.
    """
    try:
        import estates_db as _e
        return _e
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════
# Escrow columns on land_bids
# ══════════════════════════════════════════════════════════════════════════

_ESCROW_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'bid'"),
    ("idem_key", "TEXT"),
    ("capture_key", "TEXT"),
    ("hold_id", "TEXT"),
    ("hold_expires_at", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("last_error", "TEXT"),
    ("claimed_at", "TEXT"),
    ("settled_at", "TEXT"),
)

_ESCROW_READY = False


def _ensure_escrow_columns() -> None:
    """Add the escrow columns to `land_bids` if they are not there. Idempotent.

    `ALTER TABLE ... ADD COLUMN` on an existing column raises `OperationalError` and
    nothing else, so the per-column try/except is the whole migration — the same shape
    `Restocker_db._migrate` already uses for its own ALTER list. A UNIQUE index on
    `idem_key` is created separately and partially: legacy rows have NULL keys and
    SQLite treats NULLs as distinct, so the constraint binds new rows without rejecting
    the ones that predate it.
    """
    global _ESCROW_READY
    if _ESCROW_READY:
        return
    db = _core_db()
    with db.db() as conn:
        have = {r[1] for r in conn.execute("PRAGMA table_info(land_bids)").fetchall()}
        for name, decl in _ESCROW_COLUMNS:
            if name in have:
                continue
            try:
                conn.execute(f"ALTER TABLE land_bids ADD COLUMN {name} {decl}")
            except Exception as e:  # pragma: no cover - duplicate column on a race
                log.debug("[estates] land_bids.%s not added: %s", name, e)
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_land_bids_idem "
                         "ON land_bids(idem_key) WHERE idem_key IS NOT NULL")
        except Exception as e:
            log.debug("[estates] land_bids idem index not created: %s", e)
    _ESCROW_READY = True


# ══════════════════════════════════════════════════════════════════════════
# Small helpers
# ══════════════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _secs_left(ends_at) -> Optional[int]:
    d = _parse_ts(ends_at)
    if d is None:
        return None
    return max(0, int((d - _now()).total_seconds()))


def _hold_ttl(deadline, grace: int) -> int:
    """Seconds a hold should live: to the deadline plus the domain's grace, clamped.

    Clamped to the ledger's own bounds rather than passed through raw, because a lot
    that has already ended would otherwise ask for a negative TTL and be refused with
    `bad_expiry` — which reads to a bidder as "your bid was rejected" when what happened
    is that the page and the clock disagreed by a second.
    """
    L = _ledger()
    d = _parse_ts(deadline)
    base = int((d - _now()).total_seconds()) if d else grace
    return max(L.MIN_HOLD_SECONDS, min(L.MAX_HOLD_SECONDS, base + grace))


def _min_next_bid(listing: dict) -> int:
    """The lowest acceptable next bid, ALWAYS an int.

    `land_listings.reserve` is REAL and `min_increment_pct` is a percentage, so the
    first bid on a listing is where fractional coins are born (LAND_FLOAT_EXPOSURE).
    A hold amount is an integer by contract — `ledger_v2` rejects anything else — so
    this rounds UP at the boundary: rounding down would produce a "minimum" the ledger
    then refuses as below the minimum.
    """
    cur = float(listing.get("current_bid") or 0)
    if cur <= 0:
        return int(-(-float(listing.get("reserve") or 0) // 1)) or 1
    pct = float(listing.get("min_increment_pct") or 5.0)
    step = max(1.0, cur * pct / 100.0)
    return int(-(-(cur + step) // 1))


_NAMES_CACHE: dict = {}
_NAMES_AT = 0.0


def _names() -> dict:
    """`{user_id: display name}` from the same `stock_names.yml` the exchange uses."""
    global _NAMES_CACHE, _NAMES_AT
    if time.time() - _NAMES_AT < 60:
        return _NAMES_CACHE
    out = {}
    try:
        import Restocker_web as _rw
        out = _rw._load_data_yaml("stock_names.yml", {}) or {}
    except Exception:
        out = {}
    _NAMES_CACHE, _NAMES_AT = out, time.time()
    return out


#: What an anonymous player is called, per screen. Anonymity is the default and does
#: not vary; the NOUN does. Calling a parcel's owner "Another bidder" on the register —
#: a screen with no auction on it — is the right privacy answer given in the wrong
#: words, and a player reading it reasonably concludes somebody is bidding on his land.
_ANON_NOUN = {
    "bidder": "Another bidder",
    "owner": "The owner",
    "tenant": "The tenant",
    "seller": "Another player",
    "player": "Another player",
}


def _who(uid, viewer: Optional[str] = None, role: str = "bidder") -> str:
    """A human name for a player in `role`. Real names over internal ids — never a raw id.

    A player who has not opted in to being named on public boards is not named: he is
    "Another bidder" on an auction ladder, "The owner" on the parcel register, and never
    a truncated snowflake — an id fragment is still an id, it just fails to identify
    anybody while looking like it should.

    `role` changes only the wording of that refusal. Every caller passes the role the
    screen is actually showing, so the noun matches the column heading above it.
    """
    uid = str(uid or "")
    if not uid:
        return "—"
    if viewer is not None and uid == str(viewer):
        return "You"
    if uid.startswith("treasury:"):
        return "V Tech treasury"
    anon = _ANON_NOUN.get(role, _ANON_NOUN["player"])
    if _is_anonymous(uid):
        return anon
    name = _names().get(uid)
    return str(name) if name else anon


def _is_anonymous(uid: str) -> bool:
    """Default TRUE, matching `_handle_api_me` and the public leaderboard.

    Defaulting to visible here would EXPOSE every player who has never touched the
    toggle — which is precisely the bug the comment at `Restocker_web:1360` records
    having already been paid for once.
    """
    try:
        import Restocker_web as _rw
        return bool((_rw._user_prefs() or {}).get(str(uid), {}).get("anonymous", True))
    except Exception:
        return True


def _bid_hold_state(row: dict) -> str:
    """What a ladder row says about the coins behind it, in the reader's language.

    A row with no `idem_key` predates escrow entirely — the ALTER gave it the column
    default `pending`, which would render as "escrow pending" for a bid that was settled
    by the old debit-and-refund path months ago. It reads "pre-escrow" instead, because
    a wrong state is worse than an unknown one on a screen about somebody's coins.
    """
    if not row.get("idem_key"):
        return "pre-escrow"
    st = str(row.get("status") or "")
    return {
        "held": "held",
        "captured": "captured",
        "released": "released",
        "releasing": "releasing",
        "capturing": "capturing",
        "placing": "placing",
        "pending": "pending",
        "failed": "not placed",
        "place_unknown": "unconfirmed",
        "capture_unknown": "unconfirmed",
        "release_unknown": "unconfirmed",
        "capture_refused": "parked",
        "release_refused": "parked",
    }.get(st, st or "legacy")


# ══════════════════════════════════════════════════════════════════════════
# Auctions — read
# ══════════════════════════════════════════════════════════════════════════

def _lot_payload(listing: dict, viewer: str, with_bids: bool = True) -> dict:
    db = _core_db()
    lid = int(listing["id"])
    bids = db.get_land_bids(lid, limit=50) if with_bids else []
    ladder = [{
        "seq": int(b["id"]),
        "by": _who(b.get("bidder_id"), viewer, "bidder"),
        "mine": str(b.get("bidder_id")) == str(viewer),
        "amount": int(round(float(b.get("amount") or 0))),
        "when": b.get("ts"),
        "hold": _bid_hold_state(b),
    } for b in bids]
    ladder.sort(key=lambda r: -r["amount"])
    return {
        "id": lid,
        "title": listing.get("title") or f"Lot #{lid}",
        "category": listing.get("category") or ("Land" if listing.get("kind") == "land" else "Item lot"),
        "seller": _who(listing.get("seller_id"), viewer, "seller"),
        "seller_is_you": str(listing.get("seller_id")) == str(viewer),
        "mode": listing.get("mode"),
        "status": listing.get("status"),
        "reserve": int(round(float(listing.get("reserve") or 0))),
        "current_bid": int(round(float(listing.get("current_bid") or 0))),
        "leader": _who(listing.get("current_bidder"), viewer, "bidder") if listing.get("current_bidder") else None,
        "you_lead": str(listing.get("current_bidder") or "") == str(viewer),
        "min_next": _min_next_bid(listing),
        "increment_pct": float(listing.get("min_increment_pct") or 5.0),
        "commission_pct": float(listing.get("commission_pct") or 0),
        "ends_at": listing.get("ends_at"),
        "secs_left": _secs_left(listing.get("ends_at")),
        "coords": listing.get("coords"),
        "description": listing.get("description"),
        "bids": ladder,
        # Summed from the rows that are actually `held`, not read off the top of the
        # ladder: what the strip shows must be what the ledger has reserved, and the two
        # can differ for a moment while an outbid release is still in flight.
        "your_hold": sum(b["amount"] for b in ladder if b["mine"] and b["hold"] == "held"),
    }


def _subject(value) -> str:
    """The subject half of a form-key purpose, normalised so mint and verify agree.

    `41`, `"41"` and `" 41 "` are one lot. Anything that is not an id at all becomes
    `"?"`, which no minted key can match — a body with no `lot_id` is refused as a bad
    key rather than silently verified against some other lot's purpose.
    """
    try:
        return str(int(str(value).strip()))
    except (TypeError, ValueError):
        return "?"


def _bid_purpose(body: dict) -> str:
    """The form-key purpose a bid must carry: the LOT it was previewed against.

    WEB_ATTACK finding 7: with a bare `"bid"` purpose, a key minted while the player
    read lot 3's figures committed 9,000c against lot 4. The subject rides in the
    purpose so the key that confirms is the key that was previewed.
    """
    return f"bid:{_subject(body.get('lot_id'))}"


def _stake_purpose(body: dict) -> str:
    """Market AND outcome. The previewed figures — pool, odds, indicative payout — are
    per outcome, so the outcome is part of what was confirmed, not a free parameter."""
    return (f"stake:{_subject(body.get('market_id'))}"
            f":{_subject(body.get('outcome_id'))}")


async def h_lots(request):
    """The auction board. Live lots, their ladders and their hold states.

    One form key PER LOT, minted against that lot's id. Not one key for the board: a
    single key would be spendable on any lot the browser cared to name, which is the
    whole of finding 7. Keys are signed, not stored, so forty lots cost forty HMACs and
    no rows.
    """
    sess, refusal = shell.require_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])
    try:
        db = _core_db()
        _ensure_escrow_columns()
        listings = db.get_active_land_listings("auction")
    except Exception as e:
        log.exception("[estates] lot board read failed: %s", e)
        return shell.json_err("lots_unavailable",
                              "The auction exchange is not answering.", 503)
    lots = [_lot_payload(x, uid) for x in listings]
    lots.sort(key=lambda l: (l["secs_left"] is None, l["secs_left"] or 0))
    for lot in lots:
        lot["key"] = shell.mint_form_key(uid, f"bid:{lot['id']}")
    return shell.json_ok(lots=lots)


# ══════════════════════════════════════════════════════════════════════════
# Auctions — bid. The transactional path.
# ══════════════════════════════════════════════════════════════════════════

def _validate_bid(listing: Optional[dict], uid: str, amount: int) -> int:
    """Every reason a bid is refused, checked before anything is written.

    Returns the minimum next bid so the caller can put it in the refusal — a bid that
    is told "too low" without being told what would not be is a bid the player has to
    guess at.
    """
    if listing is None:
        raise shell.NoEffect("no_lot", "That lot does not exist.", 404)
    if str(listing.get("status")) != "active":
        raise shell.NoEffect("lot_closed", f"That lot is {listing.get('status')}, not open.", 409)
    if str(listing.get("mode")) != "auction":
        raise shell.NoEffect("not_auction", "That listing is fixed-price, not an auction.", 409)
    left = _secs_left(listing.get("ends_at"))
    if left is not None and left <= 0:
        raise shell.NoEffect("lot_ended", "That lot has ended.", 409)
    if str(listing.get("seller_id")) == uid:
        raise shell.NoEffect("own_lot", "You cannot bid on your own lot.", 409)
    if str(listing.get("current_bidder") or "") == uid:
        raise shell.NoEffect("already_leading", "You are already the high bidder.", 409)
    min_next = _min_next_bid(listing)
    if amount < min_next:
        raise shell.NoEffect("too_low",
                             f"The next bid on this lot is {min_next:,}c or more.", 409)
    return min_next


async def h_bid_preview(request):
    """Figures for the confirm screen. Reads only; nothing is reserved here.

    The important sentence on this screen is the one about escrow, and it is computed
    rather than asserted: the preview shows available before and after, so "held, not
    spent" is visible as arithmetic and not just as a claim in a footnote.
    """
    sess, refusal = shell.require_post_session(request)
    if refusal is not None:
        return refusal
    body = await shell.read_json(request)
    shell.note_body_identity(request, body, sess)
    uid = str(sess["user_id"])
    try:
        lid = int(body.get("lot_id"))
        amount = shell.coins(body.get("amount", 0))
    except (TypeError, ValueError) as e:
        return shell.json_err("bad_amount", str(e) or "Which lot, and how much?", 400)

    db = _core_db()
    listing = db.get_land_listing(lid)
    try:
        min_next = _validate_bid(listing, uid, amount)
        blocked = False
        note_extra = ""
    except shell.NoEffect as e:
        min_next = _min_next_bid(listing) if listing else 0
        blocked = True
        note_extra = str(e)

    # The wallet is a separate service and it can be down. A preview that 500s teaches
    # the player nothing and looks like the bid itself broke; `/api/wallet/strip`
    # already answers a named 503 here (`vt_web_shell._handle_strip`) and this screen
    # follows it. Nothing has been reserved at this point, so refusing costs nothing.
    try:
        bal = _ledger().get_balance(uid)
    except Exception as e:
        log.warning("[estates] bid preview wallet read failed for %s: %s", uid, e)
        return shell.json_err("wallet_unavailable",
                              "The wallet service is not answering, so we cannot show "
                              "you what this bid would reserve. Nothing has been "
                              "reserved. Try again in a moment.", 503)
    avail = int(bal["available"])
    if amount > avail and not blocked:
        blocked = True
        note_extra = (f"You have {avail:,}c available. "
                      f"{int(bal['held']):,}c is already held by other bids and stakes.")

    ends = (listing or {}).get("ends_at")
    return shell.json_ok(
        head="Preview — no coins move",
        rows=[
            ["Your bid", f"{amount:,}c", "num"],
            ["Minimum next bid", f"{min_next:,}c", "num"],
            ["Current high bid",
             f"{int(round(float((listing or {}).get('current_bid') or 0))):,}c", "num"],
            ["Lot closes", str(ends or "no closing time set"), ""],
            ["Hold expires", "close + 24h", ""],
        ],
        total=["Held if you bid", f"{amount:,}c", "color:var(--amber)"],
        effect=[
            ["Available now", f"{avail:,}c", ""],
            ["Available after", f"{avail - amount:,}c", ""],
            ["Balance after", f"{int(bal['balance']):,}c", "color:var(--green)"],
        ],
        blocked=blocked,
        confirm_label=f"Bid {amount:,}c",
        note=("<b>This is a hold, not a payment.</b> The coins stay in your wallet and "
              "stay yours — they are reserved so they cannot be spent twice, and they "
              "are released the moment somebody outbids you. Your balance does not "
              "change until the lot settles and you have won it."
              + (f"<br><br>{html.escape(note_extra)}" if note_extra else "")),
    )


def _refuse_legacy_debit_lot(db, lid: int, listing: dict) -> None:
    """Refuse a web bid on a lot whose top bid came from the old debit path.

    Two money models write `land_listings.current_bid/current_bidder`: this one, where
    a bid is a ledger HOLD, and `cogs/land_exchange._place_bid_core`, where a bid is a
    straight debit off `balances.coins`. Bidding across the seam moves coins the wrong
    way in both directions — outbidding a debit-path bidder never refunds him (coins
    destroyed), and a debit-path bidder outbidding a hold gets "refunded" a debit that
    never happened (coins minted). That is WEB_ATTACK finding 1.

    The tell is the `idem_key` column: every row this module writes mints one from the
    row's own id, and the cog's `add_land_bid` writes none. So a top bid with no
    `idem_key` is a legacy debit-path bid, and we refuse the lot by name rather than
    layering a hold on top of a debit.

    This guard stays correct after the cog is converted to holds: converted rows carry
    an `idem_key`, so the refusal simply stops firing. It is not a switch to remove
    later, and it costs one indexed read per bid.
    """
    if not str(listing.get("current_bidder") or ""):
        return
    try:
        with db.db() as conn:
            row = conn.execute(
                "SELECT id, idem_key, status FROM land_bids WHERE listing_id=? "
                "AND status IN ('held','placing','pending') "
                "ORDER BY amount DESC, id DESC LIMIT 1", (lid,)).fetchone()
    except Exception as e:
        log.exception("[estates] legacy-bid guard could not read lot %s: %s", lid, e)
        return
    if row is not None and row["idem_key"]:
        return
    # Either there is no escrowed bid row behind a lot that claims a leader, or the top
    # one carries no key. Both mean the leader's money is not in a hold we can release.
    raise shell.NoEffect(
        "legacy_bid_on_lot",
        "This lot's current high bid was placed through the older Discord bidding path, "
        "which takes coins instead of holding them. Bidding here would not refund that "
        "bidder. Bid with /bid in Discord for now, or ask staff to migrate the lot.",
        409)


async def _place_bid(sess, body, key) -> tuple:
    """Bid = hold. LAND_ESCROW_PLAN §2.2, in that order, with the row written first."""
    uid = str(sess["user_id"])
    try:
        lid = int(body.get("lot_id"))
    except (TypeError, ValueError):
        raise shell.NoEffect("bad_lot", "Which lot?")
    try:
        amount = shell.coins(body.get("amount", 0))
    except ValueError as e:
        raise shell.NoEffect("bad_amount", str(e))

    db = _core_db()
    L = _ledger()
    _ensure_escrow_columns()

    listing = db.get_land_listing(lid)
    _validate_bid(listing, uid, amount)
    _refuse_legacy_debit_lot(db, lid, listing)

    # ── 2. row first, keys minted from its own id, all in one transaction ──────
    with db.db() as conn:
        cur = conn.execute(
            "INSERT INTO land_bids (listing_id, bidder_id, amount, kind, status, attempts) "
            "VALUES (?,?,?,'bid','pending',0)", (lid, uid, amount))
        row_id = int(cur.lastrowid)
        idem_key = f"land:listing:{lid}:bid:{row_id}"
        conn.execute("UPDATE land_bids SET idem_key=?, capture_key=? WHERE id=?",
                     (idem_key, idem_key + ":capture", row_id))

    # ── 3. claim pending -> placing ───────────────────────────────────────────
    with db.db() as conn:
        claimed = conn.execute(
            "UPDATE land_bids SET status='placing', claimed_at=datetime('now'), "
            "attempts=attempts+1 WHERE id=? AND status='pending'", (row_id,))
        if claimed.rowcount != 1:
            raise shell.NoEffect("claim_lost", "That bid is already being placed.", 409)

    # ── 4. the hold. No balance pre-read decides this; the INSERT's WHERE does. ─
    ttl = _hold_ttl(listing.get("ends_at"), BID_HOLD_GRACE_S)
    try:
        hold = L.place_hold(SERVICE, uid, amount,
                            reason=f"realestate:bid:{lid}",
                            expires_in=ttl, key=idem_key)
    except Exception as e:
        code = getattr(e, "code", "") or type(e).__name__
        definite = code in ("insufficient", "frozen", "bad_amount", "bad_expiry",
                            "gambling_blocked", "escrow_shortfall")
        _mark_bid(row_id, "failed" if definite else "place_unknown", str(e))
        if definite:
            if code == "insufficient":
                raise shell.NoEffect("insufficient", _insufficient_msg(L, uid, "bid"), 409)
            raise shell.NoEffect(code, str(e), 409)
        # Unknown outcome: a hold MAY exist at core. The row keeps its key so that a
        # replay is safe when something finally runs one — today NOTHING DOES. The loop
        # that would is written, unscheduled, in `website/reconcile_loop.py`; until it
        # is wired into `cogs/loops.py` this row waits for staff, and the note below
        # says exactly that. Never say "nothing was taken" here — that sentence, said
        # on a timeout, is the N7 bug.
        log.warning("[estates] bid %s hold outcome unknown: %s", row_id, e)
        return 502, {"ok": False, "code": "hold_unconfirmed",
                     "big": "Not confirmed", "big_sub": f"lot #{lid}",
                     "rows": [["Your bid", f"{amount:,}c", "num"]],
                     "note": ("We could not confirm your bid with the wallet service. "
                              "Your coins may already be held against it. Nothing "
                              "clears this automatically today — do not bid again, or "
                              "you may end up with two holds. Contact staff: this bid "
                              "needs to be checked by hand.")}

    # ── 5. write the hold back, per row ───────────────────────────────────────
    hold_id = str(hold.get("hold_id"))
    with db.db() as conn:
        conn.execute("UPDATE land_bids SET status='held', hold_id=?, hold_expires_at=?, "
                     "last_error=NULL WHERE id=? AND status='placing'",
                     (hold_id, hold.get("expires_at"), row_id))

    # ── 6. claim the lead. Conditional, and BEFORE the previous hold is released. ─
    #
    # WEB_ATTACK finding 8: this used to be an unconditional
    # `update_land_listing(current_bid=..., current_bidder=...)`, so the last writer
    # led regardless of amount and the higher bidder's hold stayed open behind a lower
    # leading bid. It was not reproducible web-vs-web only because there is no `await`
    # between the read and the write — luck, not design, and it does not survive the
    # bot thread: `Restocker_web.start_webserver_thread` runs this server in its own
    # OS thread and event loop while `cogs/land_exchange` writes the same rows.
    #
    # So it is claim-first, like every other state change in this file: one atomic
    # UPDATE that only lands if we really are the high bid, and we act only if we won
    # the row.
    #
    # THERE IS NO READ HERE, deliberately. This block used to `SELECT current_bidder`
    # first and release whoever that returned, under a comment claiming the two ran in
    # one transaction. They did not: `Restocker_db.db()` yields a connection with
    # sqlite3's default `isolation_level=''`, so a bare SELECT runs in AUTOCOMMIT and
    # the implicit BEGIN only happens at the UPDATE — `conn.in_transaction` is False
    # after the SELECT and True after the UPDATE. WEB_VERIFY_R2's V1 drove a competing
    # bidder into exactly that window: the loser released the bidder the stale read
    # named and stranded the one it had actually displaced, which is finding 8's
    # original damage in miniature. A comment asserting an atomicity the code does not
    # have is worse than no comment.
    #
    # `BEGIN IMMEDIATE` would have locked the window shut. Removing the read closes it
    # instead: step 7 derives who to release from the `land_bids` rows this UPDATE
    # provably beat, which is a fact about rows we can still check afterwards rather
    # than a snapshot that can go stale between two statements.
    #
    # This runs BEFORE the release, which is a change of order: releasing first meant
    # a request that then lost the lead had already refunded the standing leader. The
    # lot is never unbacked either way — our own hold was written at step 5.
    with db.db() as conn:
        won = conn.execute(
            "UPDATE land_listings SET current_bid=?, current_bidder=?, "
            "updated_at=datetime('now') WHERE id=? AND status='active' "
            "AND (current_bid IS NULL OR current_bid < ?)",
            (amount, uid, lid, amount)).rowcount == 1

    if not won:
        # Somebody at or above us took the lead between our read and our write. Our
        # coins are held against a bid that leads nothing, so we release OUR OWN hold
        # rather than leaving it stranded — that stranded hold is the actual damage in
        # finding 8, not the pointer. The previous leader is untouched: they are either
        # still leading or were displaced by the bid that beat us, whose own request
        # releases them.
        self_released = _release_own(lid, row_id, hold_id)
        return 409, {
            "ok": False, "code": "outbid_in_flight",
            "big": "Outbid", "big_sub": f"lot #{lid}",
            "rows": [
                ["Your bid", f"{amount:,}c", "num"],
                ["Result", "another bid at or above yours landed first", ""],
                ["Your hold", "released" if self_released else "release pending", "amb"],
            ],
            "note": ("Another bidder reached this lot a moment before you did, at or "
                     "above your amount, so your bid does not lead."
                     + (" Your coins have been released and nothing was taken."
                        if self_released
                        else " We could not confirm the release of your coins — they "
                             "may still be reserved against this bid. Do not bid "
                             "again; contact staff so it can be checked by hand.")
                     + " Open the lot again to see the new price."),
        }

    # ── 7. release the holds this bid beat — AFTER step 6, never before ────────
    released = _release_beaten(lid, row_id, amount)

    rows = [
        ["Bid", f"{amount:,}c", "num"],
        ["State", "held — not spent", "amb"],
        ["Hold expires", str(hold.get("expires_at") or "—"), ""],
    ]
    after, after_note = _after_rows(L, uid, "bid")
    rows.extend(after)
    rows.append(["Previous high bid released", "yes" if released else "none to release", ""])
    return 200, {
        "ok": True,
        "big": f"{amount:,}c",
        "big_sub": f"held against lot #{lid}",
        "hold_id": hold_id,
        "rows": rows,
        "note": ("Your coins are reserved, not spent — your balance is unchanged. They "
                 "are released automatically if you are outbid, and captured only if you "
                 "win the lot." + after_note),
    }


def _after_rows(L, uid: str, what: str) -> tuple:
    """The two "after" balance rows on a receipt — or nothing, if the wallet is down.

    WEB_ATTACK finding 5. By the time this runs the money has already moved: the hold
    is placed, the row is `held`, the lot is led. This read is DECORATION, and a
    decoration that raises turns a completed operation into a 500 — which leaves the
    form key claimed, so the retry is refused, and the player is never told the bid
    landed. A display read must never invalidate a completed money operation.

    So it degrades: the receipt loses two lines and gains a sentence saying why, and
    the operation still reports success, because it succeeded.
    """
    try:
        bal = L.get_balance(uid)
    except Exception as e:
        log.warning("[estates] receipt wallet read failed for %s after %s: %s", uid, what, e)
        return [], (f" (Your {what} went through. We could not re-read your wallet just "
                    f"now, so the two balance lines are missing from this receipt — "
                    f"reload the page to see them.)")
    return [["Your available after", f"{int(bal['available']):,}c", "num"],
            ["Your balance after", f"{int(bal['balance']):,}c", "num"]], ""


def _insufficient_msg(L, uid: str, what: str) -> str:
    """The "you do not have the coins" sentence — with the figures, or without them.

    WEB_ATTACK finding 5, second half (WEB_VERIFY_R2 NEW-4). This runs inside a
    DEFINITE REFUSAL: the ledger has already told us `insufficient`, so we know that
    nothing moved and the operation is over. The balance read that follows exists only
    to put numbers in the sentence. If it raises, an unguarded read turns a refusal that
    provably moved nothing into a 500 — which leaves the form key claimed and files the
    outcome as unknown, so the player is locked out of retrying an action that never
    happened. A display read must never change the outcome of a settled operation.

    So it degrades exactly like `_after_rows`: the refusal keeps its name and its code,
    and loses only the two figures.
    """
    try:
        bal = L.get_balance(uid)
    except Exception as e:
        log.warning("[estates] insufficient-message wallet read failed for %s on %s: %s",
                    uid, what, e)
        return (f"You do not have enough available coins for that {what}. We could not "
                f"re-read your wallet just now, so we cannot show you the figures — "
                f"reload the page to see what is available. Nothing was taken.")
    return (f"You have {int(bal['available']):,}c available. "
            f"{int(bal['held']):,}c of your {int(bal['balance']):,}c is already held by "
            f"other bids and stakes, and held coins cannot back a new {what}.")


def _release_own(lid: int, row_id: int, hold_id: str) -> bool:
    """Give back the coins we just reserved, when our own bid turned out not to lead.

    Same key as every other end-of-life for this hold —
    `land:listing:<lid>:bid:<row>:release` — because it is the same money event: "hold
    N ends without capture". A distinct key per reason is a double-release wearing a
    bookkeeping costume (LAND_ESCROW_PLAN §1.3).
    """
    try:
        with _core_db().db() as conn:
            claimed = conn.execute(
                "UPDATE land_bids SET status='releasing' WHERE id=? AND status='held'",
                (row_id,)).rowcount
        if claimed != 1:
            return False
        _ledger().release_hold(SERVICE, str(hold_id),
                               key=f"land:listing:{lid}:bid:{row_id}:release",
                               reason="outbid_in_flight")
        with _core_db().db() as conn:
            conn.execute("UPDATE land_bids SET status='released', "
                         "settled_at=datetime('now') WHERE id=?", (row_id,))
        return True
    except Exception as e:
        log.warning("[estates] could not release our own losing bid %s on lot %s "
                    "(needs reconcile_loop, which is NOT scheduled): %s", row_id, lid, e)
        try:
            with _core_db().db() as conn:
                conn.execute("UPDATE land_bids SET status='release_unknown', last_error=? "
                             "WHERE id=? AND status='releasing'", (str(e)[:500], row_id))
        except Exception:
            pass
        return False


def _mark_bid(row_id: int, status: str, error: str) -> None:
    try:
        with _core_db().db() as conn:
            conn.execute("UPDATE land_bids SET status=?, last_error=? WHERE id=?",
                         (status, error[:500], row_id))
    except Exception:
        log.exception("[estates] could not mark bid %s as %s", row_id, status)


def _release_beaten(lid: int, new_row_id: int, amount: int) -> bool:
    """Release every hold on this lot that our bid provably beat. Rows, not a snapshot.

    This replaces `_release_previous(lid, <the bidder we read a moment ago>, ...)`. The
    old shape asked "who was leading?" and released them; the read and the lead-claiming
    UPDATE were NOT in one transaction (see step 6), so on a lost race it released a
    bidder who had already been displaced and stranded the one it really displaced.

    The set is derived from the rows the UPDATE beat instead, which cannot go stale:
    every `land_bids` row on this lot that is `held` (its hold is confirmed open),
    is not ours, and is for `amount` or less. Such a row can never take the lead back:
    step 6's UPDATE only lands on `current_bid < ?`, and the lot now stands at `amount`.
    So its hold is dead money and belongs to nobody's bid — release it.

    The `amount <= ?` bound is load-bearing in the other direction too. A bid HIGHER
    than ours may be sitting at `held` between its own steps 5 and 6, about to take the
    lead legitimately; releasing that one would leave the lot led by a bid with no hold
    behind it. It is left alone, and its own step 7 releases us.

    Keys are unchanged: `land:listing:<lid>:bid:<row>:release`, one per bid ROW and the
    same key whether the release is an outbid, a cancel or an expiry sweep, because they
    are one money event — "hold N ends without capture". `release_hold` fingerprints on
    the hold id, so a cancel racing the sweeper replays instead of conflicting; distinct
    keys here would be a double-release bug wearing a bookkeeping costume
    (LAND_ESCROW_PLAN §1.3). `reconcile_loop.reconcile_land_bid_holds` mints the same
    string from `idem_key`.
    """
    try:
        with _core_db().db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, hold_id FROM land_bids WHERE listing_id=? AND status='held' "
                "AND id<>? AND amount<=? AND hold_id IS NOT NULL ORDER BY id",
                (lid, new_row_id, amount)).fetchall()]
    except Exception as e:
        log.warning("[estates] could not list beaten bids on lot %s: %s", lid, e)
        return False
    return sum(1 for r in rows if _release_bid_row(lid, int(r["id"]), str(r["hold_id"]))) > 0


def _release_bid_row(lid: int, prev_id: int, hold_id: str) -> bool:
    """Claim one beaten bid row and release its hold. Claim-first: `held` -> `releasing`.

    The claim is what stops two winners racing to release the same row from both calling
    the ledger; the key would make the second call a replay anyway, but the row status is
    the thing another thread reads, so it is the thing that must be won atomically.
    """
    try:
        with _core_db().db() as conn:
            claimed = conn.execute(
                "UPDATE land_bids SET status='releasing', claimed_at=datetime('now') "
                "WHERE id=? AND status='held'", (prev_id,)).rowcount == 1
        if not claimed:
            return False
        _ledger().release_hold(SERVICE, hold_id,
                               key=f"land:listing:{lid}:bid:{prev_id}:release",
                               reason="outbid")
        with _core_db().db() as conn:
            conn.execute("UPDATE land_bids SET status='released', settled_at=datetime('now') "
                         "WHERE id=?", (prev_id,))
        return True
    except Exception as e:
        # The new bid is already held and the lot is correctly led. An unreleased
        # previous hold over-reserves ONE bidder until the hold expires at core, or
        # until `reconcile_loop.reconcile_land_bid_holds()` runs — which requires that
        # loop to be scheduled, and it is not yet. Annoying, recoverable, and strictly
        # better than the alternative ordering where the lot is briefly backed by
        # nothing at all. The row keeps its hold id and its key, so the outcome is
        # UNKNOWN and recoverable, never silently dropped.
        log.warning("[estates] outbid release of bid %s on lot %s failed (needs "
                    "reconcile_loop, which is NOT scheduled — row marked "
                    "release_unknown): %s", prev_id, lid, e)
        try:
            with _core_db().db() as conn:
                conn.execute("UPDATE land_bids SET status='release_unknown', last_error=? "
                             "WHERE id=? AND status='releasing'", (str(e)[:500], prev_id))
        except Exception:
            pass
        return False


async def h_bid(request):
    return await shell.money_post(request, "estates:bid", _bid_purpose, _place_bid)


# ══════════════════════════════════════════════════════════════════════════
# Parcels, leases and rent
# ══════════════════════════════════════════════════════════════════════════

async def h_parcels(request):
    """The parcel register: ownership, sitting leases and the rent they carry.

    Empty is EMPTY. If there are no parcels this returns an empty list and the page
    prints one muted line — it does not manufacture a demo row to fill the panel.
    """
    sess, refusal = shell.require_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])
    edb = _edb()
    if edb is None:
        return shell.json_err("estates_db_unavailable",
                             "The parcel register is not deployed on this server.", 503)
    try:
        parcels = edb.list_parcels(limit=400)
        period = edb.rent_period()
        due = edb.due_rent_charges(period)
    except Exception as e:
        log.exception("[estates] parcel read failed: %s", e)
        return shell.json_err("parcels_unavailable", "The parcel register is not answering.", 503)

    due_by_parcel = {}
    for c in due:
        due_by_parcel.setdefault(int(c["parcel_id"]), []).append({
            "period": c["period"], "amount": int(c["amount"]),
            "tenant": _who(c["tenant_id"], uid, "tenant"), "status": c["status"],
            "key": c["idem_key"],
        })
    rows = []
    for p in parcels:
        # WHOSE RENT IS THIS? Ownership and tenancy are public — they are what a
        # register is for, and a player deciding whether to make an offer needs to know
        # a parcel is taken. What a sitting tenant OWES is not: it is a private
        # obligation between two players, and it was being attached to every row for
        # every session, arrears status and ledger key included (WEB_ATTACK finding 9).
        # The key is deterministic and reconstructible so this was never a credential,
        # but "not a credential" is not a reason to publish somebody's debts.
        mine = (str(p.get("owner_id") or "") == uid or str(p.get("tenant_id") or "") == uid)
        rows.append({
            "id": int(p["id"]), "slug": p["slug"], "name": p["name"],
            "region": p.get("region"), "status": p["status"],
            "owner": _who(p.get("owner_id"), uid, "owner") if p.get("owner_id") else "nobody",
            "yours": str(p.get("owner_id") or "") == uid,
            "tenant": _who(p.get("tenant_id"), uid, "tenant") if p.get("tenant_id") else None,
            "you_lease": str(p.get("tenant_id") or "") == uid,
            "rent": int(p.get("rent_coins") or 0),
            "rent_period_days": int(p.get("rent_period_days") or 30),
            "lease_ends": p.get("lease_ends_at"),
            "last_rent_period": p.get("last_rent_period"),
            "charges": (due_by_parcel.get(int(p["id"]), []) if mine else []),
        })
    return shell.json_ok(parcels=rows, period=period)


# ══════════════════════════════════════════════════════════════════════════
# Prediction markets — pari-mutuel, and labelled as such everywhere
# ══════════════════════════════════════════════════════════════════════════

def _market_payload(edb, m: dict, uid: str) -> dict:
    pools = edb.market_pools(int(m["id"]))
    total = int(pools["total_pool"])
    mine = {}
    for s in edb.user_stakes(int(m["id"]), uid):
        if str(s["status"]) in ("held", "capturing", "captured"):
            mine[int(s["outcome_id"])] = mine.get(int(s["outcome_id"]), 0) + int(s["amount"])
    outcomes = []
    for o in pools["outcomes"]:
        whole, hundredths = int(o["odds_whole"]), int(o["odds_hundredths"])
        outcomes.append({
            "outcome_id": int(o["outcome_id"]), "label": o["label"],
            "pool": int(o["pool"]), "stakes": int(o["stakes"]),
            "share_pct": (int(o["pool"]) * 100 // total) if total else 0,
            # (0,0) from indicative_odds means "no price yet" and must render as a dash,
            # not as 0.00x — an empty side has no odds, it does not have odds of zero.
            "odds": (None if (whole == 0 and hundredths == 0)
                     else f"{whole}.{hundredths:02d}"),
            "mine": int(mine.get(int(o["outcome_id"]), 0)),
        })
    return {
        "id": int(m["id"]), "title": m["title"], "description": m.get("description"),
        "category": m.get("category"), "status": m["status"],
        "closes_at": m.get("closes_at"), "secs_left": _secs_left(m.get("closes_at")),
        "min_stake": int(m.get("min_stake") or 1),
        "max_stake": (int(m["max_stake"]) if m.get("max_stake") is not None else None),
        "rake": edb.format_bps(int(m["rake_bps"])),
        "total_pool": total,
        "outcomes": outcomes,
        "your_total": sum(mine.values()),
        # Every consumer of these numbers must say so. LEDGER_API_v2 §10: a punter who
        # believes he locked a price at stake time will correctly call it a bug.
        "indicative": str(m["status"]) in ("open", "closing"),
        "unknown_stakes": int(pools.get("unknown_stakes") or 0),
        "unknown_amount": int(pools.get("unknown_amount") or 0),
    }


async def h_markets(request):
    """Open pari-mutuel prediction markets with their live pools.

    Pari-mutuel is why this survives the casino being scrapped: punters stake against
    each other, the house takes a rake off the pool and never takes a side. There is no
    house-banked game anywhere on this site and none may be added here.
    """
    sess, refusal = shell.require_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])
    edb = _edb()
    if edb is None:
        return shell.json_err("estates_db_unavailable",
                             "Prediction markets are not deployed on this server.", 503)
    try:
        markets = [m for m in edb.list_markets(limit=100)
                   if str(m["status"]) in ("open", "closing", "closed", "resolved", "paid")]
        payload = [_market_payload(edb, m, uid) for m in markets]
    except Exception as e:
        log.exception("[estates] market read failed: %s", e)
        return shell.json_err("markets_unavailable", "Prediction markets are not answering.", 503)
    payload.sort(key=lambda m: (m["status"] != "open", m["secs_left"] or 1 << 30))
    # One key per OUTCOME, minted against the market and outcome it belongs to. The
    # figures a punter reads — this side's pool, its odds, the indicative payout — are
    # per outcome, so that is the subject the key has to bind (WEB_ATTACK finding 7).
    for mk in payload:
        for out in mk["outcomes"]:
            out["key"] = shell.mint_form_key(
                uid, f"stake:{mk['id']}:{out['outcome_id']}")
    return shell.json_ok(markets=payload)


async def h_stake_preview(request):
    """Figures for a stake. The odds shown here are indicative and say so."""
    sess, refusal = shell.require_post_session(request)
    if refusal is not None:
        return refusal
    body = await shell.read_json(request)
    shell.note_body_identity(request, body, sess)
    uid = str(sess["user_id"])
    edb = _edb()
    if edb is None:
        return shell.json_err("estates_db_unavailable", "Prediction markets are not deployed.", 503)
    try:
        mid = int(body.get("market_id"))
        oid = int(body.get("outcome_id"))
        amount = shell.coins(body.get("amount", 0))
    except (TypeError, ValueError) as e:
        return shell.json_err("bad_amount", str(e) or "Which market, which outcome, how much?", 400)

    m = edb.get_market(mid)
    if m is None:
        return shell.json_err("no_market", "That market does not exist.", 404)
    pools = edb.market_pools(mid)
    out = next((o for o in pools["outcomes"] if int(o["outcome_id"]) == oid), None)
    if out is None:
        return shell.json_err("no_outcome", "That outcome is not on this market.", 404)

    # Same rule as the bid preview: a wallet outage is a named 503 on a read-only
    # screen, never a 500. Nothing is reserved here.
    try:
        bal = _ledger().get_balance(uid)
    except Exception as e:
        log.warning("[estates] stake preview wallet read failed for %s: %s", uid, e)
        return shell.json_err("wallet_unavailable",
                              "The wallet service is not answering, so we cannot show "
                              "you what this stake would reserve. Nothing has been "
                              "reserved. Try again in a moment.", 503)
    avail = int(bal["available"])
    total_after = int(pools["total_pool"]) + amount
    side_after = int(out["pool"]) + amount
    whole, hundredths = edb.indicative_odds(side_after, total_after, int(m["rake_bps"]))
    blocked = (amount > avail or amount < int(m.get("min_stake") or 1)
               or str(m["status"]) != "open"
               or (m.get("max_stake") is not None and amount > int(m["max_stake"])))
    payout = (side_after and (amount * (total_after - total_after * int(m["rake_bps"]) // 10000))
              // side_after) or 0
    return shell.json_ok(
        head=f"{m['title']} · pari-mutuel",
        rows=[
            ["Your stake", f"{amount:,}c", "num"],
            ["On", str(out["label"]), ""],
            ["This side's pool now", f"{int(out['pool']):,}c", "num"],
            ["Whole pool now", f"{int(pools['total_pool']):,}c", "num"],
            ["House rake", edb.format_bps(int(m["rake_bps"])), ""],
            ["Return per 100 staked, if this outcome wins",
             (f"{whole}.{hundredths:02d}×" if (whole or hundredths) else "no price yet"),
             "num"],
        ],
        total=["Indicative payout if it wins", f"{int(payout):,}c", "color:var(--green)"],
        effect=[["Available now", f"{avail:,}c", ""],
                ["Available after", f"{avail - amount:,}c", ""],
                ["Balance after", f"{int(bal['balance']):,}c", "color:var(--green)"]],
        blocked=blocked,
        confirm_label=f"Stake {amount:,}c",
        note=("<b>Your stake is HELD, not spent.</b> The coins stay in your wallet and "
              "stay yours until the market closes; only then are they captured into the "
              "pool. <br><br><b>These odds are indicative.</b> A pari-mutuel pool moves "
              "with every stake placed after yours, so your final payout is set by the "
              "pool at close, not by the number on this screen. You are betting against "
              "the other punters — the house takes "
              f"{edb.format_bps(int(m['rake_bps']))} of the pool and never takes a side."),
    )


async def _place_stake(sess, body, key) -> tuple:
    """Stake = hold. `create_stake` -> `claim_stake` -> `place_hold` -> `stake_held`.

    The ledger key is `estates:market:<id>:stake:<seq>`, minted by `create_stake` and
    written to the row BEFORE the money call, so a retry re-reads the same string and
    core replays its answer instead of reserving a second time.
    """
    uid = str(sess["user_id"])
    edb = _edb()
    if edb is None:
        raise shell.NoEffect("estates_db_unavailable", "Prediction markets are not deployed.", 503)
    try:
        mid = int(body.get("market_id"))
        oid = int(body.get("outcome_id"))
    except (TypeError, ValueError):
        raise shell.NoEffect("bad_market", "Which market and outcome?")
    try:
        amount = shell.coins(body.get("amount", 0))
    except ValueError as e:
        raise shell.NoEffect("bad_amount", str(e))

    m = edb.get_market(mid)
    if m is None:
        raise shell.NoEffect("no_market", "That market does not exist.", 404)

    try:
        stake = edb.create_stake(mid, oid, uid, amount)
    except Exception as e:
        # BadAmount / BadState: nothing was written, and "nothing was taken" is true.
        raise shell.NoEffect(type(e).__name__.lower(), str(e), 409)

    row = edb.claim_stake(int(stake["id"]))
    if row is None:
        raise shell.NoEffect("claim_lost", "That stake is already being placed.", 409)

    L = _ledger()
    ttl = _hold_ttl(m.get("closes_at"), WAGER_HOLD_GRACE_S)
    try:
        hold = L.place_hold(SERVICE, uid, amount,
                            reason=f"estates:market:{mid}:stake",
                            expires_in=ttl, key=row["idem_key"])
    except Exception as e:
        code = getattr(e, "code", "") or type(e).__name__
        definite = code in ("insufficient", "frozen", "bad_amount", "bad_expiry",
                            "gambling_blocked", "escrow_shortfall")
        edb.fail_stake(int(stake["id"]), str(e)[:200], outcome_known=definite)
        if definite:
            if code == "insufficient":
                raise shell.NoEffect("insufficient", _insufficient_msg(L, uid, "stake"), 409)
            raise shell.NoEffect(code, str(e), 409)
        log.warning("[estates] stake %s hold outcome unknown: %s", stake["id"], e)
        return 502, {"ok": False, "code": "hold_unconfirmed",
                     "big": "Not confirmed", "big_sub": f"market #{mid}",
                     "rows": [["Your stake", f"{amount:,}c", "num"]],
                     "note": ("We could not confirm your stake with the wallet service. "
                              "Your coins may already be held against it. Nothing "
                              "clears this automatically today — do not stake again, or "
                              "you may end up with two holds. Contact staff: this stake "
                              "needs to be checked by hand.")}

    edb.stake_held(int(stake["id"]), str(hold["hold_id"]), hold.get("expires_at"))
    # Decoration, after the money moved — see `_after_rows`. WEB_ATTACK finding 5.
    after, after_note = _after_rows(L, uid, "stake")
    pools = edb.market_pools(mid)
    out = next((o for o in pools["outcomes"] if int(o["outcome_id"]) == oid), {})
    return 200, {
        "ok": True,
        "big": f"{amount:,}c",
        "big_sub": f"held on {out.get('label') or 'your outcome'}",
        "hold_id": str(hold["hold_id"]),
        "rows": [
            ["Stake", f"{amount:,}c", "num"],
            ["State", "held — not spent", "amb"],
            ["Hold expires", str(hold.get("expires_at") or "—"), ""],
            ["This side's pool now", f"{int(out.get('pool') or 0):,}c", "num"],
        ] + after,
        "note": ("Your coins are reserved, not spent. They are captured into the pool "
                 "when the market closes, and refunded in full if it is voided. The "
                 "odds you saw are indicative — the pool moves until close." + after_note),
    }


async def h_stake(request):
    return await shell.money_post(request, "estates:stake", _stake_purpose, _place_stake)


# ══════════════════════════════════════════════════════════════════════════
# The page
# ══════════════════════════════════════════════════════════════════════════

# Three top-level sections, each its own page + subtabs. They share one JS bundle
# and the /api/estates/* endpoints; the only per-page difference is which subtab the
# page opens on (window.__ESTTAB__) and which subtabs its nav shows.
_SECTIONS_DEF = {
    "auctions": {
        "label": "Auctions", "order": 40,
        "h1": "Auctions",
        "sub": "Bid to win lots. Every bid is an escrow hold — coins reserved, never spent, until the lot settles.",
        "subtabs": [("auctions", "Open lots"), ("mybids", "My bids")],
    },
    "lands": {
        "label": "Lands", "order": 41,
        "h1": "Lands",
        "sub": "The parcel register — ownership, tenants and rent. Nothing here debits a coin.",
        "subtabs": [("parcels", "Parcel register"), ("myparcels", "My parcels")],
    },
    "predictions": {
        "label": "Predictions", "order": 42,
        "h1": "Prediction markets",
        "sub": "Pari-mutuel markets. A stake is an escrow hold until the market closes; the house takes a rake, never a side.",
        "subtabs": [("markets", "Open markets"), ("mystakes", "My stakes")],
    },
}


def _body(key: str) -> str:
    d = _SECTIONS_DEF[key]
    parts = []
    for i, (t, lbl) in enumerate(d["subtabs"]):
        cur = ' aria-current="true"' if i == 0 else ''
        parts.append(f'<button class="nav-tab" data-t="{t}"{cur} onclick="tab(\'{t}\')">{lbl}</button>')
    tabs = "".join(parts)
    return (
        f'<div class="page-head"><div><h1>{d["h1"]}</h1>'
        f'<div class="page-sub">{d["sub"]}</div></div></div>'
        f'<nav class="subtabs" id="subtabs" style="display:flex;gap:2px;'
        f'border-bottom:1px solid var(--border);margin-bottom:16px">{tabs}</nav>'
        f'<div id="estView"><div class="empty">Loading…</div></div>'
    )

_JS = r"""
/* Form keys are NOT held here any more. Each lot carries its own, each outcome
   carries its own, and they are read off the row being acted on — one key for the
   whole board is a key that commits any lot on it. */
let E = {lots:null, parcels:null, markets:null, period:''};
let TAB = (window.__ESTTAB__ || 'auctions');

function tab(t){
  TAB = t;
  document.querySelectorAll('#subtabs .nav-tab').forEach(b =>
    b.setAttribute('aria-current', b.dataset.t === t ? 'true' : 'false'));
  render();
}
function unavailable(j, what){
  return `<div class="bank-down"><div class="bd-h">${esc(what)} unavailable</div>
    <div class="bd-b">${esc(j.error || 'Not answering.')}<br>
    Nothing on this page is estimated while a source is down — the panel is absent, not empty.</div></div>`;
}

/* ---------- auctions ---------- */
function ladder(l){
  if(!l.bids.length) return '<div class="empty">No bids yet.</div>';
  return `<div class="tablewrap"><table>
    <thead><tr><th>Bidder</th><th class="right">Bid</th><th>Escrow</th><th class="right">When</th></tr></thead>
    <tbody>${l.bids.map(b=>`<tr${b.mine?' style="background:var(--panel2)"':''}>
      <td>${esc(b.by)}</td>
      <td class="num right">${cn(b.amount)}</td>
      <td><span class="tag ${b.hold==='held'?'warn':(b.hold==='captured'?'ok':'')}">${esc(b.hold)}</span></td>
      <td class="num right muted">${esc(rel(b.when) || '')}</td></tr>`).join('')}
    </tbody></table></div>`;
}
function lotCard(l){
  return `<div class="tile s6">
    <div class="between">
      <div><div class="eyebrow">Lot #${l.id} · ${esc(l.category)}</div>
        <div class="lot-t">${esc(l.title)}</div>
        <div class="hold-s">Seller ${esc(l.seller)}</div></div>
      <div class="right"><div class="lot-bid num">${cn(l.current_bid)}</div>
        <div class="hold-s">${l.leader ? 'leading: ' + esc(l.leader) : 'no bids · reserve ' + n(l.reserve) + 'c'}</div></div>
    </div>
    <div class="kgrid" style="margin-top:12px">
      <div class="kpi"><div class="lab">Closes</div><div class="kfig num">${esc(fmtLeft(l.secs_left))}</div></div>
      <div class="kpi"><div class="lab">Minimum next bid</div><div class="kfig num">${cn(l.min_next)}</div></div>
      <div class="kpi"><div class="lab">Your hold on this lot</div>
        <div class="kfig num amb">${l.your_hold ? cn(l.your_hold) : '—'}</div>
        <div class="sub">${l.your_hold ? 'reserved, not spent' : 'nothing reserved'}</div></div>
    </div>
    <div class="section-h">Bid ladder</div>
    ${ladder(l)}
    <div class="row" style="gap:8px;margin-top:12px">
      ${l.seller_is_you ? '<span class="muted">Your own lot.</span>'
        : (l.you_lead ? '<span class="muted">You are the high bidder — your coins are held until this lot settles.</span>'
          : `<button class="btn" onclick="flowBid(${l.id}, ${l.min_next})">Bid</button>`)}
    </div>
  </div>`;
}
async function loadLots(){
  const j = await get('/api/estates/lots');
  if(!j.ok) return unavailable(j, 'The auction exchange');
  E.lots = j.lots;
  if(!j.lots.length) return '<div class="empty">No lots open.</div>';
  return '<div class="bento">' + j.lots.map(lotCard).join('') + '</div>';
}
async function loadMyBids(){
  const j = await get('/api/estates/lots');
  if(!j.ok) return unavailable(j, 'The auction exchange');
  E.lots = j.lots;
  const mine = (j.lots || []).filter(l => l.your_hold);
  if(!mine.length) return '<div class="empty">You have no active bids. Bids you place appear here, held in escrow until the lot settles.</div>';
  return '<div class="bento">' + mine.map(lotCard).join('') + '</div>';
}

/* ---------- parcels ---------- */
function parcelTable(parcels, period){
  return `<div class="tile s12"><div class="tile-h">Parcel register · rent period ${esc(period)}</div>
    <div class="tablewrap"><table>
    <thead><tr><th>Parcel</th><th>Region</th><th>Status</th><th>Owner</th><th>Tenant</th>
      <th class="right">Rent</th><th>Lease ends</th><th>Rent due</th></tr></thead>
    <tbody>${parcels.map(p=>`<tr${p.yours?' style="background:var(--panel2)"':''}>
      <td>${esc(p.name)}<div class="tsub">${esc(p.slug)}</div></td>
      <td class="muted">${esc(p.region || '—')}</td>
      <td><span class="tag">${esc(p.status)}</span></td>
      <td>${esc(p.owner)}</td>
      <td>${p.tenant ? esc(p.tenant) : '<span class="muted">—</span>'}</td>
      <td class="num right">${p.rent ? cn(p.rent) + '<div class="tsub">per ' + p.rent_period_days + ' days</div>' : '—'}</td>
      <td>${p.lease_ends ? esc(fmtD(p.lease_ends)) : '<span class="muted">—</span>'}</td>
      <td>${(p.yours || p.you_lease)
            ? (p.charges.length ? p.charges.map(c=>`<span class="tag warn">${n(c.amount)}c ${esc(c.status)}</span>`).join(' ')
               : '<span class="muted">nothing due</span>')
            : '<span class="muted">private</span>'}</td>
    </tr>`).join('')}</tbody></table></div>
    <div class="holdnote">Rent due is shown for parcels you own or lease. What another
      player owes on his own lease is between him and his landlord, so this column reads
      <b>private</b> rather than a figure. On your own rows, every rent charge carries the
      key <code>estates:parcel:&lt;id&gt;:rent:&lt;period&gt;</code>. The period in the key
      is what stops a retried collection charging two months.</div></div>`;
}
async function loadParcels(){
  const j = await get('/api/estates/parcels');
  if(!j.ok) return unavailable(j, 'The parcel register');
  E.parcels = j.parcels; E.period = j.period;
  if(!j.parcels.length) return '<div class="empty">No parcels on the register.</div>';
  return parcelTable(j.parcels, j.period);
}
async function loadMyParcels(){
  const j = await get('/api/estates/parcels');
  if(!j.ok) return unavailable(j, 'The parcel register');
  const mine = (j.parcels || []).filter(p => p.yours || p.you_lease);
  if(!mine.length) return '<div class="empty">You own or lease no parcels.</div>';
  return parcelTable(mine, j.period);
}

/* ---------- prediction markets ---------- */
function outcomeRow(m, o){
  const pct = o.share_pct;
  return `<div class="outcome ${o.mine?'mine':''}">
    <div><div class="o-name">${esc(o.label)}</div>
      <div class="o-barwrap"><div class="bar" style="width:${pct}%"></div></div>
      <div class="hold-s">${cn(o.pool)} · ${n(o.stakes)} stake${o.stakes===1?'':'s'} · ${pct}% of pool${
        o.mine ? ' · <b style="color:var(--amber)">you hold ' + n(o.mine) + 'c here</b>' : ''}</div></div>
    <div class="o-right">
      <span class="o-odds num">${o.odds ? '×' + o.odds : '—'}</span>
      <div class="hold-s">${m.indicative ? 'indicative' : 'at the closed pool'}</div>
      ${m.status === 'open' ? `<button class="btn" style="margin-top:6px"
        onclick="flowStake(${m.id}, ${o.outcome_id})">Stake</button>` : ''}
    </div></div>`;
}
function marketCard(m){
  return `<div class="tile s6">
    <div class="eyebrow">Market #${m.id} · pari-mutuel · rake ${esc(m.rake)}</div>
    <div class="lot-t">${esc(m.title)}</div>
    ${m.description ? `<div class="hold-s">${esc(m.description)}</div>` : ''}
    <div class="kgrid" style="margin-top:12px">
      <div class="kpi"><div class="lab">Pool</div><div class="kfig num">${cn(m.total_pool)}</div></div>
      <div class="kpi"><div class="lab">${m.status === 'open' ? 'Closes' : 'State'}</div>
        <div class="kfig num">${m.status === 'open' ? esc(fmtLeft(m.secs_left)) : esc(m.status)}</div></div>
      <div class="kpi"><div class="lab">Your stake, held</div>
        <div class="kfig num amb">${m.your_total ? cn(m.your_total) : '—'}</div>
        <div class="sub">${m.your_total ? 'reserved, not spent' : 'nothing staked'}</div></div>
    </div>
    <div class="section-h">Outcomes <span class="muted" style="letter-spacing:0;text-transform:none;font-weight:400">${
      m.indicative ? 'odds indicative' : 'final'}</span></div>
    ${m.outcomes.map(o => outcomeRow(m, o)).join('')}
    ${m.unknown_stakes ? `<div class="holdnote"><b>${n(m.unknown_stakes)}</b> stake(s) worth
      ${n(m.unknown_amount)}c were never confirmed by the wallet service and are not counted
      in the pool above. Nothing settles them automatically — staff check them by hand.</div>` : ''}
    <div class="foot">${m.indicative
      ? 'Odds are <b>indicative</b> — a pari-mutuel pool moves with every stake placed after yours, so your payout is set by the pool at close. You bet against the other punters; the house takes ' + esc(m.rake) + ' of the pool and never takes a side.'
      : 'Settled at the closed pool — final, not indicative.'}</div>
  </div>`;
}
async function loadMarkets(){
  const j = await get('/api/estates/markets');
  if(!j.ok) return unavailable(j, 'Prediction markets');
  E.markets = j.markets;
  if(!j.markets.length) return '<div class="empty">No prediction markets.</div>';
  return '<div class="bento">' + j.markets.map(marketCard).join('') + '</div>';
}
async function loadMyStakes(){
  const j = await get('/api/estates/markets');
  if(!j.ok) return unavailable(j, 'Prediction markets');
  E.markets = j.markets;
  const mine = (j.markets || []).filter(m => m.your_total);
  if(!mine.length) return '<div class="empty">You have no stakes. Stakes you place appear here, held until the market closes.</div>';
  return '<div class="bento">' + mine.map(marketCard).join('') + '</div>';
}

async function render(){
  const v = document.getElementById('estView');
  v.innerHTML = '<div class="empty">Loading…</div>';
  v.innerHTML = TAB === 'auctions'  ? await loadLots()
              : TAB === 'mybids'    ? await loadMyBids()
              : TAB === 'parcels'   ? await loadParcels()
              : TAB === 'myparcels' ? await loadMyParcels()
              : TAB === 'markets'   ? await loadMarkets()
              : TAB === 'mystakes'  ? await loadMyStakes()
              : await loadLots();
}

/* ---------- flows ---------- */
function flowBid(lotId, minNext){
  const l = (E.lots || []).find(x => x.id === lotId) || {};
  openFlow({
    title:'Bid on lot #' + lotId, sub:l.title || '', doneTitle:'Bid placed',
    amountStep:true, amountLabel:'Minimum next bid', amountCap:minNext,
    amount:String(minNext),
    chips:[['Minimum ' + n(minNext) + 'c', minNext],
           ['+10% ' + n(Math.ceil(minNext * 1.1)) + 'c', Math.ceil(minNext * 1.1)]],
    check:v => v < minNext ? 'The next bid on this lot is ' + n(minNext) + 'c or more.' : '',
    amountRows:() => `<div class="holdnote">This places a <b>hold</b>, not a payment.
      Your balance does not change; the coins are reserved so they cannot be spent twice,
      and they are released the moment somebody outbids you.</div>`,
    preview: async v => await post('/api/estates/bid/preview', {lot_id:lotId, amount:v}),
    commit: async v => {
      /* l.key was minted for THIS lot. A key from another lot's card is refused
         server-side with `form_key_subject_mismatch`, not quietly spent here. */
      const r = await post('/api/estates/bid', {lot_id:lotId, amount:v, idempotency_key:l.key});
      if(r.replayed) r.note = 'This was a repeat of a bid already placed — one hold exists, '
        + 'not two. ' + (r.note || '');
      return r;
    }});
}
function stakeKey(marketId, outcomeId){
  const m = (E.markets || []).find(x => x.id === marketId) || {};
  const o = (m.outcomes || []).find(x => x.outcome_id === outcomeId) || {};
  return o.key || '';
}
function flowStake(marketId, outcomeId){
  openFlow({
    title:'Stake on market #' + marketId, sub:'pari-mutuel · odds indicative',
    doneTitle:'Stake held', amountStep:true, amountLabel:'Your stake', amountCap:0,
    chips:[['500c',500],['2,000c',2000],['5,000c',5000]],
    check:v => v <= 0 ? 'Enter an amount above zero.' : '',
    amountRows:() => `<div class="holdnote">A stake is a <b>hold</b>. Your coins stay in
      your wallet until the market closes; only then are they captured into the pool.</div>`,
    preview: async v => await post('/api/estates/stake/preview',
      {market_id:marketId, outcome_id:outcomeId, amount:v}),
    commit: async v => {
      const r = await post('/api/estates/stake',
        {market_id:marketId, outcome_id:outcomeId, amount:v, idempotency_key:stakeKey(marketId, outcomeId)});
      if(r.replayed) r.note = 'This was a repeat of a stake already placed — one hold '
        + 'exists, not two. ' + (r.note || '');
      return r;
    }});
}

loadMe().then(() => { renderStrip(); render(); });
"""


def _register_with_hub(key: str, label: str, path: str, order: int) -> None:
    """Tell `hub_web` this section exists, so the hub nav lists it.

    A mechanism built is not a mechanism wired: `hub_web` makes its nav a data
    registry precisely so a section can plug in without editing that file, and a
    section that never calls it is a page nothing links to.

    Called from `register_*_routes` rather than at import time, so it does not matter
    whether the hub is imported before or after this module — `_SECTIONS` is a
    module-level list and `register_section` is idempotent on `key`. Guarded, because
    the hub is not required for this section to work: without it the pages still serve,
    they are just not in the hub's nav.
    """
    try:
        import hub_web
    except Exception:
        return
    try:
        hub_web.register_section(key, label, path, order=order)
    except Exception as e:  # pragma: no cover
        log.warning("[%s] could not register with the hub nav: %s", key, e)


async def _section_page(request, key: str):
    """One of the three estates sites. Logged out gets 401 and the sign-in card —
    every figure here is the player's own money. `key` is both the hub nav key (so
    the right top tab lights) and the section whose subtabs/initial view we render."""
    _sess, refusal = shell.require_page_session(request)
    if refusal is not None:
        return refusal
    d = _SECTIONS_DEF[key]
    init = d["subtabs"][0][0]
    js = "window.__ESTTAB__ = %r;\n" % init + _JS
    return shell.page(d["h1"] + " · V Tech", key, _body(key), js)


async def h_auctions(request):
    return await _section_page(request, "auctions")


async def h_lands(request):
    return await _section_page(request, "lands")


async def h_predictions(request):
    return await _section_page(request, "predictions")


async def h_page(request):
    """`/estates` is the old combined route — kept as a redirect to Auctions so any
    stale link or bookmark still lands somewhere real."""
    raise web.HTTPFound("/auctions")


def register_estates_routes(app) -> None:
    """Attach the estates section. Mirrors `bank_api.register_bank_routes`."""
    if web is None:  # pragma: no cover
        log.warning("[estates] aiohttp unavailable — estates not registered.")
        return
    shell.register_shell_routes(app)
    _register_with_hub("auctions", "Auctions", "/auctions", order=40)
    _register_with_hub("lands", "Lands", "/lands", order=41)
    _register_with_hub("predictions", "Predictions", "/predictions", order=42)
    app.router.add_get("/auctions", h_auctions)
    app.router.add_get("/lands", h_lands)
    app.router.add_get("/predictions", h_predictions)
    app.router.add_get("/estates", h_page)
    app.router.add_get("/api/estates/lots", h_lots)
    app.router.add_get("/api/estates/parcels", h_parcels)
    app.router.add_get("/api/estates/markets", h_markets)
    app.router.add_post("/api/estates/bid/preview", h_bid_preview)
    app.router.add_post("/api/estates/bid", h_bid)
    app.router.add_post("/api/estates/stake/preview", h_stake_preview)
    app.router.add_post("/api/estates/stake", h_stake)
    log.info("[estates] v%s registered (auctions · parcels · pari-mutuel markets)",
             ESTATES_VERSION)
