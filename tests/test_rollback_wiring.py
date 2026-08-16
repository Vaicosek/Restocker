"""Execution proof that the rollback subsystem has PRODUCERS.

The audit that prompted this file found `log_and_post` / `action_log.record`
nowhere outside cogs/rollback.py and its own tests: 471 lines of cog, 733 of
engine, 123 passing tests, and nothing that ever wrote a row. `sys_actions` was
never written, so `post_action_log` never ran, so no message carrying a `vtrb:`
custom_id was ever posted, so the ↩ Rollback button could not appear at all.

"The function exists" is not proof. This file runs the REAL money cores against a
real SQLite database with `ledger_migrate` applied, presses the button that
appears, and checks that what happened is what the surface said would happen.

WHAT IT USED TO SAY, AND WHY THAT WAS THE SECOND BUG
----------------------------------------------------
It said "…and checks the coins came back", and it asserted exactly that: buyer
credited `price`, seller debited `net`, house debited `commission`. That design
was replaced on 15 Aug — under escrow those three ops MINT, because the buyer's
coins were never destroyed, they were captured into `treasury:estates`. The
suite was not re-run, and it could not have been: without `ledger_migrate` every
core returns `paused_sentence()` and the file died at check 8 of ~92 on a `None`.
A "63/63" was reported from a tree where that was impossible.

So this file now asserts the design that SHIPS, and asserts it in the direction
that hurts:

  * a bid is a RESERVATION — `coins` does not move, `held` does;
  * the undo reverses the LISTING; the coin legs are a named staff task with the
    figures, and the buyer is still out `price` until a human moves it;
  * the confirm dialog shows BOTH figures and the big one is labelled as the one
    the button will not move (this rendered "Coins this will move: 2,000" for a
    40,000-coin sale);
  * total supply, treasury included, is conserved across every one of it.

If someone builds the compensating transfer later, the checks that say "the
buyer is still out the price" are the ones to invert — deliberately, with the
executor in hand, not by relaxing them.

    python3 tests/test_rollback_wiring.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="restocker-wiring-test-")
os.chdir(_tmp)

import Restocker_db as db                                    # noqa: E402

db.DB_PATH = Path(_tmp) / "restocker.db"
db.init_db()

# ── THE LEDGER, MIGRATED. This is not optional and it is not scenery. ───────
# Without it every land money core short-circuits on `esc.escrow_available()`
# and returns `paused_sentence()` — "the exchange is briefly paused…". This file
# ran that way for a whole round: check 8 of ~92 died on
# `TypeError: 'NoneType' object is not subscriptable` because the sale it was
# about to inspect had never happened, and a "63/63" was reported from a tree
# where the suite could not reach line 301. A wiring proof that runs against a
# paused exchange proves the pause.
import ledger_migrate                                          # noqa: E402
ledger_migrate.migrate(db.DB_PATH, verbose=False)
import ledger_v2                                               # noqa: E402
ledger_v2._local.__dict__.clear()
import land_escrow as _esc_boot                                # noqa: E402
_esc_boot.set_ledger(_esc_boot.LedgerV2InProcess())
assert _esc_boot.escrow_available(), (
    "the escrow is not available even after ledger_migrate — every core below "
    "would return paused_sentence() and every assertion would be vacuous")

FAILURES, _n = [], 0


def check(label, cond, detail=""):
    global _n
    _n += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def head(t):
    print(f"\n=== {t}")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def coins(uid):
    return int((db.get_balance(str(uid)) or {}).get("coins") or 0)


def held(uid):
    """Coins RESERVED against this wallet at core. Under escrow this is where a
    bid lives — `coins` does not move when you bid, which is why every balance
    assertion in this file had to be re-read after the retrofit."""
    return int((ledger_v2.get_balance(str(uid)) or {}).get("held") or 0)


def avail(uid):
    return int((ledger_v2.get_balance(str(uid)) or {}).get("available") or 0)


def treasury():
    return coins(_esc_boot.TREASURY)


# ── A stand-in for the 18k-line main module ─────────────────────────────────
# Only the handful of names the cores actually reach for. Everything else
# resolves to a no-op so the cog's ~40 import-time pulls and its command
# decorators bind; the MONEY functions below are the real ones from Restocker_db.
async def _ac(interaction, current):
    return []


class _Core(types.ModuleType):
    def __getattr__(self, n):
        if n.startswith("__"):
            raise AttributeError(n)
        f = _ac if n.endswith("autocomplete") else (lambda *a, **k: None)
        setattr(self, n, f)
        return f


core = _Core("Restocker_main")
core.log = types.SimpleNamespace(warning=lambda *a, **k: None,
                                 info=lambda *a, **k: None)


def add_coins(uid, amount, *, counts_as_principal=True, reason=""):
    c, p, applied = db.adjust_balance(uid, int(amount),
                                      counts_as_principal=counts_as_principal)
    db.record_coin_ledger(str(uid), applied, c, reason)
    return c, p


def deduct_coins(uid, amount, *, reduce_principal=True, reason=""):
    c, p, applied = db.adjust_balance(uid, -int(amount), reduce_principal=reduce_principal)
    db.record_coin_ledger(str(uid), applied, c, reason)
    return c, p


_PLATFORM_YAML = {"balance": 0}


def _credit_platform_balance(amount, *, market_id="", note="", month=None):
    amt = int(amount or 0)
    if amt <= 0:
        return 0
    db.set_platform_balance(db.get_platform_balance() + amt)
    db.add_platform_balance_log(month or "2026-08", market_id or "", float(amt), note or "")
    _PLATFORM_YAML["balance"] += amt
    return amt


def _add_platform_fee(amount, *, market_id, month, note=""):
    """The YAML mirror `_credit_platform_balance` keeps in step (Restocker_main.py:9798)."""
    _PLATFORM_YAML["balance"] += int(amount)
    return _PLATFORM_YAML["balance"]


core.add_coins = add_coins
core.deduct_coins = deduct_coins
core._credit_platform_balance = _credit_platform_balance
core._add_platform_fee = _add_platform_fee
core.utcnow_iso = lambda: "2026-08-15T09:00:00Z"
core.is_manager = lambda i: True
core.bot = None
core.FUNDS_REPORT_CHANNEL_ID = 4242
sys.modules["Restocker_main"] = core

_val = types.ModuleType("cogs.valuation")
_val.value_plot = lambda *a, **k: {"assessed_value": 0}
sys.modules["cogs.valuation"] = _val

import action_log                                            # noqa: E402
import panel_skus                                            # noqa: E402
from cogs import rollback as rb                              # noqa: E402
import cogs.land_exchange as le                              # noqa: E402

action_log.ensure_schema()
panel_skus.ensure_schema()


# ── Fake Discord surface ────────────────────────────────────────────────────
class FakeMessage:
    _next = 9000

    def __init__(self, channel, embed=None, view=None):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.channel = channel
        self.embeds = [embed] if embed else []
        self.view = view
        self.edits = []

    def custom_ids(self):
        out = []
        for c in getattr(self.view, "children", []) or []:
            cid = getattr(c, "custom_id", None) or getattr(
                getattr(c, "item", None), "custom_id", None)
            if cid:
                out.append(cid)
        return out

    async def edit(self, **kw):
        self.edits.append(kw)
        if kw.get("embed"):
            self.embeds = [kw["embed"]]
        if "view" in kw:
            self.view = kw["view"]


class FakeChannel:
    def __init__(self, cid=4242):
        self.id = cid
        self.guild = None
        self.sent = []

    async def send(self, content=None, *, embed=None, view=None, **kw):
        m = FakeMessage(self, embed, view)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        for m in self.sent:
            if m.id == int(mid):
                return m
        raise KeyError(mid)


class FakeClient:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, cid):
        return self.channel if int(cid) == self.channel.id else None


class FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name


class FakeResponse:
    def __init__(self):
        self._done = False
        self.sent = []

    def is_done(self):
        return self._done

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, content=None, *, embed=None, view=None, **kw):
        self._done = True
        self.sent.append({"content": content, "embed": embed, "view": view})

    async def edit_message(self, **kw):
        self._done = True
        self.sent.append(kw)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, embed=None, view=None, **kw):
        self.sent.append({"content": content, "embed": embed, "view": view})


class FakeInteraction:
    def __init__(self, user, client, message=None):
        self.user = user
        self.client = client
        self.guild = None
        self.message = message
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edits = []

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


CH = FakeChannel()
CLIENT = FakeClient(CH)
STAFF = FakeUser(999, "Vaicos")

db.set_config("ops_log_channel_id", str(CH.id))


# ── B0 · the mechanism has producers at all ─────────────────────────────────
head("B0 · action_log.record now has PRODUCTION callers, not just tests")
_prod = {}
for path in sorted(ROOT.rglob("*.py")):
    rel = path.relative_to(ROOT).as_posix()
    if rel.startswith("tests/") or rel.startswith("test_"):
        continue
    if rel in ("action_log.py",):
        continue
    hits = len(re.findall(r"(?:action_log|_al)\.record\(|log_and_post\(", path.read_text()))
    if hits:
        _prod[rel] = hits
check("at least one non-test module writes audit rows", bool(_prod), str(_prod))
check("the land exchange is one of them", "cogs/land_exchange.py" in _prod, str(_prod))
check("the main module is one of them", "Restocker_main.py" in _prod, str(_prod))


# ── B1 · a land sale settles, and the button appears ────────────────────────
head("B1 · a real land sale writes an audit row and posts a ↩ Rollback button")
SELLER, BUYER, LOSER = 111, 222, 333
add_coins(SELLER, 0)
add_coins(BUYER, 500_000)
add_coins(LOSER, 300_000)
db.set_platform_balance(0)

lot = db.create_land_listing(
    seller_id=str(SELLER), kind="land", title="Spawn Plot A3", category="Land",
    chunks=4, mode="auction", reserve=1000, buy_now=100_000,
    commission_pct=5.0, status="active")

# The pre-empted bidder: LOSER holds the top bid, BUYER instant-buys over them.
_SUPPLY0 = coins(SELLER) + coins(BUYER) + coins(LOSER) + treasury() + int(db.get_platform_balance() or 0)

res_bid = le._place_bid_core(lot, LOSER, 20_000)
# UNDER ESCROW A BID IS A RESERVATION, NOT A DEBIT. This used to assert
# `coins(LOSER) == 280_000` — a balance that only moved because the old design
# deducted the bid, which is the design the escrow retrofit replaced. Asserting
# the reservation is strictly stronger: it pins the coins in place AND pins the
# hold, and it fails if either half stops being true.
check("a standing bid RESERVES the loser's coins without moving them",
      res_bid["ok"] and coins(LOSER) == 300_000 and held(LOSER) == 20_000
      and avail(LOSER) == 280_000,
      f"loser coins={coins(LOSER):,} held={held(LOSER):,} available={avail(LOSER):,}")

_bal_before = {SELLER: coins(SELLER), BUYER: coins(BUYER), LOSER: coins(LOSER)}
_plat_before = int(db.get_platform_balance() or 0)

res = le._instant_buy_core(lot, BUYER)
check("the instant buy settled", res.get("ok"), str(res.get("error")))
check("the buyer paid the full price", coins(BUYER) == _bal_before[BUYER] - 100_000,
      f"{_bal_before[BUYER]:,} -> {coins(BUYER):,}")
check("the seller was paid net of commission", coins(SELLER) == 95_000, f"{coins(SELLER):,}")
check("the house took its commission", int(db.get_platform_balance()) == _plat_before + 5_000,
      f"{db.get_platform_balance()}")
check("the pre-empted bidder got their escrow back", coins(LOSER) == 300_000, f"{coins(LOSER):,}")

row = action_log.by_key(le.sale_action_key(lot))
check("AN AUDIT ROW EXISTS for the sale", row is not None,
      str(row and row["summary"]))
check("its summary is the lot's real name and an integer coin figure",
      row and "Spawn Plot A3" in row["summary"] and "100,000" in row["summary"],
      row and row["summary"])
# NEW-8. `money_coins` is `money_total(ops)` — the coins the BUTTON moves, and
# under escrow the button moves almost nothing: the only automatic money op on a
# land sale is the `platform -commission` reporting mirror. The 100,000 lives on
# the `manual` op as a declared exposure. This check used to assert one number
# (100,000 + 95,000 + 5,000) and, when the reverse ops became a staff task, it
# would have gone on passing as a single wrong figure if it had been "relaxed"
# to a range. So it pins BOTH numbers and pins which is which — that is the
# defect: the confirm dialog rendered 2,000 for a 40,000-coin sale because one
# number was being asked to be both.
_ops1 = action_log.ops_of(int(row["id"]))
check("the automatic money figure is the commission mirror ONLY — the button "
      "moves no player coins",
      int(row["money_coins"]) == 5_000 == action_log.money_total(_ops1),
      f"money_coins={int(row['money_coins']):,}")
check("...and the REAL exposure is declared on the staff task, so the confirm "
      "dialog can show the figure the sale was actually about",
      action_log.manual_total(_ops1) == 100_000,
      f"manual_total={action_log.manual_total(_ops1):,} "
      f"ops={[(o['t'], o.get('coins')) for o in _ops1]}")
check("...and the two are NOT the same number, which is the whole finding",
      action_log.money_total(_ops1) != action_log.manual_total(_ops1))
check("the undo is classified BY HAND, so the button cannot be labelled ↩ Rollback",
      action_log.undo_kind(int(row["id"])) == action_log.UNDO_BY_HAND,
      action_log.undo_kind(int(row["id"])))
check("the button on the ops-log message says what it does, not what it is called",
      rb.undo_label(int(row["id"])) == "↩ Reverse status · coins by hand",
      rb.undo_label(int(row["id"])))

msg = run(rb.post_by_key(CLIENT, le.sale_action_key(lot)))
check("A MESSAGE WAS POSTED to the ops log", msg is not None)
check("...carrying a vtrb: rollback custom_id — the button can now appear",
      any(c.startswith("vtrb:") for c in msg.custom_ids()), str(msg.custom_ids()))
check("...and the row remembers where it went, so the button disables in place",
      str(action_log.get(int(row["id"]))["message_id"]) == str(msg.id))
check("posting twice does not post a second button",
      run(rb.post_by_key(CLIENT, le.sale_action_key(lot))) is None
      and len(CH.sent) == 1, f"{len(CH.sent)} message(s)")


# ── B2 · pressing it actually moves the money back ──────────────────────────
#
# WHAT THIS SECTION ASSERTED UNTIL 15 AUG, AND WHY IT NO LONGER CAN.
# ------------------------------------------------------------------
# It asserted the buyer got `price` back, the seller gave `net` back and the
# house gave `commission` back — three `{"t":"coins"}` reverse ops. That design
# is GONE, and it did not go quietly: under escrow the buyer's coins were never
# destroyed, they were captured into `treasury:estates`, which paid the seller
# out and kept the commission as real coins. `{"t":"coins"}` is `adjust_balance`,
# which credits from nothing — so crediting the buyer back MINTS `commission`
# every press. `_sale_reverse_ops` closed that and emits a named staff task with
# the exact transfers instead.
#
# The consequence is real and is not hidden here: THE BUYER IS OUT `price` UNTIL
# A HUMAN MOVES IT. Owner's decision, 15 Aug — rename the button, do not
# half-build the executor. So these checks now assert the shipped design, and
# they assert it in the direction that hurts: the money did NOT come back, the
# row is not `done`, and the surface says so before the press and after it.
head("B2 · pressing the undo reverses the LISTING, and says so before it does")
aid = int(row["id"])
i = FakeInteraction(STAFF, CLIENT, message=msg)
run(rb.handle_rollback_click(i, aid))
confirm = i.followup.sent[-1]
check("the click previews rather than moving anything", confirm["embed"] is not None)
check("nothing moved during the preview", coins(SELLER) == 95_000, f"{coins(SELLER):,}")
_fig = "\n".join(f.value for f in confirm["embed"].fields)
_names = "\n".join(f.name for f in confirm["embed"].fields)
# THE NEW-8 REGRESSION, PINNED. The confirm dialog is irreversible and rendered
# "Coins this will move: 2,000" for a 40,000-coin sale — 5% of the truth, in the
# reassuring direction. Both figures must be on the dialog and the BIG one must
# be labelled as the one the button will not move.
check("the preview shows FIGURES, not intentions — including the big one",
      "100,000" in (_fig + _names) and "5,000" in (_fig + _names),
      (_names + " | " + _fig)[:200].replace("\n", " · "))
check("the headline does NOT claim the button moves the 100,000",
      "does NOT move 100,000 coins" in _names
      and "Coins this button will move: 5,000" in _names,
      _names.replace("\n", " · "))
check("the confirm title says status, not rollback",
      confirm["embed"].title == "Confirm — reverse status only", confirm["embed"].title)
check("the confirm BUTTON says it too, so the two cannot drift",
      confirm["view"].confirm.label == "Confirm — reverse status, no coins move",
      confirm["view"].confirm.label)
check("and it says nothing has moved yet",
      "Nothing has moved yet" in (confirm["embed"].footer.text or ""),
      confirm["embed"].footer.text)

view = confirm["view"]
i2 = FakeInteraction(STAFF, CLIENT, message=msg)
run(view.confirm.callback(i2))

check("THE BUYER IS STILL OUT THE PRICE — this button does not refund, and the "
      "suite says so rather than asserting a refund that stopped happening",
      coins(BUYER) == _bal_before[BUYER] - 100_000,
      f"{coins(BUYER):,} vs {_bal_before[BUYER]:,}")
check("the seller KEEPS the net — no clawback against a balance they may have spent",
      coins(SELLER) == 95_000, f"{coins(SELLER):,}")
check("NO COIN WAS MINTED by the press — the reverse ops contain no `coins` op "
      "at all, which is the one shape that credits from nothing",
      not any(o["t"] == "coins" for o in action_log.ops_of(aid)),
      str([o["t"] for o in action_log.ops_of(aid)]))
check("the buyer's price is where it actually is: treasury:estates",
      treasury() == 5_000, f"treasury={treasury():,}")
check("the house's commission SCALAR is backed out — that op is a REPORT, and "
      "the coins behind it are still in the treasury above",
      int(db.get_platform_balance()) == _plat_before,
      f"{db.get_platform_balance()}")
check("the YAML platform mirror moved too, so the two stores did not drift",
      _PLATFORM_YAML["balance"] == 0, str(_PLATFORM_YAML["balance"]))
check("the pre-empted bidder KEEPS the release settlement gave them — no re-hold "
      "against a balance they may have spent",
      coins(LOSER) == 300_000 and held(LOSER) == 0,
      f"coins={coins(LOSER):,} held={held(LOSER):,}")
# THE HONEST ENDING. `partial` is not a bug here, it is the shipped state: the
# staff task is outstanding. What must never happen is `done`, which would mean
# the bot considered a sale with a 100,000-coin hole in it fully reversed.
_arow = action_log.get(aid)
check("the audit row ends `partial`, NOT `done` — a rollback with an outstanding "
      "staff task must not report itself complete",
      _arow["state"] == "partial", _arow["state"])
_tasks = [o for o in action_log.ops_of(aid) if o["t"] == "manual"]
check("a staff task carries the exact compensating transfers, with figures, "
      "account names and idempotency keys — nobody is asked to invent one",
      any("100,000" in o.get("hint", "") and "95,000" in o.get("hint", "")
          and "reverse:buyer" in o.get("hint", "")
          and "reverse:seller" in o.get("hint", "") for o in _tasks),
      str([o.get("what") for o in _tasks]))
check("the staff-facing summary of the press says the coins have NOT moved",
      any("have NOT moved" in (s.get("content") or "") for s in i2.followup.sent),
      str([s.get("content") for s in i2.followup.sent])[:200])
check("SUPPLY IS CONSERVED across settle + reverse — no mint, no burn",
      (coins(SELLER) + coins(BUYER) + coins(LOSER) + treasury()
       + int(db.get_platform_balance() or 0)) == _SUPPLY0,
      f"{_SUPPLY0:,} -> {coins(SELLER) + coins(BUYER) + coins(LOSER) + treasury() + int(db.get_platform_balance() or 0):,}")
_l = db.get_land_listing(lot)


# ── B2b · NEW-1: the rolled-back lot is terminal, and every path agrees ─────
# This is the regression that cost +40,000 coins. The rollback restored
# `status='active'` with the pre-settlement bidder still on the row;
# `auction_sweep_loop` polls `status='active' AND ends_at <= now` once a minute
# and settled the SAME sale again, paying the seller `net` and the house
# `commission` out of nothing — against the same `land:sale:<id>` key, so
# `action_log.record` returned the existing row and no second button or embed
# ever appeared. The alternative path was as bad: a manager unwind on the
# restored listing refunded the winner their bid a second time.
#
# The shipped 63/63 only ever drove the INSTANT-BUY path, where buyer != standing
# bidder. The auction path — the exchange's default mode — was never tested. It
# is B2c below.
head("B2b · a rolled-back listing is terminal, and NO path will settle it again")
check("the listing did NOT go back to 'active'", _l["status"] == "rolled_back", _l["status"])
check("...and carries no phantom escrow",
      _l["current_bid"] is None and _l["current_bidder"] is None,
      f"{_l['current_bidder']} @ {_l['current_bid']}")
check("...and the sale marks are cleared",
      _l["sold_price"] is None and _l["sold_to"] is None,
      f"{_l['sold_price']} / {_l['sold_to']}")

db.update_land_listing(lot, ends_at="2020-01-01 00:00:00")   # long past its deadline
check("PATH 1 auction_sweep_loop — the lot is not in the expired queue",
      lot not in [r["id"] for r in db.get_expired_active_listings()],
      str([r["id"] for r in db.get_expired_active_listings()]))
check("PATH 1b _settle_expired's own re-read gate refuses it too",
      le._settle_gate(db.get_land_listing(lot)) is not None)
check("PATH 2 /realestate buy + the 🛒 Buy button",
      not le._instant_buy_core(lot, BUYER).get("ok"))
check("PATH 3 _finalize_sale_core, the settlement core itself",
      not le._finalize_sale_core(lot, BUYER, 100_000.0).get("ok"))
check("PATH 4 close_listing_core — manager force-settle",
      not le.close_listing_core(lot, refund_bidder=False).get("ok"))
check("PATH 4b close_listing_core(refund_bidder=True) — NO second refund",
      not le.close_listing_core(lot, refund_bidder=True).get("ok"))
check("PATH 5 /realestate bid + the 💰 Bid button",
      not le._place_bid_core(lot, LOSER, 250_000).get("ok"))
check("PATH 6 cancel_listing_core", not le.cancel_listing_core(lot, SELLER).get("ok"))
check("PATH 7 the satellite's headless routes reach those same cores and add no "
      "status logic of their own",
      all(fn in open(f"{ROOT}/Restocker_main.py").read()
          for fn in ("_instant_buy_core", "_place_bid_core", "close_listing_core",
                     "cancel_listing_core")))
check("it is off the public board the satellite renders (empty, not a ghost row)",
      lot not in [r["id"] for r in db.get_active_land_listings()])
check("the refusal says WHY and what to do, in real words",
      "/sell" in (le._settle_gate(db.get_land_listing(lot)) or ""),
      le._settle_gate(db.get_land_listing(lot)))
# The old form of this check spelled out the post-refund balances, so it was
# really two claims wearing one label: "nothing minted" AND "the money came
# back". The second is no longer true and its failure was masking the first.
# Split: conservation is the mint check, and it is the stronger one — it holds
# whatever the individual balances are, including the treasury, which the old
# form did not even look at.
_supply_now = (coins(BUYER) + coins(SELLER) + coins(LOSER) + treasury()
               + int(db.get_platform_balance() or 0))
check("NOT ONE COIN WAS MINTED OR BURNED by any of those attempts",
      _supply_now == _SUPPLY0,
      f"{_SUPPLY0:,} -> {_supply_now:,} ({_supply_now - _SUPPLY0:+,})  "
      f"buyer={coins(BUYER):,} seller={coins(SELLER):,} loser={coins(LOSER):,} "
      f"treasury={treasury():,} plat={db.get_platform_balance()}")
check("...and no attempt moved a single balance",
      coins(BUYER) == _bal_before[BUYER] - 100_000 and coins(SELLER) == 95_000
      and coins(LOSER) == 300_000 and int(db.get_platform_balance()) == _plat_before,
      f"buyer={coins(BUYER):,} seller={coins(SELLER):,} loser={coins(LOSER):,} "
      f"plat={db.get_platform_balance()}")
check("...and left nothing reserved on anyone",
      held(BUYER) == 0 and held(LOSER) == 0 and held(SELLER) == 0,
      f"buyer={held(BUYER)} loser={held(LOSER)} seller={held(SELLER)}")
check("and no reverse op anywhere in the list restores 'active'",
      not any(o.get("fields", {}).get("status") == "active"
              for o in action_log.ops_of(aid)),
      str([o.get("fields") for o in action_log.ops_of(aid) if o["t"] == "setfields"]))

# STRUCTURAL — so the eighth path cannot be added quietly. The mint happened
# because seven copies of `status != "active"` were spread across six functions
# and a SQL WHERE clause, and nothing owned the question. These two checks fail
# the moment someone reintroduces a private copy of the gate.
import ast as _ast2
_lex = _ast2.parse(open(f"{ROOT}/cogs/land_exchange.py").read())
_lefns = {}
for _node in _ast2.walk(_lex):
    if isinstance(_node, (_ast2.FunctionDef, _ast2.AsyncFunctionDef)):
        _lefns[_node.name] = _ast2.unparse(_node)
def _code_only(node):
    """The function's CODE — docstring dropped, so prose about `active` (this
    file is full of it now) is not mistaken for a live comparison."""
    body = list(node.body)
    if (body and isinstance(body[0], _ast2.Expr)
            and isinstance(getattr(body[0], "value", None), _ast2.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(_ast2.unparse(s) for s in body)


_MONEY_PATHS = ["_place_bid_core", "_instant_buy_core", "_finalize_sale_core",
                "close_listing_core", "cancel_listing_core", "_settle_expired",
                "close", "cancel"]
#: The six headless cores that hold the gate themselves. A slash-command handler
#: is covered when it delegates to one of these AND decides no status of its own.
_GATED_CORES = ("_place_bid_core", "_instant_buy_core", "_finalize_sale_core",
                "close_listing_core", "cancel_listing_core", "_settle_expired")


def _gate_cover(fn: str) -> str:
    """How `fn` is covered by the gate, or "" if it is not.

    TIGHTENED 15 AUG, AND NOT RELAXED. The old form demanded the literal string
    `_settle_gate` inside every named function, which made `/realestate cancel`
    a permanent red — it is a four-line handler that delegates to
    `cancel_listing_core`, which is gated. A guard that is red for a correct
    design is a guard nobody reads, and this one had been red across a whole
    round with `['cancel']` printed beside it and nobody looking.

    What it still refuses to accept: delegation is only cover if the delegating
    function decides NO listing status itself. The bug this guard exists for was
    seven private copies of `status != "active"` spread across six functions, and
    a handler that both delegates AND compares status is exactly that shape
    coming back. It reports the MECHANISM per path, so swapping one for the
    other is visible in the output instead of silently still passing.
    """
    src = _lefns.get(fn, "")
    if not src:
        return ""
    if "_settle_gate" in src:
        return "direct"
    delegates = [c for c in _GATED_CORES if (c + "(") in src]
    if not delegates:
        return ""
    body = _code_only_by_name(fn)
    if "'active'" in body.replace('"active"', "'active'") or "LIVE_STATUS" in body:
        return ""            # it delegates AND keeps an opinion — not cover
    return "delegates:" + "+".join(sorted(delegates))


def _code_only_by_name(name: str) -> str:
    for _n in _ast2.walk(_lex):
        if isinstance(_n, (_ast2.FunctionDef, _ast2.AsyncFunctionDef)) and _n.name == name:
            return _code_only(_n)
    return ""


_cover = {f: _gate_cover(f) for f in _MONEY_PATHS}
check("every path that moves coins against a listing is behind _settle_gate — all "
      f"{len(_MONEY_PATHS)} of them, each by a named mechanism",
      all(_cover.values()),
      str(_cover))
check("...and at least the six headless cores hold the gate DIRECTLY — delegation "
      "is cover for a handler, never for a core",
      all(_cover.get(c) == "direct" for c in _GATED_CORES),
      str({c: _cover.get(c) for c in _GATED_CORES}))
# TIGHTENED 15 AUG, AND NOT RELAXED. The old form flagged EVERY line naming
# 'active' outside three whitelisted functions, with a single get-out for lines
# that also said `create_land_listing`. That put `create_listing_core` and the
# `/sell` handler permanently in the red for
# `_db.update_land_listing(listing_id, status='active')` — a draft→active flip on
# a row the same function minted two lines earlier, which is the one write of
# 'active' that cannot possibly resurrect anything.
#
# The real question was never "does this line say active". It is TWO questions,
# and merging them is what made the guard cry wolf:
#
#   1. does anything COMPARE against 'active'?  -> that is a private copy of
#      `_settle_gate`, and it is the seven-copies bug coming back. Still flagged
#      everywhere outside the gate.
#   2. does anything WRITE 'active' onto a row it did not just create? -> that
#      is the resurrection NEW-1 was. Flagged unless the same function also
#      calls `create_land_listing`, which is a checkable fact about the
#      function, not a substring on the line.
#
# Both halves are strictly narrower than "the line mentions active", and the
# second one is new: the old guard had no concept of write-vs-compare at all, so
# a rollback restoring `status='active'` in a function that happened to mention
# `create_land_listing` anywhere on the line would have passed it.
_RENDERERS = ("_settle_gate", "_listing_embed", "_listing_view",
              "_listing_for_network")
_own_copy, _resurrect = [], []
for _node in _ast2.walk(_lex):
    if not isinstance(_node, (_ast2.FunctionDef, _ast2.AsyncFunctionDef)):
        continue
    if _node.name in _RENDERERS:
        continue                       # the gate itself + the renderers
    _body = _code_only(_node)
    _creates = "create_land_listing" in _body
    for _ln in _body.splitlines():
        _norm = _ln.replace('"active"', "'active'")
        if "'active'" not in _norm:
            continue
        if "==" in _norm or "!=" in _norm or " in " in _norm:
            _own_copy.append(f"{_node.name}: {_ln.strip()[:70]}")
        elif not _creates:
            _resurrect.append(f"{_node.name}: {_ln.strip()[:70]}")
check("...and nobody keeps a private COMPARISON against 'active': outside the "
      "gate and the renderers, no function decides liveness for itself",
      not _own_copy, str(_own_copy))
check("...and nothing WRITES 'active' onto a listing it did not just create — "
      "that write is the resurrection NEW-1 was built out of",
      not _resurrect, str(_resurrect))


# ── B2c · the AUCTION path — the one the mint actually lived on ─────────────
head("B2c · rolling back an AUCTION WIN (buyer IS the standing bidder) mints nothing")
WINNER = 444
add_coins(WINNER, 500_000)
# The treasury is IN the supply now. It was not before, and it had to be: under
# escrow the price does not vanish on settlement, it moves to `treasury:estates`,
# so a conservation check that omits the treasury reads a completed sale as a
# 40,000-coin burn. (The old form of this check compared only seller + winner +
# platform and passed by coincidence, because the old design really did destroy
# the buyer's coins.)
_supply = lambda: (coins(SELLER) + coins(WINNER) + treasury()
                   + int(db.get_platform_balance()))
_T_prebid = _supply()          # every account BEFORE the bid: winner 500,000, rest 0
_sellerA0, _platA0 = coins(SELLER), int(db.get_platform_balance() or 0)
_treasA0 = treasury()
lotA = db.create_land_listing(
    seller_id=str(SELLER), kind="land", title="Sniped Plot", category="Land", chunks=4,
    mode="auction", reserve=1000, commission_pct=5.0, status="active",
    ends_at="2027-01-01 00:00:00")
le._place_bid_core(lotA, WINNER, 40_000)
db.update_land_listing(lotA, ends_at="2020-01-01 00:00:00")      # the auction ends
le._finalize_sale_core(lotA, WINNER, 40_000.0)                   # the sweep settles it
check("the auction settled: seller net, house commission",
      coins(SELLER) == _sellerA0 + 38_000
      and int(db.get_platform_balance()) == _platA0 + 2_000
      and coins(WINNER) == 460_000,
      f"seller={coins(SELLER):,} (+{coins(SELLER) - _sellerA0:,}) "
      f"plat={db.get_platform_balance()} winner={coins(WINNER):,}")
check("...and the winner's 40,000 is in the treasury, not destroyed",
      treasury() == _treasA0 + 2_000,
      f"treasury={treasury():,} (+{treasury() - _treasA0:,})")
_rowA = action_log.by_key(le.sale_action_key(lotA))
action_log.apply_rollback(int(_rowA["id"]), staff_id=STAFF.id, guild=None)
# THE AUCTION-WIN CASE IS THE ONE THE MINT LIVED ON, and it is also the one
# where returning the money automatically is most obviously a mint: the buyer IS
# the standing bidder, so `{"t":"coins"}` crediting them `price` while the
# treasury still holds `commission` puts 2,000 coins into the economy that
# nobody gave up. This check asserted the credit. It now asserts the absence of
# the credit and the presence of the task, which is the shipped design.
check("the winner is NOT paid back by the button — the reverse ops carry no "
      "`coins` op, so nothing can be credited from nothing",
      coins(WINNER) == 460_000
      and not any(o["t"] == "coins" for o in action_log.ops_of(int(_rowA["id"]))),
      f"winner={coins(WINNER):,} "
      f"ops={[o['t'] for o in action_log.ops_of(int(_rowA['id']))]}")
check("...and the 40,000 they are owed is named on a staff task, with the "
      "treasury account and the idempotency key",
      any(o["t"] == "manual" and o.get("coins") == 40_000
          and "reverse:buyer" in o.get("hint", "")
          for o in action_log.ops_of(int(_rowA["id"]))),
      str([(o["t"], o.get("coins")) for o in action_log.ops_of(int(_rowA["id"]))]))
_lA = db.get_land_listing(lotA)
check("the lot is terminal with no bidder on it — NOT active with a phantom 40,000",
      _lA["status"] == "rolled_back" and _lA["current_bidder"] is None,
      f"{_lA['status']} bidder={_lA['current_bidder']}")
check("the once-a-minute sweep does not pick it up (this was the +40,000)",
      lotA not in [r["id"] for r in db.get_expired_active_listings()])
check("TOTAL SUPPLY is back exactly where it was BEFORE the bid — nothing minted, "
      "nothing burned, including the treasury the coins are actually sitting in",
      _supply() == _T_prebid,
      f"{_T_prebid:,} -> {_supply():,}  ({_supply() - _T_prebid:+,})  "
      f"winner={coins(WINNER):,} seller={coins(SELLER):,} treasury={treasury():,} "
      f"plat={db.get_platform_balance()}")
check("...and the house's reporting scalar IS backed out, even though the "
      "commission coins themselves are still in the treasury",
      int(db.get_platform_balance()) == _platA0, f"{db.get_platform_balance()}")
check("...and the winner has nothing reserved — the escrow is closed even "
      "though the money is not returned", held(WINNER) == 0, f"held={held(WINNER)}")
check("a manager unwind cannot refund the winner a second time",
      not le.close_listing_core(lotA, refund_bidder=True).get("ok")
      and coins(WINNER) == 460_000, f"{coins(WINNER):,}")
check("the audit row is marked, so a second press cannot pay again",
      action_log.get(aid)["state"] in ("done", "partial"), action_log.get(aid)["state"])
check("the ORIGINAL message's button was disabled in place",
      any("vtspent" in str(getattr(c, "custom_id", ""))
          for c in getattr(msg.view, "children", [])), str(msg.custom_ids()))

_st = action_log.op_states(aid)
check("every op carries its own marker (rule 2), not one flag for the batch",
      len(_st) == len(action_log.ops_of(aid)) and all(
          v["state"] in ("done", "manual") for v in _st.values()),
      str({k: v["state"] for k, v in _st.items()}))

i3 = FakeInteraction(FakeUser(1000, "Second Staffer"), CLIENT, message=msg)
run(rb.handle_rollback_click(i3, aid))
check("a second staffer is told it is already done, and nothing moves",
      "Already rolled back" in (i3.followup.sent[-1]["content"] or ""),
      i3.followup.sent[-1]["content"])
check("...and the buyer was not paid — not once, not twice. The second click is "
      "a no-op on a row whose money never moved automatically in the first place",
      coins(BUYER) == _bal_before[BUYER] - 100_000, f"{coins(BUYER):,}")


# ── B3 · the manager unwind ─────────────────────────────────────────────────
head("B3 · a manager force-unwind is logged and reversible")
lot2 = db.create_land_listing(seller_id=str(SELLER), kind="land", title="Cliff Plot B1",
                              mode="auction", reserve=500, commission_pct=5.0,
                              status="active")
_loser0 = coins(LOSER)
le._place_bid_core(lot2, LOSER, 30_000)
# A BID IS A RESERVATION. Same correction as B1: the balance does not move, the
# `held` does. Asserting the reservation is what makes the next line meaningful —
# an unwind "refunding" a bidder whose coins were never taken is a release, and
# the two are only distinguishable if you look at `held`.
check("the bidder's coins are RESERVED, not taken",
      coins(LOSER) == _loser0 and held(LOSER) == 30_000 and avail(LOSER) == _loser0 - 30_000,
      f"coins={coins(LOSER):,} held={held(LOSER):,} available={avail(LOSER):,}")

res2 = le.close_listing_core(lot2, refund_bidder=True)
check("the unwind RELEASED them — nothing was refunded because nothing was taken",
      res2["ok"] and coins(LOSER) == _loser0 and held(LOSER) == 0
      and avail(LOSER) == _loser0,
      f"coins={coins(LOSER):,} held={held(LOSER):,}")
check("...and the audit row says `released`, not `refunded` — the word "
      "close_listing_core's docstring promises it uses",
      "reservation released" in (action_log.by_key(le.unwind_action_key(lot2)) or {})["summary"]
      and "refunded" not in (action_log.by_key(le.unwind_action_key(lot2)) or {})["summary"],
      (action_log.by_key(le.unwind_action_key(lot2)) or {}).get("summary"))
row2 = action_log.by_key(le.unwind_action_key(lot2))
check("AN AUDIT ROW EXISTS for the unwind", row2 is not None)
check("its summary says what happened in real words and integer coins",
      row2 and "Cliff Plot B1" in row2["summary"] and "30,000" in row2["summary"],
      row2 and row2["summary"])

msg2 = run(rb.post_by_key(CLIENT, le.unwind_action_key(lot2)))
check("a Rollback button was posted for it",
      msg2 is not None and any(c.startswith("vtrb:") for c in msg2.custom_ids()))

i4 = FakeInteraction(STAFF, CLIENT, message=msg2)
run(rb.handle_rollback_click(i4, int(row2["id"])))
run(i4.followup.sent[-1]["view"].confirm.callback(FakeInteraction(STAFF, CLIENT, message=msg2)))
# The unwind's reverse used to claw the refund back and set the lot 'active' again.
# Both halves are unsafe together: `adjust_balance_tx` floors a clawback at 0, so a
# bidder who has spent the refund leaves a LIVE auction whose escrow is only partly
# there and the sweep pays the seller out of nothing; and `closed_at=None` with the
# original `ends_at` restores a listing already past its own deadline, which the
# sweep settles within a minute, unattended, on a row a manager touched seconds ago.
check("rolling back an unwind does NOT claw the refund back out of the bidder",
      coins(LOSER) == _loser0, f"{coins(LOSER):,}")
check("...and does NOT re-open the auction — the lot is terminal",
      db.get_land_listing(lot2)["status"] == "rolled_back",
      db.get_land_listing(lot2)["status"])
db.update_land_listing(lot2, ends_at="2020-01-01 00:00:00")
check("...so the sweep cannot settle it either",
      lot2 not in [r["id"] for r in db.get_expired_active_listings()])
check("...and a second manager unwind cannot refund again",
      not le.close_listing_core(lot2, refund_bidder=True).get("ok")
      and coins(LOSER) == _loser0, f"{coins(LOSER):,}")
check("the bidder is told, by name and figure, that the refund stands",
      any(o["t"] == "manual" and "30,000" in o.get("hint", "")
          for o in action_log.ops_of(int(row2["id"]))),
      str([o["t"] for o in action_log.ops_of(int(row2["id"]))]))


# ── B4 · the treasury op ────────────────────────────────────────────────────
head("B4 · a dividend's treasury leg is reversed too, with figures")
db.upsert_market("greyhames", "Greyhames Trading Co.", owner_id=str(SELLER))
db.upsert_market_shares("greyhames", active=1, share_price=100.0,
                        last_dividend_month="2026-07")
db.set_market_treasury_absolute("greyhames", 50_000.0)
_t0 = float(db.get_treasury("greyhames") or 0)
aid5 = action_log.record(
    "dividend_manual", "Paid a 12,000-coin dividend for 2026-08 · Greyhames",
    [{"t": "coins", "user_id": str(BUYER), "amount": -12_000, "principal": True},
     {"t": "treasury", "market_id": "greyhames", "delta": 12_000},
     {"t": "setfields", "table": "market_shares", "where": {"market_id": "greyhames"},
      "fields": {"last_dividend_month": "2026-07"}}],
    action_key="dividend:manual:greyhames:2026-08")
db.upsert_market_shares("greyhames", last_dividend_month="2026-08")
db.adjust_treasury("greyhames", -12_000.0)
add_coins(BUYER, 12_000)
_bal5 = coins(BUYER)

pv = action_log.preview(aid5)
check("the treasury shows up in the preview MOVEMENTS with before/after",
      any("treasury" in m[0].lower() for m in pv["movements"]), str(pv["movements"]))
_tm = [m for m in pv["movements"] if "treasury" in m[0].lower()][0]
check("...with the real figures on it",
      int(_tm[1]) == 38_000 and int(_tm[2]) == 12_000 and int(_tm[3]) == 50_000, str(_tm))
check("the headline money figure counts the dividend ONCE, not both legs",
      int(action_log.get(aid5)["money_coins"]) == 12_000,
      str(action_log.get(aid5)["money_coins"]))

won, _ = action_log.claim(aid5, STAFF.id, STAFF.display_name)
rep = action_log.apply_rollback(aid5, staff_id=STAFF.id, guild=None)
check("the rollback applied", won and rep["state"] == "done", str(rep["state"]))
check("THE TREASURY WAS RESTORED", int(db.get_treasury("greyhames")) == int(_t0),
      f"{db.get_treasury('greyhames')} vs {_t0}")
check("the holder's coins were clawed back", coins(BUYER) == _bal5 - 12_000,
      f"{coins(BUYER):,}")
# THE SIBLING OF NEW-1. The ops above are hand-built by this test, so they still
# carry the old `last_dividend_month` un-stamp — that is what makes the check
# below meaningful: it reads PRODUCTION, not the fixture.
#
# `_payout_share_dividends` is reached from the CSN month ingest, which re-runs
# every time a report lands, and it refuses a month on two guards: (1)
# `last_dividend_month == month_key`, and (2) `dividend_paid()`, a permanent
# `stock_dividend_log` row. That un-stamp cleared guard 1 — and bought nothing,
# because guard 2 already blocked the hook. Except in one state: `_declare_
# dividend` writes the stamp and the log row in ONE try/except, so if
# `log_dividend` raises, the stamp lands and the permanent row does not, and
# guard 1 is all that is left. Rolling back then cleared the last guard and the
# next ingest would re-pay the whole month — minted coins, by that function's own
# admission. So the producer no longer emits an op that can clear a payment
# guard; it opens a task with the figures instead.
_src_main = open(f"{ROOT}/Restocker_main.py").read()
_divfn = _src_main[_src_main.index("def _record_dividend_action("):]
_divfn = _divfn[:_divfn.index("\nasync def _post_audit_row")]
check("the dividend reverse ops no longer contain a setfields at all — the un-stamp "
      "was the only op in the vocabulary that could REMOVE a payment guard",
      '"t": "setfields"' not in _divfn, "still emits a setfields")
check("...nor a delrow that could delete the permanent stock_dividend_log row",
      '"t": "delrow"' not in _divfn)
check("...and it says so with the figures, as a staff task instead",
      '"t": "manual"' in _divfn and "still recorded as paid" in _divfn)


# ── B5 · a delisted market fails loudly instead of silently ─────────────────
head("B5 · a treasury restore with nowhere to go opens a staff task, not silence")
aid6 = action_log.record("dividend_manual", "Dividend for a market since delisted",
                         [{"t": "treasury", "market_id": "vanished", "delta": 5_000}],
                         action_key="dividend:manual:vanished:2026-08")
action_log.claim(aid6, STAFF.id)
rep6 = action_log.apply_rollback(aid6, staff_id=STAFF.id, guild=None)
check("it did not report success", rep6["state"] != "done", rep6["state"])
check("it opened a staff task with the figure", len(rep6["tasks"]) == 1, str(rep6["tasks"]))
_t = action_log.get_task(rep6["tasks"][0])
check("...and the task says what was NOT applied",
      "+5,000" in _t["body"] and "NOT applied" in _t["body"], _t["body"][:120])


# ── B6 · the REAL _pay_dividend_now, not a re-implementation ────────────────
head("B6 · Restocker_main._pay_dividend_now itself writes the row")
# Restocker_main is 18.6k lines and cannot be imported headless, so lift the two
# functions' REAL source out of the file by AST and run them against stubs. If
# either is edited to stop recording, this fails.
import ast                                                   # noqa: E402

_src = (ROOT / "Restocker_main.py").read_text()
_tree = ast.parse(_src)
# `_execute_dividend_run` joined this set when the manual dividend gained
# per-holder progress markers: it is the loop that claims each holder's leg,
# charges the treasury for it and credits it, so lifting `_pay_dividend_now`
# without it no longer lifts the money path. Same rule as before — if the real
# file stops recording, this fails.
_want = {"_pay_dividend_now", "_record_dividend_action", "_execute_dividend_run"}
_ns = {"log": core.log, "add_coins": add_coins, "STOCK_TREASURY_ENABLED": True,
       "_drip_reinvest": lambda uid, amt, mid: (0, 0),
       "_market_stock_label": lambda mid: "GREY",
       "__name__": "Restocker_main_slice"}
_found = set()
for node in _tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _want:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "Restocker_main.py", "exec"), _ns)
        _found.add(node.name)
check("both functions were lifted from the real file", _found == _want, str(_found))

db.upsert_market("ironvale", "Ironvale Depot", owner_id=str(SELLER))
db.upsert_market_shares("ironvale", active=1, share_price=10.0,
                        last_dividend_month="2026-06")
db.set_market_treasury_absolute("ironvale", 80_000.0)
db.adjust_holding(str(BUYER), "ironvale", 60.0, 600.0)
db.adjust_holding(str(LOSER), "ironvale", 40.0, 400.0)

_b_before, _l_before = coins(BUYER), coins(LOSER)
_prev = _ns["_pay_dividend_now"]("ironvale", 10_000, "2026-08", False)
check("the preview moved nothing", _prev["ok"] and coins(BUYER) == _b_before,
      f"{coins(BUYER):,}")
check("no audit row is written for a preview",
      action_log.by_key("dividend:manual:ironvale:2026-08") is None)

_out = _ns["_pay_dividend_now"]("ironvale", 10_000, "2026-08", True)
check("the dividend paid", _out["ok"] and _out["paid"] == 10_000, str(_out.get("paid")))
check("holders were credited pro-rata",
      coins(BUYER) == _b_before + 6_000 and coins(LOSER) == _l_before + 4_000,
      f"{coins(BUYER) - _b_before:,} / {coins(LOSER) - _l_before:,}")
check("THE FUNCTION RETURNED AN ACTION KEY for its async caller to post",
      _out.get("action_key") == "dividend:manual:ironvale:2026-08", str(_out.get("action_key")))

drow = action_log.by_key("dividend:manual:ironvale:2026-08")
check("AN AUDIT ROW EXISTS for the dividend", drow is not None)
check("its summary carries real names and integer coins",
      drow and "10,000-coin dividend" in drow["summary"] and "2 holder(s)" in drow["summary"],
      drow and drow["summary"])
_ops = action_log.ops_of(int(drow["id"]))
check("the reverse ops came from what was actually CREDITED, not from the plan",
      sorted(o["amount"] for o in _ops if o["t"] == "coins") == [-6_000, -4_000],
      str([o for o in _ops if o["t"] == "coins"]))
check("the treasury leg is there because this dividend charged the treasury",
      any(o["t"] == "treasury" and o["delta"] == 10_000 for o in _ops), str(_ops))

msg6 = run(rb.post_by_key(CLIENT, _out["action_key"]))
check("and a ↩ Rollback button was posted for it",
      msg6 is not None and any(c.startswith("vtrb:") for c in msg6.custom_ids()))

i6 = FakeInteraction(STAFF, CLIENT, message=msg6)
run(rb.handle_rollback_click(i6, int(drow["id"])))
run(i6.followup.sent[-1]["view"].confirm.callback(FakeInteraction(STAFF, CLIENT, message=msg6)))
check("PRESSING IT UNWINDS THE WHOLE DIVIDEND",
      coins(BUYER) == _b_before and coins(LOSER) == _l_before,
      f"{coins(BUYER):,} / {coins(LOSER):,}")
check("...including the treasury it was paid out of",
      int(db.get_treasury("ironvale")) == 80_000, str(db.get_treasury("ironvale")))
check("...but NOT the month stamp — a rollback must never clear a payment guard",
      db.get_market_shares("ironvale")["last_dividend_month"] == "2026-08",
      str(db.get_market_shares("ironvale")["last_dividend_month"]))
check("...and the permanent stock_dividend_log guard is still standing, so the "
      "automatic month-close hook still refuses 2026-08",
      db.dividend_paid("ironvale", "2026-08") is True,
      str(db.dividend_paid("ironvale", "2026-08")))
check("the staff task says the month is still recorded as paid, with the figure "
      "and the previous value",
      any(o["t"] == "manual" and "10,000" in o.get("hint", "")
          and "2026-06" in o.get("hint", "")
          for o in action_log.ops_of(int(drow["id"]))),
      str([o["t"] for o in action_log.ops_of(int(drow["id"]))]))


print(f"\n{'=' * 60}\n{_n - len(FAILURES)}/{_n} checks passed.")
print(f"temp db: {db.DB_PATH}")
if FAILURES:
    print("FAILED:\n  - " + "\n  - ".join(FAILURES))
    sys.exit(1)
