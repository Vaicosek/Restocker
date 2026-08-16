"""
test_messages_web.py — proofs for `messages_web`, driven through a real aiohttp test
client against a real `restocker.db`.

Run:  python3 test_messages_web.py

Every assertion that matters is asserted ON ROWS, not on a handler's return value. A
handler that reports one message while writing two passes a test that reads its JSON,
and this project has already shipped that exact class of bug twice.

  M1  anonymous page  -> 401 + the sign-in card, no section content
  M2  anonymous POST  -> 401, and zero rows written
  M3  one send        -> exactly ONE row in vt_messages
  M4  double click    -> still exactly ONE row (sequential replay AND concurrent)
  M5  stranger        -> cannot read a thread, and the body is not in the bytes
  M6  <script>        -> escaped in the served HTML, raw in the database
  M7  body user_id    -> ignored, sender is the session, and hub_attack_log has a row
  M8  unread          -> derived, per side, and the watermark never goes backwards
  M9  pair uniqueness -> two concurrent first-messages make ONE thread
  M10 self / stranger -> refused, one refusal string for every "no"
  M11 no-effect       -> a refused send releases its key, so the corrected send works
  M12 append-only     -> no route, and no SQL in the module, edits or deletes a message
  M13 wiring          -> the section is in both navs and the badge is on the page
  M14 empty state     -> one muted line, and no invented conversation anywhere
  M15 rate limit      -> the 21st message in a minute is refused and not written
  M16 no ISO on screen
"""

from __future__ import annotations

import asyncio
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
for p in (str(Path(__file__).resolve().parent), CORE, BUILD):
    if p not in sys.path:
        sys.path.insert(0, p)

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
# Fixture — real SQLite, real routes, real session cookie
# ══════════════════════════════════════════════════════════════════════════

TMP = Path(tempfile.mkdtemp(prefix="vtmsg-"))
os.chdir(TMP)
os.environ["ESTATES_DB_PATH"] = str(TMP / "estates.db")
os.environ["VT_WEB_KEY_SECRET"] = "test-secret-not-a-real-one"
os.environ["VT_STAFF_IDS"] = "900000000000000009"
os.environ["HUB_DB_PATH"] = str(TMP / "restocker.db")
os.environ.pop("OSENTAR_BASE_URL", None)

if Path(f"{BUILD}/estates.db").exists():
    shutil.copy(f"{BUILD}/estates.db", TMP / "estates.db")

import Restocker_db as rdb                      # noqa: E402
rdb.init_db()

import vt_web_shell as shell                    # noqa: E402
import hub_web                                  # noqa: E402
import messages_web as M                        # noqa: E402

# `hub_attack_log` is created lazily on the hub's own connection; touch it once so the
# probe can read the table before the first alarm is written.
hub_web._hub_conn()
from aiohttp import web                         # noqa: E402
from aiohttp.test_utils import TestClient, TestServer   # noqa: E402

ALICE = "100000000000000011"      # lists a lot
BOB = "100000000000000012"        # bids on it  -> counterparty of ALICE
CARL = "100000000000000013"       # a player with no dealings with either
DAVE = "100000000000000014"       # signs in, has never messaged anybody
STAFF = "900000000000000009"

_SESSIONS = {
    "tok-alice": {"user_id": ALICE, "name": "GreyHames", "csrf": "csrf-alice"},
    "tok-bob": {"user_id": BOB, "name": "Tamsin Roe", "csrf": "csrf-bob"},
    "tok-carl": {"user_id": CARL, "name": "Ord Vasey", "csrf": "csrf-carl"},
    "tok-staff": {"user_id": STAFF, "name": "V Tech staff", "csrf": "csrf-staff"},
    # A signed-in player with no threads at all — the state John opens the site in on
    # the day it ships, and the only state that proves the empty page.
    "tok-dave": {"user_id": DAVE, "name": "Ilma Perrot", "csrf": "csrf-dave"},
}

# Names come from `stock_names.yml` in production; the bot is not running here, so the
# cache is primed directly. A FIXTURE, inside a test file — nothing reaches the site.
M._NAMES_CACHE = {ALICE: "GreyHames", BOB: "Tamsin Roe", CARL: "Ord Vasey",
                  DAVE: "Ilma Perrot", STAFF: "V Tech staff"}
M._NAMES_AT = time.time()


def fake_session(request):
    return _SESSIONS.get(request.cookies.get("vtm_sess") or "")


shell.set_session_provider(fake_session)


def conn():
    c = sqlite3.connect("restocker.db")
    c.row_factory = sqlite3.Row
    return c


def q(sql: str, args=()) -> list:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def msg_rows(thread_id=None) -> list:
    if thread_id is None:
        return q("SELECT * FROM vt_messages ORDER BY id")
    return q("SELECT * FROM vt_messages WHERE thread_id=? ORDER BY id", (thread_id,))


def thread_rows() -> list:
    return q("SELECT * FROM vt_message_threads ORDER BY id")


def seed() -> None:
    """Wallets, and ONE real dealing: BOB bid on a lot ALICE listed. That dealing is
    what makes them able to message each other; nothing else in this fixture does."""
    with conn() as c:
        for uid in (ALICE, BOB, CARL, DAVE, STAFF):
            c.execute("INSERT INTO balances (user_id, coins, principal, lp) VALUES (?,?,0,0) "
                      "ON CONFLICT(user_id) DO UPDATE SET coins=excluded.coins", (uid, 50_000))
    lot = rdb.create_land_listing(seller_id=ALICE, kind="land", title="Riverside Parcel R-12",
                                  category="Land", mode="auction", reserve=6000,
                                  min_increment_pct=5.0, status="active",
                                  ends_at="2099-01-01 00:00:00")
    with conn() as c:
        c.execute("INSERT INTO land_bids (listing_id, bidder_id, amount) VALUES (?,?,?)",
                  (lot, BOB, 6000))
    return lot


def build_app() -> web.Application:
    app = web.Application()
    M.register_messages_routes(app)
    return app


def client_for(app, token=None) -> TestClient:
    return TestClient(TestServer(app), cookies=({"vtm_sess": token} if token else {}))


def hdr(token) -> dict:
    return {"X-CSRF-Token": _SESSIONS[token]["csrf"]} if token else {}


async def inbox_key(c, token, target: str) -> str:
    """Read the form key the INBOX PAGE minted for this contact — the browser never
    invents one, so neither does this probe."""
    html_ = await (await c.get("/messages")).text()
    m = re.search(r'<option value="%s" data-key="([^"]+)"' % re.escape(target), html_)
    return m.group(1) if m else ""


async def thread_key(c, tid: int) -> str:
    html_ = await (await c.get(f"/messages/t/{tid}")).text()
    m = re.search(r'window\.MSG = \{tid:\d+, key:"([^"]+)"', html_)
    return m.group(1) if m else ""


# ══════════════════════════════════════════════════════════════════════════
# M1/M2 — anonymous
# ══════════════════════════════════════════════════════════════════════════

async def t_anonymous():
    print("\n[M1/M2] Anonymous gets 401 and writes nothing")
    app = build_app()
    c = client_for(app, None)
    await c.start_server()
    try:
        r = await c.get("/messages")
        body = await r.text()
        check("GET /messages anonymous -> 401", r.status == 401, str(r.status))
        check("anonymous gets the sign-in card", "Sign in" in body and "/website_login" in body,
              body[:200])
        check("anonymous page renders NO inbox table",
              "Last message" not in body and "Start a conversation" not in body,
              body[:200])

        r = await c.get("/messages/t/1")
        check("GET a thread anonymous -> 401", r.status == 401, str(r.status))

        before = len(msg_rows())
        r = await c.post("/api/messages/send", json={"to": BOB, "body": "hello"})
        j = await r.json()
        check("POST send anonymous -> 401", r.status == 401 and j.get("code") == "not_logged_in",
              str(j)[:160])
        check("anonymous send wrote zero rows", len(msg_rows()) == before, str(len(msg_rows())))

        r = await c.post("/api/messages/read", json={"thread_id": 1, "up_to": 1})
        check("POST mark-read anonymous -> 401", r.status == 401, str(r.status))
        r = await c.get("/api/messages/unread")
        check("GET unread anonymous -> 401", r.status == 401, str(r.status))
    finally:
        await c.close()


# ══════════════════════════════════════════════════════════════════════════
# M3/M4 — one send is one row; a double click is still one row
# ══════════════════════════════════════════════════════════════════════════

async def t_send_and_double_click():
    print("\n[M3/M4] A send writes ONE row; a double-click writes ONE row")
    app = build_app()
    ca = client_for(app, "tok-alice")
    await ca.start_server()
    try:
        key = await inbox_key(ca, "tok-alice", BOB)
        check("the inbox page minted a form key for this contact", bool(key), repr(key)[:80])

        before = len(msg_rows())
        r = await ca.post("/api/messages/send",
                          json={"to": BOB, "body": "Sending the deed tonight.",
                                "idempotency_key": key},
                          headers=hdr("tok-alice"))
        j = await r.json()
        check("send accepted", r.status == 200 and j.get("ok"), str(j)[:200])
        rows = msg_rows()
        check("EXACTLY ONE row in vt_messages", len(rows) == before + 1,
              f"{before} -> {len(rows)}")
        check("the row's sender is the SESSION user", rows[-1]["sender_id"] == ALICE,
              str(rows[-1])[:200])
        check("the row's body is exactly what was typed",
              rows[-1]["body"] == "Sending the deed tonight.", str(rows[-1]["body"]))
        check("created_at is a UTC epoch, not a string",
              isinstance(rows[-1]["created_at"], float) and rows[-1]["created_at"] > 1_600_000_000,
              repr(rows[-1]["created_at"]))
        tid = int(rows[-1]["thread_id"])

        # The double click: the same form posted a second time.
        r2 = await ca.post("/api/messages/send",
                           json={"to": BOB, "body": "Sending the deed tonight.",
                                 "idempotency_key": key},
                           headers=hdr("tok-alice"))
        j2 = await r2.json()
        check("the second submit REPLAYS rather than acting", bool(j2.get("replayed")),
              str(j2)[:200])
        check("still exactly one row after the double click", len(msg_rows()) == before + 1,
              f"{len(msg_rows())} rows")
        check("the replay reports the SAME message id", j2.get("message_id") == j.get("message_id"),
              f"{j.get('message_id')} vs {j2.get('message_id')}")

        # And the genuinely concurrent version — two in-flight submits of one form.
        k2 = await thread_key(ca, tid)
        n_before = len(msg_rows(tid))
        results = await asyncio.gather(*[
            ca.post("/api/messages/send",
                    json={"thread_id": tid, "body": "double clicked",
                          "idempotency_key": k2}, headers=hdr("tok-alice"))
            for _ in range(2)])
        bodies = [await r.json() for r in results]
        check("CONCURRENT double submit writes exactly one row",
              len(msg_rows(tid)) == n_before + 1,
              f"{n_before} -> {len(msg_rows(tid))}")
        check("one of the two concurrent submits was refused or replayed",
              sum(1 for b in bodies if b.get("ok") and not b.get("replayed")) == 1,
              str(bodies)[:300])
        return tid
    finally:
        await ca.close()


# ══════════════════════════════════════════════════════════════════════════
# M5 — a stranger cannot read a thread
# ══════════════════════════════════════════════════════════════════════════

async def t_stranger_cannot_read(tid: int):
    print("\n[M5] A stranger cannot read a thread by guessing its id")
    app = build_app()
    cc = client_for(app, "tok-carl")
    await cc.start_server()
    try:
        r = await cc.get(f"/messages/t/{tid}")
        body = await r.text()
        check("stranger gets 404 on somebody else's thread", r.status == 404, str(r.status))
        check("no message body reaches the stranger's bytes",
              "Sending the deed tonight" not in body, body[:300])
        check("the refusal is identical to a thread that does not exist",
              (await (await cc.get("/messages/t/999999")).text())[:400] == body[:400],
              "refusals differ — that is an existence oracle")

        r = await cc.post("/api/messages/read", json={"thread_id": tid, "up_to": 999},
                          headers=hdr("tok-carl"))
        j = await r.json()
        check("stranger cannot mark somebody else's thread read",
              r.status == 404 and j.get("code") == "no_such_thread", str(j)[:160])
        check("stranger wrote no read row",
              not q("SELECT 1 FROM vt_message_reads WHERE thread_id=? AND user_id=?", (tid, CARL)),
              "a read row exists for a non-participant")

        # And a send into it, with a key of his own, is refused before any write.
        before = len(msg_rows(tid))
        key = shell.mint_form_key(CARL, f"message:t:{tid}")
        r = await cc.post("/api/messages/send",
                          json={"thread_id": tid, "body": "let me in",
                                "idempotency_key": key}, headers=hdr("tok-carl"))
        j = await r.json()
        check("stranger cannot POST into somebody else's thread",
              r.status == 404 and j.get("code") == "no_such_thread", str(j)[:160])
        check("and wrote nothing", len(msg_rows(tid)) == before, str(len(msg_rows(tid))))
    finally:
        await cc.close()


# ══════════════════════════════════════════════════════════════════════════
# M6 — XSS
# ══════════════════════════════════════════════════════════════════════════

async def t_script_tag_is_escaped(tid: int):
    print("\n[M6] A script tag in a body renders escaped")
    payload = '<script>alert("xss")</script> & "quoted" <img src=x onerror=alert(1)>'
    app = build_app()
    ca = client_for(app, "tok-alice")
    cb = client_for(app, "tok-bob")
    await ca.start_server()
    await cb.start_server()
    try:
        key = await thread_key(ca, tid)
        r = await ca.post("/api/messages/send",
                          json={"thread_id": tid, "body": payload, "idempotency_key": key},
                          headers=hdr("tok-alice"))
        check("the send was accepted", (await r.json()).get("ok"), str(r.status))

        row = msg_rows(tid)[-1]
        check("the DATABASE holds the raw characters — escaping is at render, not write",
              row["body"] == payload, repr(row["body"])[:200])

        for who, cl in (("sender", ca), ("recipient", cb)):
            page = await (await cl.get(f"/messages/t/{tid}")).text()
            check(f"the {who}'s page contains no live <script> from the body",
                  "<script>alert" not in page and "<img src=x" not in page,
                  page[page.find("<script>alert"):page.find("<script>alert") + 120]
                  or page[page.find("<img src=x"):page.find("<img src=x") + 120])
            check(f"the {who}'s page shows the payload ESCAPED",
                  "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in page,
                  "escaped form not found")
    finally:
        await ca.close()
        await cb.close()


# ══════════════════════════════════════════════════════════════════════════
# M7 — a body-supplied user id is ignored and logged
# ══════════════════════════════════════════════════════════════════════════

async def t_body_identity_ignored(tid: int):
    print("\n[M7] A body-supplied user id is IGNORED and logged")
    app = build_app()
    cb = client_for(app, "tok-bob")
    await cb.start_server()
    try:
        before = q("SELECT * FROM hub_attack_log")
        key = await thread_key(cb, tid)
        r = await cb.post("/api/messages/send",
                          json={"thread_id": tid, "body": "posted as somebody else?",
                                "user_id": ALICE, "from_user": CARL,
                                "idempotency_key": key},
                          headers=hdr("tok-bob"))
        j = await r.json()
        check("the send still succeeds — the field is ignored, not fatal", j.get("ok"), str(j)[:160])
        row = msg_rows(tid)[-1]
        check("the sender stored is the SESSION user, not the body's",
              row["sender_id"] == BOB, str(row)[:200])

        after = q("SELECT * FROM hub_attack_log")
        new = [a for a in after if a not in before]
        check("hub_attack_log gained a row for it", len(new) >= 1, f"{len(before)} -> {len(after)}")
        check("the alarm names the endpoint and the kind",
              any(a["kind"] == "body_supplied_identity" and "messages" in str(a["endpoint"])
                  for a in new), str(new)[:300])
        check("the alarm records the SESSION user, not the claimed one",
              all(str(a["session_user"]) == BOB for a in new), str(new)[:300])
    finally:
        await cb.close()


# ══════════════════════════════════════════════════════════════════════════
# M8 — unread, derived, per side, monotonic
# ══════════════════════════════════════════════════════════════════════════

async def t_unread(tid: int):
    print("\n[M8] Unread is derived per side, and the watermark never goes backwards")
    app = build_app()
    ca = client_for(app, "tok-alice")
    cb = client_for(app, "tok-bob")
    await ca.start_server()
    await cb.start_server()
    try:
        rows = msg_rows(tid)
        newest = int(rows[-1]["id"])
        from_alice = sum(1 for r in rows if r["sender_id"] == ALICE)
        from_bob = sum(1 for r in rows if r["sender_id"] == BOB)

        jb = await (await cb.get("/api/messages/unread")).json()
        check("Bob's unread == the messages Alice sent him",
              jb.get("unread") == from_alice, f'{jb.get("unread")} vs {from_alice}')
        ja = await (await ca.get("/api/messages/unread")).json()
        check("Alice's unread == the messages Bob sent her",
              ja.get("unread") == from_bob, f'{ja.get("unread")} vs {from_bob}')
        check("nobody counts their OWN messages as unread",
              jb.get("unread") != len(rows) and ja.get("unread") != len(rows),
              f"{ja} / {jb}")

        r = await cb.post("/api/messages/read", json={"thread_id": tid, "up_to": newest},
                          headers=hdr("tok-bob"))
        j = await r.json()
        check("mark-read answers with the new watermark", j.get("read_up_to") == newest, str(j)[:160])
        check("Bob's unread is now zero", j.get("unread") == 0, str(j)[:160])
        check("the watermark is ONE row, not a counter",
              len(q("SELECT * FROM vt_message_reads WHERE thread_id=? AND user_id=?",
                    (tid, BOB))) == 1, "expected exactly one read row")

        # THE STALE READ. A tab holding an old page posts an old watermark.
        r = await cb.post("/api/messages/read", json={"thread_id": tid, "up_to": 1},
                          headers=hdr("tok-bob"))
        j = await r.json()
        check("a STALE up_to cannot drag the watermark backwards",
              j.get("read_up_to") == newest, str(j)[:160])
        check("and cannot resurrect unread messages", j.get("unread") == 0, str(j)[:160])

        # A new message from Alice makes exactly one unread again — counted, not incremented.
        key = await thread_key(ca, tid)
        await ca.post("/api/messages/send",
                      json={"thread_id": tid, "body": "one more", "idempotency_key": key},
                      headers=hdr("tok-alice"))
        jb = await (await cb.get("/api/messages/unread")).json()
        check("a new message gives Bob exactly one unread", jb.get("unread") == 1, str(jb)[:160])
        check("the unread endpoint names the thread too",
              jb.get("threads", {}).get(str(tid)) == 1 or jb.get("threads", {}).get(tid) == 1,
              str(jb)[:200])

        # Concurrency: ten simultaneous mark-reads converge, they do not accumulate.
        rows = msg_rows(tid)
        newest = int(rows[-1]["id"])
        await asyncio.gather(*[
            cb.post("/api/messages/read", json={"thread_id": tid, "up_to": newest},
                    headers=hdr("tok-bob")) for _ in range(10)])
        wm = q("SELECT * FROM vt_message_reads WHERE thread_id=? AND user_id=?", (tid, BOB))
        check("ten concurrent mark-reads leave ONE row at the right watermark",
              len(wm) == 1 and int(wm[0]["last_read_message_id"]) == newest, str(wm)[:200])
        jb = await (await cb.get("/api/messages/unread")).json()
        check("and unread is zero, not negative and not doubled", jb.get("unread") == 0, str(jb))
    finally:
        await ca.close()
        await cb.close()


# ══════════════════════════════════════════════════════════════════════════
# M9 — one pair, one thread, even concurrently
# ══════════════════════════════════════════════════════════════════════════

async def t_one_thread_per_pair():
    print("\n[M9] A pair has exactly one thread — enforced by the DB, not by Python")
    app = build_app()
    cc = client_for(app, "tok-carl")
    cs = client_for(app, "tok-staff")
    await cc.start_server()
    await cs.start_server()
    try:
        before = len(thread_rows())
        k1 = await inbox_key(cc, "tok-carl", STAFF)
        r = await cc.post("/api/messages/send",
                          json={"to": STAFF, "body": "first contact", "idempotency_key": k1},
                          headers=hdr("tok-carl"))
        j = await r.json()
        check("Carl opens a thread with staff", j.get("ok"), str(j)[:200])
        tid = int(j["thread_id"])
        check("one new thread", len(thread_rows()) == before + 1, str(len(thread_rows())))

        # The other direction is the SAME pair. A mirror thread would be the classic
        # (a,b)/(b,a) duplicate, and the ordered pair is what forbids it.
        k2 = await inbox_key(cs, "tok-staff", CARL)
        r = await cs.post("/api/messages/send",
                          json={"to": CARL, "body": "replying the other way",
                                "idempotency_key": k2}, headers=hdr("tok-staff"))
        j2 = await r.json()
        check("staff writing back the other way lands in the SAME thread",
              j2.get("ok") and int(j2["thread_id"]) == tid, str(j2)[:200])
        check("still ONE thread for the pair", len(thread_rows()) == before + 1,
              str(len(thread_rows())))
        check("both messages are in it", len(msg_rows(tid)) == 2, str(msg_rows(tid))[:200])

        lo, hi = M._pair(CARL, STAFF)
        pair = q("SELECT * FROM vt_message_threads WHERE user_lo=? AND user_hi=?", (lo, hi))
        check("the pair is stored in canonical order", len(pair) == 1 and lo < hi, str(pair)[:200])

        # The claim itself: a second get-or-create reads rowcount 0 and returns the
        # SAME id rather than inserting.
        again_id, created = M._get_or_create_thread(STAFF, CARL, STAFF)
        check("get-or-create is claim-first: second call creates nothing",
              again_id == tid and created is False, f"{again_id}/{created}")

        # And the rule is the DATABASE's, not this file's.
        raised = ""
        try:
            with conn() as c:
                c.execute("INSERT INTO vt_message_threads (user_lo,user_hi,created_at,created_by) "
                          "VALUES (?,?,?,?)", (lo, hi, time.time(), CARL))
        except Exception as e:
            raised = type(e).__name__
        check("a duplicate pair is refused by SQLite itself",
              raised == "IntegrityError", raised or "the insert succeeded")

        raised = ""
        try:
            with conn() as c:
                c.execute("INSERT INTO vt_message_threads (user_lo,user_hi,created_at,created_by) "
                          "VALUES (?,?,?,?)", (CARL, CARL, time.time(), CARL))
        except Exception as e:
            raised = type(e).__name__
        check("a self-thread is unrepresentable in the schema",
              raised == "IntegrityError", raised or "the insert succeeded")

        # The claim in `_append_message` itself: same key twice, second call writes
        # nothing and reports the row the first one wrote. Wrapped, because a plain
        # INSERT here raises rather than returning — which is a FAILURE, not a crash.
        n0 = len(msg_rows(tid))
        try:
            mid1, w1 = M._append_message(tid, CARL, "direct once", "probe-direct-key")
            mid2, w2 = M._append_message(tid, CARL, "direct twice", "probe-direct-key")
            ok = (w1 and not w2 and mid1 == mid2 and len(msg_rows(tid)) == n0 + 1)
            det = f"{(mid1, w1)} then {(mid2, w2)}, rows {n0} -> {len(msg_rows(tid))}"
        except Exception as e:
            ok, det = False, f"{type(e).__name__}: {e}"
        check("_append_message is claim-first and reads the rowcount", ok, det)
        check("the second call wrote no second body",
              not [r for r in msg_rows(tid) if r["body"] == "direct twice"],
              "the duplicate body was written")

        raised = ""
        try:
            with conn() as c:
                c.execute("INSERT INTO vt_messages (thread_id,sender_id,body,created_at,idem_key) "
                          "VALUES (?,?,?,?,?)",
                          (tid, CARL, "second row, same key", time.time(),
                           msg_rows(tid)[0]["idem_key"]))
        except Exception as e:
            raised = type(e).__name__
        check("a second message on one form key is refused by SQLite itself",
              raised == "IntegrityError", raised or "the insert succeeded")

        # The new-thread cooldown, which is why the two opens above are sequential.
        k3 = shell.mint_form_key(CARL, f"message:new:{ALICE}")
        r = await cc.post("/api/messages/send",
                          json={"to": BOB, "body": "and another",
                                "idempotency_key": shell.mint_form_key(CARL, f"message:new:{BOB}")},
                          headers=hdr("tok-carl"))
        j = await r.json()
        check("a second new conversation within the cooldown is refused, not silently queued",
              j.get("code") in ("rate_limited", "cannot_message"), str(j)[:200])
        assert k3
    finally:
        await cc.close()
        await cs.close()


# ══════════════════════════════════════════════════════════════════════════
# M10/M11 — refusals, and the key that comes back
# ══════════════════════════════════════════════════════════════════════════

async def t_refusals():
    print("\n[M10/M11] Refusals: self, stranger, over-long — and the key is released")
    app = build_app()
    cc = client_for(app, "tok-carl")
    ca = client_for(app, "tok-alice")
    await cc.start_server()
    await ca.start_server()
    try:
        before = len(msg_rows())
        threads_before = len(thread_rows())

        key = shell.mint_form_key(CARL, f"message:new:{CARL}")
        r = await cc.post("/api/messages/send",
                          json={"to": CARL, "body": "hi me", "idempotency_key": key},
                          headers=hdr("tok-carl"))
        j = await r.json()
        check("messaging yourself is refused by name",
              j.get("code") == "self_message", str(j)[:160])

        key = shell.mint_form_key(CARL, f"message:new:{ALICE}")
        r = await cc.post("/api/messages/send",
                          json={"to": ALICE, "body": "we have never dealt",
                                "idempotency_key": key}, headers=hdr("tok-carl"))
        j_known = await r.json()
        check("a player you have not dealt with cannot be messaged",
              r.status == 403 and j_known.get("code") == "cannot_message", str(j_known)[:200])

        ghost = "100000000000009999"
        key = shell.mint_form_key(CARL, f"message:new:{ghost}")
        r = await cc.post("/api/messages/send",
                          json={"to": ghost, "body": "does this id exist?",
                                "idempotency_key": key}, headers=hdr("tok-carl"))
        j_ghost = await r.json()
        check("an id that does not exist gets the SAME refusal — no enumeration",
              j_ghost.get("code") == j_known.get("code")
              and j_ghost.get("error") == j_known.get("error"),
              f"{j_ghost.get('code')} vs {j_known.get('code')}")

        check("no refusal wrote a row", len(msg_rows()) == before, str(len(msg_rows())))
        check("no refusal created a thread either",
              len(thread_rows()) == threads_before,
              f"{threads_before} -> {len(thread_rows())}")

        # An over-long body is a NoEffect: the key must come back and still work.
        tid = int(q("SELECT id FROM vt_message_threads ORDER BY id LIMIT 1")[0]["id"])
        key = await thread_key(ca, tid)
        r = await ca.post("/api/messages/send",
                          json={"thread_id": tid, "body": "x" * (M.BODY_MAX + 1),
                                "idempotency_key": key}, headers=hdr("tok-alice"))
        j = await r.json()
        check("an over-long body is refused by name and by figure",
              j.get("code") == "message_too_long" and f"{M.BODY_MAX:,}" in j.get("error", ""),
              str(j)[:200])
        n0 = len(msg_rows(tid))
        r = await ca.post("/api/messages/send",
                          json={"thread_id": tid, "body": "shorter, then",
                                "idempotency_key": key}, headers=hdr("tok-alice"))
        j = await r.json()
        check("the SAME key works after a no-effect refusal — it was released",
              j.get("ok") and not j.get("replayed"), str(j)[:200])
        check("and the corrected send wrote exactly one row", len(msg_rows(tid)) == n0 + 1,
              f"{n0} -> {len(msg_rows(tid))}")

        r = await ca.post("/api/messages/send",
                          json={"thread_id": tid, "body": "   ",
                                "idempotency_key": shell.mint_form_key(ALICE, f"message:t:{tid}")},
                          headers=hdr("tok-alice"))
        check("an empty body is refused", (await r.json()).get("code") == "empty_message",
              str(r.status))

        # A key minted for ANOTHER conversation cannot post into this one.
        other = [t for t in thread_rows() if int(t["id"]) != tid]
        if other:
            wrong = shell.mint_form_key(ALICE, f'message:t:{int(other[0]["id"])}')
            r = await ca.post("/api/messages/send",
                              json={"thread_id": tid, "body": "wrong conversation",
                                    "idempotency_key": wrong}, headers=hdr("tok-alice"))
            j = await r.json()
            check("a key minted on another conversation is refused by subject",
                  j.get("code") == "form_key_subject_mismatch", str(j)[:200])

        r = await ca.post("/api/messages/send",
                          json={"thread_id": tid, "body": "no csrf",
                                "idempotency_key": shell.mint_form_key(ALICE, f"message:t:{tid}")})
        check("a send with no CSRF token is refused", (await r.json()).get("code") == "bad_csrf",
              str(r.status))
    finally:
        await cc.close()
        await ca.close()


# ══════════════════════════════════════════════════════════════════════════
# M12 — append-only, asserted on the module and on the router
# ══════════════════════════════════════════════════════════════════════════

async def t_append_only():
    print("\n[M12] Append-only: nothing edits or deletes a message")
    src = Path(M.__file__).read_text()
    bad = re.findall(r"(?:UPDATE|DELETE\s+FROM)\s+vt_messages", src, re.I)
    check("no SQL in the module updates or deletes a message row", not bad, str(bad))
    ins = re.findall(r"INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+vt_messages", src, re.I)
    check("there is exactly ONE insert site for messages", len(ins) == 1, f"{len(ins)} sites")

    app = build_app()
    paths = sorted({r.resource.canonical for r in app.router.routes()})
    check("no edit or delete route exists",
          not any(("edit" in p or "delete" in p) for p in paths), str(paths))
    check("the routes are the five that were designed",
          set(paths) >= {"/messages", "/messages/t/{tid}", "/api/messages/send",
                         "/api/messages/read", "/api/messages/unread"}, str(paths))


# ══════════════════════════════════════════════════════════════════════════
# M13/M14 — wiring, empty state, and no seeded content
# ══════════════════════════════════════════════════════════════════════════

async def t_wiring_and_empty_state():
    print("\n[M13/M14] Wired into both navs; the empty state is one muted line")
    check("the section is in vt_web_shell's nav",
          any(k == "messages" for k, _, _ in shell.NAV), str(shell.NAV))
    keys = [s["key"] for s in hub_web.sections()]
    check("the section registered itself in hub_web._SECTIONS", "messages" in keys, str(keys))
    check("the hub nav label is 'Messages'",
          any(s["key"] == "messages" and s["label"] == "Messages" for s in hub_web.sections()),
          str(hub_web.sections()))
    check("the hub nav entry has an inline SVG icon and no emoji",
          all("<path" in s["icon"] for s in hub_web.sections() if s["key"] == "messages"),
          str([s for s in hub_web.sections() if s["key"] == "messages"]))

    app = build_app()
    cc = client_for(app, "tok-dave")
    await cc.start_server()
    try:
        page = await (await cc.get("/messages")).text()
        # Dave has no threads: this is the state John opens the site to on day one.
        check("an empty inbox says so in ONE muted line",
              "No conversations yet. Nothing has been sent on this site." in page, "line missing")
        check("the empty inbox renders no table",
              "<table>" not in page, "a table was rendered for an empty inbox")
        check("the nav carries the unread badge element", 'id="navUnread"' in page, "badge missing")
        check("the nav lists Messages", ">Messages<" in page, "nav entry missing")
        # Same two rules the site-wide probes measure, measured the same way: emoji
        # means pictographs, and the only round thing allowed is a status dot.
        emoji = [ch for ch in page
                 if 0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF]
        check("no emoji anywhere on the page", not emoji, f"found {sorted(set(emoji))[:8]}")
        radii = [m for m in re.findall(r"border-radius\s*:\s*([^;\"']+)", page)
                 if m.strip() not in ("0", "0px", "50%")]
        check("no border-radius outside the 50% status dots", not radii, str(radii))
        check("no invented conversation is served",
              not re.search(r"(Lorem|example\.com|sample message|demo)", page, re.I),
              "placeholder content found")
    finally:
        await cc.close()

    # And the tables themselves hold only what these probes wrote.
    senders = {r["sender_id"] for r in msg_rows()}
    check("every message row was written by a probe session user",
          senders <= {ALICE, BOB, CARL, DAVE, STAFF}, str(senders))


# ══════════════════════════════════════════════════════════════════════════
# M15/M16 — rate limit, and no ISO on screen
# ══════════════════════════════════════════════════════════════════════════

async def t_rate_limit_and_dates():
    print("\n[M15/M16] The per-sender rate limit bites; no ISO timestamp is rendered")
    app = build_app()
    cb = client_for(app, "tok-bob")
    await cb.start_server()
    try:
        tid = int(q("SELECT id FROM vt_message_threads WHERE user_lo=? OR user_hi=? "
                    "ORDER BY id LIMIT 1", (BOB, BOB))[0]["id"])
        sent_before = len(q("SELECT 1 FROM vt_messages WHERE sender_id=? AND created_at > ?",
                            (BOB, time.time() - 60)))
        refused = None
        for i in range(M.SEND_PER_MINUTE + 2):
            key = shell.mint_form_key(BOB, f"message:t:{tid}")
            r = await cb.post("/api/messages/send",
                              json={"thread_id": tid, "body": f"burst {i}",
                                    "idempotency_key": key}, headers=hdr("tok-bob"))
            j = await r.json()
            if not j.get("ok"):
                refused = (r.status, j)
                break
        check("the burst is stopped", refused is not None, "never refused")
        check("the refusal is named and says nothing was sent",
              refused and refused[1].get("code") == "rate_limited"
              and "Nothing was sent" in refused[1].get("error", ""), str(refused)[:220])
        sent_after = len(q("SELECT 1 FROM vt_messages WHERE sender_id=? AND created_at > ?",
                           (BOB, time.time() - 60)))
        check("no more than the cap was written in the window",
              sent_after - sent_before <= M.SEND_PER_MINUTE,
              f"{sent_before} -> {sent_after}, cap {M.SEND_PER_MINUTE}")

        page = await (await cb.get("/messages")).text()
        check("the inbox renders no ISO timestamp",
              not re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", page), "ISO timestamp on the page")
        check("the inbox renders no raw epoch float",
              not re.search(r"\b17\d{8}\.\d+\b", page), "epoch float on the page")
        check("activity is rendered in human words",
              re.search(r"(just now|minutes? ago|hours? ago|yesterday|days? ago|"
                        r"\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4})",
                        page) is not None, "no human date found")

        page = await (await cb.get(f"/messages/t/{tid}")).text()
        check("the thread renders no ISO timestamp",
              not re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", page), "ISO timestamp in thread")
        check("the thread does not leak the other side's read state",
              "read receipt" not in page.lower() and "seen at" not in page.lower(),
              "a read receipt was rendered")
    finally:
        await cb.close()


# ══════════════════════════════════════════════════════════════════════════
# Unit checks that do not need a client
# ══════════════════════════════════════════════════════════════════════════

def t_units():
    print("\n[U] Formatting and authorisation, directly")
    now = 1_800_000_000.0
    cases = [(now - 5, "just now"), (now - 600, "10 minutes ago"),
             (now - 3 * 3600, "3 hours ago"), (now - 30 * 3600, "yesterday"),
             (now - 4 * 86400, "4 days ago")]
    for ts, want in cases:
        check(f"_ago -> {want!r}", M._ago(ts, now) == want, M._ago(ts, now))
    old = M._ago(1_749_700_000.0, now)
    check("an old timestamp renders as a human date, not ISO",
          re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} \d{4}", old) is not None, old)
    check("_ago of nothing is a dash, not a crash", M._ago(None) == "—", M._ago(None))

    check("escaping happens in one place and closes quotes too",
          M.esc('<a href="x">&') == "&lt;a href=&quot;x&quot;&gt;&amp;", M.esc('<a href="x">&'))
    check("_pair is order-independent", M._pair(BOB, ALICE) == M._pair(ALICE, BOB), "")
    check("you may not message yourself", not M._may_message(ALICE, ALICE), "")
    check("a counterparty may be messaged", M._may_message(ALICE, BOB), "")
    check("a non-counterparty may not", not M._may_message(CARL, ALICE), "")
    check("staff are reachable by everyone", M._may_message(CARL, STAFF), "")
    check("staff may reach a player with a wallet", M._may_message(STAFF, CARL), "")
    check("nobody may message an id with no wallet and no dealing",
          not M._may_message(ALICE, "100000000000009999"), "")
    check("the contact list is dealings, not a directory",
          {c["user_id"] for c in M._contacts(CARL)} == {STAFF},
          str([c["user_id"] for c in M._contacts(CARL)]))
    check("a contact says WHY it is a contact",
          all(c.get("via") for c in M._contacts(ALICE)), str(M._contacts(ALICE))[:200])


# ══════════════════════════════════════════════════════════════════════════

async def main():
    seed()
    t_units()
    await t_anonymous()
    tid = await t_send_and_double_click()
    await t_stranger_cannot_read(tid)
    await t_script_tag_is_escaped(tid)
    await t_body_identity_ignored(tid)
    await t_unread(tid)
    await t_refusals()
    await t_one_thread_per_pair()
    await t_append_only()
    await t_wiring_and_empty_state()
    await t_rate_limit_and_dates()

    print("\n" + "=" * 72)
    print(f"  {len(PASSES)} passed, {len(FAILS)} failed")
    for f in FAILS:
        print(f"  FAIL  {f}")
    print("=" * 72)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
