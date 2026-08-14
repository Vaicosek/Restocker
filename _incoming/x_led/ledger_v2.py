"""
ledger_v2.py — core-side implementation of LEDGER_API_v2.md.

Mounts alongside `bank_api.py` in `Restocker_web.start_webserver()`:

    try:
        import ledger_v2
        ledger_v2.register_ledger_routes(app)
    except Exception as _e:
        print(f"⚠️  Ledger v2 not registered: {_e}")

────────────────────────────────────────────────────────────────────────────
CORRECTION TO THE BRIEF: this is an aiohttp module, not a Flask blueprint.
Restocker's web layer is aiohttp (`web.Application`, `app.router.add_get`);
there is no Flask, no Blueprint and no Jinja anywhere in it. The extension
point that actually exists is `register_X_routes(app)`, which `bank_api.py`
already uses, so that is the shape used here. Nothing else about the design
changes — "blueprint" was the intent, `register_ledger_routes` is the spelling.
────────────────────────────────────────────────────────────────────────────

WHAT THIS MODULE DOES NOT REUSE, AND WHY
========================================
It deliberately does NOT call `Restocker_db.adjust_balance` / `add_coins` /
`deduct_coins` for money movement. Three reasons, each load-bearing:

 1. `adjust_balance` CLAMPS a debit at zero (`coins = MAX(0, coins - ?)`) rather
    than failing. Every existing caller guards it with a read-then-write
    pre-check, which is a TOCTOU window. v2 needs a debit that FAILS.
 2. `Restocker_db.db()` does not nest and never issues `BEGIN IMMEDIATE`. An
    inner `with db()` commits the outer block's partial work, so a hold cannot
    be composed from existing helpers and still be atomic.
 3. `add_coins`/`deduct_coins` take `int(user_id)`. `int("treasury:estates")`
    raises. Treasuries are wallet rows with non-numeric ids by design.

So this module owns its own connection (`_conn()`), its own explicit
`BEGIN IMMEDIATE` transactions (`_tx()`), and writes `balances` with raw SQL.
It is a *separate connection to the same file* — WAL makes that safe, and it
avoids changing `isolation_level` on the thread-local connection that the rest
of Restocker shares.

THE CLAIM-FIRST GUARD
=====================
Every coin mutation in this file is one conditional UPDATE whose WHERE clause
carries the entire precondition, followed by a `rowcount == 1` check:

    UPDATE balances
       SET coins = coins - :amt
     WHERE user_id = :uid
       AND frozen = 0
       AND CAST(coins AS INTEGER) - :held >= :amt

If `rowcount` is 0 the row was not won — insufficient available funds, frozen,
or lost to a concurrent writer — and nothing was applied. There is no
read-then-write anywhere in a money path. Same shape for claiming a hold, and
for claiming an idempotency key.

ESCROW IS ENFORCED IN THE DATABASE, NOT BY THIS MODULE (S1)
===========================================================
This module checks `available = balance - held` on every path it owns. It owns
none of the legacy ones, and `Restocker_db.adjust_balance` — the mutator every
shop, hive and payout goes through — writes `coins = MAX(0, coins - ?)` with no
knowledge of `ledger_holds`. So a bidder's escrow could be spent in a shop and
the auction's capture would then fail forever on an already-won lot.

`ledger_migrate.py` installs triggers on `balances` that ABORT any UPDATE,
DELETE or REPLACE which would drop a wallet below its open-hold total, or
deepen an existing shortfall. They apply to every writer, including this one.
An `INSERT` that is IGNORED (the ensure-wallet idiom) is not covered and does
not need to be — it changes nothing. Three consequences while editing here:

  * `capture_hold` MUST keep claiming the hold row (`state='open'` →
    `'captured'`) BEFORE it debits. That ordering is the only reason a capture
    is allowed to spend the coins its own hold reserved — there is no bypass.
  * `capture_hold` MUST also keep writing `settling` in that same claim UPDATE
    and clearing it after the debit (N2). Ordering tells the guard the
    reservation is gone; `settling` tells it the debit that follows is the
    settlement of that reservation, which is what makes an over-committed
    wallet able to honour the holds it CAN cover instead of none of them.
  * A legacy caller that would have eaten escrow now gets
    `sqlite3.IntegrityError: insufficient: would spend coins reserved by an open
    hold`. Loud, and never a partial debit. See LEDGER_API_v2.md §5.1.

THE TREASURY MAY GO NEGATIVE (S12)
==================================
§3 promises that a satellite bug "misallocates money but can never create it —
the treasury goes negative and screams". `_debit` used to apply the same floor
to `treasury:estates` as to a person, so it could not, and an over-payout looked
exactly like a punter with an empty wallet. A `treasury:*` debit may now land
below zero (up to `TREASURY_MAX_DEFICIT`); it logs at ERROR, writes a
`treasury_insolvent` audit row, and every balance read of that account returns
`insolvent: true`. Past the limit the call fails with the distinct code
`treasury_insolvent`, never `insufficient`.

MONEY IS INTEGER COINS
======================
No float touches a money path here. `balances.coins` is REAL in the legacy
schema (see ledger_migrate.py) so every read casts with `CAST(... AS INTEGER)`
in SQL and `int()` in Python; every write is an integer literal. Division uses
`//`; where a remainder exists the caller is told where it went.

KNOWN LIMIT (documented, not hidden)
====================================
The legacy stock trade engine lives in `Restocker_main.exec_stock_trade`, is not
transactional, and is kept safe only by every caller marshalling through
`run_on_bot_loop`. The `/stock/*` endpoints here are a passthrough: they add an
`available` pre-check and idempotency, then hand off. That pre-check is a
TOCTOU window that this module cannot close from outside `Restocker_main`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp is a hard dep of the web server
    web = None  # type: ignore[assignment]

log = logging.getLogger("ledger_v2")

LEDGER_API_VERSION: str = "2.0"

#: Idempotency records are replayable for this long, then swept.
IDEMPOTENCY_TTL_SECONDS: int = 30 * 24 * 3600

#: S3. How long an `in_progress` claim may sit before another attempt with the
#: SAME key and the SAME money fields may take it over.
#:
#: This is only safe because completion is now recorded INSIDE the money
#: transaction, so `in_progress` provably means "the coins did not move", and
#: because that completion is itself claim-first: the money transaction commits
#: only if it still owns the claim it took (`created_at` is the claim token). A
#: stalled original that wakes up after a takeover therefore rolls its own money
#: back rather than paying a second time.
#:
#: 900s matches `estates_db.requeue_stuck_row`'s stuck-row threshold, so the
#: satellite's retry is not rejected by the very key it is retrying.
IDEMPOTENCY_STALE_SECONDS: int = 900

#: A hold must expire. This is the ceiling a caller may ask for.
MAX_HOLD_SECONDS: int = 400 * 24 * 3600
MIN_HOLD_SECONDS: int = 60

#: How many expired holds one sweep pass releases before yielding.
SWEEP_BATCH: int = 200

#: S12. How far a `treasury:*` row may go below zero before core refuses.
#:
#: §3's guarantee is that a satellite bug "misallocates money but can never
#: create it — the treasury goes negative and screams". It could not: `_debit`'s
#: availability guard applied to every account including `treasury:estates`, so
#: an over-payout came back as `insufficient` — indistinguishable from a punter
#: with an empty wallet — and `fail_payout_row` retried it five times and parked
#: it `failed`. The invariant held and the scream was inaudible.
#:
#: Now a treasury debit is allowed to land below zero, is logged at ERROR, is
#: written to `ledger_entries` as `treasury_insolvent`, and is reported on every
#: balance read as `insolvent: true`. The limit is a circuit breaker for a
#: runaway loop, not the alarm: past it the call fails with the DISTINCT code
#: `treasury_insolvent` (never `insufficient`), so even the refusal names the
#: real problem. Set `LEDGER_TREASURY_MAX_DEFICIT=0` to restore the old
#: fail-closed behaviour.
TREASURY_MAX_DEFICIT: int = max(
    0, int(os.getenv("LEDGER_TREASURY_MAX_DEFICIT") or 10_000_000)
)

#: Non-numeric wallet ids that belong to a service, not a person.
TREASURY_PREFIX: str = "treasury:"


def _is_treasury(user_id: str) -> bool:
    return str(user_id).startswith(TREASURY_PREFIX)


# ══════════════════════════════════════════════════════════════════════════
# Services, tokens, scopes
# ══════════════════════════════════════════════════════════════════════════

SCOPE_READ = "wallet.read"
SCOPE_TRANSFER = "wallet.transfer"
SCOPE_MINT = "wallet.mint"
SCOPE_STOCKS = "stocks.trade"
SCOPE_HOLD = "hold.*"

#: LEDGER_API_v2.md §3. `estates` has no `wallet.mint` and no `stocks.trade`:
#: an auction or betting bug can misallocate money but can never create it —
#: treasury:estates goes negative and screams instead of inflating the economy.
SERVICE_SCOPES: dict[str, frozenset[str]] = {
    "osentar": frozenset({SCOPE_READ, SCOPE_TRANSFER, SCOPE_MINT, SCOPE_STOCKS, SCOPE_HOLD}),
    "estates": frozenset({SCOPE_READ, SCOPE_TRANSFER, SCOPE_HOLD}),
}

#: Each service's own treasury. `capture` without `to_user` destroys coins and
#: needs wallet.mint; estates must always capture into this row.
SERVICE_TREASURY: dict[str, str] = {
    "osentar": "treasury:osentar",
    "estates": "treasury:estates",
}

#: Env var per service. An empty/absent value disables that service (503).
TOKEN_ENV: dict[str, str] = {
    "osentar": "LEDGER_TOKEN_OSENTAR",
    "estates": "LEDGER_TOKEN_ESTATES",
}

#: Keys that must be domain-derived. LEDGER_API_v2.md §6 bans uuid4 here,
#: because a per-attempt key means a satellite retry double-charges.
#:
#: R5-B — `stock.buy`/`stock.sell` were missing from this set for five rounds,
#: and they are the pair that needed it most: their money moves OUTSIDE this
#: module's transaction (`_is_out_of_band`), so the key is the only thing that
#: can refuse a second trade. Two attempts with two fresh uuid4s both took a
#: fresh claim, both dispatched, both answered 200, and the punter bought the
#: shares twice with nothing logged (`/tmp/r5/m2.py`). Every `applied_unknown`
#: and `idempotency_unresolved` protection in this module is voluntary until
#: this line is right, because all of them key off the key.
#:
#: `_key_field`'s docstring walks EVERY endpoint that reaches it and says why it
#: is in this set or out of it. Read that before adding or removing a name here.
UUID4_BANNED_ENDPOINTS: frozenset[str] = frozenset({
    "hold.capture", "transfer", "adjust", "stock.buy", "stock.sell",
})

_TOKENS: dict[str, str] = {}
_TOKENS_LOADED: bool = False
_TOKENS_LOCK = threading.Lock()


def load_tokens(force: bool = False) -> dict[str, str]:
    """Read the per-service tokens from the environment.

    v1 read `BANK_API_TOKEN` once at import time, so rotating it needed a
    process restart. This caches but exposes `force=True`, so an admin command
    can reload after editing `.env` without bouncing the bot.
    """
    global _TOKENS_LOADED
    with _TOKENS_LOCK:
        if _TOKENS_LOADED and not force:
            return dict(_TOKENS)
        _TOKENS.clear()
        for service, env_name in TOKEN_ENV.items():
            tok = (os.getenv(env_name) or "").strip()
            if tok:
                _TOKENS[service] = tok
        # v1 compatibility: if BANK_API_TOKEN is set and no osentar token is,
        # the old shared secret keeps working as osentar's token so the bank
        # bot does not have to change on day one (§9).
        legacy = (os.getenv("BANK_API_TOKEN") or "").strip()
        if legacy and "osentar" not in _TOKENS:
            _TOKENS["osentar"] = legacy
        _TOKENS_LOADED = True
        return dict(_TOKENS)


def enabled_services() -> list[str]:
    return sorted(load_tokens().keys())


def _resolve_service(request: Any) -> Optional[str]:
    """Constant-time match the presented token against every configured service.

    Every candidate is compared even after a match, so response time does not
    leak which service's token was presented.
    """
    supplied = (
        request.headers.get("X-Service-Token")
        or request.headers.get("X-Bank-Token")
        or ""
    ).strip()
    if not supplied:
        return None
    found: Optional[str] = None
    for service, token in load_tokens().items():
        if hmac.compare_digest(supplied, token):
            found = service
    return found


def has_scope(service: str, scope: str) -> bool:
    return scope in SERVICE_SCOPES.get(service, frozenset())


# ══════════════════════════════════════════════════════════════════════════
# Connection + transaction primitives
# ══════════════════════════════════════════════════════════════════════════

_local = threading.local()


def _db_path() -> str:
    import Restocker_db as db
    return str(db.DB_PATH)


def _conn() -> sqlite3.Connection:
    """A ledger-owned connection to restocker.db, one per thread.

    `isolation_level=None` puts this connection in autocommit mode so `_tx()`
    can issue an explicit `BEGIN IMMEDIATE`. That setting is why this is a
    separate connection rather than `Restocker_db._get_conn()` — changing the
    isolation level on the shared thread-local connection would silently alter
    transaction behaviour for every other Restocker caller on that thread.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_db_path(), check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # Longer than Restocker's 5s: a v2 write may queue behind a bot-loop
        # write, and failing a money call because a scan was running is worse
        # than waiting for it.
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def _discard_conn(conn: Optional[sqlite3.Connection] = None) -> None:
    """Throw this thread's connection away so the next call builds a fresh one.

    S8: a connection whose transaction could not be ended is unusable — every
    later `_tx()` finds `in_transaction` True and raises the "not re-entrant"
    RuntimeError, which `_require` reports as `500 internal_error`. One failed
    COMMIT would therefore take every money endpoint on that web thread offline
    until the process bounced, and the log would say "internal error" rather
    than "the disk was busy". Dropping the connection makes the failure a single
    failed request instead of a dead thread.
    """
    target = conn if conn is not None else getattr(_local, "conn", None)
    _local.conn = None
    if target is not None:
        try:
            target.close()
        except Exception:  # pragma: no cover - close() on a broken handle
            pass


@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """One real, serialised write transaction. NOT nestable — do not compose.

    `BEGIN IMMEDIATE` takes the write lock up front, so a read-then-conditional-
    write pair inside cannot lose the row to another writer at upgrade time
    (which is how deferred transactions produce SQLITE_BUSY halfway through a
    money path).

    COMMIT is inside the try (S8). If it raises — SQLITE_BUSY past the
    `busy_timeout`, SQLITE_FULL, a disk I/O error — the transaction is still
    open on this connection, so it is rolled back if possible and the connection
    is discarded either way before the exception is re-raised. The caller still
    sees the error; the thread is not poisoned by it.
    """
    conn = _conn()
    if conn.in_transaction:
        raise RuntimeError(
            "ledger_v2._tx() is not re-entrant — a nested transaction would "
            "commit the outer block's partial work. Restructure the caller."
        )
    def _unwind() -> None:
        """End the transaction, whatever it takes. Never leave one open."""
        try:
            conn.execute("ROLLBACK")
        except Exception:
            _discard_conn(conn)
            return
        if conn.in_transaction:  # pragma: no cover - defensive
            _discard_conn(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        _unwind()
        raise
    else:
        try:
            conn.execute("COMMIT")
        except Exception:
            # S8. The write did NOT land, the transaction is still open, and
            # this handle is suspect (busy/full/IO). Unwind, bin the connection,
            # and let the caller see the real exception.
            _unwind()
            _discard_conn(conn)
            raise


def _now() -> float:
    return time.time()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════
# Errors and responses
# ══════════════════════════════════════════════════════════════════════════

class LedgerError(Exception):
    """A domain failure with a machine-readable code from LEDGER_API_v2.md §7."""

    def __init__(self, code: str, status: int = 400, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"ok": False, "error": self.code}
        if self.detail:
            body["detail"] = self.detail
        return body


def _json(body: dict[str, Any], status: int = 200) -> Any:
    return web.json_response(body, status=status)


def _err(code: str, status: int = 400, detail: str = "") -> Any:
    err = LedgerError(code, status, detail)
    return _json(err.payload(), status)


# ══════════════════════════════════════════════════════════════════════════
# Idempotency — claim-first, 30-day replay store
# ══════════════════════════════════════════════════════════════════════════

#: S9. The fields that identify WHICH MONEY an endpoint moves. The fingerprint
#: is built from these alone, so a field that varies between attempts of the
#: same business event cannot turn a legitimate replay into a hard 409.
#:
#: The bug this closes: estates computes `expires_in` from the clock
#: (`max(600, int((closes - utcnow()).total_seconds()) + GRACE)`). Attempt 1
#: sends 86402, core commits the hold, the response is lost. Attempt 2 sixty
#: seconds later sends 86342 — same key, different body — and got
#: `409 idempotency_conflict`, whose own client docstring says "do NOT retry,
#: fix the key". The hold existed at core, nothing in estates.db named it, and
#: the punter's coins were reserved and invisible for the full TTL.
#:
#: `expires_in` and `reason` are deliberately NOT here: neither changes which
#: coins move. `acting_user` IS here, because it is an authorisation input —
#: replaying a transfer under a different asserted actor must fail loudly.
#: An endpoint absent from this map falls back to hashing the whole body.
FINGERPRINT_FIELDS: dict[str, tuple[str, ...]] = {
    "hold":         ("user_id", "amount"),
    "hold.capture": ("hold_id", "amount", "to_user"),
    "hold.release": ("hold_id",),
    "transfer":     ("from_user", "to_user", "amount", "acting_user"),
    "adjust":       ("user_id", "amount"),
    "stock.buy":    ("user_id", "market_id", "shares"),
    "stock.sell":   ("user_id", "market_id", "shares"),
}

#: N1. The endpoints whose money move is a statement in the SAME transaction as
#: the idempotency completion, and are therefore the ONLY ones for which
#: `state='in_progress'` provably means "the coins did not move".
#:
#: This list is what makes the stale-claim takeover safe, and it is an ALLOWLIST
#: on purpose. Round 2 added the takeover (S3, fix 2) and applied it to every
#: endpoint; `stock.buy` / `stock.sell` dispatch the trade through
#: `run_on_bot_loop` into `Restocker_main.exec_stock_trade` and complete the key
#: in a SEPARATE transaction afterwards, so an interrupted attempt leaves a row
#: that says `in_progress` over coins that have already moved. 900s later the
#: same key was re-granted and the user paid twice for the same 7 shares.
#:
#: A denylist of the two stock endpoints would have closed that and rotted the
#: moment a third out-of-band endpoint was added. Default-deny does not: a new
#: endpoint is out-of-band until somebody proves otherwise, and the proof is
#: mechanical rather than editorial — `_finalize_idempotency` is reachable ONLY
#: from inside a money transaction, so it refuses to complete a key whose
#: endpoint is not declared here, and any endpoint that IS in-band trips that
#: refusal on its first call in test if the declaration is missing.
#:
#: Everything not listed here is claimed with `applied_unknown = 1`, which no
#: takeover and no release may cross — see `_claim_idempotency`.
IN_BAND_ENDPOINTS: frozenset[str] = frozenset({
    "hold", "hold.capture", "hold.release", "transfer", "adjust",
})


def _is_out_of_band(endpoint: str) -> bool:
    """True when this endpoint's money may move outside our transaction."""
    return endpoint not in IN_BAND_ENDPOINTS


def _canon(value: Any) -> Any:
    """Normalise one fingerprint input so equal money compares equal.

    `100` and `100.0` are the same amount to `_int_field`, so they must be the
    same fingerprint too — otherwise the narrowing above just moves the false
    conflict somewhere quieter.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _fingerprint(payload: dict[str, Any], endpoint: Optional[str] = None) -> str:
    """Stable hash of the money-identifying part of a request body.

    `sort_keys` + no whitespace means key order and formatting cannot make the
    same business request look like two different ones. `idempotency_key` is
    excluded — it is the lookup, not part of what is being compared.

    With `endpoint`, only `FINGERPRINT_FIELDS[endpoint]` is hashed (S9). Without
    it, the whole body is hashed — that form is still used to SYNTHESISE a key
    for the v1 alias, where two adjusts differing only in `reason` must not
    collapse onto one key.
    """
    if endpoint and endpoint in FINGERPRINT_FIELDS:
        scrubbed: dict[str, Any] = {
            k: _canon(payload.get(k)) for k in FINGERPRINT_FIELDS[endpoint]
        }
    else:
        scrubbed = {k: v for k, v in payload.items() if k != "idempotency_key"}
    blob = json.dumps(scrubbed, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _subject(body: dict[str, Any]) -> str:
    """Who this request is about, for a LOG LINE ONLY. Never a money input.

    R3-1's fix makes one operator-facing failure reachable: a stock trade whose
    response failed to store leaves the claim unresolved, and the retry is
    refused until a human checks the stock ledger by hand. A key is a poor
    starting point for that check and `estates:market:77:payout:user:123` is not
    the format `/stock/*` uses, so the log has to name the wallet outright.

    Kept out of `_fingerprint` deliberately: this reads whichever of the user
    fields are present and never fails, which is right for a log and wrong for
    anything that decides which coins move.
    """
    uid = str(body.get("user_id") or "").strip()
    if uid:
        return uid
    src = str(body.get("from_user") or "").strip()
    dst = str(body.get("to_user") or "").strip()
    if src or dst:
        return f"{src or '?'}->{dst or '?'}"
    return "unknown-user"


class Replay(Exception):
    """Raised internally to short-circuit a handler with a stored response."""

    def __init__(self, body: dict[str, Any], status: int) -> None:
        super().__init__("replay")
        self.body = body
        self.status = status


class _Idem:
    """One owned idempotency claim, carried into the money transaction.

    `claim_ts` is the `created_at` this attempt wrote. It is the claim token:
    the completion UPDATE requires it, so an attempt whose claim was taken over
    by a later retry cannot commit its money.

    `body` is filled in by `_finalize_idempotency` with the response that was
    actually STORED, so the handler returns byte-identically what a later replay
    will return. There is no second construction of the body to drift from it.

    R3-6: `endpoint` is REQUIRED, and it used to default to `""`. The default was
    not cosmetic — `_finalize_idempotency` reads it to check that an endpoint
    completing inside the money transaction has declared itself in-band, and that
    check begins `if idem.endpoint`, so an empty one turned the check off. The
    two v1 aliases took it, which meant 2 of the 7 money paths completed with the
    guard disabled. No default, no silent opt-out.
    """

    __slots__ = ("key", "claim_ts", "endpoint", "status", "body_fn", "body")

    def __init__(self, key: str, claim_ts: float, endpoint: str, status: int = 200,
                 body_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None):
        self.key = key
        self.claim_ts = claim_ts
        self.endpoint = endpoint
        self.status = status
        self.body_fn = body_fn
        self.body: Optional[dict[str, Any]] = None


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    """The one place a success envelope is built. Used by the money functions
    (which store it) and by the handlers (which return it)."""
    return {"ok": True, **result}


def _finalize_idempotency(conn: sqlite3.Connection, idem: Optional[_Idem],
                          result: dict[str, Any]) -> None:
    """Mark the claim `done` INSIDE the caller's money transaction (S3, fix 1).

    Before this, the claim, the money move and the completion were three
    separate transactions, and `_tx()` is explicitly non-re-entrant so they
    could not be composed. That left a window where the coins had moved and the
    key was still `in_progress`, and nothing resolved a stale `in_progress` row
    inside 30 days: the retry got `409 idempotency_in_progress` forever, the row
    parked `failed`, staff paid the winner by hand, and the winner was paid
    twice.

    Now the completion is one more statement in the same transaction as the
    debit and the credit: they commit together or not at all, so `in_progress`
    means "not applied" and `done` means "applied, here is the response".

    It is also claim-first, which is what makes takeover of a stale claim safe:
    `WHERE state='in_progress' AND created_at = <the value I wrote>`. If another
    attempt took the claim over, `rowcount` is 0 and this raises — rolling back
    the money in this transaction rather than paying twice.
    """
    if idem is None:
        return
    if _is_out_of_band(idem.endpoint):
        # N1, and the reason `IN_BAND_ENDPOINTS` is checkable rather than a
        # comment: reaching here means this endpoint DOES complete inside the
        # money transaction, so it must say so — the takeover rule reads that
        # declaration. Failing here rolls this transaction back; the alternative
        # is a claim the takeover would later re-grant over moved coins.
        #
        # R3-6: this used to be `if idem.endpoint and ...`, so a claim carrying
        # no endpoint skipped the check entirely — which is what the two v1
        # aliases did. An unnamed endpoint is an undeclared one; it fails here.
        raise LedgerError(
            "internal_error", 500,
            f"endpoint '{idem.endpoint}' completes inside the money transaction "
            f"but is not declared in IN_BAND_ENDPOINTS — add it there, or its "
            f"idempotency claims will be treated as takeover-unsafe forever")
    body = idem.body_fn(result) if idem.body_fn else _ok(result)
    cur = conn.execute(
        "UPDATE ledger_idempotency "
        "SET state='done', status_code=?, response_json=?, completed_at=? "
        "WHERE key=? AND state='in_progress' AND created_at=?",
        (int(idem.status),
         json.dumps(body, separators=(",", ":"), default=str),
         _now(), idem.key, idem.claim_ts),
    )
    if cur.rowcount != 1:
        raise LedgerError(
            "idempotency_in_progress", 409,
            "this attempt no longer owns the idempotency claim — another "
            "attempt is applying it; nothing was moved by this one")
    idem.body = body


def _claim_idempotency(key: str, service: str, endpoint: str,
                       fingerprint: str, subject: str = "unknown-user") -> float:
    """Claim `key` for this request, or raise `Replay` / `LedgerError`.

    Returns the claim token (`created_at`), which must be handed to
    `_finalize_idempotency` so the money transaction can prove it still owns the
    claim it is completing.

    Claim-first: a single `INSERT ... ON CONFLICT` on a PRIMARY KEY. Two
    concurrent identical retries cannot both win the row. Only the winner
    proceeds to move money.

    S3, fix 2 — the conflict clause takes over a STALE claim, and only a stale
    one: same service, same endpoint, same money fingerprint, still
    `in_progress`, `applied_unknown = 0`, and older than
    `IDEMPOTENCY_STALE_SECONDS`. Taking it over is just re-stamping
    `created_at`, which mints a new claim token and thereby invalidates the
    original attempt's. This ordering matters: it is only safe on top of fix 1.
    Without completion inside the money transaction, `in_progress` would not
    mean "not applied" and a takeover would double-pay.

    N1 — which is exactly what happened for `/stock/*`. So the row records, at
    claim time and before any money can move, whether this endpoint's completion
    is in-band (`applied_unknown = 0`, takeover-able) or not
    (`applied_unknown = 1`, never takeover-able, never released by a stalled
    attempt). It is written by the SAME statement that takes the claim, so there
    is no window in which an out-of-band claim exists without its flag, and it
    is derived from `IN_BAND_ENDPOINTS`, which defaults to "unknown".

    Note the ordering rule this enforces and v1 violated: the caller validates
    the request FULLY before calling this. v1 claimed the key at the top of the
    handler and discovered `insufficient` afterwards, so a concurrent retry
    could receive a success-shaped `deduped: true` for a request that was about
    to be rejected.
    """
    claim_ts = _now()
    stale_before = claim_ts - IDEMPOTENCY_STALE_SECONDS
    unknown = 1 if _is_out_of_band(endpoint) else 0
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO ledger_idempotency "
            "(key, service, endpoint, payload_hash, state, created_at, applied_unknown) "
            "VALUES (?, ?, ?, ?, 'in_progress', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET created_at = excluded.created_at "
            "  WHERE ledger_idempotency.state = 'in_progress' "
            "    AND ledger_idempotency.applied_unknown = 0 "
            "    AND ledger_idempotency.created_at < ? "
            "    AND ledger_idempotency.service = excluded.service "
            "    AND ledger_idempotency.endpoint = excluded.endpoint "
            "    AND ledger_idempotency.payload_hash = excluded.payload_hash",
            (key, service, endpoint, fingerprint, claim_ts, unknown, stale_before),
        )
        if cur.rowcount == 1:
            return claim_ts  # inserted, or took over a stale claim — we own it

        row = conn.execute(
            "SELECT service, endpoint, payload_hash, state, status_code, "
            "       response_json, applied_unknown, created_at "
            "FROM ledger_idempotency WHERE key=?",
            (key,),
        ).fetchone()

    if row is None:
        # Swept between the INSERT and the SELECT. Astronomically unlikely;
        # treat as a conflict rather than guessing.
        raise LedgerError("idempotency_conflict", 409, "key vanished mid-claim")

    if row["service"] != service:
        raise LedgerError("idempotency_conflict", 409,
                          "key already used by another service")
    if row["payload_hash"] != fingerprint or row["endpoint"] != endpoint:
        # §6: same key + different payload fails LOUDLY. This is the check that
        # catches a key-derivation bug instead of silently applying the wrong
        # amount under a stale key.
        raise LedgerError("idempotency_conflict", 409,
                          "key already used with a different request body")

    if row["state"] == "done" and row["response_json"]:
        body = json.loads(row["response_json"])
        body["replayed"] = True
        raise Replay(body, int(row["status_code"] or 200))

    if int(row["applied_unknown"] or 0) and float(row["created_at"] or 0) < stale_before:
        # N1. Out-of-band money: the trade was dispatched to the bot loop and
        # never came back, so this key is the ONLY record that it may have
        # applied. Re-granting it is the double-charge; deleting it is the
        # double-charge one retry later. It is neither retryable nor
        # self-healing, and saying so is the whole point of the distinct code.
        age = int(_now() - float(row["created_at"] or claim_ts))
        log.error("[ledger_v2] UNRESOLVED OUT-OF-BAND CLAIM: user=%s key=%s "
                  "service=%s endpoint=%s age=%ds — the trade was dispatched to "
                  "the bot loop and its outcome was never recorded, so it MAY "
                  "have applied. Check %s's stock ledger by hand; if the trade "
                  "did not land, DELETE this key to unblock the retry. This is "
                  "deliberately manual: re-granting it automatically is the "
                  "double-charge.",
                  subject, key, row["service"], row["endpoint"], age, subject)
        raise LedgerError(
            "idempotency_unresolved", 409,
            f"this key dispatched a trade outside the ledger transaction {age}s "
            f"ago and its outcome was never recorded. It will NOT be re-granted "
            f"automatically — check whether the trade applied before retrying.")

    # Same payload, still running and not yet stale. The original is mid-flight;
    # the caller must retry rather than have two attempts race on the same money.
    # This is a RETRYABLE condition (§7), and for an IN-BAND endpoint it provably
    # means the coins have NOT moved: `IDEMPOTENCY_STALE_SECONDS` later the retry
    # takes the claim over instead of being told this for thirty days.
    #
    # R3-1: for an out-of-band claim (`applied_unknown = 1`) it means only "not
    # recorded" — the trade may well have committed on the bot loop. Retrying is
    # still the right instruction, because a retry cannot double-charge: the
    # takeover WHERE excludes this row, so once it goes stale the retry gets
    # `idempotency_unresolved` and a human, not a second trade. What must NOT be
    # inferred from this code is "nothing happened, pay it by hand".
    raise LedgerError("idempotency_in_progress", 409,
                      "an identical request is still in flight — retry, do not "
                      "pay this by hand")


def _complete_idempotency(key: str, body: dict[str, Any], status: int,
                          claim_ts: Optional[float] = None) -> bool:
    """Store the response in a transaction of its OWN. Returns True if it landed.

    Only for paths whose money move cannot be composed into one of our
    transactions — the `/stock/*` passthrough, where the trade happens inside
    `Restocker_main.exec_stock_trade` on the bot loop. Everything this module
    moves itself completes via `_finalize_idempotency`, inside the money
    transaction, and never reaches here.

    Deliberately NOT `sort_keys=True` here (unlike `_fingerprint`): sorting would
    reorder the keys relative to the response the first caller received, which is
    "same values" but not "byte-identical". The fingerprint sorts because it is a
    comparison; this is a recording.
    """
    sql = ("UPDATE ledger_idempotency "
           "SET state='done', applied_unknown=0, status_code=?, response_json=?, "
           "    completed_at=? "
           "WHERE key=? AND state='in_progress'")
    args: tuple[Any, ...] = (int(status),
                             json.dumps(body, separators=(",", ":"), default=str),
                             _now(), key)
    if claim_ts is not None:
        sql += " AND created_at=?"
        args = args + (claim_ts,)
    with _tx() as conn:
        return int(conn.execute(sql, args).rowcount or 0) == 1


def _release_idempotency(key: str, claim_ts: Optional[float] = None) -> None:
    """Drop an unfinished claim so a corrected retry is not blocked forever.

    Only ever called for a request that moved NO money. A key whose transaction
    committed is `done`, and `done` rows are never deleted here — which is what
    makes it safe to call this on the way out of a failed handler even when the
    money transaction inside it succeeded.

    `claim_ts` scopes the delete to THIS attempt's claim, so a slow attempt
    unwinding late cannot delete the claim a newer attempt has since taken over.

    N1: `applied_unknown = 0` scopes it to claims that provably moved nothing.
    Deleting an out-of-band claim whose dispatch raised would be the same
    double-charge as taking it over, one retry sooner and without the 900s wait
    — the handler saw an exception, not a refusal, and an exception from
    `run_on_bot_loop` does not mean the trade did not commit.

    R3-1: that scope is only worth anything if the flag is still set when this
    runs, and until round 4 it was not. `_stock_trade` cleared it as soon as the
    bot loop answered — including on the success path, several statements before
    the outcome was stored — so this DELETE matched a claim over a committed
    trade and handed the key straight back. The clear now happens on exactly two
    paths, both of which make this delete correct rather than catastrophic:
    `_resolve_out_of_band` on a definite REFUSAL (nothing moved, so the claim
    must go), and `_complete_idempotency` in the same statement as
    `state='done'` (in which case `state` alone already excludes the row here).
    An out-of-band claim in any other condition keeps the flag, and this
    function is a no-op on it by design.
    """
    sql = ("DELETE FROM ledger_idempotency "
           "WHERE key=? AND state='in_progress' AND applied_unknown=0")
    args: tuple[Any, ...] = (key,)
    if claim_ts is not None:
        sql += " AND created_at=?"
        args = args + (claim_ts,)
    with _tx() as conn:
        conn.execute(sql, args)


def _resolve_out_of_band(idem: Optional[_Idem]) -> None:
    """N1. Record that an out-of-band dispatch came back REFUSED. Refusals only.

    R4-4 — "refused" means a refusal this module POSITIVELY recognised, which is
    `_classify_stock_result` returning `STOCK_REFUSED` and nothing else. It does
    NOT mean "the answer was not a success": `None`, `{}` and an unreadable dict
    all fail that test, and calling this on one of them clears the flag over a
    trade that may have committed — the double charge of R4-4, reproduced with
    two executions and no wait. There is exactly one caller and it is that test.

    Between `_claim_idempotency` and this call, the claim carries
    `applied_unknown = 1`: the trade is somewhere inside `exec_stock_trade` on
    the bot loop and nothing here can say whether the coins moved. That flag is
    what stops a later attempt taking the claim over (N1's double-charge) and
    what stops a failing handler deleting it on the way out.

    R3-1 — WHY THIS IS THE REFUSAL PATH AND NOT "the return path".
    Round 3 called this as soon as `run_on_bot_loop` returned, on the theory
    that a returned call is a known outcome. It is a known outcome; it is not a
    RECORDED one, and the flag guards the record, not the knowledge. On the
    success path this commit landed two DB transactions and one payload build
    before `_complete_idempotency` wrote `state='done'`, leaving the row
    `in_progress, applied_unknown=0` over coins that had already moved. A kill
    in that window let the 900s takeover re-grant the key; a raise in it (the
    payload's own `get_balance` hitting `database is locked` is enough) sent
    `_idempotent` into `_release_idempotency`, which then matched the cleared
    flag and DELETED the claim over a committed trade, no wait at all. Both
    were reproduced; both charged the user twice for one order.

    A refusal has no such window. Core said no, so there is nothing to record
    beyond releasing the claim, and clearing the flag is precisely what lets
    `_release_idempotency` do that — otherwise a refused trade would be
    unretryable for thirty days.

    On success nothing calls this: `_complete_idempotency` writes
    `applied_unknown=0` in the same statement as `state='done'` (`:741-744`), so
    the flag comes off exactly when, and only if, the outcome is durably stored.
    A raise, a cancelled task or a killed process therefore leaves the flag set,
    which is the correct reading of what is known — and the cost of that reading
    is finding 1's stated trade: a success whose store fails is a LOUD manual
    check (`idempotency_unresolved` + an ERROR log naming the key and the user)
    instead of a silent second charge.
    """
    if idem is None:
        return
    with _tx() as conn:
        conn.execute(
            "UPDATE ledger_idempotency SET applied_unknown = 0 "
            " WHERE key=? AND state='in_progress' AND created_at=?",
            (idem.key, idem.claim_ts),
        )


def sweep_idempotency(now: Optional[float] = None) -> int:
    """Delete records past the 30-day replay window. Returns rows removed."""
    cutoff = (now if now is not None else _now()) - IDEMPOTENCY_TTL_SECONDS
    with _tx() as conn:
        cur = conn.execute("DELETE FROM ledger_idempotency WHERE created_at < ?", (cutoff,))
        return int(cur.rowcount or 0)


def _looks_like_uuid4(key: str) -> bool:
    """True for a bare 32-hex or dashed uuid4 — i.e. a per-attempt key.

    A domain key always contains ':' (`estates:market:77:payout:user:123`), so
    the test is cheap and has no false positives against the documented format.
    """
    k = key.strip().lower().replace("-", "")
    return len(k) == 32 and all(c in "0123456789abcdef" for c in k)


# ══════════════════════════════════════════════════════════════════════════
# Balance: the single source of available funds
# ══════════════════════════════════════════════════════════════════════════

_SQL_HELD = (
    "SELECT COALESCE(SUM(amount - captured_amount - released_amount), 0) "
    "FROM ledger_holds WHERE user_id = ? AND state = 'open'"
)


def _read_balance(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """`{balance, held, available, frozen, ...}` — LEDGER_API_v2.md §4.

    `balance` is cast to INTEGER in SQL rather than in Python because
    `balances.coins` is REAL in the legacy schema: casting on the SQL side means
    the comparison inside every claim-first guard uses the same integer the
    caller was shown, instead of comparing an int against a float that prints
    the same and compares differently.
    """
    uid = str(user_id)
    row = conn.execute(
        "SELECT CAST(coins AS INTEGER) AS coins, "
        "       COALESCE(frozen, 0)    AS frozen, "
        "       frozen_reason, frozen_by, frozen_at "
        "FROM balances WHERE user_id = ?",
        (uid,),
    ).fetchone()
    balance = int(row["coins"]) if row else 0
    frozen = bool(row["frozen"]) if row else False
    held = int(conn.execute(_SQL_HELD, (uid,)).fetchone()[0] or 0)
    return {
        "user_id": uid,
        "balance": balance,
        "held": held,
        "available": balance - held,
        # S12: only a `treasury:*` row can ever be negative, and when one is,
        # every read of it says so. A satellite that shows a house position
        # should render this; a monitor should alert on it.
        "insolvent": balance < 0,
        "frozen": frozen,
        "frozen_reason": (row["frozen_reason"] if row else None) or None,
        "frozen_by": (row["frozen_by"] if row else None) or None,
        "frozen_at": (row["frozen_at"] if row else None) or None,
    }


def get_balance(user_id: str) -> dict[str, Any]:
    """Read-only snapshot. No `_tx()` — a read needs no write lock."""
    return _read_balance(_conn(), str(user_id))


def _ensure_wallet(conn: sqlite3.Connection, user_id: str) -> None:
    """Create the wallet row if absent. Never resets an existing one.

    Treasury ids are non-numeric strings and go through this same path — the
    column is TEXT, and nothing in this module calls `int(user_id)`.
    """
    conn.execute(
        "INSERT INTO balances (user_id, coins, principal, lp) VALUES (?, 0, 0, 0) "
        "ON CONFLICT(user_id) DO NOTHING",
        (str(user_id),),
    )


def _assert_not_frozen(conn: sqlite3.Connection, *user_ids: str) -> None:
    """§8: EVERY money endpoint checks freeze, for every account it touches."""
    for uid in user_ids:
        row = conn.execute(
            "SELECT COALESCE(frozen, 0) AS frozen, frozen_reason "
            "FROM balances WHERE user_id = ?",
            (str(uid),),
        ).fetchone()
        if row and int(row["frozen"] or 0):
            raise LedgerError("frozen", 409,
                              f"{uid}: {row['frozen_reason'] or 'no reason recorded'}")


def _record(
    conn: sqlite3.Connection,
    *,
    service: str,
    action: str,
    user_id: str,
    delta: int = 0,
    balance_after: Optional[int] = None,
    hold_id: Optional[str] = None,
    counterparty: Optional[str] = None,
    reason: str = "",
    key: Optional[str] = None,
) -> None:
    """Append to `ledger_entries` INSIDE the caller's transaction.

    This is the fix for the legacy split where `record_coin_ledger` was a
    best-effort, exception-swallowing call the caller had to remember to make
    separately from `adjust_balance` — so the audit trail could silently diverge
    from `balances`. Here the entry and the balance write commit together or
    not at all. `coin_ledger` is mirrored too, in the same transaction, so the
    existing Restocker views keep working.
    """
    conn.execute(
        "INSERT INTO ledger_entries "
        "(service, action, user_id, delta, balance_after, hold_id, counterparty, "
        " reason, idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)",
        (service, action, str(user_id), int(delta),
         None if balance_after is None else int(balance_after),
         hold_id, counterparty, reason[:200], key),
    )
    if delta and balance_after is not None:
        conn.execute(
            "INSERT INTO coin_ledger (user_id, delta, balance_after, reason) "
            "VALUES (?,?,?,?)",
            (str(user_id), int(delta), int(balance_after),
             f"[{service}] {action}: {reason}"[:200]),
        )


# ══════════════════════════════════════════════════════════════════════════
# THE claim-first money primitives. Everything below goes through these two.
# ══════════════════════════════════════════════════════════════════════════

def _debit(conn: sqlite3.Connection, user_id: str, amount: int,
           *, respect_holds: bool = True) -> int:
    """Remove `amount` coins or fail. Returns the balance after.

    ONE atomic conditional UPDATE. The entire precondition — wallet exists, not
    frozen, and enough AVAILABLE (not raw) balance — lives in the WHERE clause,
    and `rowcount == 0` means nothing was applied.

    This is the exact opposite of `Restocker_db.adjust_balance`, which does
    `coins = MAX(0, coins - ?)`: asked to remove 500 from a 300-coin wallet it
    removes 300 and reports success-ish. Here that call raises `insufficient`
    and the wallet is untouched.

    `respect_holds=False` is used only when capturing a hold, where the coins
    being debited are the very ones the hold reserved — subtracting them twice
    would make a user's own escrow block its own capture.

    A `treasury:*` row is allowed to go NEGATIVE (S12). See TREASURY_MAX_DEFICIT.
    """
    amt = int(amount)
    if amt <= 0:
        raise LedgerError("bad_amount", 400, "amount must be a positive integer")
    uid = str(user_id)
    _ensure_wallet(conn, uid)
    held = int(conn.execute(_SQL_HELD, (uid,)).fetchone()[0] or 0) if respect_holds else 0

    # The floor this debit may not cross. 0 for a person; -TREASURY_MAX_DEFICIT
    # for a house account, which is what makes "the treasury goes negative and
    # screams" true rather than aspirational.
    treasury = _is_treasury(uid)
    floor = -TREASURY_MAX_DEFICIT if treasury else 0

    try:
        cur = conn.execute(
            "UPDATE balances "
            "   SET coins = CAST(coins AS INTEGER) - ?, "
            "       principal = MAX(0, CAST(principal AS INTEGER) - ?), "
            "       updated_at = datetime('now') "
            " WHERE user_id = ? "
            "   AND COALESCE(frozen, 0) = 0 "
            "   AND CAST(coins AS INTEGER) - ? - ? >= ?",
            (amt, amt, uid, held, amt, floor),
        )
    except sqlite3.IntegrityError as exc:
        # The `ledger_balances_respect_holds` trigger fired (S1). Inside this
        # module that should be unreachable: `respect_holds=True` already
        # subtracts the open-hold total, and the one `respect_holds=False` path
        # (capture) declares its settlement so the guard judges it against its
        # own reservation (N2). Reaching here means the wallet was
        # over-committed BEFORE the guard existed — legacy code spent escrow
        # while the trigger was not yet installed — so say that, rather than
        # surfacing a raw IntegrityError as a 500.
        #
        # N2: report what is actually held, NOT the local `held`, which is 0 on
        # every capture path (`respect_holds=False`) and told the operator
        # "(0 held)" while 9,000 was reserved. RAISE(ABORT) undoes the statement
        # and leaves the transaction usable, so these reads are of the same
        # state the trigger judged.
        snap = _read_balance(conn, uid)
        blocking = [
            f"{r['hold_id']}({int(r['open_amount'])})"
            for r in conn.execute(
                "SELECT hold_id, amount - captured_amount - released_amount "
                "       AS open_amount "
                "  FROM ledger_holds WHERE user_id=? AND state='open' "
                " ORDER BY open_amount DESC LIMIT 10", (uid,)).fetchall()
        ]
        raise LedgerError(
            "escrow_shortfall", 409,
            f"{uid}: debiting {amt} would leave {snap['balance'] - amt} against "
            f"{snap['held']} still reserved by open holds "
            f"[{', '.join(blocking) or 'none'}]. The wallet is over-committed — "
            f"reconcile ledger_holds against balances before retrying. [{exc}]"
        ) from exc

    if cur.rowcount != 1:
        # Distinguish the failures for the caller, from inside the same
        # transaction, so the answer cannot be stale.
        _assert_not_frozen(conn, uid)
        snap = _read_balance(conn, uid)
        if treasury:
            # NOT `insufficient`: a house account that cannot pay is an
            # operational emergency, not a user with an empty wallet.
            log.error("[ledger_v2] TREASURY DEFICIT LIMIT HIT: %s balance=%d "
                      "held=%d needs=%d limit=%d — payouts are blocked",
                      uid, snap["balance"], snap["held"], amt, TREASURY_MAX_DEFICIT)
            raise LedgerError(
                "treasury_insolvent", 409,
                f"{uid} is at {snap['balance']} and this debit of {amt} would "
                f"exceed the {TREASURY_MAX_DEFICIT} deficit limit")
        raise LedgerError("insufficient", 409,
                          f"{uid} has {snap['available']} available, needs {amt}")

    after = int(conn.execute(
        "SELECT CAST(coins AS INTEGER) FROM balances WHERE user_id=?", (uid,)
    ).fetchone()[0])

    if after < 0:
        # §3's scream, made audible: an ERROR log, a durable audit row in the
        # same transaction as the debit, and `insolvent: true` on every balance
        # read of this account until it is topped up.
        log.error("[ledger_v2] TREASURY INSOLVENT: %s is at %d after a debit of "
                  "%d — coins have been paid out that were never captured in. "
                  "A satellite has misallocated; nothing has been minted.",
                  uid, after, amt)
        _record(conn, service="core", action="treasury_insolvent", user_id=uid,
                delta=0, balance_after=after,
                reason=f"deficit {after} after debiting {amt}")
    return after


def _credit(conn: sqlite3.Connection, user_id: str, amount: int,
            *, counts_as_principal: bool = True) -> int:
    """Add `amount` coins. Returns the balance after.

    Credits have no precondition beyond the row existing, but the write is
    still a single UPDATE that is checked — a credit that silently did nothing
    (missing row) is how coins go missing on a transfer's second leg.

    A credit is NOT blocked by a freeze: freezing an account must stop money
    leaving it, not strand money owed to it. The endpoints still reject frozen
    *counterparties* before reaching here; this is the last-resort behaviour for
    a compensating credit (e.g. releasing a hold on a since-frozen account).
    """
    amt = int(amount)
    if amt <= 0:
        raise LedgerError("bad_amount", 400, "amount must be a positive integer")
    uid = str(user_id)
    _ensure_wallet(conn, uid)
    cur = conn.execute(
        "UPDATE balances "
        "   SET coins = CAST(coins AS INTEGER) + ?, "
        "       principal = CAST(principal AS INTEGER) + ?, "
        "       updated_at = datetime('now') "
        " WHERE user_id = ?",
        (amt, amt if counts_as_principal else 0, uid),
    )
    if cur.rowcount != 1:
        raise LedgerError("credit_failed", 500, f"no wallet row for {uid}")
    return int(conn.execute(
        "SELECT CAST(coins AS INTEGER) FROM balances WHERE user_id=?", (uid,)
    ).fetchone()[0])


# ══════════════════════════════════════════════════════════════════════════
# Holds: open → captured | released | expired
# ══════════════════════════════════════════════════════════════════════════

def _new_hold_id() -> str:
    return "h_" + secrets.token_hex(12)


def _hold_row(conn: sqlite3.Connection, hold_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ledger_holds WHERE hold_id = ?", (hold_id,)).fetchone()
    if row is None:
        raise LedgerError("hold_not_found", 404)
    return row


def _hold_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "hold_id": row["hold_id"],
        "service": row["service"],
        "user_id": row["user_id"],
        "amount": int(row["amount"]),
        "captured_amount": int(row["captured_amount"]),
        "released_amount": int(row["released_amount"]),
        "state": row["state"],
        "reason": row["reason"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def _check_hold_owner(row: sqlite3.Row, service: str) -> None:
    """§5: a hold belongs to the service that created it."""
    if row["service"] != service:
        raise LedgerError("forbidden_hold", 403,
                          "this hold belongs to another service")


def place_hold(service: str, user_id: str, amount: int, reason: str,
               expires_in: int, key: Optional[str] = None,
               *, idem: Optional[_Idem] = None) -> dict[str, Any]:
    """Reserve `amount` of a user's AVAILABLE balance. Coins do not move.

    Claim-first: the INSERT itself carries the availability test in its WHERE
    clause, so two concurrent holds on the same nearly-empty wallet cannot both
    succeed. `rowcount == 0` means nothing was written.
    """
    amt = int(amount)
    if amt <= 0:
        raise LedgerError("bad_amount", 400, "amount must be a positive integer")
    ttl = int(expires_in)
    if ttl < MIN_HOLD_SECONDS or ttl > MAX_HOLD_SECONDS:
        # §5: expires_in is REQUIRED. A hold with no expiry is a coin leak the
        # first time a satellite crashes mid-flow.
        raise LedgerError("bad_expiry", 400,
                          f"expires_in must be {MIN_HOLD_SECONDS}..{MAX_HOLD_SECONDS} seconds")

    uid = str(user_id)
    hold_id = _new_hold_id()
    expires_at = _iso(_utcnow() + timedelta(seconds=ttl))

    with _tx() as conn:
        _ensure_wallet(conn, uid)
        _assert_not_frozen(conn, uid)
        cur = conn.execute(
            "INSERT INTO ledger_holds "
            "  (hold_id, service, user_id, amount, reason, idempotency_key, expires_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ? "
            " WHERE ( SELECT CAST(b.coins AS INTEGER) FROM balances b WHERE b.user_id = ? ) "
            "     - ( SELECT COALESCE(SUM(h.amount - h.captured_amount - h.released_amount), 0) "
            "           FROM ledger_holds h WHERE h.user_id = ? AND h.state = 'open' ) "
            "     >= ?",
            (hold_id, service, uid, amt, reason[:200], key, expires_at, uid, uid, amt),
        )
        if cur.rowcount != 1:
            snap = _read_balance(conn, uid)
            raise LedgerError("insufficient", 409,
                              f"{uid} has {snap['available']} available, needs {amt}")
        _record(conn, service=service, action="hold", user_id=uid, delta=0,
                hold_id=hold_id, reason=reason, key=key)
        row = _hold_row(conn, hold_id)
        snap = _read_balance(conn, uid)
        out = _hold_payload(row)
        out.update({"balance": snap["balance"], "held": snap["held"],
                    "available": snap["available"]})
        # Same transaction as the reservation (S3).
        _finalize_idempotency(conn, idem, out)
    return out


def capture_hold(service: str, hold_id: str, amount: Optional[int] = None,
                 to_user: Optional[str] = None, key: Optional[str] = None,
                 reason: str = "", *, idem: Optional[_Idem] = None) -> dict[str, Any]:
    """Capture up to the held amount; the remainder is released atomically.

    §5: `capture(hold, amount=X)` where X < held captures X and releases the
    rest in the same transaction. That is how a losing pari-mutuel stake refunds
    cleanly on a voided outcome without a second call that could fail on its own.

    `to_user` omitted destroys the coins and therefore requires `wallet.mint` —
    which is why `estates` must always pass one, and it must be its own treasury.
    """
    with _tx() as conn:
        row = _hold_row(conn, hold_id)
        _check_hold_owner(row, service)

        want = int(row["amount"]) if amount is None else int(amount)
        if want < 0 or want > int(row["amount"]):
            raise LedgerError("bad_amount", 400,
                              f"capture must be 0..{int(row['amount'])}")

        # ── claim-first on the hold row itself ──────────────────────────────
        # Mark it terminal in one UPDATE gated on `state='open'`, and only then
        # move money. Act-then-mark double-pays if the process dies between.
        #
        # DO NOT REORDER (S1). The `ledger_balances_respect_holds` trigger
        # refuses any balance write that would drop a wallet below its OPEN hold
        # total. This capture is allowed to spend the coins THIS hold reserved
        # for exactly one reason: the UPDATE below has already moved the row out
        # of 'open', so by the time `_debit` fires the trigger the floor no
        # longer includes it. Debit first and the capture aborts against its own
        # escrow. That is the whole exemption — there is no trigger bypass.
        #
        # `settling` is the second half of that (N2). Ordering alone told the
        # trigger that THIS hold's reservation had gone; it did not tell it that
        # the debit which follows is the settlement OF that reservation. On an
        # over-committed wallet the two are indistinguishable from a shop
        # purchase, so the guard blocked every capture — hold A blocked by B and
        # B by A, with no exit but DB surgery. `settling` carries the amount
        # this transaction is about to debit against a reservation it has just
        # retired, so the guard judges the capture against its OWN hold instead
        # of the sum of the others. It is written and cleared inside one
        # transaction: a rollback discards it, and the clear below is
        # unconditional, so it cannot survive a commit. `escrow_settling_leaks`
        # checks that rather than asserting it in a comment.
        remainder = int(row["amount"]) - want
        claimed = conn.execute(
            "UPDATE ledger_holds "
            "   SET state = 'captured', captured_amount = ?, released_amount = ?, "
            "       settling = ?, terminal_key = ?, updated_at = datetime('now') "
            " WHERE hold_id = ? AND state = 'open'",
            (want, remainder, want, key, hold_id),
        )
        if claimed.rowcount != 1:
            # §5: a terminal hold is 409 hold_not_open — UNLESS the idempotency
            # key matches the call that terminated it, in which case the outer
            # replay store has already returned the original result. Reaching
            # here with a matching key means the key was reused across holds.
            raise LedgerError("hold_not_open", 409,
                              f"hold is '{row['state']}', not 'open'")

        uid = row["user_id"]
        result: dict[str, Any] = {"hold_id": hold_id, "captured": want,
                                  "released": remainder}

        if want > 0:
            # respect_holds=False: this hold is now terminal but the SUM() for
            # `held` is computed from open rows only, and the coins being taken
            # are precisely the ones it reserved.
            after = _debit(conn, uid, want, respect_holds=False)
            _record(conn, service=service, action="capture", user_id=uid,
                    delta=-want, balance_after=after, hold_id=hold_id,
                    counterparty=to_user, reason=reason or row["reason"], key=key)
            if to_user:
                dst = str(to_user)
                _ensure_wallet(conn, dst)
                dst_after = _credit(conn, dst, want, counts_as_principal=False)
                _record(conn, service=service, action="capture_in", user_id=dst,
                        delta=want, balance_after=dst_after, hold_id=hold_id,
                        counterparty=uid, reason=reason or row["reason"], key=key)
                result["to_user"] = dst
                result["to_balance"] = dst_after
            else:
                result["destroyed"] = want

        if remainder > 0:
            # No coin movement — the remainder was never debited, it was only
            # reserved. Releasing it is the state change, nothing else.
            _record(conn, service=service, action="release_remainder", user_id=uid,
                    delta=0, hold_id=hold_id, reason="partial capture remainder",
                    key=key)

        # The settlement has happened; the declaration must not outlive it (N2).
        # Unconditional, in the same transaction as the debit, so `settling != 0`
        # outside a transaction is impossible rather than merely unlikely.
        conn.execute("UPDATE ledger_holds SET settling = 0 WHERE hold_id = ?",
                     (hold_id,))

        snap = _read_balance(conn, uid)
        result.update({"user_id": uid, "state": "captured",
                       "balance": snap["balance"], "held": snap["held"],
                       "available": snap["available"]})
        # Same transaction as the debit and the credit (S3).
        _finalize_idempotency(conn, idem, result)
    return result


def release_hold(service: str, hold_id: str, key: Optional[str] = None,
                 reason: str = "", *, idem: Optional[_Idem] = None) -> dict[str, Any]:
    """Release an open hold in full. No coins move; the reservation ends."""
    with _tx() as conn:
        row = _hold_row(conn, hold_id)
        _check_hold_owner(row, service)
        claimed = conn.execute(
            "UPDATE ledger_holds "
            "   SET state = 'released', released_amount = amount, terminal_key = ?, "
            "       updated_at = datetime('now') "
            " WHERE hold_id = ? AND state = 'open'",
            (key, hold_id),
        )
        if claimed.rowcount != 1:
            raise LedgerError("hold_not_open", 409,
                              f"hold is '{row['state']}', not 'open'")
        _record(conn, service=service, action="release", user_id=row["user_id"],
                delta=0, hold_id=hold_id, reason=reason or row["reason"], key=key)
        snap = _read_balance(conn, row["user_id"])
        result = {"hold_id": hold_id, "user_id": row["user_id"], "state": "released",
                  "released": int(row["amount"]), "balance": snap["balance"],
                  "held": snap["held"], "available": snap["available"]}
        # Same transaction as the state change (S3).
        _finalize_idempotency(conn, idem, result)
    return result


def extend_hold(service: str, hold_id: str, expires_in: int) -> dict[str, Any]:
    """Push an open hold's expiry out. Only the owning service may do this."""
    ttl = int(expires_in)
    if ttl < MIN_HOLD_SECONDS or ttl > MAX_HOLD_SECONDS:
        raise LedgerError("bad_expiry", 400,
                          f"expires_in must be {MIN_HOLD_SECONDS}..{MAX_HOLD_SECONDS} seconds")
    new_expiry = _iso(_utcnow() + timedelta(seconds=ttl))
    with _tx() as conn:
        row = _hold_row(conn, hold_id)
        _check_hold_owner(row, service)
        claimed = conn.execute(
            "UPDATE ledger_holds SET expires_at = ?, updated_at = datetime('now') "
            " WHERE hold_id = ? AND state = 'open'",
            (new_expiry, hold_id),
        )
        if claimed.rowcount != 1:
            raise LedgerError("hold_not_open", 409,
                              f"hold is '{row['state']}', not 'open'")
        _record(conn, service=service, action="extend", user_id=row["user_id"],
                delta=0, hold_id=hold_id, reason=f"expires_at={new_expiry}")
        row = _hold_row(conn, hold_id)
    return _hold_payload(row)


def get_hold(service: str, hold_id: str) -> dict[str, Any]:
    row = _hold_row(_conn(), hold_id)
    _check_hold_owner(row, service)
    return _hold_payload(row)


def list_holds(service: str, user_id: str, state: str = "open") -> list[dict[str, Any]]:
    """This service's holds for one user. Never another service's."""
    sql = ("SELECT * FROM ledger_holds WHERE service = ? AND user_id = ?"
           + (" AND state = ?" if state != "all" else "")
           + " ORDER BY created_at DESC LIMIT 200")
    args: tuple[Any, ...] = ((service, str(user_id)) if state == "all"
                             else (service, str(user_id), state))
    return [_hold_payload(r) for r in _conn().execute(sql, args).fetchall()]


# ══════════════════════════════════════════════════════════════════════════
# Expiry sweep — per-row progress marker
# ══════════════════════════════════════════════════════════════════════════

def sweep_expired_holds(limit: int = SWEEP_BATCH, now: Optional[str] = None) -> int:
    """Release holds past `expires_at`. Returns how many were released.

    Rule 2, and S11 twice over. The progress marker here is the ROW STATE, not a
    cursor: the candidate query selects `state='open' AND expires_at <= now`, and
    each row's own transaction moves it to `expired`. A released row is out of
    the candidate set by construction, so a sweep killed halfway resumes on the
    next pass with exactly the rows it had not reached, and re-processes none of
    the ones it had.

    There WAS a `hold_sweep_cursor` written here, per row, and read nowhere —
    twice reviewed, twice surviving, and each round the docstring claimed more
    for it than the round before. It is deleted, and the migration deletes the
    dead key. A cursor would have been worse than useless
    on this query: `ORDER BY expires_at, hold_id` is not stable against holds
    that expire while the sweep runs, so `AND hold_id > cursor` — the
    optimisation that comment was inviting — would have skipped live holds and
    left them reserved past expiry.

    Each row is its own claim-first UPDATE gated on `state='open'`, so a second
    sweeper running concurrently loses the race and skips instead of double-
    releasing.
    """
    cutoff = now or _iso(_utcnow())
    conn = _conn()
    candidates = conn.execute(
        "SELECT hold_id, service, user_id, amount, reason FROM ledger_holds "
        " WHERE state = 'open' AND expires_at <= ? "
        " ORDER BY expires_at ASC, hold_id ASC LIMIT ?",
        (cutoff, int(limit)),
    ).fetchall()

    released = 0
    for row in candidates:
        try:
            with _tx() as txn:
                claimed = txn.execute(
                    "UPDATE ledger_holds "
                    "   SET state = 'expired', released_amount = amount, "
                    "       updated_at = datetime('now') "
                    " WHERE hold_id = ? AND state = 'open'",
                    (row["hold_id"],),
                )
                if claimed.rowcount != 1:
                    continue  # another sweeper or a live capture won it
                _record(txn, service=row["service"], action="expire",
                        user_id=row["user_id"], delta=0, hold_id=row["hold_id"],
                        reason=f"expired: {row['reason']}")
                # The progress marker IS this UPDATE: the row has left the
                # candidate set, in the same transaction as the release.
            released += 1
        except sqlite3.Error as exc:
            # One bad row must not abort the run; the next pass retries it
            # because it is still 'open'.
            log.warning("[ledger_v2] sweep failed on %s: %s", row["hold_id"], exc)
    return released


def escrow_settling_leaks() -> list[dict[str, Any]]:
    """N2. Holds left declaring an unfinished settlement. Must always be empty.

    `capture_hold` sets `settling` and clears it inside one transaction, so a
    non-zero value can only exist mid-capture — a reader outside that
    transaction never sees one, and a rollback discards it. A row here therefore
    means somebody added a second writer of `settling` that can commit without
    clearing it, and until it is cleared the escrow guard will let that user's
    balance dip by that much. This is the check that keeps the paragraph above
    `capture_hold`'s claim UPDATE from being merely a promise; the sweeper calls
    it once a minute and shouts.
    """
    rows = _conn().execute(
        "SELECT hold_id, user_id, state, settling, updated_at FROM ledger_holds "
        " WHERE settling <> 0"
    ).fetchall()
    return [dict(r) for r in rows]


async def _sweeper_loop(app: Any) -> None:  # pragma: no cover - background task
    import asyncio
    while True:
        try:
            await asyncio.sleep(60)
            n = sweep_expired_holds()
            if n:
                log.info("[ledger_v2] swept %d expired hold(s)", n)
            leaks = escrow_settling_leaks()
            if leaks:
                log.error("[ledger_v2] ESCROW GUARD WEAKENED: %d hold(s) still "
                          "declare an unfinished settlement: %s — the escrow "
                          "floor for those users is short by that much until it "
                          "is cleared", len(leaks), leaks[:5])
            # Idempotency retention is cheap; run it hourly-ish off the same tick.
            if int(_now()) % 3600 < 60:
                sweep_idempotency()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[ledger_v2] sweeper error: %s", exc)


# ══════════════════════════════════════════════════════════════════════════
# Freeze — core-side, checked by every money endpoint
# ══════════════════════════════════════════════════════════════════════════

def set_frozen(user_id: str, frozen: bool, reason: str = "", by: str = "core") -> dict[str, Any]:
    """Freeze/unfreeze a wallet. §8: freeze lives in core, not in bank.db.

    Osentar's existing `accounts.frozen` rows migrate up by calling this once
    per frozen account on first boot of v2 — a loop the bank owns, not this
    module, because only it knows which of its accounts are still frozen.
    """
    uid = str(user_id)
    with _tx() as conn:
        _ensure_wallet(conn, uid)
        conn.execute(
            "UPDATE balances SET frozen = ?, frozen_reason = ?, frozen_by = ?, "
            "       frozen_at = CASE WHEN ? THEN datetime('now') ELSE NULL END "
            " WHERE user_id = ?",
            (1 if frozen else 0, reason[:200] if frozen else None,
             by if frozen else None, 1 if frozen else 0, uid),
        )
        _record(conn, service="core", action="freeze" if frozen else "unfreeze",
                user_id=uid, delta=0, reason=reason)
        return _read_balance(conn, uid)


# ══════════════════════════════════════════════════════════════════════════
# Transfer and adjust
# ══════════════════════════════════════════════════════════════════════════

def transfer(service: str, from_user: str, to_user: str, amount: int,
             reason: str = "", key: Optional[str] = None,
             *, idem: Optional[_Idem] = None) -> dict[str, Any]:
    """Move coins between two wallets in ONE transaction.

    v1's transfer was two `run_on_bot_loop` hops with a compensating refund in
    between: if the process died after the debit and before the credit, the
    coins were gone AND the idempotency key stayed claimed, so the retry was
    treated as a duplicate. Here the debit and the credit are the same
    transaction — there is no window to die in, and no compensating path to get
    wrong.
    """
    amt = int(amount)
    if amt <= 0:
        raise LedgerError("bad_amount", 400, "amount must be a positive integer")
    src, dst = str(from_user), str(to_user)
    if src == dst:
        raise LedgerError("bad_accounts", 400, "cannot transfer to the same account")

    with _tx() as conn:
        _ensure_wallet(conn, src)
        _ensure_wallet(conn, dst)
        _assert_not_frozen(conn, src, dst)
        src_after = _debit(conn, src, amt)
        dst_after = _credit(conn, dst, amt, counts_as_principal=True)
        _record(conn, service=service, action="transfer_out", user_id=src, delta=-amt,
                balance_after=src_after, counterparty=dst, reason=reason, key=key)
        _record(conn, service=service, action="transfer_in", user_id=dst, delta=amt,
                balance_after=dst_after, counterparty=src, reason=reason, key=key)
        result = {
            "amount": amt,
            "from": _read_balance(conn, src),
            "to": _read_balance(conn, dst),
        }
        # THE S3 FIX, in one line: the completion record commits with the debit
        # and the credit. There is no longer a window in which the coins have
        # moved and the key still says `in_progress`.
        _finalize_idempotency(conn, idem, result)
        return result


def adjust(service: str, user_id: str, amount: int, reason: str,
           key: Optional[str] = None,
           *, idem: Optional[_Idem] = None) -> dict[str, Any]:
    """Mint (amount > 0) or destroy (amount < 0) coins. `wallet.mint` only.

    Scope is enforced at the handler; the reason is enforced here because an
    untagged mint is unauditable and §3 requires it non-empty.
    """
    amt = int(amount)
    if amt == 0:
        raise LedgerError("bad_amount", 400, "amount must be non-zero")
    if not (reason or "").strip():
        raise LedgerError("missing_reason", 400, "every mint must carry a reason")
    uid = str(user_id)
    with _tx() as conn:
        _ensure_wallet(conn, uid)
        _assert_not_frozen(conn, uid)
        if amt > 0:
            after = _credit(conn, uid, amt, counts_as_principal=True)
        else:
            after = _debit(conn, uid, -amt)
        _record(conn, service=service, action="adjust", user_id=uid, delta=amt,
                balance_after=after, reason=reason, key=key)
        result = {"applied": amt, **_read_balance(conn, uid)}
        # Same transaction as the mint/burn (S3).
        _finalize_idempotency(conn, idem, result)
        return result


# ══════════════════════════════════════════════════════════════════════════
# HTTP plumbing
# ══════════════════════════════════════════════════════════════════════════

def _require(scope: str) -> Callable:
    """Decorator: token → service → scope. Attaches `request['service']`."""
    def decorator(handler: Callable) -> Callable:
        async def wrapper(request: Any) -> Any:
            if not load_tokens():
                return _err("disabled", 503,
                            "no LEDGER_TOKEN_* configured on the server")
            service = _resolve_service(request)
            if not service:
                return _err("unauthorized", 401)
            if not has_scope(service, scope):
                # §3: unknown/ungranted scope is 403 forbidden_scope. This is
                # what makes "estates cannot mint" a server-side guarantee
                # rather than a satellite-side convention.
                return _err("forbidden_scope", 403,
                            f"service '{service}' lacks '{scope}'")
            request["service"] = service
            request["scope"] = scope
            try:
                return await handler(request)
            except LedgerError as exc:
                return _json(exc.payload(), exc.status)
            except Replay as rep:
                return _json(rep.body, rep.status)
            except Exception as exc:  # pragma: no cover
                log.exception("[ledger_v2] %s failed: %s",
                              getattr(handler, "__name__", "?"), exc)
                return _err("internal_error", 500)
        wrapper.__name__ = getattr(handler, "__name__", "wrapper")
        return wrapper
    return decorator


async def _body(request: Any) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _int_field(body: dict[str, Any], name: str, required: bool = True) -> Optional[int]:
    """Parse a coin amount as an INTEGER. No floats reach a money path.

    A value like 10.5 is rejected rather than silently rounded — a satellite
    sending a fraction has a bug upstream, and rounding it here hides that bug
    and creates the half-coin the invariant exists to prevent.
    """
    raw = body.get(name)
    if raw is None:
        if required:
            raise LedgerError("bad_amount", 400, f"missing {name}")
        return None
    if isinstance(raw, bool):
        raise LedgerError("bad_amount", 400, f"{name} must be an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw != int(raw):
            raise LedgerError("bad_amount", 400,
                              f"{name} must be a whole number of coins, got {raw}")
        return int(raw)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise LedgerError("bad_amount", 400, f"{name} must be an integer") from None


def _key_field(body: dict[str, Any], endpoint: str) -> str:
    """Extract and validate the caller-minted idempotency key.

    §6: the key is derived from the DOMAIN EVENT, so the same business action
    produces the same key however many times it is attempted. A per-attempt
    uuid4 is rejected outright on the endpoints in `UUID4_BANNED_ENDPOINTS`.

    That set used to be described here as "exactly the calls where a retry with
    a fresh key double-pays". It was not: it named capture, transfer and adjust
    and left out `stock.buy`/`stock.sell`, the one pair this module itself calls
    out-of-band (`_is_out_of_band`, `_stock_trade`) — the pair where the key is
    the ONLY refusal there is. So, per endpoint that reaches this function, in
    or out and why:

      BANNED — a fresh key moves money a second time and nothing else stops it
        `adjust`        mints or burns; a second call with a new key mints again.
        `transfer`      moves coins; there is no state that makes the second one
                        a no-op, and `from_user` may be a treasury.
        `hold.capture`  capture may be PARTIAL, so a second capture under a new
                        key is a legal further movement up to the unclaimed
                        remainder. `CHECK (captured + released <= amount)` bounds
                        the total; it does not make the second call a no-op.
        `stock.buy`     R5-B. The trade runs in `Restocker_main.exec_stock_trade`,
        `stock.sell`    outside any transaction of ours, and nothing in this
                        module or in the DB refuses a repeat: a fresh key takes a
                        fresh claim, the dispatch runs again, and both attempts
                        return a clean 200 that is not marked `replayed`. Worse
                        than the three above, not better: they at least have a
                        transaction and a row state behind them.

      NOT BANNED — and the reason is a mechanism, not an opinion
        `hold.release`  `release_hold`'s UPDATE carries `AND state = 'open'`, so
                        the second release — fresh key or not — matches no row
                        and raises `409 hold_not_open`. The hold's own state
                        machine is what makes it once-only; the key is not
                        load-bearing here. It also moves no coins (`delta=0`).
        `hold`          THE BORDERLINE ONE, left out on a judgement this docstring
                        will not pretend is free. A fresh key really does create a
                        second, independent hold. But a hold moves no coins
                        (`delta=0`); it can only become a payment through
                        `hold.capture`, which IS banned and needs a `hold_id`; and
                        it ends by itself, because `expires_in` is mandatory and
                        `sweep_expired_holds` collects it. The COST, named because
                        it is real: whichever of the two holds the caller lost the
                        response for is one it cannot name and so cannot release,
                        and the punter's `available` is short by that amount until
                        the TTL runs out — a TTL the caller chose, up to
                        `MAX_HOLD_SECONDS` (400 days) at the ceiling. That is a
                        reservation that self-heals, not a coin that left the
                        wallet, and this set is defined by the second. If a
                        satellite is ever seen minting per-attempt hold keys, put
                        `"hold"` in the set — nothing here depends on it staying
                        out. `ledger_client._require_key` treats `hold` as
                        non-terminal for this same reason, so the two agree.

      NEVER REACHES HERE — no idempotency claim at all
        `hold.extend`   `h_hold_extend` takes no key. It moves no coins and writes
                        an ABSOLUTE `expires_at`, so repeating it is not
                        cumulative — the second call sets the same value.
        the reads       `health`, `ping`, `balance`, `holds`, `hold.get`, `stocks`,
                        `portfolio` mutate nothing.
    """
    key = str(body.get("idempotency_key") or "").strip()
    if not key:
        raise LedgerError("missing_idempotency_key", 400,
                          "mint one from the domain event, e.g. "
                          "estates:market:77:payout:user:123")
    if len(key) > 200:
        raise LedgerError("bad_idempotency_key", 400, "key too long (max 200)")
    if endpoint in UUID4_BANNED_ENDPOINTS and _looks_like_uuid4(key):
        raise LedgerError(
            "bad_idempotency_key", 400,
            f"a per-attempt uuid4 is banned on {endpoint} — a retry that mints a "
            f"fresh key is a SECOND payment, not a repeat of the first, and "
            f"nothing downstream can tell them apart. Derive the key from the "
            f"domain event (e.g. estates:market:77:payout:user:123) so the same "
            f"business action always produces the same key.")
    return key


@contextmanager
def _idempotent(request: Any, endpoint: str, body: dict[str, Any]) -> Iterator[dict]:
    """Claim → run → store. Releases the claim if the body moved no money.

    Validate BEFORE entering this block: the claim must not be taken for a
    request that is about to be rejected, or a concurrent retry sees a
    success-shaped duplicate for an operation that never happened.

    `slot["idem"]` is the claim, and every money function in this module takes
    it as `idem=` and completes it inside its own transaction (S3). When that
    happened, `slot["idem"].body` is the response that was stored and it becomes
    the response returned, so what the caller gets and what a replay gets are
    the same bytes by construction. Only the `/stock/*` passthrough, which moves
    money outside this module, still falls through to `_complete_idempotency`.

    N1: which is why the claim for such an endpoint is taken with
    `applied_unknown = 1` (see `IN_BAND_ENDPOINTS`). The release below then
    deletes nothing until `_resolve_out_of_band` has been called — which, since
    R3-1, happens only on a definite refusal, and since R4-4 only on one this
    module positively recognised (`_classify_stock_result`) rather than on any
    answer that failed to look like a success. A handler that raises after
    dispatching a trade therefore cannot hand the key back, whether it raised
    before the bot loop answered or after.

    R3-1's accepted cost lives at the bottom of this function: on the
    passthrough, `_complete_idempotency` is the statement that both records the
    outcome and clears the unknown flag, so if it fails the claim stays
    `in_progress, applied_unknown=1` and the next retry is refused with
    `idempotency_unresolved` until a human looks. Both ways it can fail are
    logged at ERROR with the key and the wallet, because that log is the only
    thing standing between a stalled claim and a punter whose trade nobody ever
    checked.
    """
    service = request["service"]
    key = _key_field(body, endpoint)
    subject = _subject(body)
    claim_ts = _claim_idempotency(key, service, endpoint,
                                  _fingerprint(body, endpoint), subject)
    idem = _Idem(key, claim_ts, endpoint)
    slot: dict[str, Any] = {"key": key, "idem": idem, "status": 200, "body": None}
    try:
        yield slot
    except Exception:
        # Deletes nothing if the money transaction already marked it `done`, and
        # nothing at all if the claim is still `applied_unknown = 1`.
        _release_idempotency(key, claim_ts)
        raise
    if idem.body is not None:
        slot["body"] = idem.body
        slot["status"] = idem.status
    elif slot["body"] is None:
        _release_idempotency(key, claim_ts)
    else:
        # Only reachable on the /stock/* passthrough, whose trade happens outside
        # any transaction of ours.
        try:
            landed = _complete_idempotency(key, slot["body"], slot["status"], claim_ts)
        except Exception as exc:
            # R3-1. The trade COMMITTED on the bot loop and this is the write
            # that was to record it — SQLITE_BUSY past the busy_timeout, a full
            # disk, a dead handle. The claim keeps `applied_unknown = 1`, so no
            # retry may take it over or delete it, and the next one is refused
            # with `idempotency_unresolved`. That is the intended outcome, and
            # it is only safe if somebody is told.
            log.error("[ledger_v2] UNRECORDED OUT-OF-BAND TRADE: user=%s key=%s "
                      "endpoint=%s — the trade APPLIED but storing its result "
                      "failed (%s). The claim stays unresolved on purpose; a "
                      "retry will be refused with idempotency_unresolved until "
                      "somebody checks %s's stock ledger and either deletes the "
                      "key or leaves it. Do NOT re-run the trade blind.",
                      subject, key, endpoint, exc, subject)
            raise
        if not landed:
            # Say so loudly: the trade landed and the replay store did not
            # record it, because the claim was taken over or swept underneath us.
            log.error("[ledger_v2] UNRECORDED OUT-OF-BAND TRADE: user=%s key=%s "
                      "endpoint=%s completed but its idempotency claim was gone "
                      "— a retry of this key will NOT replay it. Check %s's "
                      "stock ledger before retrying.",
                      subject, key, endpoint, subject)


# ── handlers ──────────────────────────────────────────────────────────────

async def h_health(request: Any) -> Any:
    """PUBLIC. Reveals only that the ledger is mounted and which services exist."""
    return _json({
        "ok": True,
        "service": "restocker-ledger",
        "version": LEDGER_API_VERSION,
        "enabled": bool(load_tokens()),
        "services": enabled_services(),
        "ts": _now(),
    })


@_require(SCOPE_READ)
async def h_ping(request: Any) -> Any:
    service = request["service"]
    return _json({
        "ok": True, "service": service, "version": LEDGER_API_VERSION,
        "scopes": sorted(SERVICE_SCOPES.get(service, frozenset())),
        "treasury": SERVICE_TREASURY.get(service),
        "ts": _now(),
    })


@_require(SCOPE_READ)
async def h_balance(request: Any) -> Any:
    uid = (request.query.get("user_id") or "").strip()
    if not uid:
        return _err("missing_user_id", 400)
    snap = get_balance(uid)
    return _json({"ok": True, **snap})


@_require(SCOPE_MINT)
async def h_adjust(request: Any) -> Any:
    body = await _body(request)
    uid = str(body.get("user_id") or "").strip()
    amount = _int_field(body, "amount")
    reason = str(body.get("reason") or "").strip()
    if not uid:
        return _err("missing_user_id", 400)
    if not reason:
        return _err("missing_reason", 400, "every mint must carry a reason")
    with _idempotent(request, "adjust", body) as slot:
        result = adjust(request["service"], uid, int(amount or 0), reason,
                        slot["key"], idem=slot["idem"])
        slot["body"] = _ok(result)
    return _json(slot["body"], slot["status"])


@_require(SCOPE_TRANSFER)
async def h_transfer(request: Any) -> Any:
    """§7: `from_user` must be the service's own treasury or the acting user.

    Without this a compromised satellite token could sweep any wallet into its
    own treasury. `acting_user` is the user who pressed the button in Discord;
    the satellite asserts it, and the satellite's token is what makes that
    assertion trustworthy.
    """
    body = await _body(request)
    service = request["service"]
    src = str(body.get("from_user") or "").strip()
    dst = str(body.get("to_user") or "").strip()
    acting = str(body.get("acting_user") or "").strip()
    amount = _int_field(body, "amount")
    if not src or not dst:
        return _err("missing_accounts", 400)
    treasury = SERVICE_TREASURY.get(service, "")
    if src != treasury and src != acting:
        return _err("forbidden_source", 403,
                    f"from_user must be {treasury} or the acting_user")
    if src.startswith("treasury:") and src != treasury:
        return _err("forbidden_source", 403, "not this service's treasury")

    with _idempotent(request, "transfer", body) as slot:
        result = transfer(service, src, dst, int(amount or 0),
                          str(body.get("reason") or ""), slot["key"],
                          idem=slot["idem"])
        slot["body"] = _ok(result)
    return _json(slot["body"], slot["status"])


@_require(SCOPE_HOLD)
async def h_hold(request: Any) -> Any:
    body = await _body(request)
    uid = str(body.get("user_id") or "").strip()
    amount = _int_field(body, "amount")
    expires_in = _int_field(body, "expires_in")
    if not uid:
        return _err("missing_user_id", 400)
    if expires_in is None:
        return _err("bad_expiry", 400, "expires_in is required")
    with _idempotent(request, "hold", body) as slot:
        result = place_hold(request["service"], uid, int(amount or 0),
                            str(body.get("reason") or ""), int(expires_in),
                            slot["key"], idem=slot["idem"])
        slot["body"] = _ok(result)
    return _json(slot["body"], slot["status"])


@_require(SCOPE_HOLD)
async def h_hold_capture(request: Any) -> Any:
    body = await _body(request)
    service = request["service"]
    hold_id = str(body.get("hold_id") or "").strip()
    if not hold_id:
        return _err("hold_not_found", 404, "missing hold_id")
    amount = _int_field(body, "amount", required=False)
    to_user = str(body.get("to_user") or "").strip() or None

    # Destroying coins is minting in reverse and needs the mint scope; estates
    # has neither, which is precisely the "estates can misallocate but never
    # create" guarantee. Enforce before claiming a key.
    if to_user is None and not has_scope(service, SCOPE_MINT):
        return _err("forbidden_scope", 403,
                    "capture without to_user destroys coins and requires wallet.mint")
    if to_user and to_user.startswith("treasury:") and to_user != SERVICE_TREASURY.get(service):
        return _err("forbidden_scope", 403, "cannot capture into another service's treasury")

    with _idempotent(request, "hold.capture", body) as slot:
        result = capture_hold(service, hold_id, amount, to_user, slot["key"],
                              str(body.get("reason") or ""), idem=slot["idem"])
        slot["body"] = _ok(result)
    return _json(slot["body"], slot["status"])


@_require(SCOPE_HOLD)
async def h_hold_release(request: Any) -> Any:
    body = await _body(request)
    hold_id = str(body.get("hold_id") or "").strip()
    if not hold_id:
        return _err("hold_not_found", 404, "missing hold_id")
    with _idempotent(request, "hold.release", body) as slot:
        result = release_hold(request["service"], hold_id, slot["key"],
                              str(body.get("reason") or ""), idem=slot["idem"])
        slot["body"] = _ok(result)
    return _json(slot["body"], slot["status"])


@_require(SCOPE_HOLD)
async def h_hold_extend(request: Any) -> Any:
    body = await _body(request)
    hold_id = str(body.get("hold_id") or "").strip()
    expires_in = _int_field(body, "expires_in")
    if not hold_id:
        return _err("hold_not_found", 404, "missing hold_id")
    result = extend_hold(request["service"], hold_id, int(expires_in or 0))
    return _json({"ok": True, **result})


@_require(SCOPE_HOLD)
async def h_hold_get(request: Any) -> Any:
    hold_id = str(request.match_info.get("hold_id") or "").strip()
    if not hold_id:
        return _err("hold_not_found", 404)
    return _json({"ok": True, **get_hold(request["service"], hold_id)})


@_require(SCOPE_HOLD)
async def h_holds(request: Any) -> Any:
    uid = (request.query.get("user_id") or "").strip()
    if not uid:
        return _err("missing_user_id", 400)
    state = (request.query.get("state") or "open").strip()
    holds = list_holds(request["service"], uid, state)
    # Rule 8: an empty result is an empty list. No placeholder row.
    return _json({"ok": True, "user_id": uid, "holds": holds,
                  "held": get_balance(uid)["held"]})


# ── stocks passthrough ────────────────────────────────────────────────────

def _main() -> Any:
    import Restocker_main as m
    return m


def _dbmod() -> Any:
    import Restocker_db as db
    return db


@_require(SCOPE_STOCKS)
async def h_stocks(request: Any) -> Any:
    """List tradeable markets.

    v1's version returned `[]` unconditionally: it iterated `db.get_markets()`,
    which returns a DICT keyed by market_id, so `for mk in markets` yielded
    strings, `isinstance(mk, dict)` was always False and every market was
    skipped. Using `get_public_markets()` fixes it and skips the redundant
    per-row `get_market_shares` call.
    """
    db = _dbmod()
    out: list[dict[str, Any]] = []
    try:
        rows = db.get_public_markets() or {}
    except Exception as exc:
        log.warning("[ledger_v2] get_public_markets failed: %s", exc)
        rows = {}
    items = rows.items() if isinstance(rows, dict) else enumerate(rows)
    for mid, mk in items:
        info = mk if isinstance(mk, dict) else {}
        market_id = str(info.get("market_id") or mid)
        out.append({
            "market_id": market_id,
            "name": str(info.get("name") or market_id),
            "price": int(info.get("share_price") or 0),
            "shares_outstanding": int(info.get("shares_outstanding") or 0),
            "pe": float(info.get("pe_multiplier") or 0),
        })
    return _json({"ok": True, "markets": out})


@_require(SCOPE_STOCKS)
async def h_portfolio(request: Any) -> Any:
    uid = (request.query.get("user_id") or "").strip()
    if not uid:
        return _err("missing_user_id", 400)
    holdings = _dbmod().get_portfolio(uid) or []
    return _json({"ok": True, "user_id": uid,
                  "holdings": [dict(h) for h in holdings],
                  **get_balance(uid)})


#: R4-4. The `code` values from `Restocker_main.exec_stock_trade` that mean the
#: trade was REFUSED — core looked at the request, said no, and moved nothing.
#: Read off the documented result set (CORE_MONEY_PRIMITIVES.md §4:
#: buy → `ok | not_public | bad_shares | no_shares_available | insufficient_funds
#: | error | deduped`; sell → `ok | not_listed | bad_shares | insufficient_shares
#: | error | deduped`).
#:
#: This is an ALLOWLIST, for the same reason `IN_BAND_ENDPOINTS` is: clearing
#: `applied_unknown` is what lets the claim be deleted, so it must be decided by
#: a code this module recognises, never by "the answer I could not read was not a
#: success". Two documented codes are deliberately absent:
#:   `error`   — the engine's unclassified failure. It is raised from inside the
#:               trade as well as before it, so it does NOT prove nothing moved.
#:   `deduped` — the legacy engine's own dedupe. It means an EARLIER attempt
#:               applied this trade; that is the opposite of a refusal.
#: A code added to `exec_stock_trade` later and not added here is treated as
#: unknown — the safe direction, at the cost named in `_stock_trade`'s LIMIT.
DEFINITE_STOCK_REFUSALS: frozenset[str] = frozenset({
    "insufficient_funds", "no_shares_available", "insufficient_shares",
    "not_public", "not_listed", "bad_shares",
})

#: The three things a dispatch to the bot loop can mean. `unknown` is not a
#: failure mode; it is the honest reading of most answers.
STOCK_APPLIED = "applied"
STOCK_REFUSED = "refused"
STOCK_UNKNOWN = "unknown"


def _classify_stock_result(r: Any) -> tuple[str, Optional[str]]:
    """Classify one `exec_stock_trade` return as applied / refused / unknown.

    Returns `(verdict, code)`; `code` is the normalised `code` field when there
    was a readable one, else `None`.

    R4-4 — the whole point is that `refused` is POSITIVE. It requires a dict,
    a falsy `ok`, and a `code` this module recognises as a pre-trade rejection.
    `None`, `{}`, a dict whose `code` is `error`/`deduped`/absent/misspelled, a
    string, a list — anything this function cannot positively identify — is
    `unknown`, because a value the module cannot read says nothing about whether
    the coins moved. `run_on_bot_loop` has a 20s timeout over a synchronous core
    that cannot be cancelled, so "no answer over a committed trade" is a shape
    that really can occur.

    `applied` is deliberately the looser test (`ok` truthy, not `ok is True`):
    being strict there would turn a trade that DID happen into an operator page,
    and it cannot cause a double charge — an applied verdict stores the response
    and marks the key `done`, which blocks the retry rather than granting it.
    The asymmetry is intentional: strictness is spent where the mistake costs a
    second charge.
    """
    if not isinstance(r, dict):
        return STOCK_UNKNOWN, None
    raw = r.get("code")
    code = raw.strip().lower() if isinstance(raw, str) else None
    if r.get("ok"):
        return STOCK_APPLIED, code
    if code in DEFINITE_STOCK_REFUSALS:
        return STOCK_REFUSED, code
    return STOCK_UNKNOWN, code


async def _stock_trade(request: Any, side: str) -> Any:
    """Shared buy/sell path. Now checks `available`, not raw balance.

    LIMIT: `exec_stock_trade` lives in `Restocker_main`, is not transactional,
    and is safe only because every caller marshals through `run_on_bot_loop`.
    So the affordability test here is a PRE-CHECK, not a claim — a hold placed
    in the microseconds between the check and the trade can still let a buy
    dip into reserved funds. Closing that needs the guard moved inside
    `exec_stock_trade` itself, which is outside this module.

    N1: this is also the only endpoint whose money moves outside our
    transaction, so its idempotency claim is takeover-proof from the instant it
    is taken (`IN_BAND_ENDPOINTS`) and stays that way until the outcome has been
    RECORDED — not merely learned.

    R3-1 is the difference between those two. Round 3 cleared the flag the
    moment `run_on_bot_loop` returned, in a transaction of its own, and then
    built the payload (one more DB read) and let `_complete_idempotency` write
    `state='done'` in a third transaction. Between the second and the third the
    row read `in_progress, applied_unknown=0` over coins that had already moved
    — the exact state the flag exists to forbid — so a kill, a SQLITE_BUSY on
    that COMMIT or a raise from the payload read handed the key straight back
    and the user bought the same 7 shares twice.

    So the flag is now cleared on ONE path only: a definite refusal, where the
    coins provably did not move and the claim must be released or a corrected
    retry is blocked for thirty days. On success there is nothing to clear
    separately — `_complete_idempotency` writes `applied_unknown=0` in the same
    statement as `state='done'` (`:741-744`), which is atomic by construction.
    Everything between the dispatch and that statement stays "unknown", because
    that is what it is.

    R4-4 — WHAT COUNTS AS "a definite refusal". Round 4 answered that with
    `r = r or {}` and `if payload["ok"]`, i.e. "anything I could not read as a
    success". `None` read as a refusal: the flag was cleared, `_idempotent`
    deleted the claim, and the retry bought the same shares again with no wait
    (reproduced, `/tmp/r4/m1.py` case E). The clearing is now decided by a
    PROTOCOL — `_classify_stock_result` must positively recognise a dict with a
    falsy `ok` and a `code` in `DEFINITE_STOCK_REFUSALS` — and by nothing else.
    Three outcomes, and only one of them releases the key:

      applied  → store the response; `_complete_idempotency` clears the flag in
                 the same statement as `state='done'`.
      refused  → `_resolve_out_of_band` clears the flag; the claim is deleted so
                 a corrected retry works today rather than in thirty days.
      unknown  → NOTHING is cleared. The claim keeps `applied_unknown = 1` and an
                 ERROR names the key and the wallet. An answer that arrived but
                 could not be read gets `idempotency_unresolved` (409), not a 200
                 that reads as "nothing was taken"; a dispatch that RAISED keeps
                 the exception (500 `internal_error` from the wrapper) rather
                 than having it swallowed, and is logged identically. Either way
                 the claim survives: a retry inside `IDEMPOTENCY_STALE_SECONDS`
                 gets `idempotency_in_progress`, past it `idempotency_unresolved`,
                 and neither re-runs the trade.

    LIMIT, named because the round-4 pattern was docstrings that did not name
    theirs: `DEFINITE_STOCK_REFUSALS` is a hand-maintained copy of a code set
    that lives in `Restocker_main`, which this module cannot import to check.
    A refusal code added there and not here degrades to `unknown` — the trade is
    correctly not re-run, but its key needs an operator to release it. That is
    the direction the error is chosen to fall in; it is not free.
    """
    body = await _body(request)
    uid = str(body.get("user_id") or "").strip()
    mid = str(body.get("market_id") or "").strip()
    shares = _int_field(body, "shares")
    if not uid or not mid:
        return _err("missing_user_id", 400, "user_id and market_id are required")
    if not shares or shares <= 0:
        return _err("bad_shares", 400, "shares must be a positive integer")

    snap = get_balance(uid)
    if snap["frozen"]:
        return _err("frozen", 409, snap["frozen_reason"] or "")
    if side == "buy" and snap["available"] <= 0:
        return _err("insufficient", 409,
                    f"available is {snap['available']} ({snap['held']} held)")

    with _idempotent(request, f"stock.{side}", body) as slot:
        m = _main()
        key = slot["key"]
        try:
            r = await m.run_on_bot_loop(m.exec_stock_trade, side, uid, mid,
                                        int(shares), body.get("name"))
        except BaseException as exc:
            # R4-4. A timeout (`run_on_bot_loop` gives up at 20s over a sync core
            # that cannot be cancelled), a cancellation, a crash in the engine —
            # none of them say the trade did not commit. `BaseException` and not
            # `Exception` on purpose: `asyncio.CancelledError` is not an
            # `Exception`, and a cancelled dispatch is the likeliest shape of all
            # of these. Nothing is swallowed — the log is the only statement here
            # and the raise is unconditional. The claim keeps
            # `applied_unknown = 1`, so `_idempotent`'s release below is a no-op
            # and no retry can take it over; this log is the only thing that
            # tells anyone the wallet needs looking at.
            log.error("[ledger_v2] UNRESOLVED OUT-OF-BAND TRADE: user=%s key=%s "
                      "endpoint=stock.%s market=%s shares=%d — the dispatch to "
                      "the bot loop raised (%s: %s), which does NOT mean the "
                      "trade did not commit. The claim stays unresolved on "
                      "purpose; check %s's stock ledger and delete the key only "
                      "if the trade did not land. Do NOT re-run it blind.",
                      uid, key, side, mid, int(shares),
                      type(exc).__name__, exc, uid)
            raise
        verdict, code = _classify_stock_result(r)
        if verdict == STOCK_UNKNOWN:
            # R4-4. Silence, a shape we cannot read, or a failure code that does
            # not prove the trade was rejected before it moved anything. This is
            # exactly what `applied_unknown` exists to represent, so it stays
            # set: nothing here clears it, `_release_idempotency` will not match
            # the row, and the retry is refused rather than granted.
            log.error("[ledger_v2] UNRESOLVED OUT-OF-BAND TRADE: user=%s key=%s "
                      "endpoint=stock.%s market=%s shares=%d — the bot loop "
                      "answered %.200r, which is neither a success nor a "
                      "recognised refusal (%s), so the trade MAY have applied. "
                      "The claim stays unresolved on purpose; check %s's stock "
                      "ledger by hand and delete the key only if the trade did "
                      "not land. Do NOT re-run it blind.",
                      uid, key, side, mid, int(shares), r,
                      f"code={code!r}" if code else "no readable code", uid)
            raise LedgerError(
                "idempotency_unresolved", 409,
                "the trade was dispatched to the bot loop and its answer could "
                "not be identified as a refusal, so core cannot say whether it "
                "applied. This key will NOT be re-granted automatically — check "
                "the stock ledger before retrying.")
        payload = {
            "ok": verdict == STOCK_APPLIED,
            "code": r.get("code"),
            "message": r.get("msg"),
            "fill_price": r.get("fill"),
            "total": r.get("total"),
            "new_price": r.get("new_price"),
            **get_balance(uid),
        }
        if verdict == STOCK_APPLIED:
            # Only a successful trade earns a stored, replayable response, and
            # the flag stays SET until that store commits — `_complete_idempotency`
            # clears it in the same statement (R3-1). Nothing here may clear it
            # early: between here and that commit the coins have moved.
            slot["body"] = payload
            return _json(payload)
        # Refused, and POSITIVELY so: a dict, `ok` falsy, and a code in
        # `DEFINITE_STOCK_REFUSALS` — core looked at the request and rejected it,
        # so the coins provably did not move. This is the one place the outcome
        # is known AND final without a write of ours. Clearing the flag is what
        # lets `_idempotent`'s release below delete the claim, so a corrected
        # retry is not blocked for thirty days.
        _resolve_out_of_band(slot["idem"])
        return _json(payload, 409 if code == "insufficient_funds" else 200)


@_require(SCOPE_STOCKS)
async def h_stock_buy(request: Any) -> Any:
    return await _stock_trade(request, "buy")


@_require(SCOPE_STOCKS)
async def h_stock_sell(request: Any) -> Any:
    return await _stock_trade(request, "sell")


# ══════════════════════════════════════════════════════════════════════════
# v1 compatibility shim — /api/v1/bank/* mapped to the osentar service
# ══════════════════════════════════════════════════════════════════════════
#
# §9: the bank bot does not have to change on day one. These handlers speak v1's
# request and response shapes (`coins`/`principal`/`lp`, `deduped`, the
# "insufficient" string) on top of the v2 engine, and force the service to
# `osentar` regardless of which token was presented — an estates token on a
# /api/v1/bank/* path is rejected, not silently upgraded.
#
# NOTE ON MOUNTING: `bank_api.register_bank_routes(app)` already owns
# `/api/bank/*` and `/api/v1/bank/*`. aiohttp raises on a duplicate route, so
# `register_ledger_routes` skips any alias path already registered and logs it.
# Cutting over means REMOVING the `bank_api` registration, not running both.

def _v1_service(request: Any) -> str:
    if not load_tokens():
        raise LedgerError("disabled", 503, "no LEDGER_TOKEN_* configured")
    service = _resolve_service(request)
    if service != "osentar":
        raise LedgerError("unauthorized", 401,
                          "the /api/v1/bank/* alias is the osentar service only")
    return service


def _v1_balance_payload(user_id: str) -> dict[str, Any]:
    snap = get_balance(user_id)
    row = _conn().execute(
        "SELECT CAST(principal AS INTEGER) AS principal, CAST(lp AS INTEGER) AS lp "
        "FROM balances WHERE user_id=?", (str(user_id),)
    ).fetchone()
    return {
        "user_id": str(user_id),
        "coins": snap["balance"],
        "principal": int(row["principal"]) if row else 0,
        "lp": int(row["lp"]) if row else 0,
        # Additive only — v1 clients ignore unknown keys, v2-aware ones can use
        # these without moving off the old prefix.
        "held": snap["held"],
        "available": snap["available"],
        "frozen": snap["frozen"],
    }


def _v1_wrap(handler: Callable) -> Callable:
    async def wrapper(request: Any) -> Any:
        try:
            request["service"] = _v1_service(request)
            request["scope"] = "v1"
            return await handler(request)
        except LedgerError as exc:
            return _json(exc.payload(), exc.status)
        except Replay as rep:
            body = dict(rep.body)
            body["deduped"] = True  # v1's spelling of "replayed"
            return _json(body, rep.status)
        except Exception as exc:  # pragma: no cover
            log.exception("[ledger_v2] v1 alias error: %s", exc)
            return _err("Internal error.", 500)
    wrapper.__name__ = "v1_" + getattr(handler, "__name__", "wrapper")
    return wrapper


async def _v1_health(request: Any) -> Any:
    return _json({
        "ok": True, "service": "restocker-bank-api",
        # A FLOOR, not an equality target. `restocker_client` currently tests
        # `sv == "1.1"` and would take the bank offline against "2.0"; it must
        # be changed to a >= comparison BEFORE this ships (§9).
        "version": LEDGER_API_VERSION,
        "min_client_version": "1.1",
        "enabled": bool(load_tokens()), "ts": _now(),
    })


async def _v1_ping(request: Any) -> Any:
    return _json({"ok": True, "service": "restocker-bank-api",
                  "version": LEDGER_API_VERSION, "ts": _now()})


async def _v1_balance(request: Any) -> Any:
    uid = (request.query.get("user_id") or "").strip()
    if not uid:
        return _err("Missing user_id.", 400)
    return _json({"ok": True, **_v1_balance_payload(uid)})


async def _v1_adjust(request: Any) -> Any:
    body = await _body(request)
    uid = str(body.get("user_id") or "").strip()
    if not uid:
        return _err("Missing user_id.", 400)
    amount = _int_field(body, "amount")
    if not amount:
        return _err("amount must be non-zero.", 400)
    reason = str(body.get("reason") or "").strip() or "v1 adjust"
    key = str(body.get("idempotency_key") or "").strip()
    if not key:
        # v1 allowed a missing key. Synthesise a deterministic one from the
        # payload so an unkeyed retry of the SAME request still dedupes,
        # instead of v1's behaviour where no key meant "always new".
        # NB: the whole body is fingerprinted HERE on purpose. This is key
        # DERIVATION, not comparison — two v1 adjusts differing only in `reason`
        # must not collapse onto one synthetic key.
        key = "v1:adjust:" + _fingerprint(body)[:32]
        body = {**body, "idempotency_key": key}
    claim_ts = _claim_idempotency(key, "osentar", "adjust",
                                  _fingerprint(body, "adjust"), _subject(body))
    # The v1 response shape is not the v2 one, so the claim carries a body
    # builder. It runs INSIDE the money transaction (S3), which is also why
    # `_v1_balance_payload` reads the post-adjust figures correctly — it shares
    # this thread's connection.
    #
    # R3-6: `endpoint` is passed, and must be — `_finalize_idempotency`'s
    # declaration guard is `if idem.endpoint and ...`, so the default `""` skips
    # it silently and this path completes in-band without ever proving it is
    # allowed to. The claim above was already taken as `adjust`; this makes the
    # claim and the completion say the same word.
    idem = _Idem(key, claim_ts, "adjust", body_fn=lambda _r: {
        "ok": True, "user_id": uid, "applied": int(amount),
        **_v1_balance_payload(uid)})
    try:
        adjust("osentar", uid, int(amount), reason, key, idem=idem)
    except Exception:
        _release_idempotency(key, claim_ts)
        raise
    return _json(idem.body or {"ok": True, "user_id": uid, "applied": int(amount)})


async def _v1_transfer(request: Any) -> Any:
    body = await _body(request)
    src = str(body.get("from_user") or "").strip()
    dst = str(body.get("to_user") or "").strip()
    if not src or not dst:
        return _err("Missing from_user/to_user.", 400)
    if src == dst:
        return _err("Cannot transfer to the same account.", 400)
    amount = _int_field(body, "amount")
    if not amount or amount <= 0:
        return _err("amount must be positive.", 400)
    key = str(body.get("idempotency_key") or "").strip()
    if not key:
        # Whole-body fingerprint: derivation, not comparison. See _v1_adjust.
        key = "v1:transfer:" + _fingerprint(body)[:32]
        body = {**body, "idempotency_key": key}
    claim_ts = _claim_idempotency(key, "osentar", "transfer",
                                 _fingerprint(body, "transfer"), _subject(body))
    # R3-6: endpoint passed for the same reason as in `_v1_adjust` — an empty
    # one turns `_finalize_idempotency`'s in-band declaration guard off.
    idem = _Idem(key, claim_ts, "transfer", body_fn=lambda _r: {
        "ok": True, "amount": int(amount),
        "from": _v1_balance_payload(src), "to": _v1_balance_payload(dst)})
    try:
        transfer("osentar", src, dst, int(amount), str(body.get("reason") or ""),
                 key, idem=idem)
    except Exception:
        _release_idempotency(key, claim_ts)
        raise
    return _json(idem.body or {"ok": True, "amount": int(amount)})


V1_ROUTES: tuple[tuple[str, str, Callable], ...] = (
    ("get", "/health", _v1_health),
    ("get", "/ping", _v1_wrap(_v1_ping)),
    ("get", "/balance", _v1_wrap(_v1_balance)),
    ("post", "/adjust", _v1_wrap(_v1_adjust)),
    ("post", "/transfer", _v1_wrap(_v1_transfer)),
)

V2_ROUTES: tuple[tuple[str, str, Callable], ...] = (
    ("get", "/health", h_health),
    ("get", "/ping", h_ping),
    ("get", "/balance", h_balance),
    ("post", "/adjust", h_adjust),
    ("post", "/transfer", h_transfer),
    ("post", "/hold", h_hold),
    ("post", "/hold/capture", h_hold_capture),
    ("post", "/hold/release", h_hold_release),
    ("post", "/hold/extend", h_hold_extend),
    ("get", "/hold/{hold_id}", h_hold_get),
    ("get", "/holds", h_holds),
    ("get", "/stocks", h_stocks),
    ("get", "/portfolio", h_portfolio),
    ("post", "/stock/buy", h_stock_buy),
    ("post", "/stock/sell", h_stock_sell),
)


#: Prefixes that must never be shed by the dashboard's per-IP rate limiter.
#: A payout run is one HTTP call per winner; 200 winners at 120 req/min/IP means
#: the run rate-limits ITSELF, and `_request` does not retry a 429 (correctly —
#: it is an answer, not a connection failure), so rows park `failed` and staff
#: pay them by hand. Server-to-server ledger traffic is authenticated per
#: service and already serialised by SQLite's write lock; the 120/min limiter
#: exists for anonymous dashboard hits.
RATE_LIMIT_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/bank/",       # already exempt in Restocker_web._rate_limit_mw
    "/api/v1/bank/",    # the v1 alias mounted here — was NOT exempt
    "/api/v1/ledger/",  # v2 — was NOT exempt
)


def _is_rate_limit_exempt(path: str) -> bool:
    return str(path).startswith(RATE_LIMIT_EXEMPT_PREFIXES)


def _install_rate_limit_exemption(app: Any) -> str:
    """Wrap the app's rate-limit middleware so ledger paths skip it.

    `Restocker_web._rate_limit_mw` exempts the literal prefix `/api/bank/` only,
    so `/api/v1/ledger/*` was throttled at 120 req/min/IP and a bulk payout run
    throttled itself (finding at ledger_v2.py:1615-1618, and the proximate cause
    of S4's stranded hammer price).

    Editing `Restocker_web.py` is the tidier fix and is still worth doing —
    widen its check to `RATE_LIMIT_EXEMPT_PREFIXES`. This does it from here so
    that mounting the ledger is sufficient on its own and the two files cannot
    drift: the existing middleware is replaced, in place, by one that calls the
    handler directly for exempt paths and delegates everything else to the
    original. It is idempotent (a second call finds its own marker and stops)
    and it degrades to a loud warning rather than an exception if the middleware
    list is already frozen or has no rate limiter in it.

    Returns a one-word status for the log line: wrapped | already | absent |
    frozen | error.
    """
    try:
        middlewares = app.middlewares
    except Exception as exc:  # pragma: no cover - not an aiohttp Application
        log.warning("[ledger_v2] cannot inspect middlewares: %s", exc)
        return "error"

    if any(getattr(mw, "_ledger_v2_exempt", False) for mw in middlewares):
        return "already"
    if getattr(middlewares, "frozen", False):  # pragma: no cover
        log.warning("[ledger_v2] middlewares are frozen — register the ledger "
                    "BEFORE the app starts, or widen _rate_limit_mw by hand.")
        return "frozen"

    for index, original in enumerate(list(middlewares)):
        if "rate_limit" not in getattr(original, "__name__", "").lower():
            continue

        @web.middleware
        async def _ledger_exempt_rate_limit(request: Any, handler: Callable,
                                            _original: Callable = original) -> Any:
            if _is_rate_limit_exempt(request.path):
                return await handler(request)
            return await _original(request, handler)

        _ledger_exempt_rate_limit._ledger_v2_exempt = True  # type: ignore[attr-defined]
        try:
            middlewares[index] = _ledger_exempt_rate_limit
        except Exception as exc:  # pragma: no cover
            log.warning("[ledger_v2] could not replace %s: %s",
                        getattr(original, "__name__", "?"), exc)
            return "error"
        return "wrapped"

    log.info("[ledger_v2] no rate-limit middleware found — nothing to exempt.")
    return "absent"


def _existing_paths(app: Any) -> set[str]:
    paths: set[str] = set()
    for resource in app.router.resources():
        info = resource.get_info()
        p = info.get("path") or info.get("formatter")
        if p:
            paths.add(str(p))
    return paths


def register_ledger_routes(app: Any, *, mount_v1_alias: bool = True,
                           start_sweeper: bool = True) -> None:
    """Attach the v2 ledger to an existing aiohttp Application.

    Call it right beside `bank_api.register_bank_routes(app)` in
    `Restocker_web.start_webserver()`.

    Two operational notes:

    * `/api/v1/bank/*` is skipped path-by-path if `bank_api` already claimed it,
      because aiohttp raises on a duplicate route. Both can be mounted during a
      soak; the v2 alias only takes effect once the `bank_api` registration is
      removed, and the log line says which one is live.
    * `_rate_limit_mw` in `Restocker_web.py` exempts only the literal prefix
      `/api/bank/`, so `/api/v1/ledger/*` was throttled at 120 req/min/IP and a
      bulk payout loop rate-limited itself. `_install_rate_limit_exemption`
      below now wraps that middleware at registration time, so mounting the
      ledger is enough — see `RATE_LIMIT_EXEMPT_PREFIXES`. Widening the check in
      `Restocker_web.py` by hand is still welcome; both are idempotent.
    """
    if web is None:
        log.warning("[ledger_v2] aiohttp unavailable — ledger not registered.")
        return

    rl_state = _install_rate_limit_exemption(app)

    taken = _existing_paths(app)
    mounted = 0
    skipped: list[str] = []

    def add(prefix: str, method: str, path: str, handler: Callable) -> None:
        nonlocal mounted
        full = prefix + path
        if full in taken:
            skipped.append(full)
            return
        (app.router.add_get if method == "get" else app.router.add_post)(full, handler)
        taken.add(full)
        mounted += 1

    for method, path, handler in V2_ROUTES:
        add("/api/v1/ledger", method, path, handler)

    if mount_v1_alias:
        for prefix in ("/api/bank", "/api/v1/bank"):
            for method, path, handler in V1_ROUTES:
                add(prefix, method, path, handler)

    if start_sweeper:
        async def _on_startup(app_: Any) -> None:  # pragma: no cover
            import asyncio
            app_["ledger_sweeper"] = asyncio.create_task(_sweeper_loop(app_))

        async def _on_cleanup(app_: Any) -> None:  # pragma: no cover
            task = app_.get("ledger_sweeper")
            if task is not None:
                task.cancel()

        app.on_startup.append(_on_startup)
        app.on_cleanup.append(_on_cleanup)

    services = enabled_services()
    state = f"ENABLED ({', '.join(services)})" if services else "DISABLED (no LEDGER_TOKEN_*)"
    log.info("[ledger_v2] %d route(s) mounted, %d skipped — %s "
             "(rate-limit exemption: %s)", mounted, len(skipped), state, rl_state)
    if rl_state not in ("wrapped", "already", "absent"):
        log.warning("[ledger_v2] /api/v1/ledger/* is still rate limited at "
                    "120 req/min/IP — a bulk payout run WILL throttle itself. "
                    "Widen _rate_limit_mw in Restocker_web.py to %s.",
                    RATE_LIMIT_EXEMPT_PREFIXES)
    if skipped:
        log.info("[ledger_v2] skipped (already registered by bank_api): %s",
                 ", ".join(sorted(skipped)))
    print(f"💰  Ledger API v{LEDGER_API_VERSION} {state}: /api/v1/ledger/* "
          f"({mounted} routes"
          + (f", {len(skipped)} v1 alias paths left to bank_api)" if skipped else ")"))
