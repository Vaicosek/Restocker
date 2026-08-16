"""
Proof for hub_web.py, driven through a real aiohttp test client.

The four things the build had to prove:
  1. a logged-out request is refused                        -> test_logged_out_page_refused
  2. a logged-in page renders REAL figures                  -> test_logged_in_page_renders_real_figures
  3. a POST with a body-supplied user id is ignored+logged  -> test_body_supplied_user_id_ignored_and_logged
  4. a replayed POST returns the ORIGINAL result            -> test_replayed_post_returns_original_result

The stubs below stand in for the bot, but the DATA is real SQLite: balances and
ledger_holds are actual rows, and the figures asserted on the page are the ones
those rows imply. A test that asserted on hardcoded HTML would prove nothing
about whether the page reads the ledger.

Run:  python3 -m pytest test_hub_web.py -q
"""

from __future__ import annotations

import os
import sqlite3
import sys
import types

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# ══════════════════════════════════════════════════════════════════════════
# A real SQLite ledger with real rows.
# ══════════════════════════════════════════════════════════════════════════

USER_ID = "205000000000000001"
USER_NAME = "GreyHames"
OTHER_ID = "205000000000000999"

BALANCE = 94900          # coins in the wallet
HOLD_A = 30000           # Osentar loan collateral
HOLD_B = 16700           # Estates lot escrow
HELD = HOLD_A + HOLD_B   # 46,700
AVAILABLE = BALANCE - HELD   # 48,200

MARKET_ID = "greyhames"
SHARE_PRICE = 1240.0
SHARES_OUT = 1000.0
MY_SHARES = 18.0


def _build_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE balances (
            user_id TEXT PRIMARY KEY, coins REAL DEFAULT 0, principal REAL DEFAULT 0,
            lp REAL DEFAULT 0, frozen INTEGER DEFAULT 0, frozen_reason TEXT,
            frozen_by TEXT, frozen_at TEXT
        );
        CREATE TABLE ledger_holds (
            hold_id TEXT PRIMARY KEY, service TEXT NOT NULL, user_id TEXT NOT NULL,
            amount INTEGER NOT NULL, captured_amount INTEGER DEFAULT 0,
            released_amount INTEGER DEFAULT 0, state TEXT NOT NULL, reason TEXT,
            created_at TEXT, expires_at TEXT
        );
    """)
    conn.execute("INSERT INTO balances (user_id, coins) VALUES (?,?)", (USER_ID, BALANCE))
    conn.execute("INSERT INTO balances (user_id, coins) VALUES (?,?)", (OTHER_ID, 500))
    conn.executemany(
        "INSERT INTO ledger_holds (hold_id, service, user_id, amount, state, reason, "
        "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("h1", "osentar", USER_ID, HOLD_A, "open", "Loan collateral",
             "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z"),
            ("h2", "estates", USER_ID, HOLD_B, "open", "Lot 41 bid escrow",
             "2026-08-10T00:00:00Z", "2026-08-20T00:00:00Z"),
            # A released hold must NOT count towards held.
            ("h3", "estates", USER_ID, 9999, "released", "Lot 12 outbid",
             "2026-07-01T00:00:00Z", None),
            # Another user's hold must never appear on this user's strip.
            ("h4", "osentar", OTHER_ID, 777, "open", "Someone else's hold",
             "2026-08-01T00:00:00Z", None),
        ],
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════
# Stubs for the bot modules hub_web imports lazily.
# ══════════════════════════════════════════════════════════════════════════

class Calls:
    """Counts what the engine was actually asked to do."""

    def __init__(self):
        self.trades: list[tuple] = []


def _install_stubs(db_path: str, calls: Calls, sessions: dict) -> None:
    # ---- ledger_v2 ------------------------------------------------------
    lg = types.ModuleType("ledger_v2")

    def _conn():
        c = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    def get_balance(user_id):
        c = _conn()
        row = c.execute(
            "SELECT CAST(coins AS INTEGER) AS coins, COALESCE(frozen,0) AS frozen, "
            "frozen_reason FROM balances WHERE user_id=?", (str(user_id),)).fetchone()
        bal = int(row["coins"]) if row else 0
        held = int(c.execute(
            "SELECT COALESCE(SUM(amount - captured_amount - released_amount),0) "
            "FROM ledger_holds WHERE user_id=? AND state='open'", (str(user_id),)
        ).fetchone()[0] or 0)
        return {"user_id": str(user_id), "balance": bal, "held": held,
                "available": bal - held, "frozen": bool(row["frozen"]) if row else False,
                "frozen_reason": row["frozen_reason"] if row else None}

    lg._conn = _conn
    lg.get_balance = get_balance
    sys.modules["ledger_v2"] = lg

    # ---- Restocker_db ---------------------------------------------------
    db = types.ModuleType("Restocker_db")
    db.DB_PATH = db_path

    db.get_market_shares = lambda mid: (
        {"share_price": SHARE_PRICE, "shares_outstanding": SHARES_OUT, "active": 1}
        if mid == MARKET_ID else None)
    db.get_portfolio = lambda uid: (
        [{"market_id": MARKET_ID, "shares": MY_SHARES, "cost_basis": 20000.0}]
        if str(uid) == USER_ID else [])
    db.get_holders = lambda mid: [{"user_id": USER_ID, "shares": MY_SHARES}]
    db.get_holding = lambda uid, mid: (
        {"shares": MY_SHARES, "cost_basis": 20000.0}
        if str(uid) == USER_ID and mid == MARKET_ID else None)
    db.get_config = lambda key, default=None: None
    db.get_csn_daily_sales = lambda mid, days=30: []
    db.get_hive_ledger_months = lambda mid: {}
    sys.modules["Restocker_db"] = db

    # ---- Restocker_main -------------------------------------------------
    m = types.ModuleType("Restocker_main")

    def _quote_trade(price, shares, shares_out, side):
        price, shares, shares_out = float(price), float(shares), float(shares_out)
        if price <= 0 or shares_out <= 0:
            return round(price, 2), round(price, 2)
        frac = 0.5 * shares / shares_out
        sign = 1.0 if side == "buy" else -1.0
        new_mid = max(1.0, price * (1.0 + sign * frac))
        avg = (price + new_mid) / 2.0
        fill = max(1.0, avg * (1.0 + sign * 0.02))
        return round(fill, 2), round(new_mid, 2)

    def _do_stock_trade(side, user_id, market_id, shares, name=None):
        calls.trades.append((side, str(user_id), market_id, int(shares), name))
        fill, new_mid = _quote_trade(SHARE_PRICE, shares, SHARES_OUT, side)
        total = int(round(fill * shares))
        return {"ok": True, "code": "ok", "side": side, "shares": int(shares),
                "fill": fill, "total": total, "new_price": new_mid,
                "msg": f"Bought {shares} shares at {fill}."}

    async def run_on_bot_loop(fn, *args, _timeout=20.0, **kwargs):
        return fn(*args, **kwargs)

    m._quote_trade = _quote_trade
    m._do_stock_trade = _do_stock_trade
    m.run_on_bot_loop = run_on_bot_loop
    m._owner_markets_for_user = lambda uid: []
    sys.modules["Restocker_main"] = m

    # ---- Restocker_web --------------------------------------------------
    w = types.ModuleType("Restocker_web")
    w._SESSIONS = sessions

    def _session_user(request):
        tok = request.cookies.get("vtm_sess")
        return sessions.get(tok) if tok else None

    w._session_user = _session_user
    w._load_sessions = lambda: dict(sessions)
    w._save_sessions = lambda s: True
    w._market_ticker = lambda mid: "VTEC" if mid == MARKET_ID else mid.upper()[:4]
    w._load_markets = lambda: {MARKET_ID: {"name": "V Tech Industries"}}
    w._load_inventory_data = lambda: {"markets": []}
    w._load_stock_data = lambda: {"markets": [{
        "mid": MARKET_ID, "name": "V Tech Industries", "ticker": "VTEC",
        "price": SHARE_PRICE, "shares": SHARES_OUT, "mcap": SHARE_PRICE * SHARES_OUT,
        "pct": 2.4, "change": 29.0,
        "history": [{"t": "2026-08-0%d" % i, "price": 1200.0 + i * 5} for i in range(1, 9)],
        "holders_count": 1, "top_holders": [],
    }]}
    sys.modules["Restocker_web"] = w


# ══════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "restocker.db")
    _build_db(db_path)
    calls = Calls()
    sessions: dict = {}
    _install_stubs(db_path, calls, sessions)

    monkeypatch.setenv("HUB_DB_PATH", db_path)
    monkeypatch.setenv("HUB_INSECURE_COOKIES", "1")

    for mod in ("hub_web",):
        sys.modules.pop(mod, None)
    import hub_web
    hub_web.reset_connections()

    token = "test-session-token"
    sessions[token] = {"user_id": USER_ID, "name": USER_NAME, "csrf": "test-csrf",
                       "expires": 9_999_999_999}

    return types.SimpleNamespace(hub=hub_web, db_path=db_path, calls=calls,
                                 sessions=sessions, token=token)


@pytest_asyncio.fixture()
async def client(env):
    app = web.Application()
    env.hub.register_hub_routes(app)
    server = TestServer(app)
    cl = TestClient(server)
    await cl.start_server()
    yield cl
    await cl.close()


def _login(cl, env):
    cl.session.cookie_jar.update_cookies({"vtm_sess": env.token})


# ══════════════════════════════════════════════════════════════════════════
# 1. Logged out is refused.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_logged_out_page_refused(client, env):
    r = await client.get("/hub/markets")
    body = await r.text()
    assert r.status == 401
    assert "Sign in" in body
    # No section content and no figures leaked into the refusal.
    assert "VTEC" not in body
    assert "48,200" not in body


@pytest.mark.asyncio
async def test_logged_out_api_refused(client, env):
    for path in ("/hub/api/me", "/hub/api/markets"):
        r = await client.get(path)
        assert r.status == 401, path


@pytest.mark.asyncio
async def test_logged_out_trade_post_refused(client, env):
    r = await client.post(f"/hub/markets/{MARKET_ID}/trade",
                          json={"side": "buy", "shares": 1, "idempotency_key": "x"})
    assert r.status == 401
    assert env.calls.trades == []


# ══════════════════════════════════════════════════════════════════════════
# 2. Logged in renders real figures.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_logged_in_page_renders_real_figures(client, env):
    _login(client, env)
    r = await client.get("/hub/markets")
    body = await r.text()
    assert r.status == 200

    # Money strip, computed server-side from the rows in the ledger.
    assert f"{AVAILABLE:,}" in body                 # 48,200 = 94,900 - 46,700
    assert f"{HELD:,}" in body                      # 46,700

    # The drawer NAMES what is holding the coins, with real service names.
    assert "Loan collateral" in body
    assert "Osentar Bank" in body
    assert "Lot 41 bid escrow" in body
    assert "Estates" in body
    # Dates a person reads, not the ISO timestamp the ledger stored.
    assert "expires 1 Sep 2026" in body
    assert "2026-09-01T00:00:00Z" not in body
    assert "reserved by 2 things" in body

    # A released hold is not held, and another user's hold is not mine.
    assert "Lot 12 outbid" not in body
    assert "Someone else" not in body

    # Savings/net have no source in this schema, so they are absent, not zeroed.
    assert "Net position" not in body
    assert "Savings and net position need Osentar Bank" in body

    # Markets figures from the exchange snapshot + the user's real holding.
    assert "VTEC" in body
    assert "1,240.00" in body                        # mid price
    assert f"{int(MY_SHARES * SHARE_PRICE):,}" in body   # 22,320 portfolio value

    # Theme is served, and there is no emoji anywhere on the page.
    assert "IBM+Plex+Mono" in body
    assert "border-radius" not in body.split("</style>")[0].replace(
        "border-radius:50%", "")   # only status dots are round
    assert not any(ord(ch) > 0x2500 for ch in body if ch not in "—·…▲▼&"), "no emoji"


@pytest.mark.asyncio
async def test_api_me_returns_ledger_figures(client, env):
    _login(client, env)
    r = await client.get("/hub/api/me")
    j = await r.json()
    assert r.status == 200
    assert j["user_id"] == USER_ID
    assert j["money"]["available"] == AVAILABLE
    assert j["money"]["held"] == HELD
    assert j["money"]["savings"] is None      # no source, so no figure
    assert j["money"]["net"] is None
    assert {h["reason"] for h in j["money"]["holds"]} == {"Loan collateral", "Lot 41 bid escrow"}


@pytest.mark.asyncio
async def test_preview_shows_figures_and_mints_a_key(client, env):
    _login(client, env)
    r = await client.post(f"/hub/markets/{MARKET_ID}/preview",
                          data={"csrf": "test-csrf", "side": "buy", "shares": "10"})
    body = await r.text()
    assert r.status == 200
    assert "Confirm buy" in body
    assert "Fill per share" in body
    assert "Available after" in body
    assert env.calls.trades == []              # a preview moves nothing

    conn = env.hub._hub_conn()
    row = conn.execute("SELECT * FROM hub_idempotency WHERE state='minted'").fetchone()
    assert row is not None
    assert row["user_id"] == USER_ID
    assert row["endpoint"] == f"markets/trade:{MARKET_ID}"   # the TICKER is bound, not just the action


# ══════════════════════════════════════════════════════════════════════════
# 3. A body-supplied user id is ignored and logged.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_body_supplied_user_id_ignored_and_logged(client, env):
    _login(client, env)
    key = env.hub.mint_key(USER_ID, f"markets/trade:{MARKET_ID}")

    r = await client.post(
        f"/hub/markets/{MARKET_ID}/trade",
        json={"csrf": "test-csrf", "idempotency_key": key, "side": "buy", "shares": 3,
              "user_id": OTHER_ID, "from_user": OTHER_ID},
        headers={"Accept": "application/json"},
    )
    j = await r.json()
    assert r.status == 200 and j["ok"] is True

    # The trade was booked for the SESSION user, not the body's user.
    assert len(env.calls.trades) == 1
    side, uid, mid, shares, _name = env.calls.trades[0]
    assert uid == USER_ID
    assert uid != OTHER_ID
    assert (side, mid, shares) == ("buy", MARKET_ID, 3)

    # And the attempt is on the record as an attack signal.
    rows = env.hub._hub_conn().execute(
        "SELECT * FROM hub_attack_log WHERE kind='body_supplied_identity'").fetchall()
    assert len(rows) == 1
    assert rows[0]["session_user"] == USER_ID
    assert rows[0]["endpoint"] == f"markets/trade:{MARKET_ID}"
    assert OTHER_ID in rows[0]["detail"]


@pytest.mark.asyncio
async def test_another_users_key_is_refused(client, env):
    """A key minted for someone else cannot be spent by this session."""
    _login(client, env)
    stolen = env.hub.mint_key(OTHER_ID, f"markets/trade:{MARKET_ID}")
    r = await client.post(f"/hub/markets/{MARKET_ID}/trade",
                          json={"csrf": "test-csrf", "idempotency_key": stolen,
                                "side": "buy", "shares": 1},
                          headers={"Accept": "application/json"})
    assert r.status == 403
    assert env.calls.trades == []


@pytest.mark.asyncio
async def test_a_key_minted_on_another_ticker_cannot_trade_this_one(client, env):
    """WEB_ATTACK finding 7: the confirm screen's key must bind the TICKER it priced.

    Before the subject went into the key, a key minted while previewing one listing
    booked a trade on any other listing the body named, at any size — the figures the
    player read were advisory. This is his own key, so it is refused as a mismatch and
    not logged as key theft.
    """
    _login(client, env)
    elsewhere = env.hub.mint_key(USER_ID, "markets/trade:SOMEOTHER")
    r = await client.post(f"/hub/markets/{MARKET_ID}/trade",
                          json={"csrf": "test-csrf", "idempotency_key": elsewhere,
                                "side": "buy", "shares": 3},
                          headers={"Accept": "application/json"})
    assert r.status == 409
    assert (await r.json())["code"] == "form_key_subject_mismatch"
    assert env.calls.trades == []

    conn = env.hub._hub_conn()
    rows = conn.execute("SELECT * FROM hub_attack_log").fetchall()
    assert not [x for x in rows if x["kind"] == "idempotency_key_theft"]


@pytest.mark.asyncio
async def test_unminted_key_is_refused(client, env):
    _login(client, env)
    r = await client.post(f"/hub/markets/{MARKET_ID}/trade",
                          json={"csrf": "test-csrf", "idempotency_key": "invented-by-client",
                                "side": "buy", "shares": 1},
                          headers={"Accept": "application/json"})
    assert r.status == 409
    assert env.calls.trades == []


@pytest.mark.asyncio
async def test_bad_csrf_is_refused(client, env):
    _login(client, env)
    key = env.hub.mint_key(USER_ID, f"markets/trade:{MARKET_ID}")
    r = await client.post(f"/hub/markets/{MARKET_ID}/trade",
                          json={"csrf": "wrong", "idempotency_key": key,
                                "side": "buy", "shares": 1},
                          headers={"Accept": "application/json"})
    assert r.status == 403
    assert env.calls.trades == []


# ══════════════════════════════════════════════════════════════════════════
# 4. A replayed POST returns the original result.
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_replayed_post_returns_original_result(client, env):
    _login(client, env)
    key = env.hub.mint_key(USER_ID, f"markets/trade:{MARKET_ID}")
    payload = {"csrf": "test-csrf", "idempotency_key": key, "side": "buy", "shares": 7}
    hdr = {"Accept": "application/json"}

    first = await (await client.post(f"/hub/markets/{MARKET_ID}/trade",
                                     json=payload, headers=hdr)).json()
    assert first["ok"] is True
    assert len(env.calls.trades) == 1

    # Same key, same everything — the user hammered Confirm.
    r2 = await client.post(f"/hub/markets/{MARKET_ID}/trade", json=payload, headers=hdr)
    second = await r2.json()

    assert len(env.calls.trades) == 1, "the engine must not have run twice"
    assert second["replayed"] is True
    for field in ("ok", "code", "message", "side", "market_id", "shares", "fill",
                  "total", "new_price"):
        assert second[field] == first[field], field

    # A third replay is still the same answer.
    third = await (await client.post(f"/hub/markets/{MARKET_ID}/trade",
                                     json=payload, headers=hdr)).json()
    assert len(env.calls.trades) == 1
    assert third["total"] == first["total"]


@pytest.mark.asyncio
async def test_form_post_replay_matches_json_replay(client, env):
    """The browser path (form post, HTML response) is the same single-use key."""
    _login(client, env)
    key = env.hub.mint_key(USER_ID, f"markets/trade:{MARKET_ID}")
    form = {"csrf": "test-csrf", "idempotency_key": key, "side": "buy", "shares": "4"}

    r1 = await client.post(f"/hub/markets/{MARKET_ID}/trade", data=form)
    b1 = await r1.text()
    assert r1.status == 200
    assert "Bought 4 shares" in b1
    assert len(env.calls.trades) == 1

    r2 = await client.post(f"/hub/markets/{MARKET_ID}/trade", data=form)
    b2 = await r2.text()
    assert len(env.calls.trades) == 1, "resubmitting the form must not trade again"
    assert "Bought 4 shares" in b2


@pytest.mark.asyncio
async def test_validation_failure_releases_the_key(client, env):
    """A rejection that provably moved nothing hands the key back, so correcting
    the form and resubmitting works instead of dead-ending the user."""
    _login(client, env)
    key = env.hub.mint_key(USER_ID, f"markets/trade:{MARKET_ID}")

    bad = await client.post(f"/hub/markets/{MARKET_ID}/trade",
                            json={"csrf": "test-csrf", "idempotency_key": key,
                                  "side": "buy", "shares": 0},
                            headers={"Accept": "application/json"})
    assert bad.status == 400
    assert env.calls.trades == []

    good = await client.post(f"/hub/markets/{MARKET_ID}/trade",
                             json={"csrf": "test-csrf", "idempotency_key": key,
                                   "side": "buy", "shares": 2},
                             headers={"Accept": "application/json"})
    j = await good.json()
    assert j["ok"] is True
    assert len(env.calls.trades) == 1


@pytest.mark.asyncio
async def test_health_is_public_and_leaks_nothing(client, env):
    r = await client.get("/hub/health")
    j = await r.json()
    assert r.status == 200
    assert j["ok"] is True and j["sections"] == ["markets"]
    assert USER_ID not in str(j)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
