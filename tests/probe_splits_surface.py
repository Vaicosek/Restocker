"""Executable proof for `cogs/splits.py` — the configuration surface for the
split engine. EXECUTION, not reading.

Every check here drives a real `/splits` callback against a real temp SQLite
database (real `Restocker_db` schema, real `ledger_migrate`, real `ledger_v2`,
real `split_rules` tables) through a fake Discord interaction, and then asserts
against the ROWS, not against the reply. A surface that says "✅ Rule added" and
writes nothing is the defect this file exists to catch; so is a surface that
writes a rule the operator never confirmed.

What is asserted, in order:

  A  the staff gate, and that a denial writes nothing
  B  render writes NOTHING; Confirm is what writes
  C  the 100% cap at the surface, WITH the figures — and that the surface's
     check does not replace the one inside `add_rule`'s transaction
  D  the preview shows current vs proposed side by side, in percent and coins
  E  basis points end to end — no float reaches a rule row
  F  retiring a rule, including the second press and the parked-run warning
  G  re-ordering: the refusals, the rows, and the MONEY consequence (who
     absorbs the remainder under `prorate`)
  H  the short-source policy
  I  the parked-run surfaces (`/splits runs`, `/splits run`) — F3's other half
  J  real names everywhere a human looks
  K  wiring both ways: every name the cog calls exists, every command it defines
     is reachable, and the cog is registered in the bot's extension list

Run:  python3 tests/probe_splits_surface.py
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import traceback
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
for candidate in (Path("/home/claude/build"), ROOT.parent / "build"):
    if (candidate / "ledger_v2.py").exists():
        sys.path.insert(0, str(candidate))
        break

# The cog reads `Restocker_main` at import. Stub it BEFORE the import, and keep
# `IS_STAFF` mutable so the staff gate can be tested from both sides.
IS_STAFF = {"yes": True}
_core = types.ModuleType("Restocker_main")
_core.is_manager = lambda interaction: bool(IS_STAFF["yes"])
_core.log = None
sys.modules.setdefault("Restocker_main", _core)

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ok   {name}")
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, e))
        print(f"  FAIL {name}: {e}")
        traceback.print_exc()


def eq(a, b, what=""):
    if a != b:
        raise AssertionError(f"{what}: {a!r} != {b!r}")


def truthy(v, what=""):
    if not v:
        raise AssertionError(f"{what}: expected truthy, got {v!r}")


def has(hay: str, needle: str, what=""):
    if needle not in hay:
        raise AssertionError(f"{what}: {needle!r} not in {hay!r}")


# ── the temp world ──────────────────────────────────────────────────────────
DB = {"path": None}


def fresh():
    tmp = tempfile.mkdtemp(prefix="splitsurf_")
    path = Path(tmp) / "restocker.db"
    import Restocker_db as db
    db.DB_PATH = path
    db._local.__dict__.clear()
    db.init_db()
    import ledger_migrate
    ledger_migrate.migrate(path, verbose=False)
    import ledger_v2
    ledger_v2._local.__dict__.clear()
    import split_rules as sr
    with ledger_v2._tx() as conn:
        sr.ensure_schema(conn)
    DB["path"] = path
    return db, sr


def give(user_id, coins):
    import Restocker_db as db
    with db.db() as conn:
        conn.execute("INSERT OR REPLACE INTO balances (user_id, coins) VALUES (?,?)",
                     (str(user_id), int(coins)))


def coins(user_id):
    import Restocker_db as db
    with db.db() as conn:
        r = conn.execute("SELECT coins FROM balances WHERE user_id=?",
                         (str(user_id),)).fetchone()
    return int(r["coins"]) if r else 0


def q(sql, args=()):
    conn = sqlite3.connect(DB["path"])
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


def world():
    conn = sqlite3.connect(DB["path"])
    n = conn.execute("SELECT COALESCE(SUM(CAST(coins AS INTEGER)),0) "
                     "FROM balances").fetchone()[0]
    conn.close()
    return int(n)


def rules_rows(src="treasury:estates"):
    return q("SELECT * FROM split_rules WHERE source_account=? ORDER BY seq, id", (src,))


def version(src="treasury:estates"):
    r = q("SELECT version FROM split_rulesets WHERE source_account=?", (src,))
    return int(r[0]["version"]) if r else 0


# ── the fake Discord surface ────────────────────────────────────────────────
class FakeRole:
    def __init__(self, rid, name):
        self.id = int(rid)
        self.name = name

    @property
    def mention(self):
        return f"<@&{self.id}>"


class FakeMember:
    def __init__(self, uid, display_name):
        self.id = int(uid)
        self.display_name = display_name

    def __str__(self):
        return self.display_name


class FakeGuild:
    def __init__(self, roles=(), members=()):
        self._roles = {int(r.id): r for r in roles}
        self._members = {int(m.id): m for m in members}

    def get_role(self, rid):
        return self._roles.get(int(rid))

    def get_member(self, uid):
        return self._members.get(int(uid))


class _Response:
    def __init__(self, itx):
        self.itx = itx

    async def send_message(self, content=None, *, embed=None, view=None,
                           ephemeral=False):
        self.itx.sent.append({"where": "response", "content": content,
                              "embed": embed, "view": view, "ephemeral": ephemeral})

    async def defer(self, *, ephemeral=False, thinking=False):
        self.itx.deferred = True

    async def edit_message(self, *, content=None, embed=None, view=None):
        self.itx.edits.append({"content": content, "embed": embed, "view": view})


class _Followup:
    def __init__(self, itx):
        self.itx = itx

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False):
        self.itx.sent.append({"where": "followup", "content": content,
                              "embed": embed, "view": view, "ephemeral": ephemeral})


class FakeInteraction:
    def __init__(self, user=None, guild=None):
        self.user = user or FakeMember(1001, "Vaicos")
        self.guild = guild
        self.sent = []
        self.edits = []
        self.deferred = False
        self.response = _Response(self)
        self.followup = _Followup(self)

    async def edit_original_response(self, *, content=None, embed=None, view=None):
        self.edits.append({"content": content, "embed": embed, "view": view})

    # ── readers the checks use ──
    @property
    def last(self):
        return self.sent[-1] if self.sent else {}

    def text(self):
        """Everything the operator can read: contents + every embed field."""
        out = []
        for s in self.sent:
            if s.get("content"):
                out.append(str(s["content"]))
            e = s.get("embed")
            if e is not None:
                out.append(str(getattr(e, "title", "") or ""))
                out.append(str(getattr(e, "description", "") or ""))
                for f in getattr(e, "fields", []):
                    out.append(f"{f.name}\n{f.value}")
                ft = getattr(e, "footer", None)
                if ft is not None and getattr(ft, "text", None):
                    out.append(str(ft.text))
        return "\n".join(out)

    def view(self):
        for s in reversed(self.sent):
            if s.get("view") is not None:
                return s["view"]
        return None

    def all_ephemeral(self):
        return all(s.get("ephemeral") for s in self.sent)


import cogs.splits as S  # noqa: E402

COG = S.SplitsCog(bot=None)
SRC = "treasury:estates"

GUILD = FakeGuild(
    roles=[FakeRole(555, "Market Owners")],
    members=[FakeMember(1001, "Vaicos"), FakeMember(2002, "Osentar")],
)


def call(cmd, itx=None, **kw):
    """Run one /splits subcommand callback to completion."""
    itx = itx or FakeInteraction(guild=GUILD)
    asyncio.run(getattr(S.SplitsCog, cmd).callback(COG, itx, **kw))
    return itx


def press(view, button="confirm", user_id=1001):
    itx = FakeInteraction(user=FakeMember(user_id, "Vaicos"), guild=GUILD)
    asyncio.run(getattr(view, button).callback(itx))
    return itx


def add_confirmed(**kw):
    """The two-step add, both steps, as an operator performs it."""
    itx = call("splits_add", **kw)
    v = itx.view()
    truthy(v is not None, f"no confirm view offered for add({kw}): {itx.text()}")
    return press(v), itx


# ═══════════════════════════════════════════════════════════════════════════
# A — the staff gate
# ═══════════════════════════════════════════════════════════════════════════

def t_non_staff_cannot_write_a_rule():
    fresh()
    IS_STAFF["yes"] = False
    try:
        itx = call("splits_add", bps=5000, account="2002")
        has(itx.text(), "Managers only", "a non-manager must be refused by name")
        eq(itx.view(), None, "and must not be offered a confirm button")
        eq(len(rules_rows()), 0, "and must not have written a rule")
        itx = call("splits_list")
        has(itx.text(), "Managers only", "even the read is staff-gated")
    finally:
        IS_STAFF["yes"] = True


def t_every_reply_is_ephemeral():
    """Rule sets are staff business; they name real accounts and real shares."""
    fresh()
    itx = call("splits_list")
    truthy(itx.all_ephemeral(), "an ephemeral surface that posts publicly is a leak")
    itx2 = call("splits_add", bps=2500, account="2002")
    truthy(itx2.all_ephemeral(), "the add preview must be ephemeral too")


# ═══════════════════════════════════════════════════════════════════════════
# B — render writes nothing; Confirm writes
# ═══════════════════════════════════════════════════════════════════════════

def t_rendering_the_preview_writes_nothing():
    fresh()
    v0 = version()
    itx = call("splits_add", bps=2500, account="2002", label="Osentar's cut")
    truthy(itx.view() is not None, "an add must offer a confirm button")
    eq(len(rules_rows()), 0, "the PREVIEW wrote a rule — nothing may be written "
                             "until Confirm is pressed")
    eq(version(), v0, "the preview bumped the ruleset version")
    has(itx.text(), "Nothing has been written yet",
        "the preview must say it is a preview")


def t_confirm_writes_exactly_one_rule():
    fresh()
    itx = call("splits_add", bps=2500, account="2002", label="Osentar's cut")
    out = press(itx.view())
    rows = rules_rows()
    eq(len(rows), 1, "confirm must write exactly one rule")
    eq(int(rows[0]["bps"]), 2500, "the share, in basis points")
    eq(rows[0]["beneficiary_ref"], "2002", "the beneficiary")
    eq(rows[0]["beneficiary_kind"], "account", "the kind")
    eq(rows[0]["label"], "Osentar's cut", "the label the operator typed")
    eq(int(rows[0]["active"]), 1, "and it is live")
    # The first write CREATES the ruleset row at v1; the second bumps it. Both
    # halves matter, because the version is what a run pins.
    eq(version(), 1, "the first rule creates the ruleset at v1")
    press(call("splits_add", bps=1000, account="3003").view())
    eq(version(), 2, "the second write bumps the ruleset version")
    has(out.text(), "25.00%", "the reply states the share as a percentage")
    has(out.text(), f"#{rows[0]['id']}", "and names the rule id it wrote")


def t_cancel_writes_nothing_and_says_so():
    fresh()
    itx = call("splits_add", bps=2500, account="2002")
    out = press(itx.view(), "cancel")
    eq(len(rules_rows()), 0, "Cancel wrote a rule")
    has(out.text(), "nothing was written", "Cancel must say what it did")


def press_concurrently(view, *buttons, user_id=1001):
    """Dispatch several presses THE WAY DISCORD DISPATCHES THEM: one task each,
    all in flight at once. `press()` runs a callback to completion before the
    next one starts, which is the sequential case and cannot see the window
    between the defer and the write."""
    itxs = [FakeInteraction(user=FakeMember(user_id, "Vaicos"), guild=GUILD)
            for _ in buttons]

    async def both():
        return await asyncio.gather(
            *(getattr(view, b).callback(i) for b, i in zip(buttons, itxs)),
            return_exceptions=True)

    results = asyncio.run(both())
    for r in results:
        if isinstance(r, BaseException):
            raise AssertionError(f"a press raised {type(r).__name__}: {r}")
    return itxs


def t_a_spent_view_cannot_be_pressed_twice():
    """Both halves. The sequential press is the easy one; the DOUBLE-CLICK is
    the one that wrote two rules, and the old version of this probe asserted
    only the first while its name promised the second."""
    fresh()
    itx = call("splits_add", bps=2500, account="2002")
    v = itx.view()
    press(v)
    eq(len(rules_rows()), 1, "first press writes")
    truthy(all(c.disabled for c in v.children),
           "every button must be disabled after the write — a live button over a "
           "spent preview is a second rule one double-click away")
    truthy(v.is_finished(), "and the view must stop listening")
    # …and pressing the spent view again writes nothing.
    again = press(v)
    eq(len(rules_rows()), 1, "a second press on a spent view wrote a second rule")
    has(again.text(), "already been used", "and the operator is told why")


def t_a_real_double_click_writes_ONE_rule():
    """N2. Two presses dispatched concurrently: the second arrives while the
    first is still inside `asyncio.to_thread(apply)`, before anything is
    disabled. `/splits add` carries no per-intent key, so two calls are two
    genuinely different rules — the guard has to be on the button."""
    fresh()
    itx = call("splits_add", bps=3000, account="2002")
    v = itx.view()
    a, b = press_concurrently(v, "confirm", "confirm")
    rows = rules_rows()
    eq(len(rows), 1, f"a double-click wrote {len(rows)} rules for one intent")
    eq(int(rows[0]["bps"]), 3000, "and the surviving rule is the one previewed")
    truthy("already been used" in (a.text() + b.text()),
           "exactly one press must be told the preview was already used")


def t_cancel_racing_confirm_cannot_deny_a_write_that_is_landing():
    """Confirm and Cancel pressed together. Whichever wins, the reply and the
    rows must agree: `Cancelled — nothing was written` over a rule that WAS
    written is the same defect pointing the other way."""
    fresh()
    itx = call("splits_add", bps=3000, account="2002")
    v = itx.view()
    press_concurrently(v, "confirm", "cancel")
    rows = rules_rows()
    said_cancelled = "Cancelled" in str(v.result or "")
    eq(len(rows), 0 if said_cancelled else 1,
       f"the view says {v.result!r} but there are {len(rows)} rule row(s)")


def t_the_preview_belongs_to_who_opened_it():
    fresh()
    itx = call("splits_add", bps=2500, account="2002")
    v = itx.view()
    other = FakeInteraction(user=FakeMember(9009, "Someone Else"), guild=GUILD)
    eq(asyncio.run(v.interaction_check(other)), False, "a stranger may not confirm")
    has(other.text(), "belongs to whoever opened it", "and is told why")
    mine = FakeInteraction(user=FakeMember(1001, "Vaicos"), guild=GUILD)
    eq(asyncio.run(v.interaction_check(mine)), True, "the opener may confirm")


# ═══════════════════════════════════════════════════════════════════════════
# C — the 100% cap, at the surface AND in the transaction
# ═══════════════════════════════════════════════════════════════════════════

def t_the_cap_is_enforced_at_the_surface_with_the_figures():
    _, sr = fresh()
    sr.add_rule(SRC, "account", "2002", 9000)
    itx = call("splits_add", bps=2000, account="1001")
    eq(itx.view(), None, "over 100% must not even be offered a confirm button")
    txt = itx.text()
    for figure in ("past 100%", "90.00%", "20.00%", "10.00%", "1000 bps"):
        has(txt, figure, "the refusal must show the arithmetic")
    eq(len(rules_rows()), 1, "and must write nothing")


def t_exactly_100_percent_is_allowed():
    _, sr = fresh()
    sr.add_rule(SRC, "account", "2002", 9000)
    itx = call("splits_add", bps=1000, account="1001")
    truthy(itx.view() is not None, "exactly 100% is legal and must be offered")
    press(itx.view())
    eq(sum(int(r["bps"]) for r in rules_rows() if int(r["active"])), 10000,
       "the rules total exactly 100%")


def t_the_surface_check_does_not_replace_the_transaction_check():
    """The b3 property: the authority is the guard inside `add_rule`'s
    BEGIN IMMEDIATE. Here the world changes between the preview and the press —
    which is exactly what a second admin does — and the write must lose."""
    _, sr = fresh()
    itx = call("splits_add", bps=2000, account="1001")
    v = itx.view()
    truthy(v is not None, "the preview was legal when it was rendered")
    sr.add_rule(SRC, "account", "2002", 9000)   # another admin, in between
    out = press(v)
    has(out.text(), "Refused", "the transaction must refuse the stale preview")
    has(out.text(), "nothing was written", "and say so definitely, not ambiguously")
    rows = [r for r in rules_rows() if int(r["active"])]
    eq(len(rows), 1, "only the other admin's rule exists")
    eq(int(rows[0]["bps"]), 9000, "and it is untouched")
    eq(sum(int(r["bps"]) for r in rows), 9000, "the total never passed 100%")


def t_bps_out_of_range_is_refused_before_anything():
    fresh()
    for bad in (0, -100, 10001):
        itx = call("splits_add", bps=bad, account="2002")
        eq(itx.view(), None, f"bps={bad} must not reach a confirm button")
        has(itx.text(), "1..10000", f"bps={bad}: the legal range must be stated")
    eq(len(rules_rows()), 0, "nothing written")


def t_exactly_one_beneficiary():
    fresh()
    itx = call("splits_add", bps=2500)
    has(itx.text(), "exactly one beneficiary", "neither account nor role")
    itx = call("splits_add", bps=2500, account="2002", role=FakeRole(555, "Market Owners"))
    has(itx.text(), "exactly one beneficiary", "both at once")
    eq(len(rules_rows()), 0, "nothing written")


# ═══════════════════════════════════════════════════════════════════════════
# D — the figures, in the same view as the button
# ═══════════════════════════════════════════════════════════════════════════

def t_the_preview_shows_current_and_proposed_side_by_side():
    _, sr = fresh()
    sr.add_rule(SRC, "account", "2002", 4000, label="Osentar")
    itx = call("splits_add", bps=2500, account="1001")
    txt = itx.text()
    for want in ("now", "after", "coins/10k", "40.00%", "25.00%",
                 "retained", "60.00%", "35.00%"):
        has(txt, want, "the confirm screen must show current vs proposed")
    # The coins column is the point: 25% of 10,000 is 2,500, floor-divided
    # exactly as `plan_split` does it.
    has(txt, "2,500", "the proposed share, in coins per 10,000 of income")
    has(txt, "3,500", "and what the source retains after it")
    truthy(itx.view() is not None, "figures and button in the SAME view")
    eq(itx.last["view"], itx.view(), "the button is attached to the message that "
                                     "carries the figures, not a later one")


def t_every_edit_states_what_happens_to_money_in_flight():
    """F1's decision, in words, at the moment of the edit: the pinned original
    wins. Every write path must say it — this is the property that surprises."""
    _, sr = fresh()
    rid = sr.add_rule(SRC, "account", "2002", 4000)["rule_id"]
    sr.add_rule(SRC, "account", "1001", 1000)
    for itx in (call("splits_add", bps=1000, account="3003"),
                call("splits_remove", rule_id=rid),
                call("splits_reorder", order=f"{rid} 2"),
                call("splits_policy", policy="prorate")):
        txt = itx.text()
        has(txt, "ruleset version", "the operator must be told the version bumps")
        has(txt, "NEXT income event", "and what the new rules govern")
        has(txt, "pinned", "and that an event already running keeps its plan")
        has(txt, "pending_funds", "including a parked run")


def t_retiring_a_rule_warns_that_a_parked_run_still_pays():
    _, sr = fresh()
    rid = sr.add_rule(SRC, "account", "2002", 4000)["rule_id"]
    itx = call("splits_remove", rule_id=rid)
    txt = itx.text()
    has(txt, "still pay", "F6: a parked run pays a beneficiary retired since")
    has(txt, "/splits runs", "and the operator is told where to look")


# ═══════════════════════════════════════════════════════════════════════════
# E — basis points end to end
# ═══════════════════════════════════════════════════════════════════════════

def t_no_float_ever_reaches_a_rule_row():
    fresh()
    for bps in (1, 7, 2500, 3333, 10000):
        _, sr = fresh()
        itx = call("splits_add", bps=bps, account="2002")
        if itx.view() is None:
            raise AssertionError(f"bps={bps} was refused: {itx.text()}")
        press(itx.view())
        row = rules_rows()[0]
        eq(type(row["bps"]), int, f"bps={bps} stored as {type(row['bps'])}")
        eq(int(row["bps"]), bps, "stored exactly as typed")


def t_percentages_are_rendered_from_the_integer():
    eq(S._pct(2500), "25.00%", "2500 bps")
    eq(S._pct(1), "0.01%", "one basis point")
    eq(S._pct(10000), "100.00%", "the whole thing")
    eq(S._pct(3333), "33.33%", "a third, near enough")
    eq(S._pct(0), "0.00%", "nothing")


def t_a_sub_one_percent_share_is_flagged_as_the_typo_it_probably_is():
    """`bps=40` is 0.40%. An operator who meant 40% must be shown both numbers
    BEFORE the button, not discover it when the first commission pays 40 coins."""
    fresh()
    itx = call("splits_add", bps=40, account="2002")
    txt = itx.text()
    has(txt, "0.40%", "the share they actually typed")
    has(txt, "4000", "and the bps for the share they probably meant")
    truthy(itx.view() is not None, "it is still allowed — it is a warning, not a veto")


# ═══════════════════════════════════════════════════════════════════════════
# F — retiring
# ═══════════════════════════════════════════════════════════════════════════

def t_remove_retires_and_never_deletes():
    _, sr = fresh()
    rid = sr.add_rule(SRC, "account", "2002", 4000)["rule_id"]
    v0 = version()
    itx = call("splits_remove", rule_id=rid)
    eq(int(rules_rows()[0]["active"]), 1, "the preview retired the rule")
    out = press(itx.view())
    rows = rules_rows()
    eq(len(rows), 1, "the row must SURVIVE — it explains coins it already moved")
    eq(int(rows[0]["active"]), 0, "…deactivated, not deleted")
    truthy(version() > v0, "retiring bumps the ruleset version")
    has(out.text(), "retired", "and the reply says so")


def t_removing_a_rule_that_is_not_there():
    fresh()
    itx = call("splits_remove", rule_id=999)
    eq(itx.view(), None, "no confirm button over a rule that does not exist")
    has(itx.text(), "not an active rule", "say which it is")


def t_losing_the_race_to_retire_is_reported_honestly():
    _, sr = fresh()
    rid = sr.add_rule(SRC, "account", "2002", 4000)["rule_id"]
    itx = call("splits_remove", rule_id=rid)
    v = itx.view()
    sr.deactivate_rule(rid, by="someone else")     # another operator, in between
    out = press(v)
    has(out.text(), "already retired", "the rowcount is the answer, not a guess")
    eq(len([r for r in rules_rows() if int(r["active"])]), 0, "still retired, once")


# ═══════════════════════════════════════════════════════════════════════════
# G — re-ordering, and what it does to coins
# ═══════════════════════════════════════════════════════════════════════════

def t_reorder_demands_the_complete_order():
    _, sr = fresh()
    a = sr.add_rule(SRC, "account", "2002", 4000)["rule_id"]
    sr.add_rule(SRC, "account", "1001", 4000)
    itx = call("splits_reorder", order=str(a))
    eq(itx.view(), None, "a partial order must not be offered a button")
    has(itx.text(), "COMPLETE order", "and must say what is wrong with it")
    itx = call("splits_reorder", order="not-a-number")
    has(itx.text(), "list of rule ids", "garbage input is named, not raised")


def t_reorder_rewrites_every_seq_and_bumps_the_version():
    _, sr = fresh()
    a = sr.add_rule(SRC, "account", "2002", 4000)["rule_id"]
    b = sr.add_rule(SRC, "account", "1001", 3000)["rule_id"]
    c = sr.add_rule(SRC, "account", "3003", 1000)["rule_id"]
    v0 = version()
    itx = call("splits_reorder", order=f"{c} {a} {b}")
    eq([int(r["id"]) for r in rules_rows()], [a, b, c], "the preview re-ordered")
    press(itx.view())
    eq([int(r["id"]) for r in rules_rows()], [c, a, b], "the rules are re-ordered")
    eq([int(r["seq"]) for r in rules_rows()], [1, 2, 3], "seq is 1..n, no ties")
    truthy(version() > v0, "a re-order bumps the ruleset version")


def t_the_engine_refuses_a_reorder_it_was_not_given_in_full():
    """The surface refuses first, but the primitive must refuse too — it is the
    one an AI tool or a future caller reaches directly."""
    _, sr = fresh()
    a = sr.add_rule(SRC, "account", "2002", 4000)["rule_id"]
    b = sr.add_rule(SRC, "account", "1001", 4000)["rule_id"]
    for bad, why in (([a], "a missing rule"), ([a, b, 77], "an unknown rule"),
                     ([a, a, b], "a duplicate"), ([], "an empty order")):
        try:
            sr.reorder_rules(SRC, bad)
            raise AssertionError(f"{why} was accepted: {bad}")
        except sr.SplitError:
            pass
    eq([int(r["seq"]) for r in rules_rows()], [0, 0], "and nothing was re-ordered")


def t_order_decides_who_absorbs_the_shortfall():
    """The reason re-ordering is a feature and not decoration: under `prorate`
    the LAST rule takes the odd coin. Run the same split both ways round and
    watch the coin move."""
    _, sr = fresh()
    sr.set_short_policy(SRC, "prorate")
    a = sr.add_rule(SRC, "account", "2002", 5000)["rule_id"]
    b = sr.add_rule(SRC, "account", "1001", 5000)["rule_id"]
    give(SRC, 101)                       # 101 coins for a 10,000-coin event
    before = world()
    res = sr.run_split("probe", 1, SRC, 10000)
    eq(res["outcome"], "applied", res.get("reason"))
    eq(world(), before, "CONSERVATION")
    eq(coins("2002") + coins("1001"), 101, "every coin present was distributed")
    eq(coins("1001"), 51, "the LAST rule absorbs the odd coin")
    eq(coins("2002"), 50, "the first takes the floor")

    itx = call("splits_reorder", order=f"{b} {a}")
    press(itx.view())
    give(SRC, 101)
    give("2002", 0)
    give("1001", 0)
    before = world()
    res = sr.run_split("probe", 2, SRC, 10000)
    eq(res["outcome"], "applied", res.get("reason"))
    eq(world(), before, "CONSERVATION")
    eq(coins("2002"), 51, "after the re-order the odd coin goes the other way")
    eq(coins("1001"), 50, "…and the other rule takes the floor")


# ═══════════════════════════════════════════════════════════════════════════
# H — the short-source policy
# ═══════════════════════════════════════════════════════════════════════════

def t_policy_change_is_previewed_then_written():
    fresh()
    itx = call("splits_policy", policy="prorate")
    txt = itx.text()
    has(txt, "strict", "the policy it is now")
    has(txt, "prorate", "and the one proposed")
    eq(q("SELECT * FROM split_rulesets"), [], "the preview wrote nothing")
    press(itx.view())
    row = q("SELECT * FROM split_rulesets WHERE source_account=?", (SRC,))[0]
    eq(row["short_policy"], "prorate", "confirm writes the policy")


def t_setting_the_policy_it_already_has_is_a_no_op():
    _, sr = fresh()
    sr.set_short_policy(SRC, "defer")
    v0 = version()
    itx = call("splits_policy", policy="defer")
    eq(itx.view(), None, "no confirm button for a change that changes nothing")
    has(itx.text(), "already", "and it says so")
    eq(version(), v0, "a no-op must not bump the ruleset version")


def t_each_policy_is_explained_in_the_view_that_sets_it():
    fresh()
    has(call("splits_policy", policy="prorate").text(), "shortfall_coins",
        "prorate must name what it records")
    has(call("splits_policy", policy="defer").text(), "never completes",
        "defer must name its failure mode")
    _, sr = fresh()
    sr.set_short_policy(SRC, "prorate")
    has(call("splits_policy", policy="strict").text(), "Nobody is paid",
        "strict must say nobody is paid")


# ═══════════════════════════════════════════════════════════════════════════
# I — the parked-run surface (F3's other half: `stuck_runs` reached a log only)
# ═══════════════════════════════════════════════════════════════════════════

def _park_a_run():
    _, sr = fresh()
    sr.set_short_policy(SRC, "defer")
    sr.add_rule(SRC, "account", "2002", 5000)
    give(SRC, 0)
    res = sr.run_split("land_commission", 77, SRC, 4000)
    eq(res["state"], "pending_funds", "the run must park")
    return sr, res


def t_a_parked_run_is_visible_without_reading_the_logs():
    sr, res = _park_a_run()
    itx = call("splits_runs", older_than_minutes=0)
    txt = itx.text()
    has(txt, res["run_id"], "the parked run must be named")
    has(txt, "4,000", "with its figure")
    has(txt, "Land commission holding", "and the account in words")


def t_one_run_shows_its_pinned_legs_in_real_names():
    sr, res = _park_a_run()
    itx = call("splits_run", run_id=res["run_id"])
    txt = itx.text()
    has(txt, "2,000", "the pinned leg amount")
    has(txt, "Osentar", "the beneficiary by name, not by user id")
    has(txt, "pins its plan", "and the surface states the pin semantics")


def t_no_parked_runs_says_so_plainly():
    fresh()
    itx = call("splits_runs")
    has(itx.text(), "No parked or stuck split runs", "an empty state is EMPTY")
    itx = call("splits_run", run_id="split:nope")
    has(itx.text(), "REFUSAL, not a pending run",
        "the absence of a row is the one thing an operator misreads here")


# ═══════════════════════════════════════════════════════════════════════════
# J — real names
# ═══════════════════════════════════════════════════════════════════════════

def t_real_names_everywhere_a_human_looks():
    _, sr = fresh()
    sr.add_rule(SRC, "role", "555", 3000, label="market owners")
    sr.add_rule(SRC, "account", "2002", 2000)
    sr.add_rule(SRC, "account", "treasury:vtech", 1000)
    itx = call("splits_list")
    txt = itx.text()
    has(txt, "@Market Owners", "a role renders as its role name")
    has(txt, "Osentar", "a user id renders as their display name")
    has(txt, "V Tech house account", "a treasury account renders as what it is for")
    has(txt, "Land commission holding", "including the source account")
    # …and the raw ids are still there, because he has to be able to type them.
    for raw in ("555", "2002", "treasury:vtech", "treasury:estates"):
        has(txt, raw, f"the raw id {raw} must stay beside the name")


def t_an_unknown_id_degrades_to_the_id_never_to_nothing():
    eq(S._display("role", "999", guild=GUILD), "role 999", "unknown role")
    eq(S._display("account", "treasury:hive_float", guild=GUILD),
       "Hive Float treasury", "an unlabelled treasury is still readable")
    truthy("4242" in S._display("account", "4242", guild=GUILD), "unknown user")
    truthy("4242" in S._display("account", "4242", guild=None), "no guild at all")


def t_an_empty_ruleset_says_what_deploying_it_does():
    fresh()
    itx = call("splits_list")
    txt = itx.text()
    has(txt, "No rules", "the empty state")
    has(txt, "nothing is routed", "…and what that MEANS")
    has(txt, "as it did before the split engine existed",
        "the claim John is relying on, on the screen he checks it from")


# ═══════════════════════════════════════════════════════════════════════════
# K — wiring, both ways
# ═══════════════════════════════════════════════════════════════════════════

def t_every_name_the_cog_calls_exists():
    import re
    import split_rules as sr
    src = (ROOT / "cogs" / "splits.py").read_text()
    # Anchored on the call/reference syntax so a mention of the FILE in a
    # docstring is not read as a name the cog uses.
    called = sorted(set(re.findall(
        r"\bsplit_rules\.([A-Za-z_][A-Za-z_0-9]*)\s*[(,)]", src)))
    truthy(len(called) >= 8, f"the cog barely touches the engine: {called}")
    for name in called:
        truthy(hasattr(sr, name), f"cogs/splits.py uses split_rules.{name}, "
                                  f"which does not exist")


def t_every_command_the_cog_defines_is_reachable():
    names = {c.name for c in S.SplitsCog.splits.commands}
    eq(names, {"list", "add", "remove", "reorder", "policy", "runs", "run", "source"},
       "the command set")
    for c in S.SplitsCog.splits.commands:
        truthy(callable(c.callback), f"/splits {c.name} has no callback")
    # every callback defined on the class is registered in the group, and nothing
    # is registered that is not defined — the orphan/phantom check this project
    # has been bitten by.
    defined = {n for n in dir(S.SplitsCog) if n.startswith("splits_")}
    eq(len(defined), len(names), f"defined {sorted(defined)} vs registered {sorted(names)}")


def t_a_refused_run_is_named_in_slash_splits_runs():
    """N1's other half. A run that refused moved no coins, so the commission is
    still sitting in the source — and before this it was in neither of the two
    lists, so `/splits runs` reported "nothing to see" over an income event
    nobody had been paid for. Neither an answer nor a job in anyone's queue."""
    _, sr = fresh()
    give(SRC, 10)                       # the source is short
    sr.add_rule(SRC, "account", "2002", 10000)
    res = sr.run_split("land_commission", 4242, SRC, 2000)
    eq(res["state"], "refused", f"the run should have refused: {res}")
    eq(coins("2002"), 0, "and nobody was paid")
    itx = call("splits_runs", older_than_minutes=0)
    txt = itx.text()
    has(txt, res["run_id"], "the refused run must be named")
    has(txt, "4242", "with the income event it belongs to")
    has(txt, "No coins moved", "and the operator must be told nothing moved")


def t_a_refused_run_that_a_later_attempt_routed_is_not_reported():
    """The control. Once the event IS routed, the old refused row is history,
    not an open job — reporting it would train the operator to ignore the list."""
    _, sr = fresh()
    give(SRC, 10)
    sr.add_rule(SRC, "account", "2002", 10000)
    first = sr.run_split("land_commission", 4243, SRC, 2000)
    eq(first["state"], "refused", "first attempt refuses")
    give(SRC, 5000)
    second = sr.run_split("land_commission", 4243, SRC, 2000)
    eq(second["outcome"], "applied", f"the re-offer must route it: {second}")
    eq(coins("2002"), 2000, "and pay exactly once")
    eq([r["run_id"] for r in sr.unrouted_runs()], [],
       "a routed event must not still be listed as unrouted")


def t_the_cog_is_registered_in_the_bot():
    main = (ROOT / "Restocker_main.py").read_text()
    ext_block = main.split("for _ext in (", 1)[1].split("):", 1)[0]
    truthy('"cogs.splits"' in ext_block,
           "cogs/splits.py is never loaded — a surface that is not registered is "
           "the same defect as no surface at all")


def t_the_engine_functions_this_round_added_have_a_caller():
    """F3 in one line: `reorder_rules` is new, and a new primitive with no
    surface is the fifth instance of this project's oldest defect."""
    import re
    src = (ROOT / "cogs" / "splits.py").read_text()
    # Both call shapes count: `split_rules.f(...)` and the `to_thread(split_rules.f, ...)`
    # this cog uses to keep money transactions off the event loop.
    called = set(re.findall(r"\bsplit_rules\.([A-Za-z_][A-Za-z_0-9]*)\s*[(,)]", src))
    for fn in ("add_rule", "deactivate_rule", "reorder_rules", "set_short_policy",
               "list_rules", "stuck_runs", "parked_runs", "unrouted_runs",
               "get_run"):
        truthy(fn in called, f"{fn} still has no product caller (found: "
                             f"{sorted(called)})")


# ═══════════════════════════════════════════════════════════════════════════

TESTS = [
    ("staff gate: a non-manager writes nothing", t_non_staff_cannot_write_a_rule),
    ("every reply is ephemeral", t_every_reply_is_ephemeral),
    ("the preview writes nothing", t_rendering_the_preview_writes_nothing),
    ("Confirm writes exactly one rule", t_confirm_writes_exactly_one_rule),
    ("Cancel writes nothing and says so", t_cancel_writes_nothing_and_says_so),
    ("a spent preview cannot be pressed twice", t_a_spent_view_cannot_be_pressed_twice),
    ("a real double-click writes ONE rule", t_a_real_double_click_writes_ONE_rule),
    ("cancel racing confirm cannot lie", t_cancel_racing_confirm_cannot_deny_a_write_that_is_landing),
    ("the preview belongs to who opened it", t_the_preview_belongs_to_who_opened_it),
    ("the 100% cap at the surface, with figures",
     t_the_cap_is_enforced_at_the_surface_with_the_figures),
    ("exactly 100% is allowed", t_exactly_100_percent_is_allowed),
    ("the transaction is still the authority (b3)",
     t_the_surface_check_does_not_replace_the_transaction_check),
    ("bps out of range never reaches a button", t_bps_out_of_range_is_refused_before_anything),
    ("exactly one beneficiary", t_exactly_one_beneficiary),
    ("current vs proposed, side by side", t_the_preview_shows_current_and_proposed_side_by_side),
    ("every edit states the in-flight rule", t_every_edit_states_what_happens_to_money_in_flight),
    ("retiring warns about a parked run", t_retiring_a_rule_warns_that_a_parked_run_still_pays),
    ("no float ever reaches a rule row", t_no_float_ever_reaches_a_rule_row),
    ("percentages render from the integer", t_percentages_are_rendered_from_the_integer),
    ("a sub-1% share is flagged as a typo", t_a_sub_one_percent_share_is_flagged_as_the_typo_it_probably_is),
    ("remove retires, never deletes", t_remove_retires_and_never_deletes),
    ("removing a rule that is not there", t_removing_a_rule_that_is_not_there),
    ("losing the retire race is honest", t_losing_the_race_to_retire_is_reported_honestly),
    ("reorder demands the complete order", t_reorder_demands_the_complete_order),
    ("reorder rewrites every seq", t_reorder_rewrites_every_seq_and_bumps_the_version),
    ("the primitive refuses a partial order", t_the_engine_refuses_a_reorder_it_was_not_given_in_full),
    ("order decides who absorbs the shortfall", t_order_decides_who_absorbs_the_shortfall),
    ("policy: previewed, then written", t_policy_change_is_previewed_then_written),
    ("policy: setting what it already is", t_setting_the_policy_it_already_has_is_a_no_op),
    ("policy: each one explained where it is set", t_each_policy_is_explained_in_the_view_that_sets_it),
    ("a parked run is visible without logs", t_a_parked_run_is_visible_without_reading_the_logs),
    ("one run shows its pinned legs by name", t_one_run_shows_its_pinned_legs_in_real_names),
    ("no parked runs says so plainly", t_no_parked_runs_says_so_plainly),
    ("real names everywhere a human looks", t_real_names_everywhere_a_human_looks),
    ("an unknown id degrades to the id", t_an_unknown_id_degrades_to_the_id_never_to_nothing),
    ("an empty ruleset says what deploying does", t_an_empty_ruleset_says_what_deploying_it_does),
    ("wiring: every name the cog calls exists", t_every_name_the_cog_calls_exists),
    ("wiring: every command is reachable", t_every_command_the_cog_defines_is_reachable),
    ("a refused run is named in /splits runs", t_a_refused_run_is_named_in_slash_splits_runs),
    ("a routed event is not reported unrouted",
     t_a_refused_run_that_a_later_attempt_routed_is_not_reported),
    ("wiring: the cog is registered", t_the_cog_is_registered_in_the_bot),
    ("wiring: the new primitive has a caller", t_the_engine_functions_this_round_added_have_a_caller),
]


def main():
    print("cogs/splits.py — the configuration surface, by execution")
    for name, fn in TESTS:
        check(name, fn)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
