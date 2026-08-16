"""
bank_api.py - token-protected HTTP API (X-Bank-Token) that lets the Banking bot
operate on Restocker's coin wallet and stock exchange. Restocker stays the source
of truth; the API returns 503 if BANK_API_TOKEN is unset. Mounted via
bank_api.register_bank_routes(app) in Restocker_web.start_webserver().
"""

from __future__ import annotations

import os
import hmac
import json
import time
import logging

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp is a hard dep of the web server
    web = None

log = logging.getLogger("bank_api")

BANK_API_VERSION = "1.2"

#: How long an unfinished claim may sit before a retry is told it is unresolved
#: rather than in flight. Matched to `ledger_v2.IDEMPOTENCY_STALE_SECONDS` and to
#: `action_log.STALE_CLAIM_SECONDS`: one number, one rule, everywhere.
IDEMPOTENCY_STALE_SECONDS = 900


BANK_API_TOKEN = os.getenv("BANK_API_TOKEN", "").strip()


#: Codes from `exec_stock_trade` that mean core POSITIVELY refused before it
#: moved anything. Only these release a claim. Everything else — `None`, `{}`,
#: an unreadable dict, the engine's unclassified `error`, its `deduped` (which
#: means an EARLIER attempt applied the trade), a dispatch that raised or timed
#: out — is an UNKNOWN outcome and keeps the claim. `run_on_bot_loop` gives up
#: after 20s over a synchronous core that cannot be cancelled, so "no answer
#: over a committed trade" is a real shape; reading a falsy return as a refusal
#: released the key and let the retry buy the shares again (LEDGER_API_v2.md §6).
DEFINITE_STOCK_REFUSALS = frozenset({
    "insufficient_funds", "no_shares_available", "insufficient_shares",
    "not_public", "not_listed", "bad_shares",
    # `no_liquidity` is definite BECAUSE of the order the sell engine now uses: the
    # treasury claim happens before `adjust_holding`, so a refusal here means the
    # holding was never touched and the claimed coins were put back. Nothing moved.
    "no_liquidity",
    # `slippage` is checked before the first coin or share moves, on both sides —
    # the price drifted past the caller's bound, or the total exceeded the figure
    # they confirmed. Nothing happened, so the key must be released and a re-quoted
    # retry allowed through. (Same docstring warning as above: a refusal code the
    # engine can return that is missing from this set locks its idempotency key.)
    "slippage",
    # `credit_refused` is now definite too, and only now. It is returned when the
    # sell path read the wallet BEFORE and AFTER a raising `add_coins` and found it
    # unchanged — shares and treasury restored, nothing moved. The case where those
    # reads themselves fail no longer lands here: it returns `trade_unknown`, which
    # is deliberately NOT in this set, so its key stays held for a human.
    "credit_refused",
})


_TABLES_READY = False


def _ensure_tables() -> None:
    """Create the bank bookkeeping tables once. Cheap to call repeatedly."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    with _db().db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_idempotency (
                key             TEXT PRIMARY KEY,
                ts              REAL NOT NULL,
                state           TEXT NOT NULL DEFAULT 'in_progress',
                applied_unknown INTEGER NOT NULL DEFAULT 1,
                status_code     INTEGER,
                response_json   TEXT,
                completed_at    REAL
            )
        """)
        # Live databases already carry the two-column version of this table.
        have = {r[1] for r in conn.execute("PRAGMA table_info(bank_idempotency)").fetchall()}
        for col, decl in (("state", "TEXT NOT NULL DEFAULT 'in_progress'"),
                          ("applied_unknown", "INTEGER NOT NULL DEFAULT 1"),
                          ("status_code", "INTEGER"),
                          ("response_json", "TEXT"),
                          ("completed_at", "REAL")):
            if col not in have:
                conn.execute(f"ALTER TABLE bank_idempotency ADD COLUMN {col} {decl}")
        # Rows written by the old code are claims with no recorded outcome. They
        # are exactly the ambiguous shape this change exists to stop creating, so
        # they are marked as such rather than silently treated as completed: a
        # retry of one of them is refused and named, not replayed as `deduped`.
        conn.execute("UPDATE bank_idempotency SET state='in_progress', applied_unknown=1 "
                     "WHERE state IS NULL")
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


def _claim_key(key: str) -> tuple[str, float, object]:
    """Claim `key`, or report what the previous attempt did with it.

    WHY THIS IS NO LONGER A BOOLEAN (product review §7)
    ---------------------------------------------------
    This used to `INSERT OR IGNORE` the key, commit, and only *then* move the
    money — so the row was a **claim**, not a result record, and a duplicate got
    `{"ok": True, "deduped": True}`, indistinguishable from success. That is a
    mint, and the bank bot now drives it deliberately:

        1. bank POSTs adjust(key='orgtx-42', amount=-1000)   # pay-in debit
        2. Restocker commits the key
        3. get_balance raises (WAL lock), or run_on_bot_loop raises during a
           restart -> 500, key NOT released
        4. bank: 500 is not 4xx -> _is_permanent False -> txn stays 'moving'
        5. org_sweeps re-POSTs 'orgtx-42' -> deduped:True, ok
        6. finish_org_pay_in credits the org 1,000 coins NEVER DEBITED

    The payout direction destroys coins instead: key claimed, `add_coins` never
    ran, resume reads `deduped`, `finish_org_pay_out` clears the hold — the org
    is debited and the payee is never paid. `_is_permanent` + `org_txns_to_resume`
    are the RIGHT design against a counterparty that records outcomes; this is
    that counterparty finally recording them.

    Returns one of:
      ("new",         claim_ts, None)          -> proceed; you own the claim
      ("replay",      0.0, (status, body))     -> return that stored response
      ("in_progress", age_seconds, None)       -> an earlier attempt is in flight
      ("unresolved",  age_seconds, None)       -> in flight past the stale window

    Modelled on `ledger_v2._claim_idempotency`, with one deliberate difference.
    Ledger v2 may take a stale claim over for its in-band endpoints, because
    those complete inside the money transaction. No endpoint here does: every
    handler in this file dispatches its money move into `Restocker_main` and
    records the outcome afterwards, in a transaction of its own. So this function
    never re-grants a stale claim — it reports one. An operator checks the
    wallet; the alternative is a silent second charge.
    """
    if not key:
        return ("new", time.time(), None)
    _ensure_tables()
    now = time.time()
    with _db().db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bank_idempotency (key, ts, state, applied_unknown) "
            "VALUES (?, ?, 'in_progress', 1)", (key, now))
        if cur.rowcount == 1:
            return ("new", now, None)
        row = conn.execute(
            "SELECT ts, state, status_code, response_json FROM bank_idempotency WHERE key=?",
            (key,)).fetchone()
    if row is None:                     # deleted between the two statements
        return ("in_progress", 0.0, None)
    if (row["state"] or "") == "done" and row["response_json"]:
        try:
            body = json.loads(row["response_json"])
        except Exception:
            body = None
        if body is not None:
            return ("replay", 0.0, (int(row["status_code"] or 200), body))
    age = max(0.0, now - float(row["ts"] or now))
    return ("unresolved" if age > IDEMPOTENCY_STALE_SECONDS else "in_progress", age, None)


def _complete_key(key: str, body: dict, status: int, claim_ts: float) -> None:
    """Record the outcome against the key. This is what makes a repeat a REPLAY.

    `claim_ts` scopes the write to THIS attempt, so a slow attempt finishing late
    cannot overwrite an answer someone else recorded. The response stored is the
    one the first caller received — built once, stored, then returned — so a
    replay is byte-identical by construction rather than by two pieces of code
    agreeing (`ledger_v2._complete_idempotency`).
    """
    if not key:
        return
    with _db().db() as conn:
        conn.execute(
            "UPDATE bank_idempotency SET state='done', applied_unknown=0, status_code=?, "
            "response_json=?, completed_at=? WHERE key=? AND state='in_progress' AND ts=?",
            (int(status), json.dumps(body, separators=(",", ":"), default=str),
             time.time(), key, float(claim_ts)))


def _release_key(key: str, claim_ts: float | None = None) -> None:
    """Drop a claim for a request that provably moved NOTHING, so a corrected
    retry is not blocked.

    "Provably" is the whole word. Only a **definite refusal** may call this: core
    said no before it touched a balance. An exception, a timeout, or an
    unreadable answer is an UNKNOWN outcome — `run_on_bot_loop` gives up after
    20s over a synchronous core that cannot be cancelled, so "no answer over a
    committed trade" is a real shape, and releasing on it hands the key back and
    lets the retry charge again. `state='in_progress'` also excludes anything
    already recorded `done`.
    """
    if not key:
        return
    try:
        sql = "DELETE FROM bank_idempotency WHERE key=? AND state='in_progress'"
        args: tuple = (key,)
        if claim_ts is not None:
            sql += " AND ts=?"
            args = args + (float(claim_ts),)
        with _db().db() as conn:
            conn.execute(sql, args)
    except Exception:
        pass


def _idem_busy(kind: str, key: str, age: float, uid: str = ""):
    """The 409 a caller gets when an earlier attempt owns the key.

    `idempotency_in_progress` is RETRYABLE and means the outcome is still
    pending; `idempotency_unresolved` is not, and means a human must look. The
    bank's `_is_permanent` knows both by name and refuses to guess at either —
    it must NOT treat these as "definitely refused, refund it".
    """
    if kind == "unresolved":
        log.error("[bank_api] idempotency key %r unresolved after %.0fs (wallet %s) — "
                  "an earlier attempt never recorded its outcome; check the wallet "
                  "and delete the key by hand if nothing landed", key, age, uid or "?")
        return _err("idempotency_unresolved", 409)
    return _err("idempotency_in_progress", 409)


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
    kind, claim, stored = _claim_key(key)
    if kind == "replay":
        # The stored payload, byte-for-byte. NOT `{"ok": True, "deduped": True}`:
        # that told the bank's resume sweep "your money moved" over a key that
        # only recorded an intention, and minted 1,000 coins per stuck pay-in.
        status, payload = stored
        return web.json_response(payload, status=int(status))
    if kind in ("in_progress", "unresolved"):
        return _idem_busy(kind, key, claim, uid)

    count_principal = bool(body.get("count_principal", True))
    reason = str(body.get("reason") or "")
    m = _main()

    if amount > 0:
        coins, principal = await m.run_on_bot_loop(m.add_coins, int(uid), amount, counts_as_principal=count_principal)
    else:
        need = -amount
        cur = _db().get_balance(uid)
        if int(cur.get("coins") or 0) < need:
            # A definite refusal, decided before anything was dispatched: the
            # coins provably did not move, so the key goes back and a corrected
            # retry is possible. This is the ONLY release on this handler.
            _release_key(key, claim)
            return _err("insufficient", 409)
        coins, principal = await m.run_on_bot_loop(m.deduct_coins, int(uid), need, reduce_principal=count_principal)

    _audit("adjust", uid, amount, reason, extra=f"key={key}")
    payload = {
        "ok": True, "user_id": uid, "applied": amount,
        "coins": int(coins), "principal": float(principal),
    }
    # Record the outcome BEFORE answering, so a caller that never sees this
    # response still gets it on the retry. Anything that raises above this line
    # leaves the claim `in_progress`: the retry is told `idempotency_in_progress`
    # (and, past 15 minutes, `idempotency_unresolved` with an ERROR log naming
    # the key and the wallet) rather than being handed a false `deduped`.
    _complete_key(key, payload, 200, claim)
    return web.json_response(payload)


@require_token
async def h_transfer(request):
    """Atomically move coins between two wallets.

    Body: {from_user, to_user, amount, reason?, idempotency_key?}

    ATOMIC, AND NOW ACTUALLY. The docstring said "atomically" over a debit on one
    trip to the bot loop and a credit on a second, with a compensating refund if
    the credit raised — three commits for one transfer. A process death between
    the debit and the credit DESTROYED the coins with nothing recording that a
    credit was owed, and the refund could not fix that because it is a third
    commit for the same death to land in; its own branch had to answer 500
    "sender refunded" about a refund that may not have landed either.

    Both legs are now `Restocker_main._transfer_coins_tx`, one transaction, one
    trip to the loop. A crash leaves "the transfer happened" or "it did not".

    THE WIRE CONTRACT IS UNCHANGED — Osentar has been through six rounds and is
    converged, so nothing here invents a status, a field or an error string:
      * 200 {ok, amount, from{...}, to{...}}      — identical, same builders
      * 409 {error: "insufficient"}               — identical; it is now ALSO the
        answer when the debit loses a race between the pre-read and the claim,
        which previously could not be detected at all
      * 400 validation errors                     — identical
      * 500 "Transfer failed; sender refunded."   — kept verbatim for the case it
        described, and it is now true by construction: the rollback IS the refund
    What did change is invisible to the caller and strictly better: the two ledger
    rows are labelled and carry the idempotency key instead of landing as
    `unlabelled: bank_api.py:NNN h_transfer`, and a definite refusal releases the
    key so a corrected retry is possible.
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
    kind, claim, stored = _claim_key(key)
    if kind == "replay":
        status, payload = stored
        return web.json_response(payload, status=int(status))
    if kind in ("in_progress", "unresolved"):
        return _idem_busy(kind, key, claim, src)

    if int(_db().get_balance(src).get("coins") or 0) < amount:
        _release_key(key, claim)          # definite refusal, nothing dispatched
        return _err("insufficient", 409)

    m = _main()
    _why = str(body.get("reason") or "").strip()
    _label = f"bank transfer{(' ' + _why) if _why else ''}{(' key=' + key) if key else ''}"
    try:
        await m.run_on_bot_loop(m._transfer_coins_tx, src, dst, amount, _label)
    except m._TradeRefused as _tr:
        # The wallet covered less than it did at the pre-read a moment ago. The
        # transaction ROLLED BACK, so this is definite: nothing moved, the key
        # goes back, and a corrected retry is possible. The old shape could not
        # reach this state -- it credited the full amount regardless of what the
        # debit actually took, which is a mint.
        _release_key(key, claim)
        log.info("[bank_api] transfer refused: %s covered %s of the %s requested. "
                 "Rolled back, nothing moved.", src, f"{int(_tr.detail or 0):,}",
                 f"{amount:,}")
        return _err("insufficient", 409)
    except Exception as e:
        _audit("transfer_failed", src, amount, str(body.get("reason") or ""), extra=f"to={dst}")
        log.exception("[bank_api] transfer failed, rolled back whole: %s", e)
        # The claim is deliberately NOT released. One transaction removes the
        # half-transfer, but not the UNKNOWN: `run_on_bot_loop` can time out or
        # report `CancelledError` for work that then runs anyway (measured, 5,113
        # coins), and a failure raised BY `commit()` may be on disk. So the
        # outcome is unknown, not refused, and handing the key back would let the
        # retry debit the sender a second time. The message is unchanged and is
        # now true for free: the rollback IS the refund.
        return _err("Transfer failed; sender refunded.", 500)

    _audit("transfer", src, amount, str(body.get("reason") or ""), extra=f"to={dst} key={key}")
    payload = {
        "ok": True, "amount": amount,
        "from": _balance_payload(src), "to": _balance_payload(dst),
    }
    _complete_key(key, payload, 200, claim)
    return web.json_response(payload)


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


def _trade_bounds(body: dict) -> dict:
    """Optional slippage bounds forwarded to the trade engine: `quote_price` (the
    mid the bank quoted its user), `max_slippage_bps`, and `max_total`/`min_total`
    (the coin figure that user confirmed). If the market has moved past them the
    engine refuses `slippage` before anything moves, and that code is in
    `DEFINITE_STOCK_REFUSALS`, so the key is released and a re-quote can go through.
    Omitting them all is the previous, unbounded behaviour."""
    out = {}
    for name, cast in (("quote_price", float), ("max_slippage_bps", int),
                       ("max_total", int), ("min_total", int)):
        v = (body or {}).get(name)
        if v in (None, ""):
            continue
        try:
            out[name] = cast(v)
        except (TypeError, ValueError):
            continue
    return out


def _stock_response(action: str, uid: str, mid: str, shares: int,
                    key: str, claim: float, r):
    """Turn one `exec_stock_trade` answer into a response AND resolve its key.

    Three outcomes, and the difference between them is the whole finding:

    * **ok** — record the payload against the key. A retry replays it.
    * **a code in `DEFINITE_STOCK_REFUSALS`** — core said no before it moved
      anything, so release the key and let a corrected call through.
    * **anything else** — UNKNOWN. Keep the claim. `not r.get("ok")` used to
      release here, which released the key over a trade that had committed and
      let the retry buy the same shares again; `deduped` in particular means an
      *earlier* attempt applied it. A retry now gets `idempotency_in_progress`
      and, past the stale window, `idempotency_unresolved` with an ERROR log.

    The cost is symmetric and accepted: a refusal code added to
    `exec_stock_trade` and not to `DEFINITE_STOCK_REFUSALS` locks its key until
    an operator releases it. That is the right way round.
    """
    payload = {
        "ok": bool(isinstance(r, dict) and r.get("ok")),
        "code": (r.get("code") if isinstance(r, dict) else None),
        "message": (r.get("msg") if isinstance(r, dict) else None),
        "fill_price": (r.get("fill") if isinstance(r, dict) else None),
        "total": (r.get("total") if isinstance(r, dict) else None),
        "new_price": (r.get("new_price") if isinstance(r, dict) else None),
        **_balance_payload(uid),
    }
    if payload["ok"]:
        _audit(action, uid, shares, mid, extra=f"key={key}")
        _complete_key(key, payload, 200, claim)
    elif payload["code"] in DEFINITE_STOCK_REFUSALS:
        _release_key(key, claim)
    else:
        log.error("[bank_api] %s left idempotency key %r UNRESOLVED for wallet %s "
                  "(answer: %r) — the trade may have committed; check the stock "
                  "ledger before releasing the key", action, key, uid, r)
    return web.json_response(payload)


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
    kind, claim, stored = _claim_key(key)
    if kind == "replay":
        status, payload = stored
        return web.json_response(payload, status=int(status))
    if kind in ("in_progress", "unresolved"):
        return _idem_busy(kind, key, claim, uid)
    _m = _main()
    r = await _m.run_on_bot_loop(_m.exec_stock_trade, "buy", int(uid), mid, shares, name,
                                 **_trade_bounds(body))
    return _stock_response("stock_buy", uid, mid, shares, key, claim, r)


@require_token
async def h_stock_sell(request):
    """Sell shares for a user, crediting their Restocker wallet.

    Body: {user_id, market_id, shares, name?, idempotency_key?}
    Returns {ok, code, message, fill_price, total, new_price, ...balance}. `code`
    is machine-readable: ok | not_listed | bad_shares | insufficient_shares |
    no_liquidity | error | deduped.

    `no_liquidity` means the market's treasury could not fund the proceeds. It is a
    definite refusal — nothing was sold and nothing was charged — and the message
    names how many shares the treasury CAN currently cover.
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
    kind, claim, stored = _claim_key(key)
    if kind == "replay":
        status, payload = stored
        return web.json_response(payload, status=int(status))
    if kind in ("in_progress", "unresolved"):
        return _idem_busy(kind, key, claim, uid)
    _m = _main()
    r = await _m.run_on_bot_loop(_m.exec_stock_trade, "sell", int(uid), mid, shares, name,
                                 **_trade_bounds(body))
    return _stock_response("stock_sell", uid, mid, shares, key, claim, r)



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
