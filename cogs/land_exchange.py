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

Escrow model: a bidder's coins are ACTUALLY deducted (core.deduct_coins) the moment
their bid is accepted, and refunded (core.add_coins) the moment they're outbid or the
listing is cancelled/expires unsold. The bidder's own balance row IS the hold — there
is no separate escrow ledger to reconcile.

Commands live under `/realestate` rather than `/land` — `/land` is already a Group
owned by cogs.lands.LandsCog (treasury/feed ingestion) and discord.py cannot share
one app_commands.Group across two Cogs safely: a duplicate top-level Group name
raises CommandAlreadyRegistered, and even routing a second cog's subcommands into an
imported Group instance leaves them bound to the WRONG cog instance at runtime
(verified — the callback's `self` resolves to whichever cog registered the Group
first, not the cog that defined the subcommand). `/realestate` keeps this cog fully
self-contained — no edits to lands.py or main.py beyond the one load_extension line.
"""
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
add_coins = core.add_coins
deduct_coins = core.deduct_coins
log = core.log
bot = core.bot

import cogs.valuation as _valuation  # value_plot() — the AI reserve-price helper

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
    # ── Loyalty (feeds the existing V Tech loyalty table; the moat the competitors lack) ──
    loyalty_flat=10.0,           # flat V Tech points to BOTH buyer & seller per completed sale
    loyalty_rate=0.0001,         # + this many points per coin of sale price (8.5M -> ~850 pts)
    loyalty_min_commission=1.0,  # loyalty discount never drops commission below this %
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


def _min_next_bid(listing: dict) -> float:
    """The smallest amount a new bid must meet to be accepted."""
    cur = listing.get("current_bid")
    if cur is None:
        return float(listing["reserve"])
    cur = float(cur)
    pct = float(listing.get("min_increment_pct") or DEF["min_increment_pct"])
    step = max(cur * pct / 100.0, DEF["min_increment_floor"])
    return round(cur + step)


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


def _listing_embed(listing: dict, bids: Optional[list] = None) -> discord.Embed:
    status = listing["status"]
    color = {"active": 0x2ECC71, "sold": 0xF1C40F,
             "expired": 0x95A5A6, "cancelled": 0xE74C3C}.get(status, 0x3498DB)
    is_land = (listing.get("kind") or "item") == "land"
    icon = "🏡" if is_land else "📦"
    title = f"{icon} {_listing_title(listing)} · #{listing['id']}"
    embed = discord.Embed(title=title, description=(listing.get("description") or "")[:2000], color=color)
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


# ── Headless core (NO Discord I/O) — the single code path for both slash commands and
#    the /api/network/land/* endpoints the satellite calls. Money moves here; the callers
#    only handle presentation (a slash reply, or the satellite's board + the home embed
#    refresh). Keeping the escrow/settlement math in ONE place is the whole point — a
#    forked copy in the network layer is exactly the bug we don't want. ─────────────────
def _listing_for_network(l: dict) -> dict:
    """Compact, JSON-safe summary of a listing for the satellite board."""
    price = l.get("current_bid") or l.get("reserve") or l.get("buy_now") or 0
    photos = _photos_of(l)
    return {
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
    return [_listing_for_network(r) for r in rows[:max(1, int(limit))]]


def _finalize_sale_core(listing_id: int, buyer_id, price: float) -> dict:
    """Headless settlement of an already-COLLECTED sale price: refund any pre-empted
    standing bidder, pay the seller net of commission, credit the house, mark sold,
    and tie the plot into the company backing config (65% rule) if a market was set.
    The caller must already have collected `price` from the buyer (a fresh buy deducts
    it; a won auction already holds it from the winning bid). Returns a result dict."""
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    if not listing:
        return {"ok": False, "error": "Listing not found."}
    if listing["status"] != "active":
        return {"ok": False, "error": "That listing is no longer active."}
    if listing.get("current_bidder") and str(listing["current_bidder"]) != str(buyer_id):
        add_coins(int(listing["current_bidder"]), int(round(listing.get("current_bid") or 0)),
                  reason=f"realestate:preempted_refund:{listing_id}")
    seller_id = listing["seller_id"]
    # Loyalty discount: a loyal seller's commission is automatically reduced by their tier.
    base_comm_pct = float(listing["commission_pct"])
    seller_loy = {}
    try:
        seller_loy = _db.get_loyalty(str(seller_id)) or {}
    except Exception:
        seller_loy = {}
    disc_pct = _loyalty_discount_pct(_db, seller_loy.get("total_earned", 0))
    min_comm = _gd(_db, "loyalty_min_commission", DEF["loyalty_min_commission"])
    eff_comm_pct = max(min_comm, base_comm_pct - disc_pct) if disc_pct else base_comm_pct
    commission = int(round(float(price) * eff_comm_pct / 100.0))
    net = int(round(price)) - commission
    add_coins(int(seller_id), net, reason=f"realestate:sale:{listing_id}")
    if commission > 0:
        core._credit_platform_balance(commission, market_id=listing.get("market_id") or "",
                                      note=f"realestate:commission:{listing_id}")
    _db.update_land_listing(listing_id, status="sold", sold_price=price, sold_to=str(buyer_id),
                            closed_at=core.utcnow_iso())
    if listing.get("market_id"):
        # Pillar 5 — the plot immediately backs the company: gather_and_value() already
        # reads this exact config key at the land haircut (65% rule). No new plumbing.
        _db.set_config(f"valuate:land_claim:{listing['market_id']}", str(float(price)))
    # Award V Tech loyalty points to BOTH sides of a real sale (can't be farmed — coins
    # actually moved). Feeds the existing loyalty table, so /loyalty stats/redemption see it.
    pts = _loyalty_award_points(_db, price)
    try:
        _db.add_loyalty_points(str(seller_id), pts)
        if str(buyer_id) != str(seller_id):
            _db.add_loyalty_points(str(buyer_id), pts)
    except Exception as e:
        log.warning("[realestate] loyalty award failed for #%s: %s", listing_id, e)
    return {"ok": True, "net": net, "commission": commission, "price": float(price),
            "seller_id": seller_id, "market_id": listing.get("market_id"),
            "commission_pct": eff_comm_pct, "loyalty_discount_pct": disc_pct,
            "loyalty_points": pts}


def _place_bid_core(listing_id: int, bidder_id, amount=None) -> dict:
    """Headless: validate + escrow a bid on an auction listing. Deducts the bidder's
    coins (the hold), refunds the previous top bidder, records the bid, and applies the
    anti-snipe extension. Returns a result dict (no Discord I/O)."""
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    if not listing or listing["status"] != "active":
        return {"ok": False, "error": "That listing isn't active."}
    if listing["mode"] != "auction":
        return {"ok": False, "error": "That's a fixed-price listing — buy it instead."}
    if listing.get("ends_at") and _epoch(listing["ends_at"]) <= datetime.now(timezone.utc).timestamp():
        return {"ok": False, "error": "That auction has already ended."}
    if str(bidder_id) == str(listing["seller_id"]):
        return {"ok": False, "error": "You can't bid on your own listing."}
    if str(listing.get("current_bidder")) == str(bidder_id):
        return {"ok": False, "error": "You already hold the top bid — raise it with a higher amount."}
    min_bid = _min_next_bid(listing)
    amt = float(amount) if amount else min_bid
    if amt < min_bid:
        return {"ok": False, "error": f"Minimum bid is {_fmt(min_bid)} coins."}
    bal = int(_db.get_balance(str(bidder_id)).get("coins") or 0)
    if bal < amt:
        return {"ok": False, "error": (f"Bid is {_fmt(amt)} coins but you have {bal:,}. "
                                       f"You need coins in the V Tech economy to bid.")}
    prev_bidder, prev_amount = listing.get("current_bidder"), listing.get("current_bid")
    deduct_coins(int(bidder_id), int(round(amt)), reason=f"realestate:bid:{listing_id}")
    if prev_bidder:
        add_coins(int(prev_bidder), int(round(prev_amount)), reason=f"realestate:outbid_refund:{listing_id}")
    _db.add_land_bid(listing_id, str(bidder_id), amt)
    updates = dict(current_bid=amt, current_bidder=str(bidder_id))
    anti_snipe_extended = False
    if listing.get("ends_at"):
        remaining_min = (_epoch(listing["ends_at"]) - datetime.now(timezone.utc).timestamp()) / 60.0
        anti_snipe = float(listing.get("anti_snipe_minutes") or DEF["anti_snipe_minutes"])
        if remaining_min < anti_snipe:
            updates["ends_at"] = _sql_now_plus(minutes=anti_snipe)
            anti_snipe_extended = True
    _db.update_land_listing(listing_id, **updates)
    return {"ok": True, "listing_id": listing_id, "amount": amt,
            "prev_bidder": prev_bidder, "prev_amount": prev_amount,
            "anti_snipe_extended": anti_snipe_extended,
            "message": f"Bid placed: {_fmt(amt)} coins on listing #{listing_id}."}


def _instant_buy_core(listing_id: int, buyer_id) -> dict:
    """Headless: collect the instant-buy price from the buyer and settle the sale."""
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    if not listing or listing["status"] != "active":
        return {"ok": False, "error": "That listing isn't active."}
    price = listing.get("buy_now")
    if not price:
        return {"ok": False, "error": "No instant-buy price on this listing — place a bid instead."}
    # Once bidding has met/passed the Buy-Now, instant-buy would let someone take it BELOW
    # the standing high bid and short the seller — force them to out-bid instead.
    if listing.get("current_bid") and float(listing["current_bid"]) >= float(price):
        return {"ok": False, "error": "Bidding has reached the Buy-Now price — place a higher bid instead."}
    if str(buyer_id) == str(listing["seller_id"]):
        return {"ok": False, "error": "You can't buy your own listing."}
    bal = int(_db.get_balance(str(buyer_id)).get("coins") or 0)
    if bal < price:
        return {"ok": False, "error": f"Price is {_fmt(price)} coins but you have {bal:,}."}
    deduct_coins(int(buyer_id), int(round(price)), reason=f"realestate:buy:{listing_id}")
    res = _finalize_sale_core(listing_id, buyer_id, price)
    if res.get("ok"):
        res["message"] = f"Bought listing #{listing_id} for {_fmt(price)} coins."
    else:
        # settlement failed after collecting — refund so coins can't be swallowed
        add_coins(int(buyer_id), int(round(price)), reason=f"realestate:buy_refund:{listing_id}")
    return res


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
    ends_at = _sql_now_plus(days=dur)

    listing_id = _db.create_land_listing(
        seller_id=str(seller_id), kind=kind, title=str(title).strip()[:120], category=cat,
        chunks=(ch or 0), market_id=(backs_company or None),
        land=(str(title).strip()[:120] if is_land else None),
        description=(str(details).strip()[:1500] if details else None), mode="auction",
        reserve=round(starting_price, 2), buy_now=(round(bn, 2) if bn else None),
        min_increment_pct=incr_pct, commission_pct=commission_pct, listing_fee=0,
        ends_at=ends_at, anti_snipe_minutes=int(anti_snipe), status="active")
    listing = _db.get_land_listing(listing_id)
    return {"ok": True, "listing": _listing_for_network(listing), "kind": kind, "ai_note": ai_note}


def set_listing_photos(listing_id: int, photo_urls: list) -> None:
    """Store the satellite-hosted photo URLs for a listing (called after /sell uploads)."""
    import Restocker_db as _db
    urls = [u for u in (photo_urls or []) if str(u).lower().startswith(("http://", "https://"))][:4]
    if urls:
        _db.update_land_listing(listing_id, photos=json.dumps(urls),
                                image_url=urls[0])


def cancel_listing_core(listing_id: int, requester_id, is_mgr: bool = False) -> dict:
    """Seller (or manager) cancels a listing — only if no bid has been placed."""
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    if not listing or listing["status"] != "active":
        return {"ok": False, "error": "That listing isn't active."}
    if str(requester_id) != str(listing["seller_id"]) and not is_mgr:
        return {"ok": False, "error": "Only the seller (or a manager) can cancel this."}
    if listing.get("current_bid"):
        return {"ok": False, "error": "A bid is already held on it — a manager must /close to unwind."}
    _db.update_land_listing(listing_id, status="cancelled", closed_at=core.utcnow_iso())
    return {"ok": True, "listing_id": listing_id}


def close_listing_core(listing_id: int, refund_bidder: bool = False) -> dict:
    """Manager force-settle/unwind (money only; the caller handles any Discord effects).
    refund_bidder cancels + refunds the standing bid; otherwise the top bid wins, or the
    listing expires if there are none."""
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    if not listing or listing["status"] != "active":
        return {"ok": False, "error": "That listing isn't active."}
    if refund_bidder:
        if listing.get("current_bidder"):
            add_coins(int(listing["current_bidder"]), int(round(listing["current_bid"] or 0)),
                      reason=f"realestate:manager_refund:{listing_id}")
        _db.update_land_listing(listing_id, status="cancelled", closed_at=core.utcnow_iso())
        return {"ok": True, "outcome": "cancelled_refunded"}
    if listing.get("current_bid") and listing.get("current_bidder"):
        res = _finalize_sale_core(listing_id, listing["current_bidder"], float(listing["current_bid"]))
        res["outcome"] = "sold"
        res["sold_to_buyer"] = str(listing["current_bidder"])
        return res
    _db.update_land_listing(listing_id, status="expired", closed_at=core.utcnow_iso())
    return {"ok": True, "outcome": "expired"}


def get_exchange_config() -> dict:
    """Current Land Exchange config knobs (commission, fees, auction defaults)."""
    import Restocker_db as _db
    return {k: _gd(_db, k, DEF[k]) for k in DEF}


def set_exchange_config(**kwargs) -> dict:
    """Set any of the config knobs (values that are None are ignored). Returns the result."""
    import Restocker_db as _db
    for key in DEF:
        val = kwargs.get(key)
        if val is not None:
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
        reply = f"✅ Bid placed: `{_fmt(res['amount'])}` 🪙. You'll be refunded automatically if outbid."
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
    note = f"💰 New bid on **#{listing_id}**: `{_fmt(res['amount'])}` 🪙 from <@{actor_id}>"
    if res.get("prev_bidder"):
        note += f" (outbidding <@{res['prev_bidder']}>, refunded `{_fmt(res['prev_amount'])}` 🪙)"
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
    async def _finalize_sale(self, listing_id: int, buyer_id: int, price: float, *, note: str):
        """Settle a sale via the shared headless core (money + DB + company backing),
        then do the Discord side (refresh, winner DM, transfer room). Caller must already
        have collected `price` from the buyer."""
        res = _finalize_sale_core(listing_id, buyer_id, price)
        if not res.get("ok"):
            log.warning("[realestate] finalize #%s failed: %s", listing_id, res.get("error"))
            return res
        await self._post_sale(listing_id, buyer_id, price, note)
        return res

    async def _post_sale(self, listing_id: int, buyer_id, price: float, note: str):
        """Everything that happens once a listing is SOLD, regardless of path (auction
        end, instant buy, manager close): refresh the message, DM the winner, and open a
        private transfer room with the seller + winner to coordinate the in-game handover."""
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        await self._refresh_message(listing_id, extra=note)
        if listing:
            await self._dm_winner(listing, buyer_id, price)
            await self._open_deal_room(listing, buyer_id, price)

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

    async def _dm_outbid(self, prev_bidder, listing_id: int, new_amount):
        """DM the bidder who was just outbid — their coins were auto-refunded, here's the
        new top bid so they can decide to come back."""
        try:
            import Restocker_db as _db
            listing = _db.get_land_listing(listing_id) or {}
            user = self.bot.get_user(int(prev_bidder)) or await self.bot.fetch_user(int(prev_bidder))
            what = _listing_title(listing) if listing else f"#{listing_id}"
            body = (f"⚠️ You've been outbid on **{what}** (#{listing_id}) — the top bid is now "
                    f"`{_fmt(new_amount)}` 🪙. Your previous bid was refunded to your balance. "
                    f"Bid again on the listing to stay in the running.")
            await user.send(body[:1900])
        except Exception as e:
            log.warning("[realestate] outbid DM failed for #%s: %s", listing_id, e)

    async def _post_bid(self, listing_id: int, res: dict, note: str):
        """Shared after-a-bid effects: refresh the listing message and DM the person who
        was just outbid. Used by the slash command, the Bid button, and the network relay."""
        await self._refresh_message(listing_id, extra=note)
        if res.get("prev_bidder"):
            await self._dm_outbid(res["prev_bidder"], listing_id, res.get("amount"))

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
                allowed_mentions=discord.AllowedMentions(roles=True))
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
        if not listing or listing["status"] != "active":
            return
        if listing.get("current_bid") and listing.get("current_bidder"):
            await self._finalize_sale(
                listing_id, int(listing["current_bidder"]), float(listing["current_bid"]),
                note=(f"🔨 Auction **#{listing_id}** ended — sold to <@{listing['current_bidder']}> "
                      f"for `{_fmt(listing['current_bid'])}` 🪙."))
        else:
            _db.update_land_listing(listing_id, status="expired", closed_at=core.utcnow_iso())
            await self._refresh_message(listing_id, extra=f"⌛ Auction **#{listing_id}** ended with no bids.")

    @tasks.loop(minutes=1)
    async def auction_sweep_loop(self):
        try:
            import Restocker_db as _db
            for listing in _db.get_expired_active_listings():
                try:
                    await self._settle_expired(listing["id"])
                except Exception as e:
                    log.warning("[realestate] auto-settle failed for #%s: %s", listing["id"], e)
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
    @app_commands.autocomplete(backs_company=_market_autocomplete)
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
        ends_at = _sql_now_plus(days=duration_days or _gd(_db, "default_auction_days", DEF["default_auction_days"]))

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
        land="(Optional) tracked land name — ties this listing to /land bind data",
        market_id="(Optional) a company this plot will back (65% rule) once sold",
        coords="(Optional) plot coordinates — your choice whether to disclose",
        description="Short description of the plot / build",
        image="(Optional) image URL of the plot — shown on the listing (land sells on looks)",
        winner_message="(Optional) note DM'd to the winner on close (coords, 'come collect', etc.)",
        duration_days="(Auction) how many days it runs — default is set by /realestate config",
        min_increment_pct="(Auction) override the minimum bid raise, as a % of the current bid",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
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
            ends_at = _sql_now_plus(days=duration_days or _gd(_db, "default_auction_days", DEF["default_auction_days"]))

        commission_pct = _gd(_db, "commission_pct", DEF["commission_pct"])
        listing_fee = _gd(_db, "listing_fee", DEF["listing_fee"])
        incr_pct = min_increment_pct if min_increment_pct and min_increment_pct > 0 else _gd(
            _db, "min_increment_pct", DEF["min_increment_pct"])
        anti_snipe = _gd(_db, "anti_snipe_minutes", DEF["anti_snipe_minutes"])

        seller_id = interaction.user.id
        if listing_fee > 0:
            bal = int(_db.get_balance(str(seller_id)).get("coins") or 0)
            if bal < listing_fee:
                return await interaction.response.send_message(
                    f"❌ Listing fee is `{_fmt(listing_fee)}` 🪙 — you have `{bal:,}`.", ephemeral=True)
            deduct_coins(seller_id, int(round(listing_fee)), reason="realestate:listing_fee")

        img = (image or "").strip() or None
        if img and not img.lower().startswith(("http://", "https://")):
            return await interaction.response.send_message(
                "❌ `image` must be a full http(s) URL.", ephemeral=True)
        listing_id = _db.create_land_listing(
            seller_id=str(seller_id), kind="land", title=(land or "Land plot"),
            market_id=market_id, land=land, chunks=chunks,
            coords=coords, description=description, image_url=img,
            winner_message=((winner_message or "").strip() or None), mode=mode, quality=quality,
            reserve=round(reserve_final, 2), buy_now=(round(buy_now, 2) if buy_now else None),
            min_increment_pct=incr_pct, commission_pct=commission_pct, listing_fee=listing_fee,
            ends_at=ends_at, anti_snipe_minutes=int(anti_snipe), status="active",
        )
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
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        if not listing or listing["status"] != "active":
            return await interaction.response.send_message(f"❌ Listing `#{listing_id}` isn't active.", ephemeral=True)
        if str(interaction.user.id) != str(listing["seller_id"]) and not is_manager(interaction):
            return await interaction.response.send_message("⛔ Only the seller (or a manager) can cancel this.",
                                                            ephemeral=True)
        if listing.get("current_bid"):
            return await interaction.response.send_message(
                "❌ A bid has already been placed — a bidder's coins are held on it. "
                "Ask a manager to `/realestate close` if this needs to be unwound.", ephemeral=True)
        _db.update_land_listing(listing_id, status="cancelled", closed_at=core.utcnow_iso())
        await self._refresh_message(listing_id, extra=f"🚫 Listing **#{listing_id}** was cancelled by the seller.")
        await interaction.response.send_message(f"✅ Cancelled `#{listing_id}`.", ephemeral=True)

    # ── /realestate close (manager dispute-resolution / force-settle) ─────────────
    @realestate.command(name="close", description="(Managers) Force-settle or unwind a listing right now")
    @app_commands.describe(listing_id="Listing to close", refund_bidder="Cancel and refund the standing bid instead of selling")
    @app_commands.autocomplete(listing_id=_listing_autocomplete)
    async def close(self, interaction: discord.Interaction, listing_id: int, refund_bidder: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        listing = _db.get_land_listing(listing_id)
        if not listing or listing["status"] != "active":
            return await interaction.response.send_message(f"❌ Listing `#{listing_id}` isn't active.", ephemeral=True)

        if refund_bidder:
            if listing.get("current_bidder"):
                add_coins(int(listing["current_bidder"]), int(round(listing["current_bid"])),
                          reason=f"realestate:manager_refund:{listing_id}")
            _db.update_land_listing(listing_id, status="cancelled", closed_at=core.utcnow_iso())
            await self._refresh_message(listing_id, extra=f"🚫 Listing **#{listing_id}** was closed by a manager and any bid refunded.")
            return await interaction.response.send_message(f"✅ Closed and refunded `#{listing_id}`.", ephemeral=True)

        if listing.get("current_bid") and listing.get("current_bidder"):
            await self._finalize_sale(
                listing_id, int(listing["current_bidder"]), float(listing["current_bid"]),
                note=(f"🔨 Listing **#{listing_id}** closed by a manager — sold to "
                      f"<@{listing['current_bidder']}> for `{_fmt(listing['current_bid'])}` 🪙."))
            return await interaction.response.send_message(f"✅ Settled `#{listing_id}` as sold.", ephemeral=True)

        _db.update_land_listing(listing_id, status="expired", closed_at=core.utcnow_iso())
        await self._refresh_message(listing_id, extra=f"⌛ Listing **#{listing_id}** was closed by a manager with no bids.")
        await interaction.response.send_message(f"✅ Closed `#{listing_id}` — no bids to settle.", ephemeral=True)

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
    )
    async def config(self, interaction: discord.Interaction, commission_pct: Optional[float] = None,
                     listing_fee: Optional[float] = None, min_increment_pct: Optional[float] = None,
                     anti_snipe_minutes: Optional[float] = None, default_auction_days: Optional[float] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        for key, val in (("commission_pct", commission_pct), ("listing_fee", listing_fee),
                         ("min_increment_pct", min_increment_pct), ("anti_snipe_minutes", anti_snipe_minutes),
                         ("default_auction_days", default_auction_days)):
            if val is not None:
                _db.set_config(f"realestate:{key}", str(float(val)))
        lines = [f"**{k}** — `{_gd(_db, k, DEF[k])}`" for k in DEF]
        embed = discord.Embed(title="⚙️ Land Exchange configuration", description="\n".join(lines), color=0x22FF7A)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(LandExchangeCog(bot))
