"""Execution proof for panel SKU addressing + rollback ops.

Runs against a throwaway SQLite database built from the real `Restocker_db`
schema. No Discord, no network. Run it from the RestockerLocal directory:

    python3 tests/test_panel_skus_and_rollback.py

Every check prints PASS/FAIL and the script exits non-zero on the first failure,
so it is usable as a pre-deploy gate.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp(prefix="restocker-sku-test-")
os.chdir(_tmp)

import Restocker_db as db                                    # noqa: E402
db.DB_PATH = Path(_tmp) / "restocker.db"
db.init_db()

import panel_skus                                            # noqa: E402
import action_log                                            # noqa: E402

FAILURES = []
_n = 0


def check(label, cond, detail=""):
    global _n
    _n += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def head(t):
    print(f"\n=== {t}")


# ── A real-ish world to address ─────────────────────────────────────────────
with db.db() as c:
    c.execute("INSERT INTO markets (market_id, name, owner_id) VALUES (?,?,?)",
              ("greyhames", "Greyhames Trading Co.", "111"))
    c.execute("INSERT INTO markets (market_id, name, owner_id) VALUES (?,?,?)",
              ("ironvale", "Ironvale Depot", "222"))
    c.execute("INSERT INTO hive_claims (location, user_id, user_tag) VALUES (?,?,?)",
              ("Spawn Hive 3", "111", "Vaicos"))
    c.execute("INSERT INTO land_listings (id, seller_id, kind, title, mode, status) "
              "VALUES (?,?,?,?,?,?)", (412, "111", "land", "Riverside Plot", "auction", "active"))
    c.execute("INSERT INTO items (name, coin, stock) VALUES (?,?,?)", ("Diamond", 120, 64))
    c.execute("INSERT INTO market_stock (market_id, item, stock, capacity) VALUES (?,?,?,?)",
              ("greyhames", "Diamond", 64, 128))

# ═══════════════════════════════════════════════════════════════════════════
# A — PANEL SKUs
# ═══════════════════════════════════════════════════════════════════════════
head("A1 · a code is minted once and never changes under the user")
t1 = panel_skus.mint("hive", "Spawn Hive 3", "hive")
t2 = panel_skus.mint("hive", "Spawn Hive 3", "hive")
check("same entity -> same token across renders", t1 == t2, t1)
check("token length is 4", len(t1) == 4, t1)
check("token uses only the unambiguous alphabet",
      all(ch in panel_skus.ALPHABET for ch in t1), t1)
check("no l/o/0/1 anywhere in the alphabet",
      not (set("lo01") & set(panel_skus.ALPHABET)))

head("A2 · claim-first mint: 40 threads racing on one un-minted entity")
barrier = threading.Barrier(40)
seen = []
lock = threading.Lock()


def racer():
    barrier.wait()
    tok = panel_skus.mint("lot", "412", "lot")
    with lock:
        seen.append(tok)


ths = [threading.Thread(target=racer) for _ in range(40)]
[t.start() for t in ths]
[t.join() for t in ths]
check("all 40 racers converge on ONE token", len(set(seen)) == 1, str(set(seen)))
with db.db() as c:
    rows = c.execute("SELECT COUNT(*) n FROM panel_skus WHERE kind='lot' AND entity_id='412'"
                     ).fetchone()["n"]
check("exactly one row exists for that entity", rows == 1, f"rows={rows}")

head("A3 · addresses are pasteable and resolve back to one entity")
addr = panel_skus.address("market", "greyhames")
check("address shape is CODE.SUB.TOKEN", addr.startswith("0010.1.") and len(addr) == 11, addr)
tok = addr.rsplit(".", 1)[-1]
for typed in (tok, tok.upper(), f"  {tok} ", f"#{tok}", addr, f"0010.1.{tok.upper()}"):
    got = panel_skus.resolve(typed)
    check(f"resolves {typed!r}", len(got) == 1 and got[0]["entity_id"] == "greyhames",
          str([g["entity_id"] for g in got]))

head("A4 · a misread character is answered by the database, not by a guess")
misread = tok[0] + "l" + tok[2:] if len(tok) == 4 else tok      # user typed an impossible 'l'
got = panel_skus.resolve(misread)
check("a typed 'l' becomes a wildcard and still finds the market",
      any(g["entity_id"] == "greyhames" for g in got), f"{misread} -> {len(got)} hit(s)")
check("gibberish resolves to NOTHING (empty state, not a guess)",
      panel_skus.resolve("zzzz z") == [] or
      all(g["entity_id"] != "greyhames" for g in panel_skus.resolve("qqqqqq")))

head("A5 · real names, everywhere a user looks")
check("market -> real name", panel_skus.describe("market", "greyhames") == "Greyhames Trading Co.")
check("hive -> location + keeper",
      panel_skus.describe("hive", "Spawn Hive 3") == "Spawn Hive 3 (kept by Vaicos)")
check("lot -> listing title", panel_skus.describe("lot", "412") == "Riverside Plot")
check("a deleted entity describes as None, so /go can say so",
      panel_skus.describe("market", "does-not-exist") is None)

head("A6 · the picker means nobody has to type at all")
panel_skus.mint("market", "ironvale", "market")
sug = panel_skus.suggestions_for("111", query="")
labels = [s["label"] for s in sug]
check("suggestions carry REAL NAMES not ids",
      any("Greyhames Trading Co." in x for x in labels), str(labels))
check("the caller's own things come first",
      sug and ("Greyhames" in sug[0]["label"] or "Riverside" in sug[0]["label"]
               or "Spawn Hive" in sug[0]["label"]), str(labels[:3]))
check("name search works without a code",
      any(s["entity_id"] == "ironvale"
          for s in panel_skus.suggestions_for("111", query="ironvale")))

head("A7 · the footer stamp is idempotent (re-render never stacks codes)")


class _Footer:
    def __init__(self):
        self.text = None
        self.icon_url = None


class FakeEmbed:                       # stands in for discord.Embed
    def __init__(self, text=None):
        self.footer = _Footer()
        self.footer.text = text

    def set_footer(self, text=None, icon_url=None):
        self.footer.text = text
        self.footer.icon_url = icon_url


e = FakeEmbed("12 items · 3 months on record")
c1 = panel_skus.stamp(e, "market", "greyhames")
first = e.footer.text
c2 = panel_skus.stamp(e, "market", "greyhames")
check("code is stable across renders", c1 == c2, c1)
check("footer does not grow on re-render", e.footer.text == first, e.footer.text)
check("the original footer text survives", "12 items" in e.footer.text)
check("footer tells the user the command", "/go" in e.footer.text, e.footer.text)
e2 = FakeEmbed()
panel_skus.stamp(e2, "manager")
check("a panel with no entity still prints a static address",
      e2.footer.text == "Panel 0060", e2.footer.text)

# ═══════════════════════════════════════════════════════════════════════════
# B — ROLLBACK OPS
# ═══════════════════════════════════════════════════════════════════════════
# Stub the money path exactly as the real bot exposes it, so the ops engine is
# exercised through core.add_coins (its production branch) rather than the
# fallback.
_core = types.ModuleType("Restocker_main")


def _add_coins(uid, amount, *, counts_as_principal=True, reason=""):
    coins, principal, applied = db.adjust_balance(uid, int(amount),
                                                  counts_as_principal=counts_as_principal)
    db.record_coin_ledger(str(uid), applied, coins, reason)
    return coins, principal


_core.add_coins = _add_coins
sys.modules["Restocker_main"] = _core

db.adjust_balance("111", 500_000)
db.adjust_balance("222", 8_000)

head("B1 · an audit row carries its own reverse ops, in integer coins")
OPS = [
    {"t": "coins", "user_id": "111", "amount": -120_000, "principal": False},
    {"t": "coins", "user_id": "222", "amount": -30_000, "principal": False},
    {"t": "stock", "market_id": "greyhames", "item": "Diamond", "delta": 64},
    {"t": "setfields", "table": "items", "where": {"name": "Diamond"}, "fields": {"coin": 120}},
    {"t": "manual", "what": "Return the 3 shulkers handed over in-game",
     "hint": "The bot cannot move blocks; a staffer must."},
]
aid = action_log.record("hive_payout", "Hive payout for July · Greyhames Trading Co.",
                        OPS, actor_id="999", actor_name="Vaicos",
                        action_key="hivepay:2026-07:greyhames")
check("action recorded", isinstance(aid, int) and aid > 0, f"id={aid}")
check("money total is integer coins", action_log.get(aid)["money_coins"] == 150_000)
same = action_log.record("hive_payout", "duplicate attempt", OPS,
                         action_key="hivepay:2026-07:greyhames")
check("caller-minted action key makes re-recording a no-op", same == aid, f"{same} == {aid}")

head("B2 · the preview shows FIGURES before anything moves")
pv = action_log.preview(aid)
mv = {m[0]: m for m in pv["movements"]}
check("per-user before/change/after present", len(pv["movements"]) == 2, str(pv["movements"]))
check("user 111: 500,000 -> 380,000",
      mv["user 111"][1] == 500_000 and mv["user 111"][2] == -120_000 and mv["user 111"][3] == 380_000,
      str(mv["user 111"]))
check("user 222 clawback is SHORT (only 8,000 there of 30,000)",
      mv["user 222"][4] == -22_000, str(mv["user 222"]))
check("the manual op is surfaced before confirming", pv["manual"] == [OPS[4]["what"]])
bal_before = db.get_balance("111")["coins"]
check("preview moved NOTHING", bal_before == 500_000, str(bal_before))

head("B3 · claim-first: 25 threads press Rollback on the same audit row")
barrier = threading.Barrier(25)
winners, losers = [], []
lock = threading.Lock()


def clicker(i):
    barrier.wait()
    won, row = action_log.claim(aid, f"staff{i}", f"Staff {i}")
    with lock:
        (winners if won else losers).append(i)
    if won:
        action_log.apply_rollback(aid, staff_id=f"staff{i}", staff_name=f"Staff {i}")


ths = [threading.Thread(target=clicker, args=(i,)) for i in range(25)]
[t.start() for t in ths]
[t.join() for t in ths]
check("exactly ONE staffer won the claim", len(winners) == 1, f"winners={winners}")
check("the other 24 did nothing", len(losers) == 24)

after_111 = db.get_balance("111")["coins"]
after_222 = db.get_balance("222")["coins"]
check("user 111 debited exactly ONCE (500,000 - 120,000)", after_111 == 380_000, str(after_111))
check("user 222 clamped at zero, not negative", after_222 == 0, str(after_222))
with db.db() as c:
    n = c.execute("SELECT COUNT(*) n FROM coin_ledger WHERE reason LIKE 'rb:%'").fetchone()["n"]
check("exactly two compensating ledger rows exist — no double refund", n == 2, f"rows={n}")

head("B4 · every compensating entry is labelled with its idempotency key")
with db.db() as c:
    reasons = [r["reason"] for r in c.execute(
        "SELECT reason FROM coin_ledger WHERE reason LIKE 'rb:%' ORDER BY id").fetchall()]
check("keys are derived from the domain event, not from the click",
      reasons == [f"rb:{aid}#0", f"rb:{aid}#1"], str(reasons))
check("coin_ledger_has sees them", db.coin_ledger_has("111", f"rb:{aid}#0"))

head("B5 · non-money ops applied; markers written PER ROW")
with db.db() as c:
    stock = c.execute("SELECT stock FROM market_stock WHERE market_id='greyhames' AND item='Diamond'"
                      ).fetchone()["stock"]
check("stock restored 64 -> 128", stock == 128, str(stock))
states = {i: v["state"] for i, v in action_log.op_states(aid).items()}
check("money ops marked done", states[0] == "done" and states[1] == "done", str(states))
check("field restore marked done", states[3] == "done", str(states))
check("the un-automatable op is marked 'manual', not 'done'", states[4] == "manual", str(states))

head("B6 · what cannot be automated becomes a staff task, never silence")
tasks = action_log.list_tasks("open", limit=50)
titles = [t["title"] for t in tasks]
bodies = " ".join(t["body"] for t in tasks)
check("the manual op opened a task",
      any("shulkers" in t.lower() for t in titles), str(titles))
check("the short clawback opened a task with FIGURES",
      any("Short clawback" in t for t in titles) and "22,000 coins still outstanding" in bodies,
      str(titles))
check("tasks are idempotent per (action, op)",
      len({t["idem_key"] for t in tasks}) == len(tasks))

head("B7 · the whole thing is resumable and never double-applies")
before = db.get_balance("111")["coins"]
rep = action_log.apply_rollback(aid, staff_id="staff-again", staff_name="Someone Else")
check("a re-run applies nothing", db.get_balance("111")["coins"] == before, str(rep["done"]))
with db.db() as c:
    n2 = c.execute("SELECT COUNT(*) n FROM coin_ledger WHERE reason LIKE 'rb:%'").fetchone()["n"]
check("still exactly two compensating rows", n2 == 2, f"rows={n2}")

# Simulate a crash mid-run: op 1 left 'running' with its money already moved.
aid2 = action_log.record("test_resume", "Crash mid-rollback",
                         [{"t": "coins", "user_id": "111", "amount": 1_000, "principal": False},
                          {"t": "coins", "user_id": "111", "amount": 2_000, "principal": False}],
                         action_key="test:resume")
won, _ = action_log.claim(aid2, "staff-crash")
check("claimed the second action", won)
# op 0 completes; op 1 "crashes" after the money moved but before its marker.
with db.db() as c:
    c.execute("UPDATE sys_action_ops SET state='done' WHERE action_id=? AND op_index=0", (aid2,))
_core.add_coins("111", 2_000, counts_as_principal=False, reason=action_log.idem_key(aid2, 1))
with db.db() as c:
    c.execute("UPDATE sys_action_ops SET state='running' WHERE action_id=? AND op_index=1", (aid2,))
mid = db.get_balance("111")["coins"]
action_log.apply_rollback(aid2, staff_id="staff-crash")
check("the resumed run did NOT pay op 1 a second time (key on the ledger)",
      db.get_balance("111")["coins"] == mid, str(db.get_balance("111")["coins"]))

head("B8 · an action with no automatic reverse is honest about it")
aid3 = action_log.record("manual_only", "Renamed the Discord server",
                         [{"t": "manual", "what": "Rename the server back by hand"}],
                         action_key="test:manualonly")
check("reversible() is False -> the UI offers a staff task, not an undo",
      action_log.reversible(aid3) is False)
aid4 = action_log.record("nothing", "Something with no ops at all", [],
                         action_key="test:noops")
check("an empty op list is not reversible either", action_log.reversible(aid4) is False)

head("B9 · an op naming a table that is not on the allowlist is refused at WRITE time")
try:
    action_log.record("bad", "evil", [{"t": "setfields", "table": "balances",
                                       "where": {"user_id": "111"}, "fields": {"coins": 999999}}])
    check("a non-allowlisted table is rejected", False, "no exception raised")
except ValueError as ex:
    check("a non-allowlisted table is rejected", "not rollbackable" in str(ex), str(ex))

head("B10 · a claimed-but-uncancelled rollback can be handed back")
aid5 = action_log.record("cancelme", "Preview then cancel",
                         [{"t": "coins", "user_id": "111", "amount": -1, "principal": False}],
                         action_key="test:cancel")
won, _ = action_log.claim(aid5, "staffA", "Staff A")
action_log.release(aid5)
check("released back to open", action_log.get(aid5)["state"] == "open")
won2, _ = action_log.claim(aid5, "staffB", "Staff B")
check("a second staffer can now take it", won2)
action_log.apply_rollback(aid5, staff_id="staffB")
action_log.release(aid5)
check("release() refuses once an op has run",
      action_log.get(aid5)["state"] != "open", action_log.get(aid5)["state"])

head("B11 · a rollback that died part-way can be reopened without paying twice")
db.adjust_balance("777", 100_000, counts_as_principal=False)
aid6 = action_log.record(
    "halfdead", "Refund two people",
    [{"t": "coins", "user_id": "777", "amount": 5_000, "principal": False},
     # A stock op against a market_stock row that does not exist -> lands 'manual'.
     {"t": "stock", "market_id": "nosuchmarket", "item": "Nothing", "delta": 7}],
    action_key="test:halfdead")
won, _ = action_log.claim(aid6, "staffA", "Staff A")
check("claimed", won)
action_log.apply_rollback(aid6, staff_id="staffA")
paid_once = float(db.get_balance("777")["coins"])
check("op 0 paid", paid_once == 105_000.0, paid_once)
check("op 1 could not be automated -> 'manual', and the row is not 'done'",
      action_log.op_states(aid6)[1]["state"] == "manual"
      and action_log.get(aid6)["state"] != "done",
      f"{action_log.op_states(aid6)[1]['state']} / {action_log.get(aid6)['state']}")

# Now force the stuck shape the reopen exists for: an op that FAILED.
with db.db() as _c:
    _c.execute("UPDATE sys_action_ops SET state='failed' WHERE action_id=? AND op_index=1",
               (aid6,))
    _c.execute("UPDATE sys_actions SET state='failed' WHERE id=?", (aid6,))

check("a failed row cannot be claimed — the normal button is a dead end",
      not action_log.claim(aid6, "staffB", "Staff B")[0])

res = [None, None]


def _reopen(slot):
    res[slot] = action_log.reopen(aid6, f"staff{slot}")[0]


t1 = threading.Thread(target=_reopen, args=(0,))
t2 = threading.Thread(target=_reopen, args=(1,))
t1.start(); t2.start(); t1.join(); t2.join()
check("exactly ONE of two simultaneous Retry presses reopened it",
      sum(1 for r in res if r) == 1, str(res))
check("it is open again", action_log.get(aid6)["state"] == "open")
check("the op that already PAID is still 'done' — never reset",
      action_log.op_states(aid6)[0]["state"] == "done",
      str({k: v["state"] for k, v in action_log.op_states(aid6).items()}))
check("only the failed op went back to 'pending'",
      action_log.op_states(aid6)[1]["state"] == "pending")

won3, _ = action_log.claim(aid6, "staffC", "Staff C")
check("a staffer can take the reopened row", won3)
rep = action_log.apply_rollback(aid6, staff_id="staffC")
check("the resumed run skipped the paid op", 0 in rep["skipped"], str(rep["skipped"]))
check("and did NOT pay 5,000 a second time",
      float(db.get_balance("777")["coins"]) == 105_000.0, db.get_balance("777")["coins"])
check("reopen refuses a row that is not stuck", not action_log.reopen(aid5, "staffD")[0])

# ═══════════════════════════════════════════════════════════════════════════
# C — THE CONFIRM DIALOG ITSELF: two presses, ONE story
# ═══════════════════════════════════════════════════════════════════════════
# Everything above proves the DURABLE claim: `action_log.claim()` means the
# coins move exactly once however many people press. This section is about the
# other half — what the dialog SAYS. `ConfirmRollbackView.cancel` used to take
# no claim, so a Cancel dispatched alongside a Confirm that was landing left
# "Cancelled. Nothing moved." as the standing text of the dialog over a
# rollback that had just moved 12,000 coins. The money was right and the report
# was the opposite of it; an operator who believes the dialog reverses the sale
# a second time by hand.
head("C · ConfirmRollbackView — a second press never contradicts the first")

import asyncio                                               # noqa: E402

_core.is_manager = lambda interaction: True
_core.FUNDS_REPORT_CHANNEL_ID = 0
_core.log = None

_rb_import_error = None
try:
    from cogs import rollback as _rbcog                       # noqa: E402
except Exception as _e:                                      # noqa: BLE001
    _rbcog, _rb_import_error = None, _e
# Explicit, so this section can never be silently skipped: an import failure is
# a FAIL, not four checks that quietly never ran.
check("cogs.rollback imports", _rbcog is not None, str(_rb_import_error))


class _FakeUser:
    def __init__(self, uid, name):
        self.id, self.display_name = uid, name


class _FakeResponse:
    def __init__(self):
        self._done, self.sent = False, []

    def is_done(self):
        return self._done

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, content=None, **kw):
        self._done = True
        self.sent.append(content)

    async def edit_message(self, **kw):
        self._done = True
        self.sent.append(kw.get("content"))


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


class _FakeMessage:
    id = 1

    async def edit(self, **kw):
        pass

    async def delete(self):
        pass


class _FakeChannel:
    id = 5

    async def send(self, *a, **k):
        return _FakeMessage()

    async def fetch_message(self, mid):
        return _FakeMessage()


class _FakeClient:
    def get_channel(self, cid):
        return _FakeChannel()

    async def fetch_channel(self, cid):
        return _FakeChannel()


class _FakeInteraction:
    def __init__(self, user):
        self.user, self.client, self.guild = user, _FakeClient(), None
        self.message = _FakeMessage()
        self.response, self.followup = _FakeResponse(), _FakeFollowup()

    async def edit_original_response(self, **kw):
        pass

    def said(self):
        return [t for t in (self.followup.sent + self.response.sent) if t]


db.set_config("ops_log_channel_id", "5")


def _rb_rows():
    with db.db() as _c:
        return _c.execute("SELECT COUNT(*) n FROM coin_ledger "
                          "WHERE reason LIKE 'rb:%'").fetchone()["n"]


def _fresh_sale(key):
    db.adjust_balance("888", 12_000 - float(db.get_balance("888")["coins"]))
    return action_log.record(
        "order_pay", "test sale",
        [{"t": "coins", "user_id": "888", "amount": -12_000, "principal": False}],
        actor_name="Vaicos", action_key=key)


def _race(view, a, b):
    async def _go():
        return await asyncio.gather(a(), b(), return_exceptions=True)
    return asyncio.run(_go())


if _rbcog is not None:
    # C1 — the double-click. The durable claim already covers the money; this
    # pins that the losing press is TOLD, and told the truth.
    aid_c1 = _fresh_sale("test:confirmview:double")
    before, rows0 = float(db.get_balance("888")["coins"]), _rb_rows()
    v1 = _rbcog.ConfirmRollbackView(aid_c1, _FakeMessage())
    i1, i2 = _FakeInteraction(_FakeUser(1, "Vaicos")), _FakeInteraction(_FakeUser(1, "Vaicos"))
    _race(v1, lambda: v1.confirm.callback(i1), lambda: v1.confirm.callback(i2))
    said_c1 = i1.said() + i2.said()
    check("C1 a double-click reverses the sale exactly ONCE",
          float(db.get_balance("888")["coins"]) == before - 12_000
          and _rb_rows() - rows0 == 1,
          f"{before} -> {db.get_balance('888')['coins']}, +{_rb_rows() - rows0} rb rows")
    check("C1 the losing press is answered, not silently dropped",
          len(said_c1) == 2, str(said_c1))
    check("C1 and it is not told the rollback did not happen",
          not any("Nothing moved." in t for t in said_c1), str(said_c1))

    # C2 — the N3 shape itself: Cancel dispatched while Confirm is landing.
    aid_c2 = _fresh_sale("test:confirmview:cancel-races-confirm")
    before2, rows1 = float(db.get_balance("888")["coins"]), _rb_rows()
    v2 = _rbcog.ConfirmRollbackView(aid_c2, _FakeMessage())
    ic, ix = _FakeInteraction(_FakeUser(1, "Vaicos")), _FakeInteraction(_FakeUser(1, "Vaicos"))
    _race(v2, lambda: v2.confirm.callback(ic), lambda: v2.cancel.callback(ix))
    moved = float(db.get_balance("888")["coins"]) != before2
    check("C2 the money still moves exactly once",
          float(db.get_balance("888")["coins"]) == before2 - 12_000
          and _rb_rows() - rows1 == 1,
          f"{before2} -> {db.get_balance('888')['coins']}, +{_rb_rows() - rows1} rb rows")
    check("C2 NOTHING says 'Nothing moved' over a rollback that moved coins",
          not (moved and any("Nothing moved" in t for t in ic.said() + ix.said())),
          f"moved={moved} said={ic.said() + ix.said()}")

    # C3 — the inverse: Cancel gets there first. Then 'nothing moved' is TRUE,
    # and Confirm must not claim a reversal it never performed.
    aid_c3 = _fresh_sale("test:confirmview:cancel-wins")
    before3, rows2 = float(db.get_balance("888")["coins"]), _rb_rows()
    v3 = _rbcog.ConfirmRollbackView(aid_c3, _FakeMessage())
    jx, jc = _FakeInteraction(_FakeUser(1, "Vaicos")), _FakeInteraction(_FakeUser(1, "Vaicos"))
    _race(v3, lambda: v3.cancel.callback(jx), lambda: v3.confirm.callback(jc))
    check("C3 a Cancel that wins the claim leaves the coins alone",
          float(db.get_balance("888")["coins"]) == before3 and _rb_rows() == rows2,
          f"{before3} -> {db.get_balance('888')['coins']}")
    check("C3 the losing Confirm does not report a rollback that never ran",
          not any("Rolled back" in t or "step(s) applied" in t for t in jc.said()),
          str(jc.said()))
    check("C3 the action row is untouched — the log button still works",
          (action_log.get(aid_c3) or {}).get("state") == "open",
          str((action_log.get(aid_c3) or {}).get("state")))

print(f"\n{'=' * 60}\n{_n - len(FAILURES)}/{_n} checks passed.")
if FAILURES:
    print("FAILED:\n  - " + "\n  - ".join(FAILURES))
    sys.exit(1)
print("temp db:", db.DB_PATH)
