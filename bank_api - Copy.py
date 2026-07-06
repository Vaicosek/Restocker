"""
bank_api.py - token-protected HTTP API (X-Bank-Token) that lets the Banking bot
operate on Restocker's coin wallet and stock exchange. Restocker stays the source
of truth; the API returns 503 if BANK_API_TOKEN is unset. Mounted via
bank_api.register_bank_routes(app) in Restocker_web.start_webserver().
"""

from __future__ import annotations

import os
import hmac
import time
import logging

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp is a hard dep of the web server
    web = None

log = logging.getLogger("bank_api")

BANK_API_VERSION = "1.1"


BANK_API_TOKEN = os.getenv("BANK_API_TOKEN", "").strip()


_TABLES_READY = False


def _ensure_tables() -> None:
    """Create the bank bookkeeping tables once. Cheap to call repeatedly."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    with _db().db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_idempotency (
                key TEXT PRIMARY KEY,
                ts  REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_audit (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                action  TEXT NOT NULL,
                user_id TEXT,
                amount  REAL,
                reason  TEXT,
                extra   TEXT,
                ts      REAL NOT NULL
            )
        """)
    _TABLES_READY = True


def _claim_key(key: str) -> bool:
    """Atomically claim an idempotency key. Returns True if it's NEW (caller
    should proceed) or False if it was already used (caller should treat the
    request as a duplicate). Atomic via the PRIMARY KEY constraint, so even
    concurrent retries can't both win."""
    if not key:
        return True
    _ensure_tables()
    with _db().db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bank_idempotency (key, ts) VALUES (?, ?)",
            (key, time.time()),
        )
        return cur.rowcount == 1


def _release_key(key: str) -> None:
    """Undo a claim when the operation was rejected (e.g. insufficient funds),
    so the caller can legitimately retry later."""
    if not key:
        return
    try:
        with _db().db() as conn:
            conn.execute("DELETE FROM bank_idempotency WHERE key=?", (key,))
    except Exception:
        pass


def _audit(action: str, user_id: str | None, amount: float | None,
           reason: str | None, extra: str = "") -> None:
    """Append a tamper-evident-ish record of every money move for forensics."""
    try:
        _ensure_tables()
        with _db().db() as conn:
            conn.execute(
                "INSERT INTO bank_audit (action, user_id, amount, reason, extra, ts) "
                "VALUES (?,?,?,?,?,?)",
                (action, str(user_id) if user_id is not None else None,
                 amount, reason, extra, time.time()),
            )
    except Exception as e:
        log.warning("[bank_api] audit write failed: %s", e)



def _err(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": message}, status=status)


def _authed(request) -> bool:
    if not BANK_API_TOKEN:
        return False
    supplied = (request.headers.get("X-Bank-Token") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, BANK_API_TOKEN)


def require_token(handler):
    """Decorator: reject requests without a valid bank token."""
    async def wrapper(request):
        if not BANK_API_TOKEN:
            return _err("Bank API disabled (no BANK_API_TOKEN set on server).", 503)
        if not _authed(request):
            return _err("Unauthorized.", 401)
        try:
            return await handler(request)
        except Exception as e:
            log.exception("[bank_api] handler error: %s", e)
            return _err("Internal error.", 500)
    wrapper.__name__ = getattr(handler, "__name__", "wrapper")
    return wrapper



def _db():
    import Restocker_db as db
    return db


def _main():
    import Restocker_main as m
    return m


def _balance_payload(user_id: str) -> dict:
    b = _db().get_balance(str(user_id))
    return {
        "user_id": str(user_id),
        "coins": int(b.get("coins") or 0),
        "principal": float(b.get("principal") or 0),
        "lp": float(b.get("lp") or 0),
    }





async def h_health(request):
    """PUBLIC health probe — no token required.

    Safe to expose: it reveals only that the bank API is mounted and whether it's
    enabled (a token is configured), never the token itself or any user data. This
    is what the scheduled monitor hits every few hours.
    """
    return web.json_response({
        "ok": True,
        "service": "restocker-bank-api",
        "version": BANK_API_VERSION,
        "enabled": bool(BANK_API_TOKEN),
        "ts": time.time(),
    })


@require_token
async def h_ping(request):
    """Authenticated probe — confirms the caller's token is valid."""
    return web.json_response({
        "ok": True, "service": "restocker-bank-api",
        "version": BANK_API_VERSION, "ts": time.time(),
    })


@require_token
async def h_balance(request):
    uid = (request.query.get("user_id") or "").strip()
    if not uid:
        return _err("Missing user_id.")
    return web.json_response({"ok": True, **_balance_payload(uid)})


@require_token
async def h_adjust(request):
    """Credit (amount>0) or debit (amount<0) a user's coin wallet.

    Body: {user_id, amount, reason?, idempotency_key?, count_principal?}
    Debits never overdraw: if the wallet has fewer coins than requested the call
    is rejected (ok:false, error:"insufficient") rather than partially applied.
    """
    body = await request.json()
    uid = str(body.get("user_id") or "").strip()
    try:
        amount = int(round(float(body.get("amount", 0))))
    except (TypeError, ValueError):
        return _err("amount must be a number.")
    if not uid:
        return _err("Missing user_id.")
    if amount == 0:
        return _err("amount must be non-zero.")

    key = str(body.get("idempotency_key") or "").strip()
    if not _claim_key(key):
        return web.json_response({"ok": True, "deduped": True, **_balance_payload(uid)})

    count_principal = bool(body.get("count_principal", True))
    reason = str(body.get("reason") or "")
    m = _main()

    if amount > 0:
        coins, principal = await m.run_on_bot_loop(m.add_coins, int(uid), amount, counts_as_principal=count_principal)
    else:
        need = -amount
        cur = _db().get_balance(uid)
        if int(cur.get("coins") or 0) < need:
            _release_key(key)
            return _err("insufficient", 409)
        coins, principal = await m.run_on_bot_loop(m.deduct_coins, int(uid), need, reduce_principal=count_principal)

    _audit("adjust", uid, amount, reason, extra=f"key={key}")
    return web.json_response({
        "ok": True, "user_id": uid, "applied": amount,
        "coins": int(coins), "principal": float(principal),
    })


@require_token
async def h_transfer(request):
    """Atomically move coins between two wallets.

    Body: {from_user, to_user, amount, reason?, idempotency_key?}
    """
    body = await request.json()
    src = str(body.get("from_user") or "").strip()
    dst = str(body.get("to_user") or "").strip()
    try:
        amount = int(round(float(body.get("amount", 0))))
    except (TypeError, ValueError):
        return _err("amount must be a number.")
    if not src or not dst:
        return _err("Missing from_user/to_user.")
    if src == dst:
        return _err("Cannot transfer to the same account.")
    if amount <= 0:
        return _err("amount must be positive.")

    key = str(body.get("idempotency_key") or "").strip()
    if not _claim_key(key):
        return web.json_response({"ok": True, "deduped": True,
                                  "from": _balance_payload(src), "to": _balance_payload(dst)})

    if int(_db().get_balance(src).get("coins") or 0) < amount:
        _release_key(key)
        return _err("insufficient", 409)

    m = _main()
    await m.run_on_bot_loop(m.deduct_coins, int(src), amount, reduce_principal=True)
    try:
        await m.run_on_bot_loop(m.add_coins, int(dst), amount, counts_as_principal=True)
    except Exception as e:
        await m.run_on_bot_loop(m.add_coins, int(src), amount, counts_as_principal=True)
        _audit("transfer_failed", src, amount, str(body.get("reason") or ""), extra=f"to={dst}")
        log.exception("[bank_api] transfer credit failed, refunded sender: %s", e)
        return _err("Transfer failed; sender refunded.", 500)

    _audit("transfer", src, amount, str(body.get("reason") or ""), extra=f"to={dst} key={key}")
    return web.json_response({
        "ok": True, "amount": amount,
        "from": _balance_payload(src), "to": _balance_payload(dst),
    })


@require_token
async def h_stocks(request):
    """List all public markets with their current quote (for the bank's /invest list)."""
    db = _db()
    out = []
    try:
        markets = db.get_markets() if hasattr(db, "get_markets") else []
    except Exception:
        markets = []
    seen = set()
    for mk in (markets or []):
        mid = mk.get("id") or mk.get("market_id") if isinstance(mk, dict) else None
        if not mid:
            continue
        listing = db.get_market_shares(mid)
        if not listing or not listing.get("active"):
            continue
        seen.add(mid)
        out.append({
            "market_id": mid,
            "name": (mk.get("name") if isinstance(mk, dict) else None) or mid,
            "price": float(listing.get("share_price") or 0),
            "shares_outstanding": float(listing.get("shares_outstanding") or 0),
            "pe": float(listing.get("pe_multiplier") or 0),
        })
    return web.json_response({"ok": True, "markets": out})


@require_token
async def h_portfolio(request):
    """A user's stock holdings with live valuation. Body/query: user_id."""
    uid = (request.query.get("user_id") or "").strip()
    if not uid:
        return _err("Missing user_id.")
    db = _db()
    holdings = []
    for h in db.get_portfolio(uid):
        mid = h.get("market_id")
        listing = db.get_market_shares(mid) or {}
        price = float(listing.get("share_price") or 0)
        shares = float(h.get("shares") or 0)
        holdings.append({
            "market_id": mid,
            "shares": shares,
            "price": price,
            "value": shares * price,
            "cost_basis": float(h.get("cost_basis") or 0),
        })
    return web.json_response({"ok": True, "user_id": uid, "holdings": holdings})


@require_token
async def h_stock_buy(request):
    """Buy shares for a user, paying from their Restocker wallet.

    Body: {user_id, market_id, shares, name?, idempotency_key?}
    Returns {ok, code, message, fill_price, total, new_price, ...balance}. `code`
    is machine-readable: ok | not_public | bad_shares | no_shares_available |
    insufficient_funds | error | deduped.
    """
    body = await request.json()
    uid = str(body.get("user_id") or "").strip()
    mid = str(body.get("market_id") or "").strip()
    try:
        shares = int(body.get("shares", 0))
    except (TypeError, ValueError):
        return _err("shares must be an integer.")
    name = body.get("name")
    if not uid or not mid:
        return _err("Missing user_id/market_id.")
    if shares <= 0:
        return _err("shares must be positive.")
    key = str(body.get("idempotency_key") or "").strip()
    if not _claim_key(key):
        return web.json_response({"ok": True, "deduped": True, "code": "deduped", **_balance_payload(uid)})
    _m = _main()
    r = await _m.run_on_bot_loop(_m.exec_stock_trade, "buy", int(uid), mid, shares, name)
    if r.get("ok"):
        _audit("stock_buy", uid, shares, mid, extra=f"key={key}")
    else:
        _release_key(key)
    return web.json_response({
        "ok": bool(r.get("ok")), "code": r.get("code"), "message": r.get("msg"),
        "fill_price": r.get("fill"), "total": r.get("total"), "new_price": r.get("new_price"),
        **_balance_payload(uid),
    })


@require_token
async def h_stock_sell(request):
    """Sell shares for a user, crediting their Restocker wallet.

    Body: {user_id, market_id, shares, name?, idempotency_key?}
    Returns {ok, code, message, fill_price, total, new_price, ...balance}. `code`
    is machine-readable: ok | not_listed | bad_shares | insufficient_shares |
    error | deduped.
    """
    body = await request.json()
    uid = str(body.get("user_id") or "").strip()
    mid = str(body.get("market_id") or "").strip()
    try:
        shares = int(body.get("shares", 0))
    except (TypeError, ValueError):
        return _err("shares must be an integer.")
    name = body.get("name")
    if not uid or not mid:
        return _err("Missing user_id/market_id.")
    if shares <= 0:
        return _err("shares must be positive.")
    key = str(body.get("idempotency_key") or "").strip()
    if not _claim_key(key):
        return web.json_response({"ok": True, "deduped": True, "code": "deduped", **_balance_payload(uid)})
    _m = _main()
    r = await _m.run_on_bot_loop(_m.exec_stock_trade, "sell", int(uid), mid, shares, name)
    if r.get("ok"):
        _audit("stock_sell", uid, shares, mid, extra=f"key={key}")
    else:
        _release_key(key)
    return web.json_response({
        "ok": bool(r.get("ok")), "code": r.get("code"), "message": r.get("msg"),
        "fill_price": r.get("fill"), "total": r.get("total"), "new_price": r.get("new_price"),
        **_balance_payload(uid),
    })



def register_bank_routes(app) -> None:
    """Attach the bank API routes to an existing aiohttp Application. Routes are
    served under both /api/bank/* (legacy) and /api/v1/bank/* (versioned) so the
    new bank bot can target a stable, versioned prefix while old callers keep
    working."""
    if web is None:
        log.warning("[bank_api] aiohttp unavailable — bank API not registered.")
        return
    routes = [
        ("get",  "/health",     h_health),
        ("get",  "/ping",       h_ping),
        ("get",  "/balance",    h_balance),
        ("get",  "/stocks",     h_stocks),
        ("get",  "/portfolio",  h_portfolio),
        ("post", "/adjust",     h_adjust),
        ("post", "/transfer",   h_transfer),
        ("post", "/stock/buy",  h_stock_buy),
        ("post", "/stock/sell", h_stock_sell),
    ]
    for prefix in ("/api/bank", "/api/v1/bank"):
        for method, path, handler in routes:
            if method == "get":
                app.router.add_get(prefix + path, handler)
            else:
                app.router.add_post(prefix + path, handler)
    state = "ENABLED" if BANK_API_TOKEN else "DISABLED (no BANK_API_TOKEN)"
    log.info("[bank_api] routes registered (/api/bank + /api/v1/bank) — %s", state)
    print(f"🏦  Bank API v{BANK_API_VERSION} {state}: /api/bank/* and /api/v1/bank/* "
          f"(health /ping /balance /adjust /transfer /stocks /portfolio /stock/buy /stock/sell)")
