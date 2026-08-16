"""Execution proof for the Discord surface: embeds, buttons, delegation.

No gateway, no token. Builds the real discord.py objects the cogs build, and
drives the click handlers against fake interactions so the claim-first path in
the UI is exercised end to end (including two staff clicking Confirm at once).

    python3 tests/test_discord_surface.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp(prefix="restocker-ui-test-")
os.chdir(_tmp)

import discord                                               # noqa: E402
import Restocker_db as db                                    # noqa: E402
db.DB_PATH = Path(_tmp) / "restocker.db"
db.init_db()

# The cogs read module-level helpers off Restocker_main; stub the two they use.
_core = types.ModuleType("Restocker_main")
_core.log = None
_core.FUNDS_REPORT_CHANNEL_ID = 0
_core.is_manager = lambda interaction: getattr(interaction, "_staff", True)


def _add_coins(uid, amount, *, counts_as_principal=True, reason=""):
    coins, principal, applied = db.adjust_balance(uid, int(amount),
                                                  counts_as_principal=counts_as_principal)
    db.record_coin_ledger(str(uid), applied, coins, reason)
    return coins, principal


_core.add_coins = _add_coins
sys.modules["Restocker_main"] = _core

import panel_skus                                            # noqa: E402
import action_log                                            # noqa: E402
from cogs import rollback as rb                              # noqa: E402
from cogs import panel_skus as sku_cog                       # noqa: E402

FAILURES, _n = [], 0


def check(label, cond, detail=""):
    global _n
    _n += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def head(t):
    print(f"\n=== {t}")


# ── Fakes ───────────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, owner):
        self.owner = owner
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, content=None, *, embed=None, view=None, **kw):
        self._done = True
        self.owner.sent.append({"content": content, "embed": embed, "view": view})

    async def edit_message(self, content=None, *, embed=None, view=None, **kw):
        self._done = True
        self.owner.edits.append({"content": content, "embed": embed, "view": view})


class FakeFollowup:
    def __init__(self, owner):
        self.owner = owner

    async def send(self, content=None, *, embed=None, view=None, **kw):
        self.owner.sent.append({"content": content, "embed": embed, "view": view})


class FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name
        self.guild_permissions = discord.Permissions(administrator=True)


class FakeMessage:
    def __init__(self, mid=1, embeds=None):
        self.id = mid
        self.embeds = embeds or []
        self.edited = []

    async def edit(self, **kw):
        self.edited.append(kw)


class FakeInteraction:
    def __init__(self, user, message=None, staff=True):
        self.user = user
        self.guild = None
        self.client = None
        self.message = message
        self.sent, self.edits = [], []
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self._staff = staff

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── World ───────────────────────────────────────────────────────────────────
with db.db() as c:
    c.execute("INSERT INTO markets (market_id, name, owner_id) VALUES (?,?,?)",
              ("greyhames", "Greyhames Trading Co.", "111"))
db.adjust_balance("111", 400_000)
db.adjust_balance("222", 5_000)

OPS = [{"t": "coins", "user_id": "111", "amount": -12_000, "principal": False},
       {"t": "coins", "user_id": "222", "amount": -30_000, "principal": False}]
aid = action_log.record("order_pay", "Paid order #3181 · Greyhames Trading Co.",
                        OPS, actor_name="Vaicos", action_key="ui:order:3181")

head("U1 · the audit embed reads in real names and integer coins")
e = rb.build_action_embed(action_log.get(aid))
check("title is the human summary", "Greyhames Trading Co." in e.title, e.title)
fields = {f.name: f.value for f in e.fields}
check("money moved is shown up front", fields.get("Money moved") == "42,000 coins", str(fields))
check("the reverse ops are listed before anyone clicks",
      "Rollback would" in fields and "12,000" in fields["Rollback would"], str(fields))

head("U2 · the confirm screen is FIGURES, not intentions")
pv = action_log.preview(aid)
ce = rb.build_confirm_embed(action_log.get(aid), pv)
blob = ce.fields[0].value
check("before/change/after table rendered", "Before" in blob and "After" in blob, blob[:80])
check("exact integers present", "400,000" in blob and "388,000" in blob, blob)
check("the short clawback is called out",
      any("Cannot fully claw back" in f.name for f in ce.fields),
      str([f.name for f in ce.fields]))
check("it says nothing has moved yet", "preview" in (ce.footer.text or "").lower())

head("U3 · button + dynamic-item plumbing")
view = rb.rollback_view(aid)
btn = view.children[0]
check("custom_id carries only the action id", btn.custom_id == f"vtrb:{aid}", btn.custom_id)
m = rb.RollbackButton.__discord_ui_compiled_template__.match(btn.custom_id)
check("the persistent template matches its own custom_id", bool(m), btn.custom_id)
check("the handler re-derives the subject from the message, not from memory",
      m and int(m["aid"]) == aid)
nrv = rb.rollback_view(action_log.record("x", "no ops", [], action_key="ui:noops"),
                       reversible=False)
check("an unreversible action offers a staff task on its face",
      nrv.children[0].item.label == "📋 Open staff task", nrv.children[0].item.label)

head("U4 · a non-staff click moves nothing")
i = FakeInteraction(FakeUser(7, "Rando"), FakeMessage(), staff=False)
run(rb.handle_rollback_click(i, aid))
check("refused", i.sent and "Only managers" in i.sent[0]["content"], str(i.sent))
check("balance untouched", db.get_balance("111")["coins"] == 400_000)

head("U5 · a staff click PREVIEWS, it does not move money")
i = FakeInteraction(FakeUser(1, "Vaicos"), FakeMessage())
run(rb.handle_rollback_click(i, aid))
check("a confirm view came back",
      i.sent and isinstance(i.sent[0]["view"], rb.ConfirmRollbackView), str(i.sent))
check("still nothing moved", db.get_balance("111")["coins"] == 400_000)
check("the row is still open (a preview reserves nothing)",
      action_log.get(aid)["state"] == "open")
confirm_view = i.sent[0]["view"]

head("U6 · two staff press Confirm at the same instant — claim-first holds")
loop = asyncio.new_event_loop()
results = []
barrier = threading.Barrier(2)


def press(n):
    i2 = FakeInteraction(FakeUser(100 + n, f"Staff {n}"), FakeMessage())
    v = rb.ConfirmRollbackView(aid, i2.message)
    lp = asyncio.new_event_loop()
    barrier.wait()
    lp.run_until_complete(v.confirm.callback(i2))
    lp.close()
    results.append(i2)


ths = [threading.Thread(target=press, args=(n,)) for n in (1, 2)]
[t.start() for t in ths]
[t.join() for t in ths]

texts = [r.sent[-1]["content"] for r in results if r.sent]
winners = [t for t in texts if t.startswith("Rolled back")]
losers = [t for t in texts if "right now" in t or "Already rolled back" in t]
check("exactly one Confirm did the work", len(winners) == 1, str(texts))
check("the other is told who has it, and moves nothing", len(losers) == 1, str(texts))
check("user 111 debited exactly once", db.get_balance("111")["coins"] == 388_000,
      str(db.get_balance("111")["coins"]))
with db.db() as c:
    n = c.execute("SELECT COUNT(*) n FROM coin_ledger WHERE reason LIKE 'rb:%'").fetchone()["n"]
check("exactly two compensating rows — no double refund", n == 2, f"rows={n}")

head("U7 · the original message's button disables IN PLACE")
origin = [r for r in results if r.sent and r.sent[-1]["content"].startswith("Rolled back")][0]
edits = origin.message.edited
check("the origin message was edited", bool(edits), str(edits))
lbl = edits[-1]["view"].children[0].label if edits else ""
check("and it now says what happened", "Rolled back" in lbl, lbl)
check("the replacement button is disabled",
      edits and edits[-1]["view"].children[0].disabled is True)

head("U8 · a short clawback surfaces as a staff task card")
tasks = action_log.list_tasks("open")
check("a task exists for the 25,000 that was not there",
      any("Short clawback" in t["title"] for t in tasks), str([t["title"] for t in tasks]))
task = [t for t in tasks if "Short clawback" in t["title"]][0]
check("its body carries the figures", "25,000 coins still outstanding" in task["body"],
      task["body"])

head("U9 · Mark done is claim-first too, and re-resolves from the message")
card = discord.Embed(title=task["title"], description=task["body"])
card.set_footer(text=f"Task #{task['id']} · opened because …")
msg = FakeMessage(embeds=[card])
check("the task id is recovered from the card, not from view state",
      rb._task_id_from_message(msg) == task["id"])
v = rb.StaffTaskView()
i1 = FakeInteraction(FakeUser(1, "A"), msg)
i2 = FakeInteraction(FakeUser(2, "B"), msg)
run(v.done.callback(i1))
run(v.done.callback(i2))
check("first close wins", i1.edits and not i1.sent, str(i1.sent))
check("second is told it is already closed",
      i2.sent and "Already closed" in i2.sent[0]["content"], str(i2.sent))

head("U9b · Retry rollback on the task card unsticks a dead run")
# The button on the log message is disabled once a run finishes badly, so the
# task card is the ONLY route back. Drive it exactly as a staffer would.
stuck = action_log.record("stuckrun", "A run that died half way",
                          [{"t": "coins", "user_id": "111", "amount": 1_000,
                            "principal": False}],
                          action_key="ui:stuck")
action_log.claim(stuck, 2, "Staff 2")
with db.db() as _c:
    _c.execute("UPDATE sys_actions SET state='failed' WHERE id=?", (stuck,))
    _c.execute("UPDATE sys_action_ops SET state='failed' WHERE action_id=?", (stuck,))
stuck_task = action_log.open_task("Rollback op 0 failed", "boom",
                                  action_id=stuck, op_index=0, opened_by=2)
scard = discord.Embed(title="📋 Rollback op 0 failed", description="boom")
scard.set_footer(text=f"Task #{stuck_task} · opened because …")
smsg = FakeMessage(embeds=[scard])
v2 = rb.StaffTaskView()
r1 = FakeInteraction(FakeUser(3, "C"), smsg)
r2 = FakeInteraction(FakeUser(4, "D"), smsg)
run(v2.retry.callback(r1))
check("the action was recovered via the task, via the card — no view state",
      r1.sent and "Reopened" in (r1.sent[0].get("content") or ""), str(r1.sent))
check("a fresh confirm PREVIEW came back, not a silent re-run",
      r1.sent and r1.sent[0].get("view") is not None
      and r1.sent[0]["embed"].title == "Confirm rollback", str(r1.sent))
check("nothing moved on Retry itself — it only reopened",
      float(db.get_balance("111")["coins"]) == 388000.0, db.get_balance("111")["coins"])
run(v2.retry.callback(r2))
check("a second staffer pressing Retry is told, and does not reopen again",
      r2.sent and "already reopened" in (r2.sent[0].get("content") or ""), str(r2.sent))

plain = discord.Embed(title="📋 Go count the shulkers", description="by hand")
plain.set_footer(text=f"Task #{action_log.open_task('Go count the shulkers', 'by hand', idem='ui:plain')} · x")
r3 = FakeInteraction(FakeUser(3, "C"), FakeMessage(embeds=[plain]))
run(v2.retry.callback(r3))
check("Retry on a task that is not attached to a rollback says so",
      r3.sent and "not attached to a rollback" in (r3.sent[0].get("content") or ""), str(r3.sent))

head("U10 · /go: empty state, exact hit, ambiguity, dead entity")
panel_skus.mint("market", "greyhames", "market")
tok = panel_skus.peek("market", "greyhames")
cog = sku_cog.PanelSkuCog(bot=None)


class FakeTree:
    def __init__(self, opened):
        self.opened = opened

    def get_command(self, name):
        return None


class FakeClient:
    def __init__(self):
        self.tree = FakeTree([])


i = FakeInteraction(FakeUser(111, "Vaicos"))
i.client = FakeClient()
run(cog.go.callback(cog, i, code="zzzz"))
check("an unknown code says so and points at the picker",
      i.sent and "No panel answers" in i.sent[0]["content"], str(i.sent))
check("and explains the alphabet", "`l`" in i.sent[0]["content"])

i = FakeInteraction(FakeUser(111, "Vaicos"))
i.client = FakeClient()
run(cog.go.callback(cog, i, code=tok))
check("a real code reaches the delegate and reports honestly when it is absent",
      i.sent and "is not registered in this build" in i.sent[0]["content"], str(i.sent))

with db.db() as c:
    c.execute("DELETE FROM markets WHERE market_id='greyhames'")
i = FakeInteraction(FakeUser(111, "Vaicos"))
i.client = FakeClient()
run(cog.go.callback(cog, i, code=tok))
check("a code whose entity was deleted says exactly that",
      i.sent and "has since been removed" in i.sent[0]["content"], str(i.sent))

i = FakeInteraction(FakeUser(999, "Nobody"))
i.client = FakeClient()
run(cog.go.callback(cog, i, code=None))
check("empty state is EMPTY — no fake list",
      i.sent and "Nothing here has an address yet" in i.sent[0]["content"], str(i.sent))

head("U11 · autocomplete offers names, not ids")
db.upsert_market("ironvale", "Ironvale Depot", owner_id="111")
panel_skus.mint("market", "ironvale", "market")
i = FakeInteraction(FakeUser(111, "Vaicos"))
choices = run(cog._code_autocomplete(i, "iron"))
check("a name query returns a choice", len(choices) == 1, str(choices))
check("the label is the real name", "Ironvale Depot" in choices[0].name, choices[0].name)
check("the value is the code the user never has to read",
      choices[0].value == panel_skus.peek("market", "ironvale"))

head("U12 · the stamp binds to the panels this build ACTUALLY has")
# Stand up the three real builder shapes this repo uses, so the wiring is proved
# against the signatures in the source and not against an invented one:
#   views.market_settings.build_embed(mid, user)      -> async, id is positional
#   cogs.land_exchange._listing_embed(listing, bids)  -> id lives INSIDE a dict
#   Restocker_main._build_stock_panel_embed(mid)      -> lives in the main module
_vms = types.ModuleType("views.market_settings")


async def _build_market(mid, user=None):
    return discord.Embed(title=f"market {mid}")


_vms.build_embed = _build_market
_views_pkg = types.ModuleType("views")
_views_pkg.__path__ = []
sys.modules["views"] = _views_pkg
sys.modules["views.market_settings"] = _vms

# cogs.land_exchange pulls ~40 helpers off Restocker_main at import time, so the
# real module cannot be imported headless. Two separate proofs instead:
#  (a) the NAME we bind to really is defined in that file — checked in its source;
#  (b) the wiring + extractor work, checked against a stand-in at the same path.
_le_src = (HERE.parent / "cogs" / "land_exchange.py").read_text()
check("cogs/land_exchange.py really defines the function we bind to",
      "def _listing_embed(listing" in _le_src)

_le_mod = types.ModuleType("cogs.land_exchange")


def _listing_embed(listing, bids=None):
    return discord.Embed(title=f"lot {listing['id']}")


_le_mod._listing_embed = _listing_embed
sys.modules["cogs.land_exchange"] = _le_mod


def _build_stock_panel_embed(market_id):
    return discord.Embed(title=f"stock {market_id}")


_core._build_stock_panel_embed = _build_stock_panel_embed


def _team_perf_embed(manager_id, days=7):
    return discord.Embed(title=f"team {manager_id}")


_core._team_perf_embed = _team_perf_embed

# cogs/loops.py:64 does exactly this, at import time, and cogs/loops.py:765 is the
# ONLY caller of _team_perf_embed in the whole tree. A value copy into another
# module's globals is invisible to setattr(core, ...).
_loops = types.ModuleType("cogs.loops")
_loops._team_perf_embed = _core._team_perf_embed          # <- the alias capture
sys.modules["cogs.loops"] = _loops

check("before wiring, the aliased builder stamps nothing",
      _loops._team_perf_embed("77", 7).footer.text is None)

sku_cog._WIRED.clear()
bound, rebinds = sku_cog._wire_panels()
check("the market panel bound", "market" in bound, str(bound))
check("the auction lot panel bound", "lot" in bound, str(bound))
check("the stock panel bound (it lives in Restocker_main, not views.*)",
      "stock" in bound, str(bound))
check("the team panel bound", "team" in bound, str(bound))
check("every stamp target in the table bound — there is no 'unbound' list any more",
      len(bound) == len(sku_cog.STAMP_TARGETS), f"{len(bound)}/{len(sku_cog.STAMP_TARGETS)}")

head("U12b · the import-time ALIAS is re-pointed too (cogs/loops.py:64)")
check("the alias holder was found and rebound",
      "cogs.loops._team_perf_embed" in rebinds, str(rebinds))
check("Restocker_main's own name is not reported as an alias",
      not any(r.startswith("Restocker_main.") for r in rebinds), str(rebinds))
_te = _loops._team_perf_embed("77", 7)          # the call site at cogs/loops.py:765
check("the team digest embed now carries an address",
      "Panel 0030.1." in (_te.footer.text or ""), _te.footer.text)
check("and the team token was actually MINTED, so /go can resolve it",
      panel_skus.peek("team", "77") is not None, str(panel_skus.peek("team", "77")))
check("the alias and the module attribute are now the same object",
      _loops._team_perf_embed is _core._team_perf_embed)

head("U12c · a stamp target that does not resolve is FATAL, not a log line")
_saved_targets = list(sku_cog.STAMP_TARGETS)
sku_cog.STAMP_TARGETS.append(
    ("ghost", sku_cog.MAIN, "_build_item_panel_embed", lambda a, k: None,
     "the entry the audit found dead"))
sku_cog._WIRED.clear()
try:
    sku_cog._wire_panels()
    _raised = ""
except sku_cog.PanelWiringError as e:
    _raised = str(e)
check("an unresolvable target raises PanelWiringError", bool(_raised))
check("and the message names the panel and the symbol it wanted",
      "ghost" in _raised and "_build_item_panel_embed" in _raised,
      _raised.splitlines()[-1] if _raised else "")
sku_cog.STAMP_TARGETS[:] = _saved_targets
sku_cog._WIRED.clear()
sku_cog._wire_panels()

db.upsert_market("greyhames", "Greyhames Trading Co.", owner_id="111")
e = run(_vms.build_embed("greyhames"))
check("the market embed now carries its own address",
      "Panel 0010.1." in (e.footer.text or ""), e.footer.text)

lot_id = db.create_land_listing(seller_id="111", title="Spawn plot", kind="land",
                                category="Land", chunks=4, coords="100,200",
                                description="", starting_price=1000, buy_now=None,
                                ends_at=None, market_id=None, images=[]) \
    if hasattr(db, "create_land_listing") else None
row = {"id": lot_id or 412, "status": "active", "kind": "land", "title": "Spawn plot"}
e2 = _le_mod._listing_embed(row)
tok_lot = panel_skus.peek("lot", row["id"])
check("the lot's address came out of the listing ROW, not the argument position",
      bool(tok_lot) and tok_lot in (e2.footer.text or ""), f"{tok_lot} :: {e2.footer.text}")

row2 = dict(row, id=(row["id"] + 1))
e3 = _le_mod._listing_embed(row2)
check("a DIFFERENT lot gets a DIFFERENT address",
      panel_skus.peek("lot", row2["id"]) not in (None, tok_lot),
      f"{tok_lot} vs {panel_skus.peek('lot', row2['id'])}")

e4 = _core._build_stock_panel_embed("greyhames")
check("the main-module panel stamps too", "Panel 0090.1." in (e4.footer.text or ""),
      e4.footer.text)

sku_cog._wire_panels()
e5 = run(_vms.build_embed("greyhames"))
check("re-wiring does not double-wrap or stack footers",
      (e5.footer.text or "").count("Panel ") == 1, e5.footer.text)
check("and the address is STABLE across renders",
      e5.footer.text == e.footer.text, f"{e.footer.text} vs {e5.footer.text}")

head("U13 · /go runs the target command's OWN app-command checks")
# `cmd.callback(binding, interaction)` is the raw function: it skips
# Command._check_can_run, so every @app_commands.checks.* decorator on the target
# is skipped with it. Build a REAL app_commands.Command carrying a real failing
# check and drive the real _open().
_opened = []


@discord.app_commands.command(name="market", description="market panel")
async def _guarded(interaction, market_id: str = None):
    _opened.append(market_id)


_guarded.add_check(lambda i: getattr(i.user, "id", 0) == 111)


class _FakeTree:
    def __init__(self, leaf):
        self._leaf = leaf

    def get_command(self, part):
        return self if part == "my" else (self._leaf if part == "market" else None)


class _FakeBot:
    def __init__(self, leaf):
        self.tree = _FakeTree(leaf)


_i_allowed = FakeInteraction(FakeUser(111, "Vaicos"))
_i_allowed.client = _FakeBot(_guarded)
_i_denied = FakeInteraction(FakeUser(222, "Randomer"))
_i_denied.client = _FakeBot(_guarded)

ok, why = run(sku_cog._open(_i_allowed, "market", "greyhames"))
check("a user who passes the target's check still opens the panel", ok and not why, why)
check("and the entity was passed through as the command's own parameter",
      _opened == ["greyhames"], str(_opened))

ok2, why2 = run(sku_cog._open(_i_denied, "market", "greyhames"))
check("a user who FAILS the target's check is refused", ok2 is False, why2)
check("the panel callback never ran for them", _opened == ["greyhames"], str(_opened))
check("and the refusal names the panel, not an internal key",
      "Market settings" in why2, why2)

print(f"\n{'=' * 60}\n{_n - len(FAILURES)}/{_n} checks passed.")
if FAILURES:
    print("FAILED:\n  - " + "\n  - ".join(FAILURES))
    sys.exit(1)
