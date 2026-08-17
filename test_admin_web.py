"""
test_admin_web.py — proofs for `admin_web` and the view-as write chokepoint, driven
through a real aiohttp test client against a COPY of `restocker_live.db`.

Run:  python3 test_admin_web.py

The properties that matter are asserted ON ROWS and on served bytes, never on a hidden
button. The whole point of "structurally read-only" is that the refusal is in the
request path, so every attack here goes through the real route.

  A1  staff gate      -> /admin 401 anon, 403 a normal player, 200 staff
  A2  nav             -> the Owner tab is in the markup for staff, absent for a player
  A3  enter/exit      -> server-side state + append-only audit rows (enter, view, exit)
  A4  view-as render  -> a staff session renders the TARGET's pages, with the banner
  A5  read-only send  -> in view-as, a send is refused and writes ZERO rows (before/after)
  A6  read-only read  -> in view-as, mark-read is refused and moves NO watermark
  A7  read-only trade -> in view-as, a markets trade is refused and writes no holding
  A8  read-only coin  -> in view-as, an estates bid is refused and writes no bid
  A9  DERIVED sweep   -> every route that goes through a write chokepoint refuses in
                         view-as; the set is derived from the handlers' source, not a list
  A10 audit visible   -> the SUBJECT can read the rows about themselves
  A11 append-only     -> no UPDATE/DELETE touches admin_audit, asserted on the AST
  A12 kill switch     -> the freeze toggles config and shows the figures it moves;
                         and it too is refused in view-as
  A13 dev login       -> refuses on EACH of the three gates independently; loopback unit
  A14 body identity   -> a body-supplied user_id on an admin write is ignored + alarmed
  A15 sales log       -> staff only, both sides named, event-dated, writes nothing,
                         and every player in the result can see it was read
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BUILD = "/home/claude/build"
CORE = "/mnt/user-data/uploads/RestockerLocal"
HERE = str(Path(__file__).resolve().parent)
for p in (HERE, CORE, BUILD):
    if p not in sys.path:
        sys.path.insert(0, p)

LIVE_DB = "/home/claude/restocker_live.db"

FAILS: list = []
PASSES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(label)
        print(f"  PASS  {label}")
    else:
        FAILS.append(f"{label} — {detail}")
        print(f"  FAIL  {label} — {detail}")


# ══════════════════════════════════════════════════════════════════════════
# Fixture — a COPY of the live db, real routes, a fake session cookie
# ══════════════════════════════════════════════════════════════════════════

TMP = Path(tempfile.mkdtemp(prefix="vtadmin-"))
os.chdir(TMP)
shutil.copy(LIVE_DB, TMP / "restocker.db")            # work on a COPY, never the original
os.environ["ESTATES_DB_PATH"] = str(TMP / "estates.db")
os.environ["VT_WEB_KEY_SECRET"] = "test-secret-not-a-real-one"
os.environ["VT_STAFF_IDS"] = "900000000000000009"
os.environ["HUB_DB_PATH"] = str(TMP / "restocker.db")
os.environ.pop("OSENTAR_BASE_URL", None)
# Dev-login baseline: two of the three gates satisfied so individual gate tests can
# flip exactly one and watch the door shut.
os.environ["VTECH_DEV_LOGIN"] = "1"
os.environ["VTECH_ENV"] = "test"

if Path(f"{BUILD}/estates.db").exists():
    shutil.copy(f"{BUILD}/estates.db", TMP / "estates.db")

import Restocker_db as rdb                      # noqa: E402
rdb.init_db()

import vt_web_shell as shell                    # noqa: E402
import hub_web                                  # noqa: E402
import banking_web                              # noqa: E402
import estates_web                              # noqa: E402
import messages_web as M                        # noqa: E402
import admin_web as A                           # noqa: E402

hub_web._hub_conn()
from aiohttp import web                         # noqa: E402
from aiohttp.test_utils import TestClient, TestServer   # noqa: E402

STAFF = "900000000000000009"
# A real wallet holder in the live db — the view-as subject / a normal player.
TARGET = "100000000000000012"
PLAYER = "100000000000000077"     # a second non-staff session
MARKET = "greyhames"              # a live listed company, for the trade route

_SESSIONS = {
    "tok-staff": {"user_id": STAFF, "name": "V Tech staff", "csrf": "csrf-staff"},
    "tok-target": {"user_id": TARGET, "name": "Tamsin Roe", "csrf": "csrf-target"},
    "tok-player": {"user_id": PLAYER, "name": "Ord Vasey", "csrf": "csrf-player"},
}

# Names, primed directly (the bot is not running here). A FIXTURE inside a test file.
import history_web as HW                      # noqa: E402  (the sales log's resolver)
for _mod in (M, A, HW):
    _mod._NAMES_CACHE = {STAFF: "V Tech staff", TARGET: "Tamsin Roe", PLAYER: "Ord Vasey"}
    _mod._NAMES_AT = time.time()

# Give TARGET and PLAYER wallets so they resolve and can be messaged.
with sqlite3.connect(TMP / "restocker.db") as _c:
    for _u in (TARGET, PLAYER, STAFF):
        _c.execute("INSERT INTO balances (user_id, coins) VALUES (?, 25000) "
                   "ON CONFLICT(user_id) DO UPDATE SET coins=25000", (_u,))


def fake_session(request):
    return _SESSIONS.get(request.cookies.get("vtm_sess") or "")


shell.set_session_provider(fake_session)


def conn():
    c = sqlite3.connect(TMP / "restocker.db")
    c.row_factory = sqlite3.Row
    return c


def q(sql: str, args=()) -> list:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def count(table: str, where: str = "", args=()) -> int:
    sql = f"SELECT COUNT(*) AS n FROM {table}" + (f" WHERE {where}" if where else "")
    try:
        with conn() as c:
            return int(c.execute(sql, args).fetchone()["n"])
    except Exception:
        return -1


def build_app() -> web.Application:
    app = web.Application()
    shell.register_shell_routes(app)
    hub_web.register_hub_routes(app)
    banking_web.register_banking_routes(app)
    estates_web.register_estates_routes(app)
    M.register_messages_routes(app)
    A.register_admin_routes(app)
    return app


def client_for(app, token=None) -> TestClient:
    return TestClient(TestServer(app), cookies=({"vtm_sess": token} if token else {}))


def hdr(token) -> dict:
    return {"X-CSRF-Token": _SESSIONS[token]["csrf"]} if token else {}


def clear_view_as():
    try:
        with conn() as c:
            c.execute("DELETE FROM admin_view_as")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# A1/A2 — the staff gate and the nav
# ══════════════════════════════════════════════════════════════════════════

async def t_gate_and_nav():
    print("\n[A1/A2] Staff gate: 401 anon, 403 a player, 200 staff — and the nav")
    app = build_app()
    for tok, want, who in ((None, 401, "anonymous"), ("tok-player", 403, "a normal player"),
                           ("tok-staff", 200, "staff")):
        c = client_for(app, tok)
        await c.start_server()
        try:
            r = await c.get("/admin")
            check(f"GET /admin as {who} -> {want}", r.status == want, str(r.status))
        finally:
            await c.close()

    # The Owner tab must be in the served markup for staff and ABSENT for a player —
    # not merely CSS-hidden. The hub markets page renders the dynamic nav.
    cs = client_for(app, "tok-staff"); await cs.start_server()
    cp = client_for(app, "tok-player"); await cp.start_server()
    try:
        staff_html = await (await cs.get("/hub/markets")).text()
        player_html = await (await cp.get("/hub/markets")).text()
        check("staff nav offers the Owner console", 'href="/admin"' in staff_html,
              "no /admin link for staff")
        check("a player's nav does NOT contain the Owner console",
              'href="/admin"' not in player_html, "player saw /admin in the nav")
    finally:
        await cs.close(); await cp.close()


# ══════════════════════════════════════════════════════════════════════════
# A3/A4 — enter/exit state + audit, and the view-as render + banner
# ══════════════════════════════════════════════════════════════════════════

async def t_enter_render_exit():
    print("\n[A3/A4] Enter -> render as target with banner -> exit; each audited")
    clear_view_as()
    app = build_app()
    cs = client_for(app, "tok-staff")
    await cs.start_server()
    try:
        # A player cannot enter view-as at all.
        cp = client_for(app, "tok-player"); await cp.start_server()
        r = await cp.post("/api/admin/view-as/enter", json={"subject": TARGET},
                          headers=hdr("tok-player"))
        check("a player cannot enter view-as -> 403", r.status == 403, str(r.status))
        await cp.close()

        # Staff enters by id.
        r = await cs.post("/api/admin/view-as/enter", json={"subject": TARGET},
                          headers=hdr("tok-staff"))
        j = await r.json()
        check("staff enters view-as -> ok", r.status == 200 and j.get("ok"), str(j)[:160])
        check("view-as state is server-side, keyed to the staff id",
              (shell.active_view_as(STAFF) or {}).get("target_id") == TARGET,
              str(shell.active_view_as(STAFF)))

        # The rendered page is the TARGET's, and it carries the un-dismissable banner.
        html_ = await (await cs.get("/messages")).text()
        check("a staff session in view-as renders the banner",
              'id="viewAsBar"' in html_ and "VIEWING AS" in html_, "no banner")
        check("the banner names the subject", "Tamsin Roe" in html_, "subject not named")
        check("the only control on the banner is Exit (no dismiss)",
              "exitViewAs()" in html_ and 'onclick="dismiss' not in html_.lower(),
              "found a dismiss control")

        # Audit: entry AND the page view are recorded.
        acts = [row["action"] for row in shell.audit_by_actor(STAFF)]
        check("entry is audited", "view_as_enter" in acts, str(acts))
        check("the page view is audited", "view_page" in acts, str(acts))

        # Exit.
        r = await cs.post("/api/admin/view-as/exit", json={}, headers=hdr("tok-staff"))
        j = await r.json()
        check("staff exits view-as -> ok", r.status == 200 and j.get("ok"), str(j)[:120])
        check("view-as state is cleared on exit", shell.active_view_as(STAFF) is None, "")
        acts = [row["action"] for row in shell.audit_by_actor(STAFF)]
        check("exit is audited", "view_as_exit" in acts, str(acts))

        # A page rendered while NOT in view-as has no banner.
        html2 = await (await cs.get("/messages")).text()
        check("no banner once view-as is off", 'id="viewAsBar"' not in html2, "banner lingered")
    finally:
        await cs.close()


# ══════════════════════════════════════════════════════════════════════════
# A5/A6 — read-only send and read, asserted on ROWS (fails before, passes after)
# ══════════════════════════════════════════════════════════════════════════

async def t_read_only_messaging():
    print("\n[A5/A6] In view-as, send and mark-read are refused and write no rows")
    clear_view_as()
    app = build_app()
    ct = client_for(app, "tok-target"); await ct.start_server()
    cs = client_for(app, "tok-staff"); await cs.start_server()
    try:
        # POSITIVE CONTROL: TARGET opens a thread with STAFF (staff is always a contact).
        page = await (await ct.get("/messages")).text()
        m = re.search(r'<option value="%s" data-key="([^"]+)"' % re.escape(STAFF), page)
        key = m.group(1) if m else ""
        before = count("vt_messages")
        r = await ct.post("/api/messages/send",
                          json={"to": STAFF, "body": "Opening a thread.", "idempotency_key": key},
                          headers=hdr("tok-target"))
        j = await r.json()
        check("positive control: a real send DOES write a row",
              r.status == 200 and count("vt_messages") == before + 1, str(j)[:160])
        tid = j.get("thread_id")

        # Now STAFF enters view-as as TARGET and tries to send a reply.
        shell.enter_view_as(STAFF, TARGET, "Tamsin Roe", "1.2.3.4")
        before = count("vt_messages")
        r = await cs.post("/api/messages/send",
                          json={"thread_id": tid, "body": "written as them",
                                "idempotency_key": "whatever"},
                          headers=hdr("tok-staff"))
        j = await r.json()
        check("in view-as, a send is refused -> 403 view_as_read_only",
              r.status == 403 and j.get("code") == "view_as_read_only", str(j)[:160])
        check("in view-as, the send wrote ZERO rows",
              count("vt_messages") == before, "a message row appeared under view-as")

        # mark-read: positive control (STAFF, not in view-as, is a participant), then refused.
        shell.exit_view_as(STAFF, "")
        before = count("vt_message_reads")
        r = await cs.post("/api/messages/read", json={"thread_id": tid, "up_to": 9},
                          headers=hdr("tok-staff"))
        check("positive control: a real mark-read writes a watermark",
              r.status == 200 and count("vt_message_reads") == before + 1, str(r.status))

        shell.enter_view_as(STAFF, TARGET, "Tamsin Roe", "1.2.3.4")
        reads_before = count("vt_message_reads")
        r = await cs.post("/api/messages/read", json={"thread_id": tid, "up_to": 10 ** 9},
                          headers=hdr("tok-staff"))
        j = await r.json()
        check("in view-as, mark-read is refused -> 403 view_as_read_only",
              r.status == 403 and j.get("code") == "view_as_read_only", str(j)[:160])
        check("in view-as, mark-read moved NO watermark (no row for the target)",
              count("vt_message_reads") == reads_before
              and count("vt_message_reads", "user_id=? AND thread_id=?", (TARGET, tid)) == 0,
              "a read watermark was written under view-as")

        # And the refused write is itself audited.
        acts = [row["action"] for row in shell.audit_by_actor(STAFF)]
        check("a refused write is audited", "write_refused" in acts, str(acts))
        shell.exit_view_as(STAFF, "")
    finally:
        await ct.close(); await cs.close()


# ══════════════════════════════════════════════════════════════════════════
# A7/A8 — read-only trade and coin move, asserted on rows
# ══════════════════════════════════════════════════════════════════════════

async def t_read_only_trade_and_coin():
    print("\n[A7/A8] In view-as, a trade and a coin move are refused, writing nothing")
    clear_view_as()
    app = build_app()
    cs = client_for(app, "tok-staff"); await cs.start_server()
    try:
        shell.enter_view_as(STAFF, TARGET, "Tamsin Roe", "1.2.3.4")

        holdings_before = count("stock_holdings")
        r = await cs.post(f"/hub/markets/{MARKET}/trade",
                          json={"side": "buy", "shares": 1, "idempotency_key": "x"},
                          headers=hdr("tok-staff"))
        j = await r.json()
        check("in view-as, a markets trade is refused -> view_as_read_only",
              r.status == 403 and j.get("code") == "view_as_read_only", str(j)[:160])
        check("in view-as, the trade wrote no holding",
              count("stock_holdings") == holdings_before, "a holding changed under view-as")

        bids_before = count("land_bids")
        r = await cs.post("/api/estates/bid",
                          json={"listing_id": 1, "amount": 5000, "idempotency_key": "x"},
                          headers=hdr("tok-staff"))
        j = await r.json()
        check("in view-as, an estates bid (a coin move) is refused -> view_as_read_only",
              r.status == 403 and j.get("code") == "view_as_read_only", str(j)[:160])
        check("in view-as, the bid wrote no row",
              count("land_bids") == bids_before, "a bid appeared under view-as")

        shell.exit_view_as(STAFF, "")
    finally:
        await cs.close()


# ══════════════════════════════════════════════════════════════════════════
# A9 — the DERIVED sweep: every write-chokepoint route refuses under view-as
# ══════════════════════════════════════════════════════════════════════════

_CHOKEPOINTS = ("money_post", "require_post_session", "idempotent_post")


def _mutating_routes(app) -> list:
    """DERIVE the set of mutating routes from the handlers' own source, not a hand
    list. A route is a mutation iff its handler (or the closure it was built from)
    reaches one of the two write chokepoints. This cannot go stale: a new POST route
    added through `money_post` next month is picked up here automatically, and the
    control-plane routes (view-as enter/exit, dev-login) fall out on their own because
    they deliberately do NOT use a chokepoint."""
    out = []
    seen = set()
    for r in app.router.routes():
        if r.method != "POST":
            continue
        res = r.resource
        canon = getattr(res, "canonical", None) or ""
        if canon in seen:
            continue
        seen.add(canon)
        try:
            src = inspect.getsource(r.handler)
        except Exception:
            src = ""
        if any(tok in src for tok in _CHOKEPOINTS):
            out.append(canon)
    return out


async def t_derived_sweep():
    print("\n[A9] Derived sweep — every chokepoint route refuses under view-as")
    clear_view_as()
    app = build_app()
    routes = _mutating_routes(app)
    check("the derived mutating-route set is non-trivial", len(routes) >= 6, str(routes))
    cs = client_for(app, "tok-staff")
    await cs.start_server()
    try:
        shell.enter_view_as(STAFF, TARGET, "Tamsin Roe", "1.2.3.4")
        for canon in routes:
            path = re.sub(r"\{[^}]+\}", MARKET, canon)   # fill any path param
            r = await cs.post(path, json={}, headers=hdr("tok-staff"))
            try:
                code = (await r.json()).get("code")
            except Exception:
                code = None
            check(f"view-as refuses {canon}",
                  r.status == 403 and code == "view_as_read_only", f"{r.status} {code}")
        shell.exit_view_as(STAFF, "")
    finally:
        await cs.close()


# ══════════════════════════════════════════════════════════════════════════
# A10 — the subject can read the rows about themselves
# ══════════════════════════════════════════════════════════════════════════

async def t_subject_can_read_audit():
    print("\n[A10] The subject can see the audit rows about themselves")
    clear_view_as()
    app = build_app()
    cs = client_for(app, "tok-staff"); await cs.start_server()
    ct = client_for(app, "tok-target"); await ct.start_server()
    try:
        await cs.post("/api/admin/view-as/enter", json={"subject": TARGET},
                      headers=hdr("tok-staff"))
        await cs.get("/messages")          # a page view, recorded about TARGET
        await cs.post("/api/admin/view-as/exit", json={}, headers=hdr("tok-staff"))

        r = await ct.get("/api/admin/observed-me")
        j = await r.json()
        acts = {row["action"] for row in j.get("observed", [])}
        check("the subject's own audit feed is readable by the subject",
              r.status == 200 and "view_as_enter" in acts and "view_page" in acts, str(acts))
        # And it is scoped to the subject — a subject sees only rows about themselves.
        rows = shell.audit_for_subject(TARGET)
        check("every row the subject sees is about the subject",
              all(str(row["subject_id"]) == TARGET for row in rows), "leaked another subject")
    finally:
        await cs.close(); await ct.close()


# ══════════════════════════════════════════════════════════════════════════
# A11 — append-only, asserted on the AST
# ══════════════════════════════════════════════════════════════════════════

def t_append_only_ast():
    print("\n[A11] admin_audit is append-only — asserted on the AST")
    offenders = []
    saw_insert = False
    for mod in (shell, A):
        src = Path(inspect.getsourcefile(mod)).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                up = " ".join(node.value.upper().split())
                if "ADMIN_AUDIT" in up:
                    if "INSERT INTO ADMIN_AUDIT" in up:
                        saw_insert = True
                    if "UPDATE ADMIN_AUDIT" in up or "DELETE FROM ADMIN_AUDIT" in up:
                        offenders.append((mod.__name__, node.value[:60]))
    check("there is an INSERT into admin_audit (the trail is written)", saw_insert, "")
    check("no UPDATE and no DELETE targets admin_audit anywhere",
          not offenders, str(offenders))


# ══════════════════════════════════════════════════════════════════════════
# A12 — the kill switch: toggles config, shows the figures, refused in view-as
# ══════════════════════════════════════════════════════════════════════════

async def t_kill_switch():
    print("\n[A12] The bidding kill switch — figures beside the button, and guarded")
    clear_view_as()
    app = build_app()
    cs = client_for(app, "tok-staff"); await cs.start_server()
    cp = client_for(app, "tok-player"); await cp.start_server()
    try:
        rdb.set_config(A.FREEZE_KEY, "0")
        page = await (await cs.get("/admin")).text()
        check("the console shows the figures the freeze will move (coin in bids)",
              "escrowed in current top bids" in page, "no figures beside the switch")
        key = re.search(r'onclick="setFreeze\(true, this\)" data-key="([^"]+)"', page)
        check("the console mints a form key for the freeze", bool(key), "no key")

        r = await cs.post("/api/admin/freeze",
                          json={"freeze": True, "idempotency_key": key.group(1) if key else ""},
                          headers=hdr("tok-staff"))
        j = await r.json()
        check("staff throws the kill switch -> ok", r.status == 200 and j.get("ok"), str(j)[:140])
        check("the switch actually set the config",
              str(rdb.get_config(A.FREEZE_KEY)) == "1", str(rdb.get_config(A.FREEZE_KEY)))

        # A player cannot touch it.
        r = await cp.post("/api/admin/freeze", json={"freeze": False}, headers=hdr("tok-player"))
        check("a player cannot throw the kill switch -> 403", r.status == 403, str(r.status))

        # And in view-as, even staff cannot — it is an economy write.
        shell.enter_view_as(STAFF, TARGET, "Tamsin Roe", "1.2.3.4")
        r = await cs.post("/api/admin/freeze", json={"freeze": False, "idempotency_key": "x"},
                          headers=hdr("tok-staff"))
        j = await r.json()
        check("in view-as, the kill switch is refused too",
              r.status == 403 and j.get("code") == "view_as_read_only", str(j)[:140])
        check("the config was NOT changed under view-as",
              str(rdb.get_config(A.FREEZE_KEY)) == "1", "config moved under view-as")
        shell.exit_view_as(STAFF, "")
    finally:
        await cs.close(); await cp.close()


# ══════════════════════════════════════════════════════════════════════════
# A13 — dev login: three independent gates, each fails shut; loopback unit
# ══════════════════════════════════════════════════════════════════════════

class _FakeReq:
    """Just enough request for `_loopback_bound`: a transport with a sockname."""
    def __init__(self, host, headers=None):
        self._host = host
        # `_loopback_bound` also refuses on any proxy header — a fake request needs a
        # headers mapping or the unit test dies before it asserts anything.
        self.headers = dict(headers or {})

        class _T:
            def get_extra_info(_s, k):
                return (host, 8080) if k == "sockname" else None
        self.transport = _T()


def t_loopback_unit():
    print("\n[A13a] _loopback_bound reads the SERVER bind — public fails, loopback passes")
    check("a public bind is NOT loopback", A._loopback_bound(_FakeReq("203.0.113.7")) is False,
          "public counted as loopback")
    check("0.0.0.0 (all interfaces) is NOT loopback",
          A._loopback_bound(_FakeReq("0.0.0.0")) is False, "0.0.0.0 counted as loopback")
    check("127.0.0.1 is loopback", A._loopback_bound(_FakeReq("127.0.0.1")) is True, "")
    check("::1 is loopback", A._loopback_bound(_FakeReq("::1")) is True, "")


async def t_dev_login_gates():
    print("\n[A13b] Dev login refuses on EACH gate independently")
    app = build_app()
    minted = {}
    A.set_dev_session_minter(lambda uid, name: minted.setdefault(uid, "tok-" + uid) or "tok-" + uid)
    c = client_for(app, None)
    await c.start_server()
    try:
        # Baseline: all three gates satisfied (VTECH_DEV_LOGIN=1, VTECH_ENV=test, loopback).
        os.environ["VTECH_DEV_LOGIN"] = "1"
        os.environ["VTECH_ENV"] = "test"
        r = await c.post("/api/admin/dev-login", json={"user_id": TARGET})
        j = await r.json()
        check("all gates met on loopback -> dev login succeeds",
              r.status == 200 and j.get("ok") and j.get("user_id") == TARGET, str(j)[:140])

        # Gate 1: the opt-in.
        os.environ["VTECH_DEV_LOGIN"] = "0"
        r = await c.post("/api/admin/dev-login", json={"user_id": TARGET})
        j = await r.json()
        check("gate 1 (VTECH_DEV_LOGIN) unmet -> 403",
              r.status == 403 and j.get("code") == "dev_login_off", str(j)[:120])
        os.environ["VTECH_DEV_LOGIN"] = "1"

        # Gate 3: the non-production marker.
        os.environ["VTECH_ENV"] = "production"
        r = await c.post("/api/admin/dev-login", json={"user_id": TARGET})
        j = await r.json()
        check("gate 3 (VTECH_ENV) unmet -> 403",
              r.status == 403 and j.get("code") == "looks_like_production", str(j)[:120])
        os.environ["VTECH_ENV"] = "test"

        # Gate 2: NOT on loopback. Simulate a non-loopback bind by making the check say so;
        # this is the "refuses when the server is not on loopback" proof at the route level,
        # on top of the pure-function unit above.
        orig = A._loopback_bound
        A._loopback_bound = lambda req: False
        try:
            r = await c.post("/api/admin/dev-login", json={"user_id": TARGET})
            j = await r.json()
            check("gate 2 (not on loopback) unmet -> 403",
                  r.status == 403 and j.get("code") == "not_loopback", str(j)[:120])
        finally:
            A._loopback_bound = orig
    finally:
        await c.close()
        A.set_dev_session_minter(None)


# ══════════════════════════════════════════════════════════════════════════
# A14 — a body-supplied identity on an admin write is ignored and alarmed
# ══════════════════════════════════════════════════════════════════════════

async def t_body_identity():
    print("\n[A14] A body-supplied user_id on an admin write is ignored + alarmed")
    clear_view_as()
    app = build_app()
    cs = client_for(app, "tok-staff"); await cs.start_server()
    try:
        before = count("hub_attack_log")
        # Try to enter view-as while smuggling a different actor id in the body.
        r = await cs.post("/api/admin/view-as/enter",
                          json={"subject": TARGET, "user_id": PLAYER, "actor_id": PLAYER},
                          headers=hdr("tok-staff"))
        check("the enter still runs off the SESSION, not the body id",
              r.status == 200, str(r.status))
        # The acting staff in the audit row is the session, never the smuggled id.
        last = shell.audit_by_actor(STAFF)
        check("the audited actor is the session, not the body id",
              bool(last) and last[0]["action"] == "view_as_enter", str(last[:1]))
        check("the smuggled id raised a durable attack alarm",
              count("hub_attack_log") > before, "no hub_attack_log row")
        shell.exit_view_as(STAFF, "")
    finally:
        await cs.close()


# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# A15 — THE SALES LOG. Staff-only, read-only, audited, and dated by the event
# ══════════════════════════════════════════════════════════════════════════

async def t_sales_log():
    print("\n[A15] The sales log: staff only, writes nothing, tells the players it read")
    app = build_app()

    # The row this page exists for: an off-book transfer between two OTHER accounts,
    # so no viewer of /history can see it. Inserted into the COPY, by the test.
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO share_gifts (key, market_id, from_user, "
                  "to_user, shares, basis, value_coins, note, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  ("test:sale:a->b", MARKET, TARGET, PLAYER, 12.5, 100.0, 250000,
                   "Tamsin -> Ord: paid 250,000c on 3 Aug 2026, in-game.",
                   "2026-08-14T09:00:00+00:00"))

    before = {t: count(t) for t in ("share_gifts", "stock_trade_log", "coin_ledger",
                                    "stock_holdings", "balances", "markets")}

    async with client_for(app) as ca:
        r = await ca.get("/admin/sales")
        check("anonymous gets 401 on the sales log", r.status == 401, str(r.status))
        anon = await r.text()
        check("the anonymous refusal carries no sale in it",
              "250,000" not in anon and TARGET not in anon, "the 401 leaks the log")

    async with client_for(app, "tok-player") as cp:
        r = await cp.get("/admin/sales")
        check("a normal player gets 403", r.status == 403, str(r.status))
        pbody = await r.text()
        check("the player's 403 carries no sale in it",
              "250,000" not in pbody and "OTC TRANSFER" not in pbody, "the 403 leaks")

    async with client_for(app, "tok-staff") as cs:
        r = await cs.get("/admin/sales")
        body = await r.text()
        check("staff get the sales log", r.status == 200, str(r.status))
        check("the log shows a transfer neither party is the viewer",
              "OTC TRANSFER" in body and "12.50" in body, "the row this page exists for")
        check("both sides of that transfer are named",
              "Tamsin Roe" in body and "Ord Vasey" in body, "a side is unnamed")
        check("it is dated by the EVENT date read from the note",
              "03 Aug 2026" in body, "the event date is missing")
        check("and it prints the text that date was read from",
              "stated in the note as" in body and "3 Aug 2026" in body,
              "a parsed date with no visible source is a claim")
        check("the row's write stamp is NOT on the page",
              "14 Aug 2026" not in body, "created_at is rendered as if it were history")
        check("exchange fills are on it too",
              "EXCHANGE" in body, "only one of the two sources is read")
        check("a zero-price unwind is not dressed as a sale",
              ("liquidated" not in body) or ("not an exchange fill" in body),
              "a forced unwind renders as a fill")
        check("the console links to it",
              "Open the sales log" in await (await cs.get("/admin")).text(), "")

        # It is a READ. Every row count in the economy is identical afterwards.
        after = {t: count(t) for t in before}
        check("driving the sales log writes no row anywhere", before == after,
              f"{before} -> {after}")

        # The filters actually narrow, and never widen.
        botc = await (await cs.get("/admin/sales?type=otc")).text()
        bex = await (await cs.get("/admin/sales?type=exchange")).text()
        bmk = await (await cs.get("/admin/sales?market=__nope__")).text()
        check("the OTC filter excludes exchange fills", "EXCHANGE BUY" not in botc, "")
        check("the exchange filter excludes OTC transfers", "OTC TRANSFER" not in bex, "")
        check("an unknown market filters to nothing, and says so",
              "No share movements match" in bmk, "an empty filter invented rows")

    # THE AUDIT. Both players in the result can see that their rows were read.
    for who, label in ((TARGET, "the sender"), (PLAYER, "the recipient")):
        rows_ = shell.audit_for_subject(who, 50)
        check(f"{label} can see the read in their own audit trail",
              any(r["action"] == "sales_log:read" and r["actor_id"] == STAFF
                  for r in rows_),
              "a staff read of their trades left no row they can see")
    async with client_for(app, "tok-target") as ct:
        j = await (await ct.get("/api/admin/observed-me")).json()
        acts = {row["action"] for row in j.get("observed", [])}
        check("and it reaches them through the route they actually use",
              "sales_log:read" in acts, str(sorted(acts))[:160])
    check("the actor's own console shows the view",
          any(r["action"] == "sales_log:view" for r in shell.audit_by_actor(STAFF, 50)), "")

    # The one thing this page must never grow: a wallet reason. Asserted on the SQL
    # STRINGS the sales functions actually execute — a docstring promising it is not a
    # guarantee, and the prose above `read_all_otc` mentions the table by name on purpose.
    tree = ast.parse(inspect.getsource(A))
    sales_fns = {"read_all_otc", "read_all_trades", "sales_events", "sales_totals",
                 "h_sales", "_sales_tile", "_audit_sales_view"}
    lits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in sales_fns:
            body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                     and isinstance(getattr(node.body[0], "value", None),
                                                    ast.Constant)) else node.body
            for sub in body:
                for n in ast.walk(sub):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        lits.append(n.value)
    # A statement, not any string containing the word "from" — the page has a
    # "From -> to" column header and an error line that reads "missing from this page".
    sql = [t for t in lits
           if re.search(r"\bSELECT\b\s.*\bFROM\b|\b(INSERT|UPDATE|DELETE|DROP|ALTER|"
                        r"CREATE|REPLACE)\b\s+(INTO|FROM|TABLE|SET|OR)\b", t, re.I | re.S)]
    check("the sales functions execute at least the two reads they promise",
          len(sql) >= 2, str(len(sql)))
    check("no wallet or balance table is named in any SQL the sales log runs",
          not any(re.search(r"coin_ledger|balances", t, re.I) for t in sql),
          "a wallet reason is one query away from staff eyes")
    check("every SQL the sales log runs is a SELECT",
          all(t.strip().upper().startswith("SELECT") for t in sql),
          "the sales log carries a write verb")


async def main():
    t_append_only_ast()
    t_loopback_unit()
    await t_gate_and_nav()
    await t_enter_render_exit()
    await t_read_only_messaging()
    await t_read_only_trade_and_coin()
    await t_derived_sweep()
    await t_subject_can_read_audit()
    await t_kill_switch()
    await t_dev_login_gates()
    await t_body_identity()
    await t_sales_log()

    print("\n" + "=" * 72)
    print(f"  {len(PASSES)} passed, {len(FAILS)} failed")
    for f in FAILS:
        print(f"  FAIL  {f}")
    print("=" * 72)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
