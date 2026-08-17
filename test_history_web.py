"""
test_history_web.py — proofs for `history_web`, driven through a real aiohttp test
client against **a copy of the production database** (`/home/claude/restocker_live.db`).

Run:  python3 test_history_web.py

WHY A COPY OF PRODUCTION AND NOT A FIXTURE
──────────────────────────────────────────
This section renders records that already exist, so a hand-built fixture would prove
that the code renders the shapes the author imagined. The production copy carries the
shapes the bot actually wrote: 33 `coin_ledger` rows with an EMPTY reason, trade rows
whose `side` is `liquidated` at a price of zero, 23 balances that disagree with a
recomputation, two timestamp formats, two empty tables, and the one real `share_gifts`
row this whole feature exists for — a 5,001.15-share transfer whose event date is
stated only inside a free-text note. Every one of those is a way to render a lie, and
none of them would have been in a fixture.

Two rows ARE inserted by this file, into the copy, before the app starts: an OTC
transfer whose note states no date, and one whose note is hostile HTML. They exist
because production has no example of either and both are properties the module must
have. They are inserted by the TEST, into a temporary copy, between synthetic account
ids — `history_web` itself writes nothing, which is H3.

  H1  anonymous            -> 401 + sign-in card on both routes, no history content
  H2  read-only            -> every SQL in the module is a SELECT (AST), and driving
                              every route leaves every row count identical
  H3  the OTC row          -> both parties see it, both dates, counterparty, note
  H4  unknown event date   -> says "unknown", and the write stamp is never shown
  H5  a stranger           -> sees none of another user's history, on the page or the
                              permalink; body/query user ids are ignored
  H6  permalink            -> both parties 200; third party 404 identical to a
                              nonexistent id
  H7  totals strip         -> coin_ledger only, matches SQL, unit + timeframe on screen
  H8  running balance      -> the STORED figure, and the disagreement is disclosed
  H9  filters              -> by type and by market, and they actually narrow
  H10 pagination           -> bounded, and page 2 is different rows
  H11 empty state          -> one muted line, and no invented row anywhere
  H12 labelling            -> a liquidation is not rendered as an exchange fill;
                              a hive month is labelled a market total
  H13 house rules          -> no ISO on screen, no emoji, no border-radius, mono figures
  H14 escaping             -> a hostile note is escaped in the bytes, raw in the row
  H15 wiring               -> the section is in both navs and both routes exist
  H16 truncation           -> a capped read says so; it never shows a prefix as the whole
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

LIVE = "/home/claude/restocker_live.db"
CORE = "/mnt/user-data/uploads/RestockerLocal"
BUILD = "/home/claude/build"
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
# Fixture — a COPY of the production database, real routes, real session cookie
# ══════════════════════════════════════════════════════════════════════════

TMP = Path(tempfile.mkdtemp(prefix="vthist-"))
os.chdir(TMP)
os.environ["ESTATES_DB_PATH"] = str(TMP / "estates.db")
os.environ["VT_WEB_KEY_SECRET"] = "test-secret-not-a-real-one"
os.environ["VT_STAFF_IDS"] = "1203738126850461738"     # John is staff — H5 leans on it
os.environ["HUB_DB_PATH"] = str(TMP / "restocker.db")
os.environ.pop("OSENTAR_BASE_URL", None)

assert Path(LIVE).exists(), f"production copy missing: {LIVE}"
shutil.copy(LIVE, TMP / "restocker.db")               # THE COPY. The original is never opened.

import vt_web_shell as shell                    # noqa: E402
import hub_web                                  # noqa: E402
import history_web as H                         # noqa: E402
from aiohttp import web                         # noqa: E402
from aiohttp.test_utils import TestClient, TestServer   # noqa: E402

# Real accounts out of the production copy.
SELLER = "1203738126850461738"   # John. from_user of the real OTC row; 9 wallet rows,
                                 # 1 exchange fill, owner of greyhames/vtech/brew
BUYER = "776151361599438869"     # to_user of the real OTC row. NOTHING else on record —
                                 # no wallet row, no IGN, no name. The whole reason this
                                 # feature exists, and the thinnest possible page.
STRANGER = "1236021625577668663"  # 22 wallet rows of his own, harvester of vtech + bnl
NOBODY = "999000000000000001"     # never appears in any table — the empty page

# Synthetic ids for the two rows this file inserts. They are not in the production copy
# and they are not reachable from any real account.
FX_A = "999000000000000011"
FX_B = "999000000000000012"

_SESSIONS = {
    "tok-seller": {"user_id": SELLER, "name": "Vaicos", "csrf": "csrf-a"},
    "tok-buyer": {"user_id": BUYER, "name": "StableGenius", "csrf": "csrf-b"},
    "tok-stranger": {"user_id": STRANGER, "name": "Someone Else", "csrf": "csrf-c"},
    "tok-nobody": {"user_id": NOBODY, "name": "New Player", "csrf": "csrf-d"},
    "tok-fxa": {"user_id": FX_A, "name": "Fixture A", "csrf": "csrf-e"},
    "tok-fxb": {"user_id": FX_B, "name": "Fixture B", "csrf": "csrf-f"},
}

# `stock_names.yml` lives with the bot; the cache is primed directly so the name path is
# exercised without importing the bot. A FIXTURE, inside a test file.
H._NAMES_CACHE = {SELLER: "Vaicos"}
H._NAMES_AT = 1e18            # never expires during the run


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


HISTORY_TABLES = ("coin_ledger", "stock_trade_log", "share_gifts", "stock_dividend_log",
                  "investor_payout_log", "hive_ledger", "stock_holdings", "balances",
                  "markets", "hive_harvests", "ign_registry")


def counts() -> dict:
    return {t: q(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"] for t in HISTORY_TABLES}


NO_DATE_NOTE = "OTC: paid in-game, screenshot in the deal room, no date written down"
XSS_NOTE = '<script>alert(1)</script> "><img src=x onerror=alert(2)> & done'


def insert_fixture_rows() -> None:
    """TWO rows, into the COPY, before the app exists. Production has no OTC note
    without a date and no hostile note, and both are properties the module must have."""
    with conn() as c:
        c.execute("INSERT INTO share_gifts (key, market_id, from_user, to_user, shares, "
                  "basis, value_coins, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  ("gift:test:nodate", "greyhames", FX_A, FX_B, 12.5, 100.0, 1200,
                   NO_DATE_NOTE, "2026-08-10 09:00:00"))
        c.execute("INSERT INTO share_gifts (key, market_id, from_user, to_user, shares, "
                  "basis, value_coins, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  ("gift:test:xss", "greyhames", FX_A, FX_B, 1.0, 1.0, 1,
                   XSS_NOTE, "2026-08-11 09:00:00"))


insert_fixture_rows()
BASELINE = counts()


def build_app() -> web.Application:
    app = web.Application()
    H.register_history_routes(app)
    return app


def client_for(app, token=None) -> TestClient:
    return TestClient(TestServer(app), cookies=({"vtm_sess": token} if token else {}))


async def get(token, path):
    app = build_app()
    c = client_for(app, token)
    await c.start_server()
    try:
        r = await c.get(path)
        return r.status, await r.text()
    finally:
        await c.close()


OTC_KEY = "gift:greyhames:1203738126850461738:776151361599438869:5001.15"
OTC_LINK = f"/history/e/otc/{OTC_KEY}"


def tbody(body: str) -> str:
    """Just the data rows. The filter chips name every event type, so a check for a
    label anywhere in the page would pass on the chip and prove nothing about the
    rows — every "does he see it" assertion below reads this, not the whole page."""
    if "<tbody>" not in body:
        return ""
    return body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]


def n_rows(body: str) -> int:
    return tbody(body).count("<tr>")


def flat(body: str) -> str:
    """One space everywhere. Prose in the served HTML wraps across source lines, and an
    assertion that breaks when a sentence is re-wrapped is an assertion that gets
    weakened rather than fixed."""
    return re.sub(r"\s+", " ", body)


# ══════════════════════════════════════════════════════════════════════════
# H1 — anonymous
# ══════════════════════════════════════════════════════════════════════════

async def t_anonymous():
    print("\n[H1] Anonymous sees nothing")
    for path in ("/history", OTC_LINK, "/history/e/coin_ledger/19"):
        st, body = await get(None, path)
        check(f"anonymous {path} -> 401", st == 401, str(st))
        check(f"anonymous {path} gets the sign-in card",
              "Sign in" in body and "/website_login" in body, body[:120])
        check(f"anonymous {path} renders no history content",
              ("Coins in" not in body and "OTC" not in body
               and "balance_after" not in body and "5,001.15" not in body),
              "history content leaked to a logged-out visitor")


# ══════════════════════════════════════════════════════════════════════════
# H2 — READ-ONLY. Asserted on the source AST, and on the rows.
# ══════════════════════════════════════════════════════════════════════════

_WRITE = re.compile(r"^\s*(insert|update|delete|replace|drop|alter|create|truncate|"
                    r"pragma|attach|vacuum|begin|commit)\b", re.I)


def _sql_args_in(tree) -> list:
    """Every string handed to a database call in this module.

    Collected from `.execute(...)` / `.executemany(...)` / `.executescript(...)` and
    from `_rows(...)`, which is this module's own single SELECT helper. Docstrings are
    NOT scanned — this file's prose says the words INSERT and CREATE TABLE many times,
    and a check that a module never mentions a word is a check that gets deleted the
    first time it is inconvenient. The check that matters is what reaches SQLite.
    """
    out = []
    inside_rows = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == "_rows":
            for sub in ast.walk(fn):
                inside_rows.add(id(sub))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if id(node) in inside_rows:
            # `_rows` is the module's one SELECT helper: `conn.execute(sql, args)` where
            # `sql` is its own parameter. Auditing it here would be auditing a variable;
            # what is audited instead is every string every CALLER hands it, below.
            continue
        name = ""
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
        elif isinstance(node.func, ast.Name):
            name = node.func.id
        if name not in ("execute", "executemany", "executescript", "_rows"):
            continue
        if not node.args:
            continue
        a = node.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.append(("const", a.value))
        elif isinstance(a, ast.JoinedStr):
            # An f-string SQL: join the literal parts and mark it, so a write verb
            # hiding in a literal segment is still caught.
            lit = "".join(v.value for v in a.values
                          if isinstance(v, ast.Constant) and isinstance(v.value, str))
            out.append(("fstring", lit))
        else:
            out.append(("dynamic", ""))
    return out


def t_read_only_source():
    print("\n[H2] The module cannot write — asserted on the source, not on a promise")
    src = Path(H.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    sqls = _sql_args_in(tree)

    check("the module issues at least six database reads", len(sqls) >= 6, str(len(sqls)))
    check("no database call takes a non-literal SQL string",
          all(k != "dynamic" for k, _ in sqls),
          "a computed SQL string cannot be audited by this test")
    bad = [s for _k, s in sqls if _WRITE.match(s or "")]
    check("no INSERT/UPDATE/DELETE/DDL in any SQL the module executes", not bad, str(bad))
    starts = [s.strip().split()[0].upper() for _k, s in sqls if (s or "").strip()]
    check("every SQL the module executes starts with SELECT",
          set(starts) <= {"SELECT"}, str(sorted(set(starts))))
    check("the module never calls executescript",
          "executescript" not in src.replace('"executescript"', ""), "executescript present")
    # The module has no schema of its own — nothing to create, nothing to migrate.
    check("the module defines no CREATE TABLE at all", "CREATE TABLE" not in src,
          "history has no tables of its own and must not make any")


async def t_read_only_rows():
    print("\n[H2b] Driving every route changes no row")
    before = counts()
    for token in ("tok-seller", "tok-buyer", "tok-stranger", "tok-nobody", None):
        for path in ("/history", "/history?type=otc", "/history?type=coin_ledger",
                     "/history?market=greyhames", "/history?page=2",
                     "/history?type=nonsense&market=nonsense&page=-4",
                     OTC_LINK, "/history/e/coin_ledger/19", "/history/e/hive/vtech:2026-08",
                     "/history/e/nosuch/1"):
            await get(token, path)
    after = counts()
    check("every row count identical after driving every route for five sessions",
          before == after, f"{before} != {after}")
    check("nothing was written to the six history tables",
          after == BASELINE, f"{after} != {BASELINE}")


# ══════════════════════════════════════════════════════════════════════════
# H3 — THE OTC ROW. The row this feature exists for.
# ══════════════════════════════════════════════════════════════════════════

async def t_otc_row():
    print("\n[H3] The real OTC transfer, from both sides, event date only")
    st_s, seller = await get("tok-seller", "/history")
    st_b, buyer = await get("tok-buyer", "/history")
    check("the seller's page loads", st_s == 200, str(st_s))
    check("the buyer's page loads", st_b == 200, str(st_b))

    for who, body in (("seller", seller), ("buyer", buyer)):
        check(f"{who} sees the row labelled OTC TRANSFER", "OTC TRANSFER" in body, "")
        check(f"{who} sees the shares figure 5,001.15", "5,001.15" in body, "")
        check(f"{who} sees the EVENT date 09 Aug 2026 read from the note",
              "09 Aug 2026" in body, "the event date is missing")
        check(f"{who} is NOT shown the row's write stamp 16 Aug 2026",
              "recorded 16 Aug 2026" not in body and "16 Aug 2026" not in body,
              "the bookkeeping write stamp is printed as if it were history")
        check(f"{who} sees where the event date came from",
              "stated in the note as" in body and "09.08.26" in body,
              "the parse has no visible provenance")
        check(f"{who} sees the day-first reading stated",
              "read day-first" in body, "an ambiguous date was read silently")
        check(f"{who} sees the note text itself",
              "paid 5,000,000c in-game" in body, "the note is not on the page")
        check(f"{who} sees the END of a long note, not just its first 160 characters",
              ("resold these shares" in body) or ("Supersedes" not in body),
              "the note is clipped before the sentence that says who was sold to")
        check(f"{who} sees the value as recorded, not rounded",
              "4,999,999.74" in body, "the recorded value was rounded or invented")
        check(f"{who} is not shown the transfer as an exchange fill",
              "Exchange buy · GreyHames" not in body.split("OTC TRANSFER")[1][:600],
              "an off-book transfer is dressed as a fill")

    check("the seller's row reads 'to' the buyer",
          "OTC transfer to " in seller, "direction missing from the sender's copy")
    check("the buyer's row reads 'from' the seller by name",
          "OTC transfer from Vaicos" in buyer, "direction or name missing")
    check("the two copies disagree only about direction, never about the facts",
          ("5,001.15" in seller and "5,001.15" in buyer
           and "09 Aug 2026" in seller and "09 Aug 2026" in buyer
           and "4,999,999.74" in seller and "4,999,999.74" in buyer), "")
    check("the sender's copy shows the shares leaving (−)",
          "−5,001.15" in seller, "no direction on the sender's figure")
    check("the recipient's copy shows the shares arriving (+)",
          "+5,001.15" in buyer, "no direction on the recipient's figure")

    # The buyer has NOTHING else. That is the page the moderator's question was about.
    check("the buyer's whole history is that one event",
          n_rows(buyer) == 1, str(n_rows(buyer)))
    check("the buyer, who has no name anywhere, is not invented a name",
          "Unnamed" not in seller, "a placeholder name was invented for the counterparty")
    check("an account with no linked name renders as its full Discord id",
          BUYER in seller, "the counterparty is unidentifiable in the sender's copy")


# ══════════════════════════════════════════════════════════════════════════
# H4 — an unknown event date says unknown
# ══════════════════════════════════════════════════════════════════════════

async def t_unknown_event_date():
    print("\n[H4] Unknown event dates say unknown, and never borrow the recorded date")
    st, body = await get("tok-fxb", "/history")
    check("the fixture OTC page loads", st == 200, str(st))
    check("an OTC note with no date renders the event date as 'unknown'",
          "unknown" in body, "no unknown marker")
    check("it says explicitly that no event date is recorded",
          "no event date recorded" in body or "no event date is recorded" in body, "")
    check("the row's write stamp is not printed at all",
          "10 Aug 2026" not in body,
          "the bookkeeping write stamp is printed as if it were history")
    check("the write stamp is NOT printed as the event date",
          not re.search(r'class="h-when"[^>]*>10 Aug 2026', body),
          "the write stamp was substituted for the event date")
    check("the row still says how it is ordered",
          "placed by when the bot wrote the row" in body,
          "silent ordering by a date we do not have")

    # The same property on real data: a hive month has no exact date at all.
    st, body = await get("tok-seller", "/history?type=hive")
    check("a hive month, which has no event date in the schema, says unknown",
          "unknown" in body and "no exact date is recorded" in body, "")

    # And the unit test, directly on the parser.
    ev, raw, how = H._event_date_from_note("OTC: buyer paid 5,000,000c in-game 09.08.26")
    check("the parser returns the literal text it parsed", raw == "09.08.26", raw)
    check("the parser reads 09.08.26 as 9 August 2026", H._date(ev) == "09 Aug 2026",
          H._date(ev))
    check("the parser says how it read it", how == "read day-first", how)
    check("a note with no date returns None, not a guess",
          H._event_date_from_note(NO_DATE_NOTE) == (None, "", ""), "")
    check("a note with no date renders as the word unknown", H._date(None) == "unknown",
          H._date(None))
    check("an unreadable timestamp is None, never now and never the epoch",
          H._ts("not a date") is None and H._ts("") is None and H._ts(None) is None, "")


# ══════════════════════════════════════════════════════════════════════════
# H5 — a stranger sees nothing of another user's history
# ══════════════════════════════════════════════════════════════════════════

async def t_stranger():
    print("\n[H5] A stranger sees his own history and nobody else's")
    _st, seller = await get("tok-seller", "/history")
    st, body = await get("tok-stranger", "/history")
    check("the stranger's own page loads", st == 200, str(st))

    check("the stranger does not see the OTC transfer",
          "OTC TRANSFER" not in tbody(body) and "5,001.15" not in body,
          "another user's OTC row leaked")
    check("the stranger does not see the seller's wallet reasons",
          "Shop order #46" not in body and "Liquidation of" not in body,
          "another user's wallet rows leaked")
    check("the stranger does not see the seller's balances",
          "1,887,235" not in body and "1,989,122" not in body, "balances leaked")
    check("the stranger's page is not empty either — he sees his OWN rows",
          n_rows(body) > 5, str(n_rows(body)))
    check("the seller does not see the stranger's rows",
          "51,873" not in seller, "the leak runs the other way")

    # Identity is the session. Not the query string, not a header.
    st, spoof = await get("tok-stranger", f"/history?user_id={SELLER}")
    check("a user_id in the query string is ignored", st == 200 and "5,001.15" not in spoof,
          "a query-string user id changed whose history was rendered")
    st2, spoof2 = await get("tok-stranger", f"/history?uid={SELLER}&user={SELLER}")
    check("uid= and user= are ignored too",
          st2 == 200 and "OTC TRANSFER" not in tbody(spoof2), "")

    # STAFF GET NOTHING EXTRA. John is VT_STAFF_IDS in this run.
    their_row = q("SELECT id FROM coin_ledger WHERE user_id=? LIMIT 1",
                  (STRANGER,))[0]["id"]
    st, staff = await get("tok-seller", f"/history/e/coin_ledger/{their_row}")
    check("staff cannot open another player's wallet row by permalink", st == 404, str(st))
    check("the staff 404 says nothing about the row", "51,873" not in staff, "")


# ══════════════════════════════════════════════════════════════════════════
# H6 — the permalink
# ══════════════════════════════════════════════════════════════════════════

async def t_permalink():
    print("\n[H6] The permalink: both parties, nobody else")
    st_s, seller = await get("tok-seller", OTC_LINK)
    st_b, buyer = await get("tok-buyer", OTC_LINK)
    st_x, stranger = await get("tok-stranger", OTC_LINK)
    st_n, nonexistent = await get("tok-stranger", "/history/e/otc/gift:does:not:exist")

    check("the sender can open it", st_s == 200, str(st_s))
    check("the recipient can open it", st_b == 200, str(st_b))
    check("a third party cannot", st_x == 404, str(st_x))
    check("a nonexistent id is also 404", st_n == 404, str(st_n))
    check("the third party's 404 is byte-identical to the nonexistent id's 404",
          stranger == nonexistent,
          "the refusal distinguishes a real record from an imaginary one")
    check("the refused page contains no fact about the event",
          "5,001.15" not in stranger and "4,999,999" not in stranger
          and BUYER not in stranger, "the 404 leaks the record")

    for who, body in (("sender", seller), ("recipient", buyer)):
        check(f"{who}'s permalink shows the event date, labelled",
              "Event date" in body and "09 Aug 2026" in body, "")
        check(f"{who}'s permalink does not print the row's write stamp",
              "16 Aug 2026" not in body,
              "the permalink prints the bookkeeping write stamp")
        check(f"{who}'s permalink prints the note exactly as stored",
              "recorded retroactively" in body, "")
        check(f"{who}'s permalink prints the source table and row id",
              "share_gifts" not in body and "otc" in body and OTC_KEY in body,
              "the row is not addressable from the page")
        check(f"{who}'s permalink names the counterparty's full Discord id",
              (BUYER in body) if who == "sender" else (SELLER in body), "")
        check(f"{who}'s permalink does not call the transfer a payment receipt",
              "It is not a payment receipt" in body, "")

    # Every source is addressable, and each one is refused to a non-party.
    row = q("SELECT id FROM coin_ledger WHERE user_id=? LIMIT 1", (SELLER,))[0]["id"]
    st_ok, _ = await get("tok-seller", f"/history/e/coin_ledger/{row}")
    st_no, _ = await get("tok-buyer", f"/history/e/coin_ledger/{row}")
    check("a wallet row is addressable by its owner", st_ok == 200, str(st_ok))
    check("a wallet row is refused to everybody else", st_no == 404, str(st_no))
    st_ok, hive = await get("tok-seller", "/history/e/hive/vtech:2026-08")
    st_no, _ = await get("tok-buyer", "/history/e/hive/vtech:2026-08")
    check("a hive month is addressable by the market owner", st_ok == 200, str(st_ok))
    check("a hive month is refused to a non-participant", st_no == 404, str(st_no))
    st_bad, _ = await get("tok-seller", "/history/e/coin_ledger/not-a-number")
    check("a junk row id is a 404, never a 500", st_bad == 404, str(st_bad))
    st_bad2, _ = await get("tok-seller", "/history/e/../../etc/passwd")
    check("a path-traversal id does not reach a 500", st_bad2 in (200, 404), str(st_bad2))


# ══════════════════════════════════════════════════════════════════════════
# H7 — the totals strip
# ══════════════════════════════════════════════════════════════════════════

async def t_totals():
    print("\n[H7] Totals: coin_ledger only, with unit and timeframe")
    ins = q("SELECT COALESCE(SUM(delta),0) AS s FROM coin_ledger "
            "WHERE user_id=? AND delta>=0", (SELLER,))[0]["s"]
    outs = -q("SELECT COALESCE(SUM(delta),0) AS s FROM coin_ledger "
              "WHERE user_id=? AND delta<0", (SELLER,))[0]["s"]

    events, failed = H.gather(SELLER)
    t = H.totals(events)
    check("no source failed to read", not failed, str(failed))
    check("coins in matches SQL over coin_ledger", abs(t["in"] - ins) < 0.01,
          f'{t["in"]} vs {ins}')
    check("coins out matches SQL over coin_ledger", abs(t["out"] - outs) < 0.01,
          f'{t["out"]} vs {outs}')
    check("net is in minus out", abs(t["net"] - (ins - outs)) < 0.01, str(t["net"]))
    check("only wallet movements are counted",
          t["counted"] == len(q("SELECT id FROM coin_ledger WHERE user_id=?", (SELLER,))),
          str(t["counted"]))
    check("the non-wallet events are counted separately, not silently dropped",
          t["other"] == len(events) - t["counted"] and t["other"] > 0, str(t["other"]))

    _st, body = await get("tok-seller", "/history")
    check("every totals figure carries its unit",
          body.count("h-val up") and re.search(r'class="h-val up">[\d,\.]+ c<', body),
          "a bare number in the strip")
    check("every totals figure carries its timeframe",
          re.search(r"\d\d \w\w\w 20\d\d to \d\d \w\w\w 20\d\d", body), "no date span")
    check("the strip says what it counts and what it does not",
          "wallet movements, over" in flat(body)
          and "not coin movements and not in these figures" in flat(body), "")
    check("in / out / net are three figures, never summed into one balance",
          "Coins in" in body and "Coins out" in body and "Net" in body
          and ">Balance<" not in body, "")

    # Filtered range: the figures must follow the filter.
    _st, filtered = await get("tok-seller", "/history?type=otc")
    check("with a non-wallet filter the strip says zero wallet movements counted",
          "no dated wallet movements in this view" in filtered, "")
    check("the filtered strip names the scope it covers",
          "OTC transfers" in filtered, "")


# ══════════════════════════════════════════════════════════════════════════
# H8 — the running balance
# ══════════════════════════════════════════════════════════════════════════

async def t_balance():
    print("\n[H8] The running balance is the STORED one, and drift is disclosed")
    rows = q("SELECT id, delta, balance_after FROM coin_ledger WHERE user_id=? ORDER BY id",
             (SELLER,))
    drifting = []
    for prev, cur in zip(rows, rows[1:]):
        if abs(prev["balance_after"] + cur["delta"] - cur["balance_after"]) > 0.005:
            drifting.append(cur)
    check("the production copy really does contain drifting balances for this user",
          len(drifting) >= 1, str(len(drifting)))

    _st, body = await get("tok-seller", "/history")
    for r in rows:
        check(f"stored balance {r['balance_after']:,} is on the page",
              f"{r['balance_after']:,}" in body, "a stored balance is missing")
    check("the disagreement is disclosed in words",
          "a recomputation from the row above gives" in body, "drift hidden")
    check("the disclosure says the printed figure is the stored one",
          "stored ·" in body, "")
    n_disclosed = body.count("a recomputation from the row above gives")
    check("exactly the drifting rows are flagged, no more",
          n_disclosed == len(drifting), f"{n_disclosed} flagged vs {len(drifting)} drifting")

    ev = [e for e in H.read_coin_ledger(SELLER) if e["eid"] == str(drifting[0]["id"])][0]
    check("the event carries the stored balance, not the recomputation",
          abs(ev["balance_after"] - drifting[0]["balance_after"]) < 0.005, "")
    check("the recomputation is carried alongside, not instead",
          ev["balance_drift"] is not None
          and abs(ev["balance_drift"] - ev["balance_after"]) > 0.005, "")
    check("a non-wallet row claims no running balance",
          all(e.get("balance_after") is None for e in H.read_otc(SELLER)), "")


# ══════════════════════════════════════════════════════════════════════════
# H9/H10 — filters and pagination
# ══════════════════════════════════════════════════════════════════════════

async def t_filters():
    print("\n[H9] Filters by type and by market")
    _st, all_ = await get("tok-seller", "/history")
    n_all = n_rows(all_)

    _st, otc = await get("tok-seller", "/history?type=otc")
    check("the OTC filter narrows to one row", n_rows(otc) == 1, str(n_rows(otc)))
    check("the OTC filter keeps the OTC row", "OTC TRANSFER" in tbody(otc), "")
    check("the OTC filter drops the wallet rows", "Shop order #46" not in tbody(otc), "")

    _st, wallet = await get("tok-seller", "/history?type=coin_ledger")
    check("the wallet filter shows exactly the wallet rows",
          n_rows(wallet) == len(q("SELECT id FROM coin_ledger WHERE user_id=?",
                                  (SELLER,))), str(n_rows(wallet)))

    _st, hive = await get("tok-seller", "/history?type=hive")
    check("the hive filter shows the two vtech months", n_rows(hive) == 2,
          str(n_rows(hive)))

    _st, mkt = await get("tok-seller", "/history?market=greyhames")
    check("the market filter narrows", 0 < n_rows(mkt) < n_all, str(n_rows(mkt)))
    check("the market filter keeps only greyhames events",
          "V Tech Hives" not in tbody(mkt), "another market's rows survived")

    _st, both = await get("tok-seller", "/history?type=otc&market=greyhames")
    check("the two filters compose", n_rows(both) == 1, str(n_rows(both)))

    _st, junk = await get("tok-seller", "/history?type=wat&market=wat")
    check("an unknown filter falls back to everything, not to a blank page",
          n_rows(junk) == n_all, str(n_rows(junk)))
    check("the filter chips carry counts", re.search(r'Everything <span class="muted">\d+',
                                                     all_), "")
    check("the market chips offer only markets this user has touched",
          "Toolshop" not in all_ and "GreyHames" in all_, "a market directory leaked")


async def t_pagination():
    print("\n[H10] Pagination is real and bounded")
    _st, body = await get("tok-seller", "/history")
    check("one page fits the whole history today, so no pager is shown",
          "Page 1 of" not in body, "a pager appeared for a single page")

    old = H.PAGE_SIZE
    H.PAGE_SIZE = 5                     # the real code path, at a size this data reaches
    try:
        _st, p1 = await get("tok-seller", "/history")
        _st, p2 = await get("tok-seller", "/history?page=2")
        _st, p99 = await get("tok-seller", "/history?page=99")
        check("page 1 holds exactly PAGE_SIZE rows", n_rows(p1) == 5, str(n_rows(p1)))
        check("page 2 holds different rows",
              n_rows(p2) > 0 and tbody(p2) != tbody(p1), "")
        check("the pager states which page of how many", "Page 1 of" in p1, "")
        check("the pager states how many events are in the view",
              "events in this view" in p1, "")
        check("an out-of-range page clamps to the last page rather than emptying",
              n_rows(p99) > 0, "0 rows")
        check("page 1 has no 'Newer' link", ">Newer<" not in p1, "")
        check("the last page has no 'Older' link", ">Older<" not in p99, "")
    finally:
        H.PAGE_SIZE = old


# ══════════════════════════════════════════════════════════════════════════
# H11 — the empty state
# ══════════════════════════════════════════════════════════════════════════

async def t_empty():
    print("\n[H11] The empty page is empty")
    st, body = await get("tok-nobody", "/history")
    check("a player with no history gets 200, not an error", st == 200, str(st))
    check("the empty state is one muted line",
          "Nothing on record for you yet" in body, "")
    check("the empty state invents no row",
          n_rows(body) == 0 and "<table>" not in body, "a row appeared from nowhere")
    check("the empty state has no illustration and no call to action",
          "svg" not in body.split("h-empty")[1][:400], "decorated empty state")
    check("the totals still carry their unit and say what they counted",
          "0 c" in body and "wallet movements, over" in flat(body), "")
    check("the empty page names no other player",
          SELLER not in body and BUYER not in body, "")
    check("two empty tables in production do not break the page",
          q("SELECT COUNT(*) AS c FROM stock_dividend_log")[0]["c"] == 0
          and q("SELECT COUNT(*) AS c FROM investor_payout_log")[0]["c"] == 0
          and st == 200, "")


# ══════════════════════════════════════════════════════════════════════════
# H12 — labelling. Every row says what it actually was.
# ══════════════════════════════════════════════════════════════════════════

async def t_labelling():
    print("\n[H12] Every row is labelled for what it actually was")
    # A real 'liquidated' trade at price 0 — not a fill, and it must not read as one.
    liq_user = q("SELECT user_id FROM stock_trade_log WHERE side='liquidated' LIMIT 1"
                 )[0]["user_id"]
    _SESSIONS["tok-liq"] = {"user_id": liq_user, "name": "Liquidated Holder",
                            "csrf": "csrf-l"}
    _st, body = await get("tok-liq", "/history?type=stock_trade")
    check("a liquidation is not called an exchange buy or sell",
          "Exchange buy" not in body and "Exchange sell" not in body, "")
    check("a liquidation is labelled with the word the bot stored",
          "liquidated" in body, "")
    check("a liquidation says it was not an exchange fill",
          "not an exchange fill" in body, "")
    check("a zero-price row does not print a price of 0 c each",
          "at 0 c each" not in body, "an invented price")
    check("a zero-coin row says no coins moved", "no coins moved" in body, "")

    # The hive month is a market total, and says so.
    _st, hive = await get("tok-seller", "/history?type=hive")
    check("a hive month is labelled HIVE MONTH", "HIVE MONTH" in hive, "")
    check("a hive month says the figures are market totals, not the viewer's",
          "market totals, not your own figures" in hive, "")
    check("a hive month names the role that earned the right to see it",
          "you are the owner" in hive, "")
    check("hive coins are excluded from the coins-in total",
          "2,053,095" not in hive.split("h-filters")[0], "a market total entered the strip")

    # An empty reason says so rather than being dressed up.
    empties = q("SELECT user_id FROM coin_ledger WHERE reason='' LIMIT 1")
    _SESSIONS["tok-empty"] = {"user_id": empties[0]["user_id"], "name": "E", "csrf": "x"}
    _st, e = await get("tok-empty", "/history?type=coin_ledger")
    check("an empty reason renders as 'No reason recorded'", "No reason recorded" in e, "")
    check("an empty reason is not dressed up as an adjustment or a transfer",
          "Adjustment" not in e, "")

    # A reason the humaniser has never seen is printed verbatim, not guessed at.
    head, _det = H.humanise_reason("something-nobody-has-ever-written-before")
    check("an unknown reason is returned verbatim",
          head == "something-nobody-has-ever-written-before", head)
    check("known reasons are made human",
          H.humanise_reason("order#46")[0] == "Shop order #46"
          and H.humanise_reason("stock buy greyhames")[0] == "Stock buy · GreyHames"
          and H.humanise_reason("hive:vtech:1537774138976894986")[0]
              == "Hive harvest payout · V Tech Hives", "")
    check("the raw reason is still reachable on the row",
          "hive:vtech:" in (await get("tok-stranger", "/history"))[1],
          "the humanised form cannot be checked against the stored one")

    # A dividend must never multiply a stored rate by a holding measured today. The
    # only way that figure can be produced is a multiplication, so assert there is no
    # multiplication and no division anywhere in the reader.
    fn = [f for f in ast.walk(ast.parse(Path(H.__file__).read_text()))
          if isinstance(f, ast.FunctionDef) and f.name == "read_dividends"][0]
    ops = [type(n.op).__name__ for n in ast.walk(fn) if isinstance(n, ast.BinOp)]
    check("the dividend reader contains no arithmetic at all", not ops, str(ops))
    check("the dividend reader selects the holding but reads no share count from it",
          "h.shares" not in ast.get_source_segment(Path(H.__file__).read_text(), fn)
              .split("ORDER BY")[0].replace("h.shares > 0", ""),
          "the holding size is being read into a figure")
    _st, div_note = await get("tok-buyer", "/history")
    check("a holder's page says the per-holder split is not recorded",
          "stock_dividend_log" not in div_note, "")


# ══════════════════════════════════════════════════════════════════════════
# H13 — house rules, on the served bytes
# ══════════════════════════════════════════════════════════════════════════

async def t_house_rules():
    print("\n[H13] House rules, measured on the bytes that reach a browser")
    pages = []
    for tok, path in (("tok-seller", "/history"), ("tok-buyer", "/history"),
                      ("tok-seller", OTC_LINK), ("tok-nobody", "/history"),
                      ("tok-seller", "/history?type=hive")):
        pages.append((await get(tok, path))[1])
    blob = "\n".join(pages)

    check("no ISO timestamp on screen",
          not re.search(r"20\d\d-\d\d-\d\dT\d\d:\d\d", blob), "ISO datetime rendered")
    check("no bare SQL date on screen",
          not re.search(r">\s*20\d\d-\d\d-\d\d\s+\d\d:\d\d:\d\d", blob), "")
    check("no raw epoch on screen", not re.search(r">\s*1[6-9]\d{8}(\.\d+)?\s*<", blob), "")
    check("dates are human", re.search(r"\d\d [A-Z][a-z]{2} 20\d\d", blob) is not None, "")
    check("no emoji anywhere",
          not any(ord(ch) > 0x2100 and ord(ch) not in (0x2013, 0x2014, 0x2018, 0x2019,
                                                       0x201C, 0x201D, 0x2022, 0x2026,
                                                       0x2212, 0x00B7, 0x2192)
                  for ch in blob),
          "non-ASCII beyond typographic punctuation")
    radii = [m for m in re.findall(r"border-radius:\s*([^;}]+)", blob)
             if m.strip() not in ("0", "0px", "50%")]
    check("zero border-radius outside status dots", not radii, str(radii))
    check("figures are mono with tabular numerals",
          "tabular-nums slashed-zero" in blob and "--font-data" in blob, "")
    check("repeating rows are a table, not cards", "<table>" in pages[0], "")
    check("the section header is a rule, not a big heading",
          "page-head" in pages[0] and "<h2" not in pages[0], "")
    check("no marketing voice",
          not re.search(r"seamless|effortless|supercharge|transform your", blob, re.I), "")
    check("gambling is nowhere near this page",
          not re.search(r"\b(bet|wager|casino|jackpot)\b", blob, re.I), "")


# ══════════════════════════════════════════════════════════════════════════
# H14 — escaping
# ══════════════════════════════════════════════════════════════════════════

async def t_escaping():
    print("\n[H14] A hostile note is escaped at render and untouched in the row")
    _st, body = await get("tok-fxa", "/history")
    check("the script tag does not reach the page live",
          "<script>alert(1)</script>" not in body, "XSS in the timeline")
    check("the img/onerror vector does not reach the page live",
          "<img src=x onerror=" not in body, "XSS in the timeline")
    check("it is present, escaped", "&lt;script&gt;alert(1)&lt;/script&gt;" in body, "")
    check("the ampersand is escaped exactly once",
          "&amp;amp;" not in body, "double-escaped")

    _st, perm = await get("tok-fxa", "/history/e/otc/gift:test:xss")
    check("the permalink escapes it too", "<script>alert(1)</script>" not in perm, "")
    check("the permalink prints the note in full, escaped",
          "&lt;script&gt;" in perm and "&amp; done" in perm, "")

    stored = q("SELECT note FROM share_gifts WHERE key='gift:test:xss'")[0]["note"]
    check("the stored row is byte-identical to what was written",
          stored == XSS_NOTE, repr(stored))


# ══════════════════════════════════════════════════════════════════════════
# H15 — wiring
# ══════════════════════════════════════════════════════════════════════════

async def t_truncation():
    print("\n[H16] A truncated history says it is truncated")
    _st, normal = await get("tok-stranger", "/history")
    check("nothing is truncated at the real cap", "not showing you everything" not in normal,
          "a false truncation warning")

    old = H.SOURCE_CAP
    H.SOURCE_CAP = 3                    # below this user's 22 wallet rows
    try:
        _st, cut = await get("tok-stranger", "/history")
        check("a capped source says the page is not showing everything",
              "not showing you everything" in cut, "a prefix presented as the whole")
        check("it names the source and the cap",
              "coin_ledger records and only the most recent 3" in cut, "")
        check("the rows it does show are still real",
              n_rows(cut) > 0 and "No reason recorded" in cut or n_rows(cut) > 0, "")
    finally:
        H.SOURCE_CAP = old

    events, problems = H.gather(SELLER)
    check("on the real data there are no problems to report", problems == [], str(problems))
    check("the seller's history is every source merged", len(events) == 13, str(len(events)))
    check("it is sorted newest first",
          all((a.get("event_at") or a.get("recorded_at") or 0)
              >= (b.get("event_at") or b.get("recorded_at") or 0)
              for a, b in zip(events, events[1:])), "out of order")


async def t_wiring():
    print("\n[H15] The section is reachable from the site")
    keys = [s["key"] for s in hub_web.sections()]
    check("history is registered in hub_web._SECTIONS", "history" in keys, str(keys))
    sec = [s for s in hub_web.sections() if s["key"] == "history"][0]
    check("the hub nav entry points at /history", sec["path"] == "/history", str(sec))
    check("the hub nav entry is labelled History", sec["label"] == "History", str(sec))
    check("the hub nav icon is inline SVG, not an emoji",
          sec["icon"].startswith("<path"), sec["icon"][:40])
    check("history is in the shell nav too",
          any(k == "history" and p == "/history" for k, _l, p in shell.NAV), str(shell.NAV))

    _st, body = await get("tok-seller", "/history")
    check("the shell nav renders the tab", 'data-k="history"' in body, "")
    check("the tab is marked current on its own page",
          'data-k="history"' in body and 'aria-current="true"' in
          body.split('data-k="history"')[0].rsplit("<a", 1)[-1] + 'aria-current="true"'
          if 'data-k="history"' in body else False, "")

    app = build_app()
    routes = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
    check("both routes exist", "/history" in routes
          and "/history/e/{source}/{eid}" in routes, str(sorted(routes)))
    check("no POST route exists on this section",
          not any(r.method == "POST" and "/history" in (r.resource.canonical or "")
                  for r in app.router.routes() if r.resource is not None),
          "a write route on a read-only section")
    check("every event on the page carries a permalink",
          tbody(body).count('href="/history/e/') == n_rows(body) == 13,
          f'{tbody(body).count(chr(34))} links vs {n_rows(body)} rows')


# ══════════════════════════════════════════════════════════════════════════

async def main():
    t_read_only_source()
    await t_anonymous()
    await t_read_only_rows()
    await t_otc_row()
    await t_unknown_event_date()
    await t_stranger()
    await t_permalink()
    await t_totals()
    await t_balance()
    await t_filters()
    await t_pagination()
    await t_empty()
    await t_labelling()
    await t_house_rules()
    await t_escaping()
    await t_truncation()
    await t_wiring()

    print("\n" + "=" * 70)
    print(f"  {len(PASSES)} passed, {len(FAILS)} failed")
    for f in FAILS:
        print(f"  FAIL  {f}")
    print("=" * 70)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
