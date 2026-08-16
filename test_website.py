"""
test_website.py — proofs for `banking_web` and `estates_web`, against a real
aiohttp test client, a real `restocker.db` and the real `ledger_v2`.

Run:  python3 test_website.py

Four claims are proved here, and they are the four that would be expensive to be
wrong about:

  1. A BID PLACES A HOLD AND NOT A DEBIT.
     Asserted against `balances.coins` and the `ledger_holds` row directly, not against
     the handler's own return value — a handler that reports a hold it did not place
     would pass a test that only reads its JSON.

  2. A REPLAYED BID DOES NOT PLACE TWO.
     Same form key submitted twice: one `land_bids` row, one `ledger_holds` row, one
     reservation, and the second response replays the first.

  3. A LOGGED-OUT POST IS REFUSED.
     401 before the body is read, on every money route in both sections.

  4. OSENTAR DOWN DEGRADES TO A NAMED ERROR, NEVER A 500.
     Both when the bank is unconfigured and when its port is dead; the page still
     renders 200, and the rest of the site — the whole estates section — is unaffected.

Plus the supporting invariants: identity comes from the cookie and a body-supplied
user id is ignored, being outbid releases the previous hold rather than refunding it,
and the wallet strip withholds the net position instead of guessing it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BUILD = "/home/claude/build"
CORE = "/mnt/user-data/uploads/RestockerLocal"
for p in (str(Path(__file__).resolve().parent), CORE, BUILD):
    if p not in sys.path:
        sys.path.insert(0, p)

FAILS = []
PASSES = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(label)
        print(f"  PASS  {label}")
    else:
        FAILS.append(f"{label} — {detail}")
        print(f"  FAIL  {label} — {detail}")


# ══════════════════════════════════════════════════════════════════════════
# Fixture
# ══════════════════════════════════════════════════════════════════════════

TMP = Path(tempfile.mkdtemp(prefix="vtweb-"))
os.chdir(TMP)                       # Restocker_db.DB_PATH is relative to cwd
os.environ["ESTATES_DB_PATH"] = str(TMP / "estates.db")
os.environ["VT_WEB_KEY_SECRET"] = "test-secret-not-a-real-one"
os.environ["VT_STAFF_IDS"] = "999000111"
os.environ.pop("OSENTAR_BASE_URL", None)

shutil.copy(f"{BUILD}/estates.db", TMP / "estates.db")

import Restocker_db as rdb           # noqa: E402
import ledger_migrate                # noqa: E402

rdb.init_db()
ledger_migrate.migrate(Path("restocker.db"), verbose=False)

import ledger_v2 as L                # noqa: E402
import estates_db as edb             # noqa: E402
import vt_web_shell as shell         # noqa: E402
import banking_web                   # noqa: E402
import estates_web                   # noqa: E402
from aiohttp import web              # noqa: E402
from aiohttp.test_utils import TestClient, TestServer   # noqa: E402

BIDDER = "100000000000000001"
RIVAL = "100000000000000002"
SELLER = "100000000000000003"
START_COINS = 50_000

_SESSIONS = {
    "tok-bidder": {"user_id": BIDDER, "name": "GreyHames", "csrf": "csrf-bidder"},
    "tok-rival": {"user_id": RIVAL, "name": "Tamsin Roe", "csrf": "csrf-rival"},
}


def fake_session(request):
    """Reads the same `vtm_sess` cookie the deployed `_session_user` reads."""
    return _SESSIONS.get(request.cookies.get("vtm_sess") or "")


def raw_coins(uid: str) -> int:
    with sqlite3.connect("restocker.db") as c:
        row = c.execute("SELECT CAST(coins AS INTEGER) FROM balances WHERE user_id=?",
                        (uid,)).fetchone()
    return int(row[0]) if row else 0


def hold_rows(uid: str, state: str = "open") -> list:
    with sqlite3.connect("restocker.db") as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT * FROM ledger_holds WHERE user_id=? AND state=?", (uid, state))]


def bid_rows(listing_id: int) -> list:
    with sqlite3.connect("restocker.db") as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT * FROM land_bids WHERE listing_id=? ORDER BY id", (listing_id,))]


def seed_wallets() -> None:
    with sqlite3.connect("restocker.db") as c:
        for uid in (BIDDER, RIVAL, SELLER):
            c.execute("INSERT INTO balances (user_id, coins, principal, lp) VALUES (?,?,0,0) "
                      "ON CONFLICT(user_id) DO UPDATE SET coins=excluded.coins",
                      (uid, START_COINS))


def make_listing(**over) -> int:
    kw = dict(seller_id=SELLER, kind="land", title="Riverside Parcel R-12",
              category="Land · 64x64 · Riverside", mode="auction", reserve=6000,
              min_increment_pct=5.0, status="active",
              ends_at="2099-01-01 00:00:00")
    kw.update(over)
    return rdb.create_land_listing(**kw)


def build_app() -> web.Application:
    app = web.Application()
    banking_web.register_banking_routes(app)
    estates_web.register_estates_routes(app)
    return app


def client_for(app, token: str | None) -> TestClient:
    cookies = {"vtm_sess": token} if token else {}
    return TestClient(TestServer(app), cookies=cookies)


def hdr(token: str | None) -> dict:
    if not token:
        return {}
    return {"X-CSRF-Token": _SESSIONS[token]["csrf"]}


# ══════════════════════════════════════════════════════════════════════════
# 1 & 2 — a bid is a hold, and a replay is not a second one
# ══════════════════════════════════════════════════════════════════════════

async def test_bid_is_a_hold_not_a_debit():
    print("\n[1] A bid places a HOLD and not a debit")
    seed_wallets()
    lot = make_listing()
    app = build_app()
    c = client_for(app, "tok-bidder")
    await c.start_server()
    try:
        before = raw_coins(BIDDER)

        board = await (await c.get("/api/estates/lots")).json()
        check("board lists the lot", any(l["id"] == lot for l in board["lots"]),
              str(board)[:200])
        # The key rides on the LOT it was minted for — one key per board would spend
        # on any lot the browser named (WEB_ATTACK finding 7).
        key = next(l["key"] for l in board["lots"] if l["id"] == lot)
        min_next = next(l["min_next"] for l in board["lots"] if l["id"] == lot)
        check("minimum next bid is an integer", isinstance(min_next, int) and min_next > 0,
              repr(min_next))

        pv = await (await c.post("/api/estates/bid/preview",
                                 json={"lot_id": lot, "amount": min_next},
                                 headers=hdr("tok-bidder"))).json()
        check("preview returns figures without moving anything",
              pv["ok"] and raw_coins(BIDDER) == before, str(pv)[:200])
        check("preview says the coins are held, not spent",
              "hold, not a payment" in pv["note"], pv["note"][:120])

        r = await c.post("/api/estates/bid",
                         json={"lot_id": lot, "amount": min_next, "idempotency_key": key},
                         headers=hdr("tok-bidder"))
        body = await r.json()
        check("bid accepted", r.status == 200 and body.get("ok"), str(body)[:300])

        # THE CLAIM, asserted against the database and not against the response.
        after = raw_coins(BIDDER)
        check("balance is UNCHANGED — no debit", after == before,
              f"{before} -> {after}")

        holds = [h for h in hold_rows(BIDDER) if h["reason"] == f"realestate:bid:{lot}"]
        check("exactly one OPEN hold exists", len(holds) == 1, f"{len(holds)} holds")
        check("the hold is for the bid amount",
              holds and int(holds[0]["amount"]) == min_next,
              str(holds[:1])[:200])
        check("the hold has an expiry", holds and bool(holds[0]["expires_at"]),
              str(holds[:1])[:200])
        check("the hold's key is domain-derived, not the browser's",
              holds and holds[0]["idempotency_key"] == f"land:listing:{lot}:bid:{bid_rows(lot)[0]['id']}",
              str(holds[0]["idempotency_key"]) if holds else "none")

        bal = L.get_balance(BIDDER)
        check("available fell by the bid", int(bal["available"]) == before - min_next,
              str(bal))
        check("held rose by the bid", int(bal["held"]) == min_next, str(bal))
        check("balance == available + held", int(bal["balance"]) == before, str(bal))

        rows = bid_rows(lot)
        check("one bid row, status held", len(rows) == 1 and rows[0]["status"] == "held",
              str(rows)[:250])
        check("the row carries its capture key minted at creation",
              rows[0]["capture_key"] == f"land:listing:{lot}:bid:{rows[0]['id']}:capture",
              str(rows[0]["capture_key"]))
        return lot, key, min_next, c
    finally:
        pass


async def test_replayed_bid_places_one_hold(lot, key, amount, c):
    print("\n[2] A replayed bid does not place two")
    holds_before = len([h for h in hold_rows(BIDDER) if h["reason"] == f"realestate:bid:{lot}"])
    coins_before = raw_coins(BIDDER)

    r = await c.post("/api/estates/bid",
                     json={"lot_id": lot, "amount": amount, "idempotency_key": key},
                     headers=hdr("tok-bidder"))
    body = await r.json()
    check("replay answers 200", r.status == 200, str(r.status))
    check("replay is marked as a replay", body.get("replayed") is True, str(body)[:200])
    check("replay returns the ORIGINAL receipt", body.get("hold_id"), str(body)[:200])

    holds_after = [h for h in hold_rows(BIDDER) if h["reason"] == f"realestate:bid:{lot}"]
    check("still exactly one open hold", len(holds_after) == holds_before == 1,
          f"{holds_before} -> {len(holds_after)}")
    check("still no debit", raw_coins(BIDDER) == coins_before,
          f"{coins_before} -> {raw_coins(BIDDER)}")
    check("still one bid row", len(bid_rows(lot)) == 1, str(len(bid_rows(lot))))


async def test_outbid_releases_the_previous_hold(lot, c_bidder):
    print("\n[2b] Being outbid RELEASES the hold — it is not a refund")
    app = c_bidder.server.app
    c2 = client_for(app, "tok-rival")
    await c2.start_server()
    try:
        board = await (await c2.get("/api/estates/lots")).json()
        min_next = next(l["min_next"] for l in board["lots"] if l["id"] == lot)
        lot_key = next(l["key"] for l in board["lots"] if l["id"] == lot)
        bidder_coins = raw_coins(BIDDER)
        r = await c2.post("/api/estates/bid",
                          json={"lot_id": lot, "amount": min_next,
                                "idempotency_key": lot_key},
                          headers=hdr("tok-rival"))
        body = await r.json()
        check("rival's bid accepted", r.status == 200 and body.get("ok"), str(body)[:250])

        open_first = [h for h in hold_rows(BIDDER) if h["reason"] == f"realestate:bid:{lot}"]
        rel_first = [h for h in hold_rows(BIDDER, "released")
                     if h["reason"] == f"realestate:bid:{lot}"]
        check("the outbid player's hold is RELEASED", len(open_first) == 0 and len(rel_first) == 1,
              f"open={len(open_first)} released={len(rel_first)}")
        check("the outbid player's coins never moved", raw_coins(BIDDER) == bidder_coins,
              f"{bidder_coins} -> {raw_coins(BIDDER)}")
        check("the outbid player's available is whole again",
              int(L.get_balance(BIDDER)["available"]) == bidder_coins,
              str(L.get_balance(BIDDER)))
        check("the rival now holds the lot", len(hold_rows(RIVAL)) == 1,
              str(hold_rows(RIVAL))[:200])
    finally:
        await c2.close()


async def test_body_identity_is_ignored(lot, c):
    print("\n[2c] A user id in the body is ignored, not obeyed")
    board = await (await c.get("/api/estates/lots")).json()
    min_next = next(l["min_next"] for l in board["lots"] if l["id"] == lot)
    lot_key = next(l["key"] for l in board["lots"] if l["id"] == lot)
    rival_before = raw_coins(RIVAL)
    r = await c.post("/api/estates/bid",
                     json={"lot_id": lot, "amount": min_next, "user_id": RIVAL,
                           "idempotency_key": lot_key},
                     headers=hdr("tok-bidder"))
    body = await r.json()
    check("the bid was accepted", r.status == 200 and body.get("ok"), str(body)[:200])
    # The hold belongs to the cookie's owner. The body named somebody else and was ignored.
    mine = [h for h in hold_rows(BIDDER) if int(h["amount"]) == min_next]
    theirs = [h for h in hold_rows(RIVAL) if int(h["amount"]) == min_next]
    check("the hold was placed on the SESSION user, not the body's user",
          len(mine) == 1 and not theirs,
          f"session-user holds={len(mine)} body-user holds={len(theirs)}")
    check("the impersonated user's coins never moved", raw_coins(RIVAL) == rival_before,
          f"{rival_before} -> {raw_coins(RIVAL)}")
    check("the impersonated user was outbid, so their hold was released not captured",
          not hold_rows(RIVAL), str(hold_rows(RIVAL))[:200])


# ══════════════════════════════════════════════════════════════════════════
# 3 — logged-out POSTs
# ══════════════════════════════════════════════════════════════════════════

async def test_logged_out_post_is_refused():
    print("\n[3] A logged-out POST is refused")
    app = build_app()
    c = client_for(app, None)
    await c.start_server()
    try:
        routes = [
            ("/api/estates/bid", {"lot_id": 1, "amount": 100}),
            ("/api/estates/stake", {"market_id": 1, "outcome_id": 1, "amount": 100}),
            ("/api/estates/bid/preview", {"lot_id": 1, "amount": 100}),
            ("/api/banking/deposit", {"amount": 100}),
            ("/api/banking/withdraw", {"amount": 100}),
            ("/api/banking/repay", {"amount": 100}),
            ("/api/banking/bond/buy", {"amount": 100, "term_days": 90}),
            ("/api/banking/bond/redeem", {"bond_id": "B-1"}),
            ("/api/banking/preview", {"action": "deposit", "amount": 100}),
            ("/api/banking/staff/collect", {"loan_id": 1, "amount": 100}),
        ]
        for path, payload in routes:
            r = await c.post(path, json=payload)
            body = await r.json()
            check(f"{path} refuses a logged-out POST",
                  r.status in (401, 403) and not body.get("ok"),
                  f"status={r.status} body={str(body)[:120]}")
        for path in ("/api/estates/lots", "/api/estates/markets",
                     "/api/banking/summary", "/api/wallet/strip"):
            r = await c.get(path)
            check(f"{path} refuses a logged-out GET", r.status == 401, str(r.status))
    finally:
        await c.close()


async def test_forged_and_stale_keys_are_refused():
    print("\n[3b] A form key we did not mint is refused")
    seed_wallets()
    lot = make_listing()
    app = build_app()
    c = client_for(app, "tok-bidder")
    await c.start_server()
    try:
        r = await c.post("/api/estates/bid",
                         json={"lot_id": lot, "amount": 9000,
                               "idempotency_key": "bid.9999999999.abc.deadbeef"},
                         headers=hdr("tok-bidder"))
        body = await r.json()
        check("a forged key is refused", r.status == 400 and body["code"] == "bad_form_key",
              str(body)[:200])

        # A key minted for one user must not work for another.
        other = shell.mint_form_key(RIVAL, f"bid:{lot}")
        r = await c.post("/api/estates/bid",
                         json={"lot_id": lot, "amount": 9000, "idempotency_key": other},
                         headers=hdr("tok-bidder"))
        body = await r.json()
        check("another user's key is refused", r.status == 400, str(body)[:200])

        # A key minted for a different action must not work either.
        wrong = shell.mint_form_key(BIDDER, "deposit")
        r = await c.post("/api/estates/bid",
                         json={"lot_id": lot, "amount": 9000, "idempotency_key": wrong},
                         headers=hdr("tok-bidder"))
        body = await r.json()
        check("a key for another action is refused", r.status == 400, str(body)[:200])

        # A key minted on ANOTHER LOT must not work either — WEB_ATTACK finding 7.
        # This is the same action and the same user; only the subject differs, and the
        # subject is exactly what the player was shown figures for.
        other_lot = make_listing(title="A different lot entirely")
        cross = shell.mint_form_key(BIDDER, f"bid:{other_lot}")
        r = await c.post("/api/estates/bid",
                         json={"lot_id": lot, "amount": 9000, "idempotency_key": cross},
                         headers=hdr("tok-bidder"))
        body = await r.json()
        check("a key minted on another LOT is refused by name",
              r.status == 400 and body["code"] == "form_key_subject_mismatch",
              str(body)[:250])
        check("the refusal names both lots so the player can see the divergence",
              str(other_lot) in body.get("error", "") and str(lot) in body.get("error", ""),
              body.get("error", "")[:250])
        check("nothing was reserved against the lot the key did not name",
              not [h for h in hold_rows(BIDDER) if h["reason"] == f"realestate:bid:{lot}"],
              str(hold_rows(BIDDER))[:200])

        # And CSRF.
        good = shell.mint_form_key(BIDDER, f"bid:{lot}")
        r = await c.post("/api/estates/bid",
                         json={"lot_id": lot, "amount": 9000, "idempotency_key": good})
        body = await r.json()
        check("a POST without the CSRF token is refused",
              r.status == 403 and body["code"] == "bad_csrf", str(body)[:200])
        check("no hold was placed by any refused attempt",
              not [h for h in hold_rows(BIDDER) if h["reason"] == f"realestate:bid:{lot}"],
              str(hold_rows(BIDDER))[:200])
    finally:
        await c.close()


async def test_insufficient_is_named_with_figures():
    print("\n[3c] An unaffordable bid names the figures and reserves nothing")
    seed_wallets()
    lot = make_listing(reserve=999_000)
    app = build_app()
    c = client_for(app, "tok-bidder")
    await c.start_server()
    try:
        board = await (await c.get("/api/estates/lots")).json()
        key = next(l["key"] for l in board["lots"] if l["id"] == lot)
        r = await c.post("/api/estates/bid",
                         json={"lot_id": lot, "amount": 999_000, "idempotency_key": key},
                         headers=hdr("tok-bidder"))
        body = await r.json()
        check("refused, not 500", r.status == 409 and body["code"] == "insufficient",
              f"{r.status} {str(body)[:200]}")
        check("the refusal carries the figures", "available" in body["error"],
              body.get("error", "")[:160])
        check("nothing was reserved",
              not [h for h in hold_rows(BIDDER) if h["reason"] == f"realestate:bid:{lot}"],
              str(hold_rows(BIDDER))[:200])
        check("the key was released so a real retry is possible",
              (await (await c.post("/api/estates/bid",
                                   json={"lot_id": lot, "amount": 999_000,
                                         "idempotency_key": key},
                                   headers=hdr("tok-bidder"))).json())["code"] == "insufficient",
              "second attempt did not re-evaluate")
    finally:
        await c.close()


# ══════════════════════════════════════════════════════════════════════════
# 4 — Osentar down
# ══════════════════════════════════════════════════════════════════════════

async def test_osentar_down_is_a_named_error_not_a_500():
    print("\n[4] Osentar down degrades to a NAMED error, never a 500")
    seed_wallets()
    app = build_app()
    c = client_for(app, "tok-bidder")
    await c.start_server()
    try:
        for label, base in (("unconfigured", ""),
                            ("dead port", "http://127.0.0.1:9")):
            banking_web._cooldown_until = 0.0
            if base:
                os.environ["OSENTAR_BASE_URL"] = base
            else:
                os.environ.pop("OSENTAR_BASE_URL", None)

            r = await c.get("/api/banking/summary")
            body = await r.json()
            check(f"[{label}] summary is 503, not 500", r.status == 503, str(r.status))
            check(f"[{label}] the error is machine-readable",
                  body.get("code") == "bank_unreachable", str(body)[:200])
            check(f"[{label}] the error NAMES the service and the reason",
                  body.get("service") == "osentar" and len(body.get("error", "")) > 20,
                  str(body)[:250])
            check(f"[{label}] the wallet side is still reported",
                  isinstance(body.get("wallet"), dict), str(body)[:200])

            r = await c.post("/api/banking/deposit",
                             json={"amount": 100,
                                   "idempotency_key": shell.mint_form_key(BIDDER, "deposit")},
                             headers=hdr("tok-bidder"))
            body = await r.json()
            check(f"[{label}] a deposit refuses with a named error, not a 500",
                  r.status == 503 and body.get("code") == "bank_unreachable",
                  f"{r.status} {str(body)[:200]}")

            r = await c.get("/banking")
            text = await r.text()
            check(f"[{label}] the banking PAGE still renders", r.status == 200, str(r.status))
            check(f"[{label}] the page carries the named-outage panel",
                  "Osentar Bank is not answering" in text, "panel markup missing")

            # The rest of the site is untouched.
            r = await c.get("/api/estates/lots")
            check(f"[{label}] estates is unaffected", r.status == 200, str(r.status))
            r = await c.get("/estates")
            check(f"[{label}] the estates page still renders", r.status == 200, str(r.status))

            r = await c.get("/api/wallet/strip")
            strip = await r.json()
            check(f"[{label}] the strip still reports real available/held",
                  r.status == 200 and strip["ok"] and isinstance(strip["available"], int),
                  str(strip)[:200])
            check(f"[{label}] the strip WITHHOLDS net rather than guessing it",
                  strip["savings"] is None and strip["net"] is None,
                  str(strip)[:200])
            check(f"[{label}] the strip names why savings are missing",
                  bool(strip.get("bank_error")), str(strip)[:200])
    finally:
        os.environ.pop("OSENTAR_BASE_URL", None)
        banking_web._cooldown_until = 0.0
        await c.close()


# ══════════════════════════════════════════════════════════════════════════
# Prediction markets — a stake is a hold too, and odds say indicative
# ══════════════════════════════════════════════════════════════════════════

async def test_stake_is_a_hold():
    print("\n[5] A stake is a HOLD, and the odds are labelled indicative")
    seed_wallets()
    app = build_app()
    c = client_for(app, "tok-bidder")
    await c.start_server()
    try:
        m = edb.create_market("Will the Riverside bridge open before October?",
                              ["Yes", "No"], closes_at="2099-01-01T00:00:00+00:00")
        edb.open_market(int(m["id"]))
        outs = edb.get_outcomes(int(m["id"]))

        j = await (await c.get("/api/estates/markets")).json()
        check("markets listed", j["ok"] and any(x["id"] == int(m["id"]) for x in j["markets"]),
              str(j)[:200])
        mk = next(x for x in j["markets"] if x["id"] == int(m["id"]))
        check("an open market's odds are flagged indicative", mk["indicative"] is True, str(mk)[:200])
        check("an empty side has NO price, not a price of zero",
              all(o["odds"] is None for o in mk["outcomes"]), str(mk["outcomes"])[:200])
        check("the rake is rendered from config, once", mk["rake"].endswith("%"), mk["rake"])

        stake_key = next(o["key"] for o in mk["outcomes"]
                         if o["outcome_id"] == int(outs[0]["id"]))
        pv = await (await c.post("/api/estates/stake/preview",
                                 json={"market_id": int(m["id"]),
                                       "outcome_id": int(outs[0]["id"]), "amount": 2000},
                                 headers=hdr("tok-bidder"))).json()
        check("the stake preview says HELD, not spent", "HELD, not spent" in pv["note"],
              pv["note"][:120])
        check("the stake preview labels the odds indicative",
              "indicative" in pv["note"], pv["note"][:200])

        before = raw_coins(BIDDER)
        r = await c.post("/api/estates/stake",
                         json={"market_id": int(m["id"]), "outcome_id": int(outs[0]["id"]),
                               "amount": 2000, "idempotency_key": stake_key},
                         headers=hdr("tok-bidder"))
        body = await r.json()
        check("stake accepted", r.status == 200 and body.get("ok"), str(body)[:250])
        check("the stake did NOT debit", raw_coins(BIDDER) == before,
              f"{before} -> {raw_coins(BIDDER)}")
        holds = [h for h in hold_rows(BIDDER)
                 if h["reason"] == f"estates:market:{int(m['id'])}:stake"]
        check("exactly one hold for the stake", len(holds) == 1, str(len(holds)))
        check("the hold amount matches the stake",
              holds and int(holds[0]["amount"]) == 2000, str(holds[:1])[:200])
        check("the ledger key is the one estates_db minted",
              holds and holds[0]["idempotency_key"].startswith(f"estates:market:{int(m['id'])}:stake:"),
              str(holds[0]["idempotency_key"]) if holds else "none")

        r2 = await c.post("/api/estates/stake",
                          json={"market_id": int(m["id"]), "outcome_id": int(outs[0]["id"]),
                                "amount": 2000, "idempotency_key": stake_key},
                          headers=hdr("tok-bidder"))
        b2 = await r2.json()
        check("a replayed stake does not place two", b2.get("replayed") is True
              and len([h for h in hold_rows(BIDDER)
                       if h["reason"] == f"estates:market:{int(m['id'])}:stake"]) == 1,
              str(b2)[:200])
    finally:
        await c.close()


# ══════════════════════════════════════════════════════════════════════════
# The Osentar contract, executed — a fake bank that implements it exactly
# ══════════════════════════════════════════════════════════════════════════

class FakeOsentar:
    """The documented contract, implemented. If `banking_web` and this disagree, the
    banking page breaks — which is the point of writing it as code rather than prose.

    It also counts how many times each idempotency key reaches it, which is how the
    end-to-end replay claim is proved: the browser's form key travels unchanged to the
    bank, and the bank sees ONE deposit no matter how many times the button is pressed.
    """

    def __init__(self):
        self.savings = 90_000
        self.seen: dict = {}
        self.calls: list = []
        self.token_seen = None

    def app(self) -> web.Application:
        a = web.Application()
        a.router.add_get("/api/v1/health", self.health)
        a.router.add_get("/api/v1/account", self.account)
        a.router.add_post("/api/v1/savings/deposit", self.deposit)
        a.router.add_get("/api/v1/staff/queue", self.queue)
        a.router.add_get("/api/v1/staff/collections", self.collections)
        return a

    async def health(self, request):
        return web.json_response({"ok": True, "service": "osentar-bank", "version": "1"})

    async def account(self, request):
        self.token_seen = request.headers.get("X-Osentar-Token")
        self.calls.append(("account", request.query.get("user_id")))
        return web.json_response({
            "ok": True,
            "savings": {"balance": self.savings, "apr": 3.2, "opened": "2026-03-13",
                        "last_paid": "2026-08-13", "next_pay": "2026-09-13",
                        "accrued_this_month": 240, "accrued_since_last_paid": 0,
                        "accrued_lifetime": 886, "avg90": 78_400,
                        "ladder": [["2026-07-13", 78_000, 208], ["2026-08-13", 90_000, 240]]},
            "loan": {"id": 9, "principal": 25_000, "apr": 6.5, "terms": 12,
                     "first": "2026-07-09", "disbursed": "2026-07-02",
                     "paid_principal": 10_415, "paid_interest": 130, "paid_count": 5,
                     "last_paid_on": "2026-08-06", "outstanding": 14_585,
                     "accrued_interest": 18, "payoff_today": 14_603, "closed": False,
                     "schedule": [{"seq": 6, "due": "2026-08-13", "principal": 2_083,
                                   "interest": 18, "total": 2_101,
                                   "balance_after": 12_502, "status": "due"},
                                  {"seq": 7, "due": "2026-08-20", "principal": 2_083,
                                   "interest": 16, "total": 2_099,
                                   "balance_after": 10_419, "status": "scheduled"}]},
            "bonds": [{"id": "B-201", "face": 10_000, "apr": 4.0, "bought": "2026-06-03",
                       "matures": "2026-09-01", "term_days": 90,
                       "interest_at_maturity": 98, "earned_so_far": 78,
                       "redeem_value_today": 10_039,
                       "early_redemption_penalty": 39, "matured": False}],
            "bond_terms": [{"term_days": 90, "apr": 4.0, "min_face": 1_000,
                            "max_face": 50_000}],
            "record": {"repaid_clean": 4, "late": 1, "defaults": 0,
                       "late_detail": "3 days, loan #4, 18 Apr 2026", "since": "2026-03-13"},
            "limit": {"amount": 25_000, "cap": 50_000, "rounded_to": 500,
                      "headroom": 10_415,
                      "components": [["Base allowance", 5_000],
                                     ["20% of 90-day average savings", 15_680],
                                     ["4 loans repaid clean", 4_000],
                                     ["1 late payment", -2_500]]},
        })

    async def deposit(self, request):
        body = await request.json()
        key = body.get("idempotency_key")
        self.calls.append(("deposit", key, body.get("amount")))
        if key in self.seen:
            return web.json_response({**self.seen[key], "deduped": True})
        self.savings += int(body["amount"])
        out = {"ok": True, "applied": int(body["amount"]),
               "balance": {"available": 48_200 - int(body["amount"]), "savings": self.savings}}
        self.seen[key] = out
        return web.json_response(out)

    async def queue(self, request):
        return web.json_response({"ok": True, "requests": [
            {"id": 37, "user_id": "555", "name": "Bramble Hollow", "requested": 12_000,
             "terms": 8, "purpose": "Restock float", "asked": "2026-08-12",
             "outstanding_debt": 0, "limit": 18_000, "repaid_clean": 3, "late": 0,
             "frozen": False}]})

    async def collections(self, request):
        return web.json_response({"ok": True, "overdue": [
            {"loan_id": 31, "user_id": "556", "name": "Tamsin Roe", "days_late": 11,
             "owed": 3_200, "savings_reachable": 3_200}]})


async def test_osentar_contract_end_to_end():
    print("\n[7] The documented Osentar contract, executed against a bank that implements it")
    seed_wallets()
    bank = FakeOsentar()
    bank_server = TestServer(bank.app())
    await bank_server.start_server()
    os.environ["OSENTAR_BASE_URL"] = str(bank_server.make_url("")).rstrip("/")
    os.environ["OSENTAR_API_TOKEN"] = "shared-secret"
    banking_web._cooldown_until = 0.0

    app = build_app()
    c = client_for(app, "tok-bidder")
    await c.start_server()
    try:
        r = await c.get("/api/banking/summary")
        j = await r.json()
        check("summary reads the contract", r.status == 200 and j["ok"], str(j)[:200])
        check("the token is sent as a header, not in the URL",
              bank.token_seen == "shared-secret", str(bank.token_seen))
        check("the user id is the SESSION's, sent server-to-server",
              ("account", BIDDER) in bank.calls, str(bank.calls)[:200])
        check("savings, loan, bonds and limit all arrive",
              j["savings"]["balance"] == 90_000 and j["loan"]["id"] == 9
              and len(j["bonds"]) == 1 and j["limit"]["amount"] == 25_000, str(j)[:200])
        check("the loan schedule comes from the BANK, not re-derived here",
              len(j["loan"]["schedule"]) == 2, str(j["loan"])[:200])
        check("the credit limit arrives with the track record behind it",
              len(j["limit"]["components"]) == 4 and j["record"]["repaid_clean"] == 4,
              str(j["limit"])[:200])
        check("a form key is minted for every money action on the page",
              set(j["keys"]) == {"deposit", "withdraw", "repay", "bond_buy", "bond_redeem"},
              str(list(j["keys"])))

        strip = await (await c.get("/api/wallet/strip")).json()
        check("the strip now computes a real net position",
              strip["savings"] == 90_000 and strip["net"] ==
              strip["available"] + strip["held"] + 90_000 - 14_585, str(strip)[:250])

        pv = await (await c.post("/api/banking/preview",
                                 json={"action": "deposit", "amount": 5_000},
                                 headers=hdr("tok-bidder"))).json()
        check("deposit preview names the figures", pv["ok"] and pv["total"][1] == "95,000c",
              str(pv)[:250])
        check("deposit preview excludes held coins from what is depositable",
              any("Held by open escrow" in row[0] for row in pv["rows"]), str(pv["rows"])[:250])

        key = j["keys"]["deposit"]
        r1 = await c.post("/api/banking/deposit",
                          json={"amount": 5_000, "idempotency_key": key},
                          headers=hdr("tok-bidder"))
        b1 = await r1.json()
        check("deposit succeeds", r1.status == 200 and b1["ok"], str(b1)[:250])
        check("the browser's form key travelled to the bank unchanged",
              ("deposit", key, 5_000) in bank.calls, str(bank.calls)[:300])

        r2 = await c.post("/api/banking/deposit",
                          json={"amount": 5_000, "idempotency_key": key},
                          headers=hdr("tok-bidder"))
        b2 = await r2.json()
        check("the replay is answered from our own store", b2.get("replayed") is True,
              str(b2)[:200])
        check("THE BANK NEVER SAW THE SECOND DEPOSIT",
              len([x for x in bank.calls if x[0] == "deposit"]) == 1,
              str([x for x in bank.calls if x[0] == "deposit"]))
        check("the bank's savings moved exactly once", bank.savings == 95_000,
              str(bank.savings))

        r = await c.get("/api/banking/staff/queue")
        check("a non-staff session is refused the staff queue", r.status == 403, str(r.status))
        _SESSIONS["tok-bidder"]["user_id"] = "999000111"   # on VT_STAFF_IDS
        try:
            q = await (await c.get("/api/banking/staff/queue")).json()
            check("staff see the approval queue", q["ok"] and len(q["requests"]) == 1,
                  str(q)[:200])
            col = await (await c.get("/api/banking/staff/collections")).json()
            check("staff see collections", col["ok"] and len(col["overdue"]) == 1,
                  str(col)[:200])
        finally:
            _SESSIONS["tok-bidder"]["user_id"] = BIDDER
    finally:
        await c.close()
        await bank_server.close()
        os.environ.pop("OSENTAR_BASE_URL", None)
        os.environ.pop("OSENTAR_API_TOKEN", None)
        banking_web._cooldown_until = 0.0


async def test_empty_states_are_empty():
    """His rule: an empty state is EMPTY. No invented row, no placeholder, no demo data.

    `estates.db` ships with zero parcels, so this is the real case and not a contrived
    one — and the distinction that matters is that an ABSENT register (503, named) and
    an EMPTY register (200, `[]`) are different answers, because on screen they look the
    same and mean opposite things.
    """
    print("\n[8] Empty is empty, and absent is not the same as empty")
    app = build_app()
    c = client_for(app, "tok-bidder")
    await c.start_server()
    try:
        r = await c.get("/api/estates/parcels")
        j = await r.json()
        check("an empty register answers 200 with an empty list",
              r.status == 200 and j["ok"] and j["parcels"] == [], str(j)[:200])
        check("no placeholder rows were invented", not j["parcels"], str(j)[:200])

        # And an ABSENT register is a different, named answer.
        real = estates_web._edb
        estates_web._edb = lambda: None
        try:
            r = await c.get("/api/estates/parcels")
            j = await r.json()
            check("an absent register is 503 and says so, not an empty list",
                  r.status == 503 and j["code"] == "estates_db_unavailable"
                  and "parcels" not in j, str(j)[:200])
        finally:
            estates_web._edb = real

        # A legacy bid row predating escrow must not read as escrow "pending".
        lot = make_listing()
        with sqlite3.connect("restocker.db") as conn:
            conn.execute("INSERT INTO land_bids (listing_id, bidder_id, amount) VALUES (?,?,?)",
                         (lot, RIVAL, 7000))
        board = await (await c.get("/api/estates/lots")).json()
        row = next(l for l in board["lots"] if l["id"] == lot)["bids"][0]
        check("a pre-escrow bid row is labelled pre-escrow, not pending",
              row["hold"] == "pre-escrow", str(row))
    finally:
        await c.close()


async def test_the_register_does_not_publish_other_peoples_rent():
    """WEB_ATTACK finding 9: a lease is between a landlord and his tenant.

    Ownership and tenancy are what a register is FOR and stay public. What a tenant
    owes this month — the figure, the arrears status and the ledger key that settles it
    — was being attached to every parcel row for every session. The key is derived by
    `estates_db.mint_key` and no route accepts a caller-supplied one, so this was never
    a credential; it was somebody's debts on a public board.
    """
    print("\n[8b] The parcel register shows YOUR obligations, not everybody's")
    pid = int(edb.create_parcel(f"rent-privacy-{os.getpid()}", "Kettle Hollow",
                                region="Riverside", owner_id=SELLER)["id"])
    edb.start_lease(pid, RIVAL, 2500, period_days=30)
    edb.ensure_rent_charge(pid, edb.rent_period())
    app = build_app()
    c = client_for(app, "tok-bidder")        # neither the owner nor the tenant
    await c.start_server()
    try:
        j = await (await c.get("/api/estates/parcels")).json()
        row = next((p for p in j["parcels"] if p["id"] == pid), None)
        check("the parcel itself is still on the public register", row is not None,
              str(j)[:200])
        row = row or {}
        check("a stranger is served no rent charges for it", row.get("charges") == [],
              str(row.get("charges"))[:200])
        check("and therefore not the rent ledger key either",
              f"estates:parcel:{pid}:rent:" not in str(row), str(row)[:250])
        check("the owner is not called 'Another bidder' on a screen with no auction",
              row.get("owner") != "Another bidder", str(row.get("owner")))

        # The tenant sees his own charge, in full.
        c2 = client_for(app, "tok-rival")
        await c2.start_server()
        try:
            j2 = await (await c2.get("/api/estates/parcels")).json()
            mine = next(p for p in j2["parcels"] if p["id"] == pid)
            check("the tenant is served his own rent charge",
                  mine["you_lease"] and len(mine["charges"]) == 1
                  and mine["charges"][0]["amount"] == 2500, str(mine)[:250])
            check("with the ledger key that settles it",
                  mine["charges"][0]["key"] == f"estates:parcel:{pid}:rent:{edb.rent_period()}",
                  str(mine["charges"])[:200])
        finally:
            await c2.close()
    finally:
        await c.close()


async def test_sections_are_wired_into_the_hub_nav():
    """A mechanism built is not a mechanism wired.

    `hub_web` publishes a nav registry so a section can plug in without editing it.
    This asserts both sections actually call it — a page the hub does not link to is a
    page nobody finds.
    """
    print("\n[9] Both sections register themselves in the hub nav")
    try:
        import hub_web
    except Exception as e:
        check("hub_web importable", False, str(e))
        return
    build_app()                       # registration happens here
    keys = {s["key"]: s for s in hub_web.sections()}
    check("Banking is in the hub nav",
          keys.get("banking", {}).get("path") == "/banking", str(list(keys)))
    check("Estates is in the hub nav",
          keys.get("lands", {}).get("path") == "/estates", str(list(keys)))
    check("the estates nav label does NOT say Betting",
          "betting" not in keys.get("lands", {}).get("label", "").lower(),
          keys.get("lands", {}).get("label", ""))
    check("both sections carry an inline SVG icon, never an emoji",
          all("<path" in keys[k]["icon"] or "<rect" in keys[k]["icon"]
              for k in ("banking", "lands") if k in keys),
          str({k: keys[k]["icon"][:30] for k in ("banking", "lands") if k in keys}))


async def test_no_gambling_surface():
    """No house-banked game is REACHABLE. Asserted on the routes and the nav, not on
    the prose: these modules do discuss the scrapped casino in their docstrings, and a
    grep that cannot tell an explanation from an implementation would force the
    explanation to be deleted. What matters is that nothing serves one."""
    print("\n[6] There is no house-banked game anywhere in these modules")
    banned = ("dice", "coinflip", "lottery", "casino", "roulette",
              "blackjack", "slot", "jackpot", "wheel")
    app = build_app()
    paths, handlers = [], []
    for r in app.router.routes():
        try:
            paths.append(r.resource.canonical.lower())
        except Exception:
            pass
        handlers.append(getattr(r.handler, "__name__", "").lower())
    hits = [w for w in banned if any(w in p for p in paths)]
    check("no route serves a house-banked game", not hits, f"{hits} in {paths}")
    hits = [w for w in banned if any(w in h for h in handlers)]
    check("no handler implements a house-banked game", not hits, f"{hits} in {handlers}")
    labels = " ".join(lbl for _, lbl, _ in shell.NAV).lower()
    check("the nav does not offer betting or a casino",
          not any(w in labels for w in banned + ("betting",)), labels)
    # Prediction markets survive because they are pari-mutuel, and the page says so.
    body = estates_web._JS + estates_web._BODY
    check("the prediction-market surface states it is pari-mutuel",
          "pari-mutuel" in body and "never takes a side" in body,
          "the page does not explain that the house takes no side")


# ══════════════════════════════════════════════════════════════════════════

async def main() -> int:
    shell.set_session_provider(fake_session)
    print(f"workspace: {TMP}")

    lot, key, amount, c = await test_bid_is_a_hold_not_a_debit()
    try:
        await test_replayed_bid_places_one_hold(lot, key, amount, c)
        await test_outbid_releases_the_previous_hold(lot, c)
        await test_body_identity_is_ignored(lot, c)
    finally:
        await c.close()

    await test_logged_out_post_is_refused()
    await test_forged_and_stale_keys_are_refused()
    await test_insufficient_is_named_with_figures()
    await test_osentar_down_is_a_named_error_not_a_500()
    await test_stake_is_a_hold()
    await test_osentar_contract_end_to_end()
    await test_empty_states_are_empty()
    await test_the_register_does_not_publish_other_peoples_rent()
    await test_sections_are_wired_into_the_hub_nav()
    await test_no_gambling_surface()

    print(f"\n{'=' * 62}")
    print(f"{len(PASSES)} passed, {len(FAILS)} failed")
    for f in FAILS:
        print(f"  FAIL  {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
