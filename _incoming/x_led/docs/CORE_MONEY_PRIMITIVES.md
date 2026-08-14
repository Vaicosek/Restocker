# Restocker core money primitives — reference for satellite builders

Scope: everything a satellite (Osentar, Estates) or the website needs to know about how
coins, stocks and idempotency actually work in `restocker.db` today, as of the uploaded
source. Line numbers are `file:line` against the read-only uploads.

> **Read this first:** `Restocker_main.py` was NOT provided. `add_coins`, `deduct_coins`,
> `exec_stock_trade`, `_do_stock_trade`, `run_on_bot_loop`, `NETWORK_SHARED_SECRET` and the
> land-exchange escrow all live there. Everything below about those is reconstructed from
> call sites in `bank_api.py` and `Restocker_web.py`. **Get `Restocker_main.py` before
> anyone writes the hold engine** — it is where the wallet write path actually is.

---

## 1. Connection, transactions, locking

`/mnt/user-data/uploads/RestockerLocal/Restocker_db.py:16-40`

```python
DB_PATH = Path("restocker.db")

_local = threading.local()


def _get_conn() -> sqlite3.Connection:                      # :20
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


@contextmanager
def db():                                                    # :31
    """Context manager — yields a connection, commits on success, rolls back on error."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
```

Facts that follow from this, and they matter:

- **One connection per thread**, cached forever on a `threading.local()`. The bot loop
  thread and the web-server thread each hold their own connection to the same file.
- **WAL + `busy_timeout=5000`.** Concurrent readers are fine; two writers serialise, and a
  writer that waits >5s raises `database is locked`.
- **`with db()` is NOT a nestable transaction.** There is no `BEGIN IMMEDIATE` anywhere in
  the file (grep for `BEGIN|isolation_level|IMMEDIATE|EXCLUSIVE` returns nothing). Python's
  default `isolation_level=""` opens a deferred transaction on the first DML and `db()`
  commits at exit. So an **inner** `with db()` inside an outer one commits the outer
  block's partial work. Every helper in `Restocker_db.py` opens its own `with db()`, which
  means *any composite operation built by calling two helpers is not atomic.*
- Deferred transactions also mean a read-then-write pair inside one `with db()` can still
  lose to a concurrent writer with `SQLITE_BUSY` at upgrade time rather than blocking.
- Cross-thread atomicity is currently achieved **not** by the DB but by funnelling every
  mutation onto the bot's event loop. `Restocker_web.py:3722-3731` says so explicitly:

  > "THREADING: the web server runs on its own OS thread and event loop. The trade engine
  > is only safe because every caller shares the bot's loop — its supply check and its
  > writes are not atomic. So every mutation goes through `run_on_bot_loop()`, which is what
  > keeps a web trade from interleaving with a Discord one."

  `bank_api.py` obeys the same rule: every write is `await m.run_on_bot_loop(m.<fn>, ...)`.
  **A hold engine must either join this convention or introduce real `BEGIN IMMEDIATE`
  transactions. It cannot quietly do its own `with db()` writes and be safe.**
- `backup_database(dest_path)` — `Restocker_db.py:1066`, uses `sqlite3`'s online backup API,
  WAL-safe.
- `init_db()` — `:943` — `conn.executescript(SCHEMA)` then `_migrate(conn)` (`:792`), which
  is a list of `ALTER TABLE ... ADD COLUMN` statements each wrapped in try/except. That is
  the pattern any new column must follow; a `CREATE TABLE IF NOT EXISTS` added to `SCHEMA`
  is a no-op on a deployed DB, so **new columns go in `_migrate`, new tables go in `SCHEMA`.**

---

## 2. Wallet reads and writes — every one of them

There are exactly **four** functions that touch the `balances` table.

| Function | Location | Signature |
|---|---|---|
| `get_balance` | `Restocker_db.py:954` | `get_balance(user_id: str) -> dict` |
| `set_balance` | `Restocker_db.py:962` | `set_balance(user_id: str, coins: float, principal: float = None, lp: float = None)` |
| `adjust_balance` | `Restocker_db.py:982` | `adjust_balance(user_id: str, delta: int, *, counts_as_principal: bool = True, reduce_principal: bool = True) -> tuple[int, int, int]` |
| `get_all_balances` | `Restocker_db.py:1024` | `get_all_balances() -> dict` — `{user_id: coins}` |

### `get_balance` — `:954`

```python
def get_balance(user_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM balances WHERE user_id=?", (str(user_id),)).fetchone()
        if row:
            return dict(row)
        return {"user_id": str(user_id), "coins": 0, "principal": 0, "lp": 0}
```

Never raises for an unknown user, never creates a row. Returns `coins`, `principal`, `lp`,
`updated_at`. **There is no `held`, no `available`, no `frozen`.**

### `adjust_balance` — `:982` — the only race-safe writer

```python
def adjust_balance(user_id: str, delta: int, *, counts_as_principal: bool = True,
                   reduce_principal: bool = True) -> tuple[int, int, int]:
    uid = str(user_id)
    d = int(delta or 0)
    with db() as conn:
        conn.execute(
            "INSERT INTO balances (user_id, coins, principal, lp) VALUES (?, 0, 0, 0) "
            "ON CONFLICT(user_id) DO NOTHING", (uid,))
        before = conn.execute("SELECT coins FROM balances WHERE user_id=?", (uid,)).fetchone()
        old_coins = int(before["coins"]) if before else 0
        if d > 0:
            conn.execute(
                "UPDATE balances SET coins = coins + ?, principal = principal + ?, "
                "updated_at = datetime('now') WHERE user_id = ?",
                (d, d if counts_as_principal else 0, uid))
        elif d < 0:
            amt = -d
            conn.execute(
                "UPDATE balances SET "
                "principal = CASE WHEN ? THEN MAX(0, principal - MIN(principal, MIN(?, coins))) "
                "ELSE principal END, "
                "coins = MAX(0, coins - ?), "
                "updated_at = datetime('now') WHERE user_id = ?",
                (1 if reduce_principal else 0, amt, amt, uid))
        row = conn.execute("SELECT coins, principal FROM balances WHERE user_id=?", (uid,)).fetchone()
        coins = int(row["coins"]); principal = int(row["principal"])
    return coins, principal, coins - old_coins
```

Returns `(coins_after, principal_after, applied_delta)`.

**The critical semantic: a debit CLAMPS at zero — `coins = MAX(0, coins - ?)` — it does not
fail.** Asking to remove 500 from a 300-coin wallet silently removes 300 and returns
`applied_delta = -300`. The only defence against an overdraw is the caller checking
`applied_delta`, or a pre-check. `bank_api.py` uses a pre-check (see §4), which is a TOCTOU
window. **Any hold implementation must use `applied_delta`, not the pre-check pattern.**

### `set_balance` — `:962`

Absolute set with `ON CONFLICT(user_id) DO UPDATE`. Read-modify-write from the caller's
perspective; unsafe under concurrency. Do not use it in a money path.

### The audit ledger — `Restocker_db.py:1031-1064`

```python
def record_coin_ledger(user_id: str, delta: int, balance_after: int, reason: str = "") -> None   # :1031
def coin_ledger_has(user_id: str, reason: str) -> bool                                            # :1042
def get_coin_ledger(user_id: str, limit: int = 20) -> list                                        # :1058
```

- `record_coin_ledger` is **best-effort, swallows every exception**, and truncates `reason`
  to 200 chars. It is not called by `adjust_balance` — the caller (in `Restocker_main.py`)
  must call it. So the ledger can silently diverge from `balances`.
- `coin_ledger_has` is the existing poor-man's idempotency for repairs, keyed on
  `(user_id, reason)`, and **fails CLOSED** (returns `True` on any exception, refusing to
  pay). Good instinct, wrong mechanism for cross-service work — see §5.

### The layer above: `add_coins` / `deduct_coins` (in the missing `Restocker_main.py`)

Reconstructed from `bank_api.py:214`, `:221`, `:260`, `:262`:

```python
coins, principal = await m.run_on_bot_loop(m.add_coins,    int(uid), amount, counts_as_principal=bool)
coins, principal = await m.run_on_bot_loop(m.deduct_coins, int(uid), need,   reduce_principal=bool)
```

- Both take an **`int` user id** (note: `int(uid)`, while the DB stores `user_id TEXT` — see
  §7, treasury ids break this).
- Both return `(coins, principal)`, a 2-tuple — they drop `adjust_balance`'s third element,
  `applied_delta`. **The overdraw signal is discarded at this layer.**
- `deduct_coins` takes a positive magnitude, not a signed delta.
- They presumably wrap `db.adjust_balance` + `db.record_coin_ledger`; nothing in the
  uploaded files calls `adjust_balance` directly (`grep -rn adjust_balance` across all
  uploads matches only its own definition).

`run_on_bot_loop(fn, *args, **kwargs)` is the cross-thread bridge: schedules a sync callable
on the bot's event loop and awaits the result. `Restocker_main._BOT_LOOP` is the loop object
(`Restocker_web.py:4444`).

---

## 3. DDL of every table holding coins, stocks, or idempotency

### Coins

`Restocker_db.py:46` — the wallet. **This is the one true coin store.**

```sql
CREATE TABLE IF NOT EXISTS balances (
    user_id         TEXT PRIMARY KEY,
    coins           REAL NOT NULL DEFAULT 0,
    principal       REAL NOT NULL DEFAULT 0,
    lp              REAL NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`Restocker_db.py:640` — the coin audit ledger.

```sql
CREATE TABLE IF NOT EXISTS coin_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_coin_ledger_user ON coin_ledger(user_id, id);
```

Note the mismatch: `balances.coins` is `REAL`, `coin_ledger.delta/balance_after` are
`INTEGER`. See §7.

Other coin pots (house money, not user wallets): `platform_balance` (`:216`),
`platform_balance_log` (`:221`), `land_balances` (`:442`), `market_shares.treasury_coins`
(added by migration, `:799`).

### Stocks

`Restocker_db.py:459`

```sql
CREATE TABLE IF NOT EXISTS market_shares (
    market_id           TEXT PRIMARY KEY REFERENCES markets(market_id),
    active              INTEGER NOT NULL DEFAULT 1,   -- 1 = publicly tradeable, 0 = delisted
    shares_outstanding  REAL NOT NULL DEFAULT 1000,
    pe_multiplier       REAL NOT NULL DEFAULT 12,
    share_price         REAL NOT NULL DEFAULT 0,
    listed_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_priced_at      TEXT,
    last_priced_month   TEXT
);
-- plus, via _migrate() at Restocker_db.py:799-801:
--   treasury_coins       REAL NOT NULL DEFAULT 0
--   dividend_pct         REAL
--   last_dividend_month  TEXT
```

`Restocker_db.py:470`

```sql
CREATE TABLE IF NOT EXISTS stock_holdings (
    user_id     TEXT NOT NULL,
    market_id   TEXT NOT NULL REFERENCES market_shares(market_id),
    shares      REAL NOT NULL DEFAULT 0,
    cost_basis  REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, market_id)
);
CREATE INDEX IF NOT EXISTS idx_stock_holdings_market ON stock_holdings(market_id);
```

`Restocker_db.py:481` / `:495` / `:505`

```sql
CREATE TABLE IF NOT EXISTS stock_trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    side            TEXT NOT NULL,                     -- 'buy' or 'sell'
    shares          REAL NOT NULL,
    price_per_share REAL NOT NULL,
    total_coins     REAL NOT NULL,
    traded_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stock_trade_log_market ON stock_trade_log(market_id);
CREATE INDEX IF NOT EXISTS idx_stock_trade_log_user   ON stock_trade_log(user_id);

CREATE TABLE IF NOT EXISTS stock_price_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    price       REAL NOT NULL,
    reason      TEXT,
    logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stock_price_log_market ON stock_price_log(market_id);

CREATE TABLE IF NOT EXISTS stock_limit_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    side            TEXT NOT NULL,
    shares          INTEGER NOT NULL,
    limit_price     REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    fill_price      REAL,
    fill_total      REAL,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_limit_orders_market ON stock_limit_orders(market_id, status);
CREATE INDEX IF NOT EXISTS idx_limit_orders_user   ON stock_limit_orders(user_id, status);
```

Also coin-adjacent: `bonds` (`:523`), `bond_holdings` (`:540`), `etf_holdings` (`:650`).

### Idempotency — the only one that exists

`bank_api.py:37-53`, created lazily by `_ensure_tables()`, **not in `SCHEMA`**:

```sql
CREATE TABLE IF NOT EXISTS bank_idempotency (
    key TEXT PRIMARY KEY,
    ts  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    action  TEXT NOT NULL,
    user_id TEXT,
    amount  REAL,
    reason  TEXT,
    extra   TEXT,
    ts      REAL NOT NULL
);
```

Both live in `restocker.db` (they use `_db().db()`). **`bank_idempotency` stores no response
and no request fingerprint** — just the key. That is enough to *suppress* a duplicate but
not enough to *replay* one, and not enough to detect key reuse with a different payload
(`409 idempotency_conflict` in the v2 contract has nothing to build on).

### Escrow-ish things that already exist (and are not escrow)

`Restocker_db.py:550` — a bookkeeping note, not a coin movement. Nothing in it debits a
wallet. Functions: `create_escrow` (`:1855`), `get_escrow` (`:1863`), `list_escrows`
(`:1869`), `update_escrow` (`:1878`).

```sql
CREATE TABLE IF NOT EXISTS escrow_deposits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    party       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'coins',
    value       REAL NOT NULL,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'held',   -- held / released / forfeited
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`Restocker_db.py:743` / `:781` — **the Land Exchange already exists in core**, and its
comment at `:736-742` is the single most important thing for Estates to read:

> "Escrow is NOT a separate ledger here: a bidder's coins are actually deducted
> (`core.deduct_coins`) the moment their bid is accepted and refunded (`core.add_coins`) the
> moment they're outbid or the listing is cancelled/expired — **the bidder's own `balances`
> row IS the hold.** See `cogs/land_exchange.py`."

`land_listings` carries `id, seller_id, kind, title, category, photos, market_id, land,
chunks, coords, description, image_url, winner_message, mode ('fixed'|'auction'), quality,
reserve, buy_now, current_bid, current_bidder, min_increment_pct, commission_pct,
listing_fee, starts_at, ends_at, anti_snipe_minutes, status (active/sold/cancelled/expired),
channel_id, message_id, sold_price, sold_to, created_at, updated_at, closed_at`, indexed on
`status` and `seller_id`. `land_bids` is `id, listing_id, bidder_id, amount, ts`, indexed on
`listing_id`.

**Estates cannot just build a fresh auction system.** There is a live one, in core's DB,
whose escrow model (debit-on-bid) is the *opposite* of the v2 hold model, and there is a
`cogs/land_exchange.py` we have not seen. Migration path for `land_listings`/`land_bids`
into `estates.db` + real holds is an open design question that must be settled before code.

---

## 4. `bank_api.py` — auth, response shape, error codes

**File:** `/mnt/user-data/uploads/RestockerLocal/bank_api.py`. Mounted from
`Restocker_web.start_webserver()` at `Restocker_web.py:4688-4692`:

```python
try:
    import bank_api
    bank_api.register_bank_routes(app)
except Exception as _e:
    print(f"⚠️  Bank API not registered: {_e}")
```

### Auth

- **Env var: `BANK_API_TOKEN`**, read **once at import time** — `bank_api.py:25`:
  `BANK_API_TOKEN = os.getenv("BANK_API_TOKEN", "").strip()`. Changing it requires a restart.
- **Header: `X-Bank-Token`**, constant-time compared — `:106-110`:

```python
def _authed(request) -> bool:
    if not BANK_API_TOKEN:
        return False
    supplied = (request.headers.get("X-Bank-Token") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, BANK_API_TOKEN)
```

- **One shared secret for all callers.** No service identity, no scopes. Anyone holding the
  token can mint coins via `/adjust`. v2's per-service tokens and the "estates cannot mint"
  guarantee require replacing `_authed` wholesale.
- `require_token` (`:113-126`) wraps every handler except `/health`: no token configured →
  `503`; bad token → `401`; any uncaught exception → logged and `500 "Internal error."`.
- **The `/api/bank/*` prefix is exempt from the web rate limiter** — `Restocker_web.py:4610`:
  `if not request.path.startswith("/api/bank/")`. Note this only exempts the legacy prefix;
  `/api/v1/bank/*` **is** rate-limited at 120 req/min/IP. Bulk payout loops will hit that.

### Response shape

Success: `{"ok": true, ...}`. Failure via `_err(message, status)` (`:102`):

```python
def _err(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": message}, status=status)
```

So the machine-readable error string is in `error`, and `restocker_client.py` sets
`RestockerError.code = data.get("error")` — i.e. the *message* doubles as the code. Reads on
a wallet return `_balance_payload` (`:140`):

```python
{"user_id": str, "coins": int, "principal": float, "lp": float}
```

### Endpoints and their existing error codes

Registered under **both** `/api/bank` and `/api/v1/bank` (`:421-426`). `BANK_API_VERSION =
"1.1"` (`:22`).

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health` | none | `{ok, service, version, enabled, ts}` |
| GET | `/ping` | token | |
| GET | `/balance?user_id=` | token | |
| GET | `/stocks` | token | **broken, see §6** |
| GET | `/portfolio?user_id=` | token | |
| POST | `/adjust` | token | |
| POST | `/transfer` | token | |
| POST | `/stock/buy` | token | |
| POST | `/stock/sell` | token | |

Error strings actually returned today:

- `503` `"Bank API disabled (no BANK_API_TOKEN set on server)."`
- `401` `"Unauthorized."`
- `500` `"Internal error."` / `"Transfer failed; sender refunded."`
- `400` `"Missing user_id."`, `"Missing from_user/to_user."`, `"Missing user_id/market_id."`,
  `"amount must be a number."`, `"amount must be non-zero."`, `"amount must be positive."`,
  `"shares must be an integer."`, `"shares must be positive."`,
  `"Cannot transfer to the same account."`
- `409` `"insufficient"` — the only lowercase, genuinely machine-readable one.
- `200` with `{"ok": true, "deduped": true, ...}` on an idempotency-key replay.
- Stock trades add a nested `code` field from `Restocker_main.exec_stock_trade`:
  buy → `ok | not_public | bad_shares | no_shares_available | insufficient_funds | error |
  deduped`; sell → `ok | not_listed | bad_shares | insufficient_shares | error | deduped`.
  Note these ride on **HTTP 200 with `ok: false`**, which `restocker_client._request`
  converts into a `RestockerError` whose `.code` is the *message*, not this `code`.

### Idempotency mechanics — `bank_api.py:57-83`

```python
def _claim_key(key: str) -> bool:
    if not key:
        return True                     # <-- no key supplied => always "new"
    _ensure_tables()
    with _db().db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bank_idempotency (key, ts) VALUES (?, ?)",
            (key, time.time()),
        )
        return cur.rowcount == 1


def _release_key(key: str) -> None:
    ...
    conn.execute("DELETE FROM bank_idempotency WHERE key=?", (key,))
```

This is genuinely claim-first (`INSERT OR IGNORE` on a `PRIMARY KEY`, atomic, concurrent
retries cannot both win) and it releases on rejection. Its limits:

1. **A duplicate returns a fresh balance read, not the original result.** `h_adjust:207`
   returns `{"ok": True, "deduped": True, **_balance_payload(uid)}` — no `applied`, no
   original figures. v2 promises byte-identical replay; nothing is stored to replay.
2. **Never expires.** No TTL sweep, no 30-day retention. The table grows forever.
3. **Claim happens before validation.** `h_adjust` claims at `:206`, then discovers
   insufficient funds at `:218` and releases at `:219`. There is a window where a concurrent
   identical retry sees "duplicate" and gets a success-shaped response for an operation that
   is about to be rejected.
4. **The client mints `uuid4()` per call** — `restocker_client.py:78-80`:
   `return uuid.uuid4().hex`, defaulted into `adjust()` and `transfer()`. This is exactly the
   defect LEDGER_API_v2 §6 calls out: safe for the client's own connection retry, unsafe for
   any retry above it. `stock_buy`/`stock_sell` send **no key at all**.
5. `EXPECTED_API_VERSION = "1.1"` with an equality test (`restocker_client.py:12, 93`):
   `return (sv == EXPECTED_API_VERSION, sv)`. A `2.0` server takes the bank offline. Fix
   before shipping core v2.

### Transfer is not atomic — `bank_api.py:255-267`

```python
if int(_db().get_balance(src).get("coins") or 0) < amount:
    _release_key(key); return _err("insufficient", 409)

await m.run_on_bot_loop(m.deduct_coins, int(src), amount, reduce_principal=True)
try:
    await m.run_on_bot_loop(m.add_coins, int(dst), amount, counts_as_principal=True)
except Exception as e:
    await m.run_on_bot_loop(m.add_coins, int(src), amount, counts_as_principal=True)
    ...
```

Two separate loop hops with a compensating refund in between. If the process dies between
the debit and the credit, **the coins are gone** — the refund never runs and the idempotency
key stays claimed, so a retry is treated as a duplicate. A real ledger needs debit+credit in
one transaction.

---

## 5. Stock trade entry points

- **HTTP:** `bank_api.h_stock_buy` / `h_stock_sell` → `m.exec_stock_trade(side, int(uid),
  market_id, shares, name)` returning `{ok, code, msg, fill, total, new_price}`
  (`bank_api.py:352`, `:389`).
- **Website:** `Restocker_web._handle_api_trade` (`:3722`) → `m._do_stock_trade(action, uid,
  mid, shares, name)` returning `{ok, msg, shares, fill, total, new_price}`. Same shape, a
  different function name, `uid` as **str** here vs **int** in `bank_api`. Also
  `m._do_bond_buy(uid, bond_id, units, name)`, `m._etf_invest(uid, coins, name)`,
  `m._etf_redeem(uid, units_or_"all", name)`.
- Both are `run_on_bot_loop`-wrapped. Neither takes an idempotency key.

Supporting DB helpers: `get_market_shares` (`:3870`), `get_public_markets` (`:3878`),
`get_all_market_shares` (`:3885`), `upsert_market_shares(market_id, **kwargs)` (`:3893`),
`get_holding(user_id, market_id)` (`:3951`), `get_portfolio(user_id)` (`:3960`),
`get_holders(market_id)` (`:3970`), `adjust_holding(user_id, market_id, delta_shares,
delta_cost_basis)` (`:3980`, "**Caller is responsible for checking that a sell doesn't take
shares negative**"), `log_stock_trade(...)` (`:3996`), `log_stock_price(...)` (`:4030`),
`get_treasury(market_id)` (`:4048`), `adjust_treasury(market_id, delta, allow_negative=True)`
(`:4056`, read-modify-write in one `with db()`, returns the actually-applied delta).

---

## 6. Website transactional surface (for the shared site)

- Session cookie **`vtm_sess`**, resolved by `_session_user(request)` (`Restocker_web.py:179`)
  → `{user_id, name, csrf, expires}`. Backed by in-memory `_SESSIONS` (`:74`) with an
  on-disk fallback so logins survive restarts. Server-side expiry enforced; sessions
  predating the `expires` field are grandfathered as valid.
- CSRF: `_csrf_ok(request)` (`:3302`) compares `X-CSRF-Token` against `sess["csrf"]`.
  Required on state-changing POSTs, not on GETs.
- Ownership: `_require_owner(request, market_id)` (`:3313`).
- Satellite-to-core relay auth is a **third** scheme: `_network_secret_ok` (`:4333`), header
  `X-Network-Secret` vs `Restocker_main.NETWORK_SHARED_SECRET`, guarding
  `/api/network/land/{listings,bid,buy,create,cancel,close,config}` — which already relay
  land bids from a partner Discord (`_handle_network_land_bid`, `:4418`).
- Rate limit: 120 req/min/IP over 60s, keyed on `X-Forwarded-For` first hop
  (`Restocker_web.py:4605-4627`).

So core already has **three** auth mechanisms (`X-Bank-Token`, `X-Network-Secret`,
`vtm_sess` + CSRF). v2 adds a fourth unless it absorbs the first two.

---

## 7. What makes adding `holds` + an available-balance check hard

Ranked by how much they will hurt.

1. **`Restocker_main.py` is missing.** The wallet write path, the trade engine, the land
   escrow and `run_on_bot_loop` are all in it. Nobody can write `hold`/`capture`/`release`
   correctly without it. **Blocker.**

2. **`balances.coins` is `REAL`, not `INTEGER`** (`Restocker_db.py:48`). Money is stored as a
   float, in direct tension with rule 9. `adjust_balance` casts to `int` on the way out, and
   `_balance_payload` does `int(b.get("coins") or 0)` (`bank_api.py:144`) — so a fractional
   balance is *truncated on read* while the stored value keeps the fraction. Once
   `available = balance - held` exists, that truncation becomes a systematic off-by-one that
   users will find. `market_shares.share_price`, `stock_holdings.shares`,
   `land_listings.current_bid`, `bank_audit.amount` are all REAL too. A `holds.amount INTEGER`
   sitting next to a `REAL` balance will produce comparisons that are right 999 times and
   wrong once. **Decide now: migrate `balances.coins` to INTEGER, or define holds as REAL and
   accept it.**

3. **Debits clamp at zero instead of failing** (`adjust_balance:1013`, `coins = MAX(0, coins - ?)`).
   Every existing overdraw guard is a *pre-check* in the caller
   (`bank_api.py:217-220`, `:255-257`) — read balance, compare, then debit in a separate
   transaction. That is a TOCTOU race today, and it becomes a worse one with holds, because
   the check must become `coins - SUM(open holds) >= amount` across two tables. The correct
   shape is a single conditional UPDATE that fails if it doesn't win the row:
   `UPDATE balances SET coins = coins - :amt WHERE user_id = :uid AND coins - :held >= :amt`
   and then check `rowcount`. That is claim-first. Nothing in the codebase does this yet.

4. **`with db()` does not nest and there is no `BEGIN IMMEDIATE`.** Placing a hold is
   inherently two writes (insert the hold row, verify available) that must be one
   transaction. Composing existing helpers cannot achieve that — each opens and commits its
   own. A hold engine needs either new bottom-level functions that do everything inside one
   `with db()`, or a real `_tx()` helper issuing `BEGIN IMMEDIATE`. Adding the latter is the
   cleaner fix but changes locking behaviour for every existing caller.

5. **Atomicity is currently a threading convention, not a database property.** Correctness
   depends on every writer going through `run_on_bot_loop`. A hold API called from the web
   thread, or from a sweep task, that writes directly will silently break the invariant that
   makes the trade engine safe. Either holds go through the loop too (and inherit its
   serialisation, and its single-point-of-failure), or the DB layer gets real transactions.

6. **`user_id` type is inconsistent.** DB column is `TEXT`; `bank_api` does `int(uid)` before
   calling `add_coins`/`deduct_coins` (`:214`, `:221`, `:260`, `:262`); the web path passes
   `str`. v2 requires `treasury:osentar` / `treasury:estates` as **wallet rows with
   non-numeric ids** — `int("treasury:estates")` raises `ValueError`. **Every `int(uid)` in
   `bank_api.py` and in `Restocker_main.add_coins`/`deduct_coins` must go before treasuries
   can exist.** This is a small change with a large blast radius; find every call site.

7. **`bank_idempotency` stores no response and no payload hash, and never expires.** v2 needs
   `(key, service, request_fingerprint, response_json, created_at)` with a 30-day sweep, to
   deliver replay and `409 idempotency_conflict`. The existing table cannot be extended into
   that shape by `_migrate` alone because it is created ad-hoc in `bank_api._ensure_tables`
   rather than in `SCHEMA` — the two creation paths must be unified first.

8. **Single shared token, no scopes.** "Estates cannot mint" is unenforceable until
   `_authed` returns a service identity. Today any token holder can call `/adjust`.

9. **Freeze lives in the wrong database.** `bank_db.py` `accounts.frozen / frozen_reason /
   frozen_at`, with `set_frozen` at `bank_db.py:170`. Core has no freeze concept at all, so a
   frozen user can trade stocks and (once Estates ships) bid on land. Migrating it up means
   new columns on `balances` and a check in every money endpoint.

10. **`land_listings`/`land_bids` already implement debit-on-bid escrow in core**
    (`Restocker_db.py:736-742`), driven by `cogs/land_exchange.py` which we do not have.
    Estates' auction flow is a rewrite of a live system, not a greenfield build, and the two
    escrow models cannot coexist on the same listings.

11. **Sweeping expired holds needs a per-row progress marker.** There is no scheduler
    primitive in `Restocker_db.py` for this — `balance_meta` (`:54`, `get_balance_meta` at
    `:1080`, `set_balance_meta` at `:1086`) is the closest thing, a `key TEXT PRIMARY KEY /
    value TEXT` store. Per rule 2 the sweep must advance its marker per released hold, not
    after the loop.

12. **`coin_ledger` is written best-effort by the caller, not by `adjust_balance`**
    (`record_coin_ledger` swallows all exceptions, `:1031`). The ledger is therefore not a
    reliable reconstruction of `balances`. If holds are to be auditable, the ledger write
    must move inside the same transaction as the balance write.

---

## 8. Confirmed live bug: `GET /api/v1/bank/stocks` always returns `[]`

`bank_api.py:282-293`:

```python
markets = db.get_markets() if hasattr(db, "get_markets") else []
...
for mk in (markets or []):
    mid = mk.get("id") or mk.get("market_id") if isinstance(mk, dict) else None
    if not mid:
        continue
```

`Restocker_db.get_markets()` (`:1189`) returns a **dict** `{market_id: {...}}`. Iterating a
dict yields its **keys** — strings. `isinstance(mk, dict)` is therefore `False` for every
iteration, `mid` is `None`, and every market is skipped. The endpoint returns
`{"ok": true, "markets": []}` unconditionally, and the bank's `/invest list` shows nothing.

Fix: `for mid, mk in (markets or {}).items():`, or use `db.get_public_markets()` (`:3878`)
which already filters `active=1` and skips the redundant `get_market_shares` call per row.

Two smaller things worth fixing in the same pass:

- `h_adjust` claims the idempotency key *before* the insufficient-funds check
  (`bank_api.py:206` vs `:218`), so a concurrent retry can receive a `deduped: true` success
  for a request that is being rejected. Validate, then claim.
- `h_transfer`'s refund path (`:264`) credits the sender with
  `counts_as_principal=True` regardless of how the original debit was booked, so a refunded
  transfer can inflate the sender's `principal`.
