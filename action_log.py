"""action_log.py — every system action carries its own undo.

WHY THIS EXISTS
---------------
The coin ledger answers "what happened". At 02:00 the question is "make it not
have happened", and the only honest answer used to be a manual `UPDATE`. So:
when an action runs, it writes down — *next to the audit row* — the exact list
of operations that reverse it. The log embed then grows one Rollback button.

WHAT THIS IS NOT
----------------
It is not an undo of the *world*; it is a compensating entry. Nothing is
deleted. A rollback of a 12,000-coin payout is a new, labelled −12,000 ledger
row that says which action it reverses.

THE RULES THIS FILE PAYS
------------------------
* **Claim-first (rule 1).** The reference implementation this was studied from
  does `if log["rolled_back"]: return` … apply ops … `mark_rolled_back()`, with a
  Discord defer inside the window. Two staff on the same message both pass the
  check and the refund lands twice. Here the claim is one atomic UPDATE with the
  state in the WHERE clause; if it did not change a row, you did not win it, and
  you do nothing. There is no read-then-act anywhere in this file.
* **Per-row markers (rule 2).** Every reverse op has its own row in
  `sys_action_ops` and its own marker, written immediately after that op — never
  after the loop. A crash at op 4 of 9 resumes at op 4, not at op 0.
* **Caller-minted idempotency keys (rule 3).** EVERY op's key is
  `rb:<action_id>#<index>`, derived from the audit row, stable across re-reads
  and across processes. The key is recorded in `sys_action_op_effects` **inside
  the same transaction as the effect it protects** — one PRIMARY KEY, one
  commit, so "the key is absent" provably means "the effect did not land".
  Belt as well as braces: the claim stops the second clicker, the per-op claim
  stops the second run, the key stops the second *process*.
* **A stale claim is reclaimable (rule 1, second half).** A process death
  mid-apply used to leave the row `claimed` forever, with no route back but a
  hand-written UPDATE. `claim()` now takes over a claim older than
  `STALE_CLAIM_SECONDS`, re-stamping `claimed_at` as the claim token so two
  staff still cannot both hold it. Modelled on `ledger_v2._claim_idempotency`,
  and safe for exactly the reason stated there: it is only sound because the
  completion record lives inside the money transaction.
* **Integer coins (rule 6).** Every amount in an op is an int. Points are not
  money and live in their own op type.
* **It says when it cannot (the brief).** An op that cannot be safely automated —
  or one whose clawback came up short because the user already spent the coins —
  does not fail silently and does not half-succeed in the dark. It opens a staff
  task with the figures in it.

OP VOCAB
--------
    {"t":"coins",    "user_id":"…", "amount": -12000, "principal": false, "why":"…"}
    {"t":"platform", "amount": -360, "month":"2026-08", "market_id":"greyhames"}
    {"t":"treasury", "market_id":"greyhames", "delta": 12000}
    {"t":"stock",    "market_id":"…", "item":"…", "delta": 64}
    {"t":"loyalty",  "user_id":"…", "market_id": null, "points": -12.5}
    {"t":"setfields","table":"items", "where":{"name":"…"}, "fields":{"coin":9}}
    {"t":"insrow",   "table":"hive_claims", "row":{…}}      # reverse of a delete
    {"t":"delrow",   "table":"hive_claims", "where":{…}}    # reverse of an insert
    {"t":"manual",   "what":"…", "hint":"…", "coins": 40000} # always a staff task
                     # `coins` is OPTIONAL and is the exposure the task covers —
                     # the coins a HUMAN must move for this rollback to be
                     # complete. It is not moved by the button. See manual_total().
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Optional

#: Op types whose `amount` is counted by money_total(), i.e. the coins PRESSING
#: THE BUTTON moves by itself. `treasury` is deliberately NOT here: a dividend's
#: 12,000 coins leave the treasury and arrive in holders' balances, and counting
#: both legs would report 24,000 for one 12,000-coin action. The treasury still
#: appears with before/after figures in preview()["movements"].
#:
#: THIS SET IS NOT "the money this action was about", and the difference is not
#: academic. Under escrow a land sale's reverse ops are a `manual` staff task
#: (the real 40,000) plus a `platform -2,000` reporting mirror, so a set-based
#: headline reported **2,000 for a 40,000-coin sale** — wrong by a factor of 20,
#: on an irreversible confirm dialog. The fix is not to widen this set (a
#: `manual` op genuinely moves nothing when the button is pressed, and counting
#: it here would make "Coins this will move" a different lie). It is
#: `manual_total()` beside it, and BOTH figures on screen. See build_confirm_embed.
MONEY_OPS = {"coins", "platform"}

#: How long a `claimed` row (or a `running` op) may sit before the next click may
#: take it over. Matched to `ledger_v2.IDEMPOTENCY_STALE_SECONDS` (900) on
#: purpose: the same number in both places means an operator learns one rule, and
#: a rollback that is waiting on a ledger retry cannot be reclaimed underneath it.
STALE_CLAIM_SECONDS = 900

#: Op types whose reversal is NOT idempotent on its own and therefore MUST be
#: applied inside a transaction that also records `idem_key(action_id, index)`.
#: `manual` moves nothing. Everything else is in here, deliberately including the
#: table ops: `INSERT OR IGNORE` is only idempotent when a UNIQUE constraint
#: happens to cover the row, and "happens to" is not a guarantee you retry money
#: against. If you add an op type, add it here or `_apply_op` refuses to run it.
KEYED_OPS = {"coins", "platform", "treasury", "stock", "loyalty",
             "setfields", "insrow", "delrow"}

# Only these tables may be touched by a stored `setfields`/`insrow`/`delrow` op,
# and only through their listed key columns. An op list is data; data that names
# its own table is an injection surface unless the surface is enumerated.
ROLLBACKABLE_TABLES: dict[str, tuple[str, ...]] = {
    "items":                ("name",),
    "markets":              ("market_id",),
    "orders":               ("id",),
    "land_listings":        ("id",),
    "market_stock":         ("market_id", "item"),
    "market_item_targets":  ("market_id", "item"),
    "stock_alarms":         ("market_id", "item"),
    "team_settings":        ("manager_id",),
    "team_members":         ("worker_id",),
    "hive_claims":          ("location",),
    "investors":            ("user_id",),
    "bonds":                ("id",),
    "market_shares":        ("market_id",),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sys_actions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action_key     TEXT UNIQUE,                   -- caller-minted; NULL allowed for one-offs
    kind           TEXT NOT NULL,
    summary        TEXT NOT NULL,                 -- real names, no ids
    actor_id       TEXT,
    actor_name     TEXT,
    guild_id       TEXT,
    channel_id     TEXT,
    message_id     TEXT,
    ops_json       TEXT NOT NULL DEFAULT '[]',
    money_coins    INTEGER NOT NULL DEFAULT 0,    -- integer coins the rollback moves
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    -- rollback state machine: open -> claimed -> done | partial | failed
    state          TEXT NOT NULL DEFAULT 'open',
    claimed_by     TEXT,
    claimed_name   TEXT,
    claimed_at     TEXT,
    finished_at    TEXT,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_sys_actions_state ON sys_actions(state, id);
CREATE INDEX IF NOT EXISTS idx_sys_actions_msg   ON sys_actions(message_id);

-- One row per reverse op. THIS is the progress marker: written per row, never
-- after the loop, so an interrupted rollback resumes where it stopped.
CREATE TABLE IF NOT EXISTS sys_action_ops (
    action_id  INTEGER NOT NULL,
    op_index   INTEGER NOT NULL,
    state      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|manual|failed
    detail     TEXT,
    done_at    TEXT,
    PRIMARY KEY (action_id, op_index)
);

-- The idempotency record for ONE reverse op. `idem_key` is `rb:<action>#<index>`,
-- minted by the caller from the audit row, and this INSERT is one more statement
-- in the same transaction as the effect it protects. That is the whole point:
-- with two transactions there is a window where the money has moved and the key
-- is absent, and a retry then pays again — the bug ledger v2 took six rounds to
-- kill (LEDGER_API_v2.md §6, "in_progress provably means the coins did not move").
-- Because they commit together, a MISSING row here provably means the op did not
-- land, which is what makes stale-claim takeover and Retry safe.
CREATE TABLE IF NOT EXISTS sys_action_op_effects (
    idem_key   TEXT PRIMARY KEY,
    action_id  INTEGER NOT NULL,
    op_index   INTEGER NOT NULL,
    op_type    TEXT NOT NULL,
    applied    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_op_effects_action ON sys_action_op_effects(action_id, op_index);

CREATE TABLE IF NOT EXISTS staff_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key    TEXT UNIQUE,                      -- so a retried rollback never doubles up
    action_id   INTEGER,
    op_index    INTEGER,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',     -- open|done
    opened_by   TEXT,
    opened_at   TEXT NOT NULL DEFAULT (datetime('now')),
    closed_by   TEXT,
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_staff_tasks_status ON staff_tasks(status, id);
"""

#: Indexes on tables this module does NOT own. They go here rather than in
#: `Restocker_db.SCHEMA` because they are *partial* — scoped to the `rb:` prefix
#: this file mints and nothing else does. A blanket `UNIQUE(user_id, reason)` on
#: `coin_ledger` would fail to build on the live database the moment two rows
#: share a free-text reason ("sale", "hive payout"), which they do in their
#: thousands; scoped to `rb:%` there can be no pre-existing duplicate, because
#: the namespace did not exist before this file. They are BELT: the strap is
#: `sys_action_op_effects`, which this module owns outright, so a deployment
#: where these cannot be built yet is degraded, not unsafe.
#:
#: SHOULD THE PARTIAL INDEX BE WIDENED? No — a SECOND partial index is the answer,
#: and the reason is worth writing down because "just make it UNIQUE(user_id,
#: reason)" is the obvious move and it is wrong twice over.
#:
#:   1. It cannot be BUILT. The live `coin_ledger` already holds thousands of
#:      duplicate (user, reason) pairs — "sale", "hive payout", "stock buy
#:      greyhames" — so `CREATE UNIQUE INDEX` fails outright and the guard that
#:      was supposed to protect every money path protects none of them.
#:   2. It would be WRONG even on an empty database. A blanket uniqueness rule
#:      says "this user may never have two coin movements with the same label",
#:      but a player buying 50 shares of greyhames twice in one evening is two
#:      real events that legitimately carry the same free-text reason. The index
#:      would refuse the second one — a correctness guard turning into a
#:      trade-refusing outage.
#:
#: What the `rb:` index actually gets right is that it is scoped to a namespace
#: whose members are idempotent BY CONSTRUCTION: every `rb:` reason names one
#: rollback op that must happen at most once. So the markets engine gets its own
#: such namespace — `mk:` — and the same treatment. A reason enters it only if it
#: is derived from a durable row and identifies work that must happen at most once
#: (`mk:div:<run_id>`, `mk:bond:<id>:coupon:<month>`). Per-attempt reasons like a
#: trade stay OUT of it deliberately: for a trade, a second identical call is a
#: second real trade, and the thing that must not happen twice is the caller's
#: RETRY, which is `bank_api._claim_key`'s job, not this index's.
_GUARD_INDEXES = (
    ("coin_ledger",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_coin_ledger_rb "
     "ON coin_ledger(user_id, reason) WHERE reason LIKE 'rb:%'"),
    ("coin_ledger",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_coin_ledger_mk "
     "ON coin_ledger(user_id, reason) WHERE reason LIKE 'mk:%'"),
    ("platform_balance_log",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_log_rb "
     "ON platform_balance_log(month, market_id, note) WHERE note LIKE 'rb:%'"),
)

_schema_ready = False


def _db():
    import Restocker_db as _d
    return _d


def _core():
    return sys.modules.get("Restocker_main") or sys.modules.get("__main__")


def ensure_schema() -> None:
    """Create this module's tables, then the partial guard indexes.

    The guard indexes sit on tables `Restocker_db` owns, so they are built in
    their own statements after `_SCHEMA`: if `init_db()` has not run yet the
    table is simply absent, and rather than aborting the whole script we leave
    `_schema_ready` False so the next call retries. `sys_action_op_effects` is
    already in place by then, so the idempotency guarantee holds either way —
    this only loses the second, cross-checking constraint.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _db().db() as conn:
        conn.executescript(_SCHEMA)
    ok = True
    for table, sql in _GUARD_INDEXES:
        try:
            with _db().db() as conn:
                conn.execute(sql)
        except sqlite3.Error as e:
            ok = False
            print(f"⚠️ action_log: guard index on {table} not built yet ({e}); "
                  f"sys_action_op_effects is still enforcing idempotency")
    _schema_ready = ok


# ── Writing an audit row ────────────────────────────────────────────────────
def money_total(ops: list[dict]) -> int:
    """Integer coins pressing the button moves AUTOMATICALLY.

    Not "the coins this action is about" — see MONEY_OPS for why the two came
    apart under escrow, and `manual_total()` for the other half. A confirm
    screen that shows only this number is showing only the part the machine
    does; both go on screen together.
    """
    total = 0
    for op in ops or []:
        if op.get("t") in MONEY_OPS:
            total += abs(int(op.get("amount") or 0))
    return total


def manual_total(ops: list[dict]) -> int:
    """Integer coins a HUMAN must move by hand for this rollback to be complete.

    Summed from the optional `"coins"` field on `manual` ops. A `manual` op with
    no `coins` declares no exposure (it is a "tell this person" or "clear this
    config key" task), and contributes 0 — absent means zero, never unknown, so
    a producer that forgets to declare simply under-reports rather than
    rendering a blank where a number belongs.

    Deliberately NOT folded into money_total(): the button does not move these.
    """
    total = 0
    for op in ops or []:
        if op.get("t") == "manual":
            total += abs(int(op.get("coins") or 0))
    return total


def _validate(ops: list[dict]) -> list[dict]:
    """Reject a malformed op list at WRITE time. An op list that cannot be
    applied is worse than no rollback button at all — it promises an undo that
    will fail at 02:00."""
    clean = []
    for i, op in enumerate(ops or []):
        t = str(op.get("t") or "")
        if t == "coins":
            op = dict(op, amount=int(op["amount"]), user_id=str(op["user_id"]),
                      principal=bool(op.get("principal", False)))
        elif t == "platform":
            op = dict(op, amount=int(op["amount"]))
        elif t == "treasury":
            op = dict(op, delta=int(op["delta"]), market_id=str(op["market_id"]))
        elif t == "stock":
            op = dict(op, delta=int(op["delta"]))
        elif t == "loyalty":
            op = dict(op, points=float(op["points"]))
        elif t in ("setfields", "insrow", "delrow"):
            table = str(op.get("table") or "")
            if table not in ROLLBACKABLE_TABLES:
                raise ValueError(f"op {i}: table {table!r} is not rollbackable")
        elif t == "manual":
            # `coins` is optional, but if it is declared it is INTEGER COINS and
            # non-negative — a magnitude, not a signed leg. It goes on a confirm
            # dialog, so a float or a negative here would print a figure nobody
            # can act on.
            if op.get("coins") is not None:
                c = int(op["coins"])
                if c < 0:
                    raise ValueError(f"op {i}: manual 'coins' is an exposure "
                                     f"magnitude and cannot be negative ({c})")
                op = dict(op, coins=c)
        else:
            raise ValueError(f"op {i}: unknown op type {t!r}")
        # The declaration is CHECKED, not trusted — the same reason ledger v2
        # verifies `IN_BAND_ENDPOINTS` at completion time. An op type added above
        # and not added to KEYED_OPS would be applied with no idempotency record,
        # and Retry would re-apply it. Fail here, at WRITE time, not at 02:00.
        if t != "manual" and t not in KEYED_OPS:
            raise ValueError(
                f"op {i}: type {t!r} changes state but is not in KEYED_OPS — add it "
                f"there and give _apply_op a branch that claims its key inside the "
                f"same transaction as its effect")
        clean.append(op)
    return clean


def record(kind: str, summary: str, ops: list[dict], *,
           actor_id=None, actor_name: str = "", guild_id=None,
           action_key: Optional[str] = None, note: str = "") -> int:
    """Write one audit row + its reverse ops. Returns the action id.

    `action_key` is the caller-minted idempotency key for the ACTION (not the
    rollback): mint it from the domain event, e.g. `hivepay:2026-08:greyhames`.
    Re-recording the same key returns the existing row instead of a second audit
    entry with a second Rollback button pointing at the same money.
    """
    ensure_schema()
    ops = _validate(ops)
    payload = json.dumps(ops, separators=(",", ":"))
    coins = money_total(ops)
    if action_key:
        existing = by_key(action_key)
        if existing:
            return int(existing["id"])
    try:
        with _db().db() as conn:
            cur = conn.execute(
                "INSERT INTO sys_actions (action_key, kind, summary, actor_id, actor_name, "
                "guild_id, ops_json, money_coins, note) VALUES (?,?,?,?,?,?,?,?,?)",
                (action_key, str(kind), str(summary), str(actor_id) if actor_id else None,
                 str(actor_name or ""), str(guild_id) if guild_id else None,
                 payload, coins, str(note or "")))
            aid = int(cur.lastrowid)
            conn.executemany(
                "INSERT OR IGNORE INTO sys_action_ops (action_id, op_index) VALUES (?,?)",
                [(aid, i) for i in range(len(ops))])
        return aid
    except sqlite3.IntegrityError:
        # Lost the race on action_key — adopt the winner's row.
        existing = by_key(action_key) if action_key else None
        if existing:
            return int(existing["id"])
        raise


def attach_message(action_id: int, channel_id, message_id) -> None:
    """Remember where the log embed lives so the button can disable *in place*."""
    ensure_schema()
    with _db().db() as conn:
        conn.execute("UPDATE sys_actions SET channel_id=?, message_id=? WHERE id=?",
                     (str(channel_id), str(message_id), int(action_id)))


def get(action_id: int) -> Optional[dict]:
    ensure_schema()
    with _db().db() as conn:
        r = conn.execute("SELECT * FROM sys_actions WHERE id=?", (int(action_id),)).fetchone()
    return dict(r) if r else None


def by_key(action_key: str) -> Optional[dict]:
    ensure_schema()
    with _db().db() as conn:
        r = conn.execute("SELECT * FROM sys_actions WHERE action_key=?",
                         (str(action_key),)).fetchone()
    return dict(r) if r else None


def ops_of(action_id: int) -> list[dict]:
    row = get(action_id)
    if not row:
        return []
    try:
        return json.loads(row["ops_json"] or "[]")
    except Exception:
        return []


def op_states(action_id: int) -> dict[int, dict]:
    ensure_schema()
    with _db().db() as conn:
        rows = conn.execute(
            "SELECT op_index, state, detail FROM sys_action_ops WHERE action_id=? "
            "ORDER BY op_index", (int(action_id),)).fetchall()
    return {int(r["op_index"]): dict(r) for r in rows}


def reversible(action_id: int) -> bool:
    """False when there is nothing to automate — the UI then offers a staff task
    instead of a Rollback button that would do nothing."""
    ops = ops_of(action_id)
    return bool(ops) and any(op.get("t") != "manual" for op in ops)


#: The three things pressing the button can mean. The UI picks its LABEL from
#: this, so a button never says "Rollback" over an action whose coins it will
#: not move. See cogs/rollback.py:undo_label.
UNDO_NONE = "none"            # nothing automatic at all — offer a staff task
UNDO_BY_HAND = "by_hand"      # it reverses RECORDS; the coins are a named task
UNDO_COINS = "coins"          # it reverses records AND moves the coins itself


def undo_kind(action_id: int) -> str:
    """What pressing the button on this row ACTUALLY does — not what it is called.

    `by_hand` is the case this function exists for. Under escrow a land sale's
    reverse ops are a 40,000-coin `manual` task plus a 2,000-coin `platform`
    reporting mirror plus a `setfields`. `reversible()` is True (there are
    automatic ops) and `money_total()` is non-zero (the mirror), so both of the
    old questions answered "yes, this is a rollback that moves money" — and the
    button said ↩ Rollback while the buyer's 40,000 stayed exactly where it was.

    The discriminator is a DECLARED manual exposure, not the absence of money
    ops: an action whose coins a human must move is `by_hand` even when the
    button also moves something, because the label has to be true about the
    biggest number on the screen.
    """
    ops = ops_of(action_id)
    if not (bool(ops) and any(op.get("t") != "manual" for op in ops)):
        return UNDO_NONE
    return UNDO_BY_HAND if manual_total(ops) > 0 else UNDO_COINS


# ── Claim-first ─────────────────────────────────────────────────────────────
def applied_effects(action_id: int) -> set:
    """Op indexes whose effect PROVABLY committed, read from the idempotency rows.

    This is the only authority on "did that op land". `sys_action_ops.state` is a
    progress marker written in its own transaction after the fact, so `running`
    means "we do not know whether the marker was written"; `sys_action_op_effects`
    is written inside the effect's transaction, so its presence or absence is the
    truth. Same distinction ledger v2 draws between a claim and a completion.
    """
    ensure_schema()
    with _db().db() as conn:
        rows = conn.execute(
            "SELECT op_index FROM sys_action_op_effects WHERE action_id=?",
            (int(action_id),)).fetchall()
    return {int(r["op_index"]) for r in rows}


def is_stale_claim(row: Optional[dict]) -> bool:
    """True when this row is `claimed` by an attempt that is no longer running.

    "No longer running" is inferred from the clock, not from a heartbeat, so it
    is a threshold and not a fact — which is exactly why nothing downstream of it
    is allowed to re-apply anything without checking `applied_effects()` first.
    """
    if not row or (row.get("state") or "") != "claimed":
        return False
    ensure_schema()
    with _db().db() as conn:
        r = conn.execute(
            "SELECT 1 FROM sys_actions WHERE id=? AND state='claimed' AND "
            "(claimed_at IS NULL OR claimed_at <= datetime('now', ?))",
            (int(row["id"]), f"-{int(STALE_CLAIM_SECONDS)} seconds")).fetchone()
    return r is not None


def _recover_ops(conn, action_id: int) -> tuple[int, int]:
    """Resolve every ambiguous op marker against the idempotency rows.

    Called by whoever wins a takeover or a reopen, inside their transaction.

    * an op marked `running`/`failed` WITH an effect row -> `done`. Its money
      moved; only the marker write was lost. Re-running it would be the
      double-refund this whole file exists to prevent, and `reopen()` used to do
      exactly that to `running` ops (product review §4).
    * an op marked `running`/`failed` WITHOUT an effect row -> `pending`. It
      provably moved nothing, so it is safe — and necessary — to retry. Leaving
      it `running` forever is how an action ends up half-reversed with no route
      on.

    `done` and `manual` markers are never touched.

    Returns (promoted_to_done, reset_to_pending) for the operator-facing note.
    """
    landed = [int(r["op_index"]) for r in conn.execute(
        "SELECT op_index FROM sys_action_op_effects WHERE action_id=?",
        (int(action_id),)).fetchall()]
    promoted = 0
    if landed:
        marks = ",".join("?" for _ in landed)
        promoted = conn.execute(
            f"UPDATE sys_action_ops SET state='done', "
            f"detail='effect committed; the marker was lost to a crash', "
            f"done_at=datetime('now') "
            f"WHERE action_id=? AND state IN ('running','failed') AND op_index IN ({marks})",
            [int(action_id), *landed]).rowcount or 0
    reset = conn.execute(
        "UPDATE sys_action_ops SET state='pending', detail=NULL "
        "WHERE action_id=? AND state IN ('running','failed')",
        (int(action_id),)).rowcount or 0
    return int(promoted), int(reset)


def claim(action_id: int, staff_id, staff_name: str = "") -> tuple[bool, Optional[dict]]:
    """Atomically take ownership of this rollback.

    ONE statement. The current state is in the WHERE clause, so the database
    decides the winner; `rowcount` reports it. Everything after this point is
    only reached by the winner. This is the whole fix for the double-refund the
    reference implementation ships.

    THE STALE TAKEOVER (product review §3)
    --------------------------------------
    `apply_rollback` has exactly one caller and it is reachable only through a
    won claim. A process death mid-apply — deploy, OOM, host restart — therefore
    left the row `claimed` with no route back: the Rollback button lost the claim
    forever ("**Staff A** is rolling this back right now"), `reopen()` refused
    anything that was not `failed`/`partial`, and the only exit was the 02:00
    `UPDATE` this file exists to abolish. Three of nine refunds applied, embed
    still reading in-flight.

    So the WHERE clause also matches a claim older than `STALE_CLAIM_SECONDS`.
    This is `ledger_v2._claim_idempotency`'s takeover clause, not a second
    answer to the same problem, and it inherits that function's two properties:

    * **`claimed_at` is the claim token.** Taking over re-stamps it in the same
      statement, so a second staff member clicking in the same second loses the
      UPDATE. Two staff can never both be compensating this action.
    * **It is only sound because the completion record is inside the money
      transaction.** The winner runs `_recover_ops`, which asks
      `sys_action_op_effects` — not the progress marker — what actually landed.
      An op whose effect committed is promoted to `done` and never re-applied;
      an op with no effect row provably moved nothing.

    A stalled original that wakes up after a takeover moves nothing either: its
    `_apply_op` re-claims the same `rb:<action>#<index>` key, loses the PRIMARY
    KEY, and returns "already applied" without touching a balance.

    Returns (won, row_now). `won=False` + row tells the caller who has it.
    """
    ensure_schema()
    with _db().db() as conn:
        cur = conn.execute(
            "UPDATE sys_actions SET state='claimed', claimed_by=?, claimed_name=?, "
            "claimed_at=datetime('now') WHERE id=? AND ("
            "  state='open'"
            "  OR (state='claimed' AND (claimed_at IS NULL "
            "      OR claimed_at <= datetime('now', ?)))"
            ")",
            (str(staff_id), str(staff_name or ""), int(action_id),
             f"-{int(STALE_CLAIM_SECONDS)} seconds"))
        won = cur.rowcount == 1
        if won:
            _recover_ops(conn, int(action_id))
        row = conn.execute("SELECT * FROM sys_actions WHERE id=?", (int(action_id),)).fetchone()
    return won, (dict(row) if row else None)


def release(action_id: int) -> None:
    """Hand a claimed-but-unstarted rollback back (the operator cancelled the
    confirm). Only ever moves claimed -> open, and only if no op has run."""
    ensure_schema()
    with _db().db() as conn:
        started = conn.execute(
            "SELECT 1 FROM sys_action_ops WHERE action_id=? AND state<>'pending' LIMIT 1",
            (int(action_id),)).fetchone()
        if started:
            return
        conn.execute("UPDATE sys_actions SET state='open', claimed_by=NULL, "
                     "claimed_name=NULL, claimed_at=NULL WHERE id=? AND state='claimed'",
                     (int(action_id),))


def reopen(action_id: int, staff_id) -> tuple[bool, Optional[dict]]:
    """Put a stuck rollback back in play, claim-first, WITHOUT replaying work.

    A rollback that dies part-way lands in `failed`/`partial`, and the button on
    the log message is already disabled — so without this the row is stuck and the
    only way forward is a manual UPDATE at 02:00, which is the exact thing this
    file exists to remove.

    Three things make this safe to press:
      * the transition is one atomic UPDATE with the state in the WHERE clause, so
        two staff pressing Retry cannot both reopen it;
      * a `claimed` row is only reopenable once the claim has gone stale
        (`STALE_CLAIM_SECONDS`), so Retry cannot yank an action out from under a
        colleague who is mid-rollback right now (product review §3);
      * `_recover_ops` decides what goes back to `pending` by reading
        `sys_action_op_effects`, not the progress marker. An op marked `done` or
        `manual` is NEVER reset, and neither is a `running` op whose effect
        provably committed — it is promoted to `done` instead.

    THE BUG THIS PARAGRAPH USED TO DEFEND (product review §4)
    ---------------------------------------------------------
    This docstring used to say `running` ops go back to `pending` and "the
    idempotency key would catch it anyway". That was true of `coins` and of
    nothing else: `stock` was a bare `UPDATE market_stock SET stock = stock + ?`,
    `loyalty` a bare `+= points`, `platform` a read-then-write guarded by a
    helper inside `except: pass`. A `running` op is precisely one whose effect
    may already have landed, so Retry corrected the same stock twice — measured,
    100 -> 90 -> 80 for one action. Every op type now carries a key
    (`KEYED_OPS`), recorded inside the effect's own transaction, and `running` is
    resolved against that record rather than guessed at.
    """
    ensure_schema()
    with _db().db() as conn:
        cur = conn.execute(
            "UPDATE sys_actions SET state='open', claimed_by=NULL, claimed_name=NULL, "
            "claimed_at=NULL, finished_at=NULL, note=COALESCE(note,'')||? "
            "WHERE id=? AND ("
            "  state IN ('failed','partial')"
            "  OR (state='claimed' AND (claimed_at IS NULL "
            "      OR claimed_at <= datetime('now', ?)))"
            ")",
            (f" reopened by {staff_id};", int(action_id),
             f"-{int(STALE_CLAIM_SECONDS)} seconds"))
        won = cur.rowcount == 1
        if won:
            _recover_ops(conn, int(action_id))
        row = conn.execute("SELECT * FROM sys_actions WHERE id=?",
                           (int(action_id),)).fetchone()
    return won, (dict(row) if row else None)


def _claim_op(conn, action_id: int, idx: int) -> bool:
    """Per-op claim. Same idiom, one level down, so a resumed run never reapplies
    an op that another run is mid-way through."""
    cur = conn.execute(
        "UPDATE sys_action_ops SET state='running' "
        "WHERE action_id=? AND op_index=? AND state='pending'",
        (int(action_id), int(idx)))
    return cur.rowcount == 1


def _mark_op(action_id: int, idx: int, state: str, detail: str = "") -> None:
    with _db().db() as conn:
        conn.execute(
            "UPDATE sys_action_ops SET state=?, detail=?, done_at=datetime('now') "
            "WHERE action_id=? AND op_index=?",
            (state, detail[:400], int(action_id), int(idx)))


class _Unapplied(Exception):
    """Raised INSIDE an op's transaction to roll its idempotency claim back.

    An op that could not be applied must not leave a key behind saying it was.
    The market gets relisted, the deleted row comes back, the stock line is
    re-created — and Retry has to be able to finish the job. Rolling the claim
    back with the (absent) effect is the difference between "not done yet" and
    "done", and those must never be confused; that confusion in either direction
    is the entire subject of this file.
    """

    def __init__(self, result: dict):
        super().__init__(result.get("title") or "op not applied")
        self.result = result


def _claim_effect(conn, action_id: int, idx: int, op_type: str) -> bool:
    """Claim `rb:<action>#<index>` for this op, INSIDE the caller's transaction.

    Returns False when the key is already recorded — which, because this INSERT
    commits with the effect and not before it, means the effect landed. The
    caller then applies nothing and reports it as already done.

    This is the generalisation of what only `coins` had (money review §4). The
    key is caller-minted from the audit row, so it is identical on every re-read
    and in every process; the PRIMARY KEY is what makes it a guarantee rather
    than a check-then-act.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO sys_action_op_effects "
        "(idem_key, action_id, op_index, op_type) VALUES (?,?,?,?)",
        (idem_key(action_id, idx), int(action_id), int(idx), str(op_type)))
    return (cur.rowcount or 0) == 1


def _note_effect(conn, action_id: int, idx: int, applied: str) -> None:
    """Record WHAT landed against the key, in the same transaction as the effect."""
    conn.execute("UPDATE sys_action_op_effects SET applied=? WHERE idem_key=?",
                 (str(applied)[:200], idem_key(action_id, idx)))


# ── Preview with FIGURES ────────────────────────────────────────────────────
def preview(action_id: int, *, guild=None) -> dict:
    """Everything the confirm screen needs. He confirms numbers, not intentions.

    Returns {lines, coins, manual_coins, movements, manual, already_done,
    resumable, stale_claim}. `movements` is [(who, before, delta, after, short)]
    with integer coins only.

    `coins` is what the button moves; `manual_coins` is what a human still has
    to move afterwards. They are separate because under escrow they diverged —
    a 40,000-coin land sale reverses as a 40,000 staff task plus a 2,000
    reporting mirror, and reporting one number reported the wrong one.

    `already_done` counts the idempotency records as well as the `done` markers.
    A rollback that crashed mid-apply has ops whose money moved and whose marker
    never got written; showing those as still-to-do would put figures on the
    confirm screen that will not move, and rule 4 is that he confirms FIGURES.
    """
    row = get(action_id)
    if not row:
        return {"lines": [], "coins": 0, "manual_coins": 0, "movements": [],
                "manual": [], "already_done": 0, "resumable": False,
                "stale_claim": False}
    ops = ops_of(action_id)
    states = op_states(action_id)
    landed = applied_effects(action_id)
    d = _db()

    lines, movements, manual = [], [], []
    already = 0
    for i, op in enumerate(ops):
        st = (states.get(i) or {}).get("state", "pending")
        if st == "done" or i in landed:
            already += 1
            st = "done"
        t = op.get("t")
        if t == "coins":
            uid = str(op["user_id"])
            amount = int(op["amount"])
            try:
                before = int((d.get_balance(uid) or {}).get("coins") or 0)
            except Exception:
                before = 0
            # deduct_coins clamps at zero: if they already spent it, the clawback
            # is SHORT and the shortfall becomes a staff task, not a silent hole.
            applied = amount if amount >= 0 else -min(-amount, before)
            short = (amount - applied) if amount < 0 else 0
            who = _who(uid, guild=guild)
            movements.append((who, before, applied, before + applied, short))
            lines.append(f"{'+' if applied >= 0 else ''}{applied:,} to {who}"
                         + (f"  ⚠ {abs(short):,} short" if short else "")
                         + ("  · already done" if st == "done" else ""))
        elif t == "platform":
            amount = int(op["amount"])
            try:
                before = int(d.get_platform_balance() or 0)
            except Exception:
                before = 0
            movements.append(("Platform balance", before, amount, before + amount, 0))
            lines.append(f"{'+' if amount >= 0 else ''}{amount:,} platform balance")
        elif t == "treasury":
            delta = int(op["delta"])
            mid = str(op.get("market_id") or "")
            try:
                before = int(d.get_treasury(mid) or 0)
            except Exception:
                before = 0
            # A market treasury is a coin location, not a statistic: it is what
            # backs the share price. It belongs in `movements` so the confirm
            # screen shows it with before/after like any other balance. It is
            # deliberately NOT in money_total() — that figure is what the ORIGINAL
            # action moved, and a dividend's 12,000 must not be counted twice
            # because it left a treasury and arrived in holders' balances.
            movements.append((f"{_market_name(mid)} treasury", before, delta,
                              before + delta, 0))
            lines.append(f"{'+' if delta >= 0 else ''}{delta:,} to the "
                         f"{_market_name(mid)} treasury")
        elif t == "stock":
            lines.append(f"{op['item']} stock {int(op['delta']):+,} in "
                         f"{_market_name(op.get('market_id'))}")
        elif t == "loyalty":
            lines.append(f"{float(op['points']):+.1f} loyalty points to "
                         f"{_who(op.get('user_id'), guild=guild)}")
        elif t == "setfields":
            fields = ", ".join(f"{k} → {v}" for k, v in (op.get("fields") or {}).items())
            lines.append(f"restore {op['table']} ({fields})")
        elif t == "insrow":
            lines.append(f"re-create the deleted {op['table']} row")
        elif t == "delrow":
            lines.append(f"remove the created {op['table']} row")
        elif t == "manual":
            # The figure rides on the task line itself. A staff task that says
            # "reverse the money by hand" without the amount is the same defect
            # as a headline that reports the wrong amount.
            _c = int(op.get("coins") or 0)
            what = op.get("what") or "needs a human"
            manual.append(what + (f" — {_c:,} coins" if _c else ""))
            lines.append(f"⚠ manual: {what}"
                         + (f" ({_c:,} coins, BY HAND)" if _c else ""))
    return {
        "lines": lines,
        "coins": int(row["money_coins"] or 0),
        "manual_coins": manual_total(ops),
        "movements": movements,
        "manual": manual,
        "already_done": already,
        "resumable": any((s or {}).get("state") in ("running", "failed")
                         for s in states.values()),
        "stale_claim": is_stale_claim(row),
    }


def _who(user_id, *, guild=None) -> str:
    try:
        import panel_skus
        return panel_skus._member_name(guild, user_id)
    except Exception:
        return f"user {user_id}"


def _market_name(market_id) -> str:
    if not market_id:
        return "?"
    try:
        with _db().db() as conn:
            r = conn.execute("SELECT name FROM markets WHERE market_id=?",
                             (str(market_id),)).fetchone()
        return r["name"] if r else str(market_id)
    except Exception:
        return str(market_id)


# ── Applying ────────────────────────────────────────────────────────────────
def idem_key(action_id: int, idx: int) -> str:
    """Caller-minted, derived from the domain event (this action, this op),
    stable across re-reads and across processes."""
    return f"rb:{int(action_id)}#{int(idx)}"


def open_run_task(action_id: int, staff_id, staff_name: str = "") -> int:
    """The "a rollback is in flight" card, opened BEFORE the first op runs.

    Product review §3: `open_task()` only ran *inside* the failure branches, so a
    process death mid-apply created no card at all — and the Retry button lives
    on a card. Opening it on entry means the record exists before anything can
    go wrong. It is idempotent per action, so a takeover reuses the same row
    rather than papering the ops channel.

    A clean finish closes it again (`apply_rollback`), so the normal case leaves
    nothing behind. Be honest about the limit: if the process dies before the
    caller posts this card, the row is in `staff_tasks` and not on Discord. The
    route out that does NOT depend on anything having been posted is the ↩
    Rollback button already on the log message, which `claim()`'s stale takeover
    now makes pressable again.
    """
    return open_task(
        f"Rollback in progress on action #{action_id}",
        f"{staff_name or staff_id} started this rollback. If this card is still "
        f"open, the run did not report a clean finish — press ↩ Retry rollback "
        f"below, or ↩ Rollback on the original log message once the claim goes "
        f"stale ({STALE_CLAIM_SECONDS // 60} minutes). Steps that already "
        f"committed are skipped; nothing is paid twice.",
        action_id=action_id, op_index=-2, opened_by=staff_id,
        idem=f"rbrun:{int(action_id)}")


def apply_rollback(action_id: int, *, staff_id, staff_name: str = "",
                   guild=None) -> dict:
    """Apply the reverse ops. MUST be called only after a won claim().

    Resumable: skips ops already `done`, re-derives the truth for `running` ops
    from the idempotency record, and writes its marker per op. Returns a report.
    """
    ensure_schema()
    ops = ops_of(action_id)
    states = op_states(action_id)
    done, skipped, failed, tasks = [], [], [], []

    # §3: the card exists before the first op does, so a crash at op 4 of 9 has
    # somewhere to be reported and something to press.
    run_task = open_run_task(action_id, staff_id, staff_name)

    for i, op in enumerate(ops):
        st = (states.get(i) or {}).get("state", "pending")
        if st == "done":
            skipped.append(i)
            continue
        if st == "manual":
            continue

        with _db().db() as conn:
            got = _claim_op(conn, action_id, i)
        if not got:
            # PRODUCT REVIEW §5. This used to read `if not got and st == "pending"`,
            # so an op in any other state fell THROUGH to `_apply_op` having lost
            # its row — a Retry click racing a redeploy applied the same stock
            # correction twice, measured 80 -> 75 with got=False. The claim is
            # either won or it is not; there is no state in which losing it means
            # "go ahead anyway". Recovery of a genuinely stuck op is `reopen()`
            # and `claim()`'s takeover, both of which are claim-first themselves.
            skipped.append(i)
            continue

        try:
            detail = _apply_op(action_id, i, op, guild=guild)
        except _Unapplied as u:
            # The op's transaction rolled back, key and all: it is "not done
            # yet", not "done". Reported as a manual step so the figures reach a
            # human, and left retryable.
            detail = u.result
        except Exception as e:                       # noqa: BLE001 - reported, not swallowed
            _mark_op(action_id, i, "failed", f"{type(e).__name__}: {e}")
            failed.append((i, f"{type(e).__name__}: {e}"))
            tasks.append(open_task(
                f"Rollback op {i} failed on action #{action_id}",
                f"Op: `{json.dumps(op)}`\nError: {type(e).__name__}: {e}\n"
                f"Nothing else was retried automatically for this op.",
                action_id=action_id, op_index=i, opened_by=staff_id))
            continue

        if detail.get("manual"):
            _mark_op(action_id, i, "manual", detail.get("why", ""))
            tasks.append(open_task(
                detail.get("title") or f"Manual step for action #{action_id}",
                detail.get("why") or "This op cannot be applied automatically.",
                action_id=action_id, op_index=i, opened_by=staff_id))
        else:
            _mark_op(action_id, i, "done", detail.get("why", ""))
            done.append(i)
            if detail.get("task"):
                tasks.append(open_task(
                    detail["task"][0], detail["task"][1],
                    action_id=action_id, op_index=i, opened_by=staff_id))

    final = op_states(action_id)
    all_done = all(v["state"] in ("done", "manual") for v in final.values()) if final else True
    any_bad = any(v["state"] in ("failed", "manual", "running") for v in final.values())
    state = "done" if (all_done and not any_bad) else ("partial" if all_done or done else "failed")
    with _db().db() as conn:
        conn.execute("UPDATE sys_actions SET state=?, finished_at=datetime('now') WHERE id=?",
                     (state, int(action_id)))
    # We got here, so the run REPORTED — whatever it reported. Everything still
    # outstanding is in `tasks`, each with its own figures. Close the in-flight
    # card: it exists only to survive the case where this line is never reached.
    # It is deliberately NOT in `tasks`; the caller decides what to do with it
    # (the cog posts it before the first op and deletes it here), and mixing it
    # in would put a "rollback in progress" card in the ops channel after every
    # successful rollback.
    close_task(run_task, staff_id)
    return {"state": state, "done": done, "skipped": skipped,
            "failed": failed, "tasks": tasks, "run_task": run_task}


def _apply_op(action_id: int, idx: int, op: dict, *, guild=None) -> dict:
    """Apply ONE reverse op, with its idempotency record inside its transaction.

    ONE OP, ONE COMMIT (money review §4 and §6)
    -------------------------------------------
    This is a dispatcher: the branches below are mutually exclusive, exactly one
    of them runs, and each opens a single `with d.db()` of its own. Within that
    block the shape is always:

        if not _claim_effect(conn, action_id, idx, t):
            return {"why": "already applied ..."}     # the key says it landed
        ...the effect, on THIS conn...
        _note_effect(conn, action_id, idx, "<what landed>")

    `_claim_effect` writes the key on the caller's connection, so it lands in
    the same commit as the effect beside it — see its own docstring for what
    that buys. Before this, only `coins` had a key at all, and even that one was
    written by `record_coin_ledger` in a SECOND `with db()` block documented
    "best-effort: never raises" and wrapped in `except Exception: pass` — money
    moves, process dies, the key is absent, the retry pays again. That is the
    bug class ledger v2 spent six rounds on, and the fix here is the one it
    settled on: see `ledger_v2._finalize_idempotency`.

    A branch may NOT call a `Restocker_db` helper that opens its own `with
    db()`: on this thread-local connection that commits our claim early and
    re-creates the split. The helpers the branches compose with
    (`adjust_balance_tx`, `adjust_treasury`, `add_loyalty_points`,
    `add_market_loyalty_points`) all take `conn=` for that reason, and display
    names are resolved before the block, never inside it.
    """
    t = op.get("t")
    key = idem_key(action_id, idx)
    d = _db()

    if t == "coins":
        uid = str(op["user_id"])
        amount = int(op["amount"])
        with d.db() as conn:
            if not _claim_effect(conn, action_id, idx, "coins"):
                return {"why": f"already applied (idempotency key `{key}`)"}
            # Belt: a row an EARLIER build wrote through the old two-transaction
            # path has a ledger tag and no effect record. Fail CLOSED on it, the
            # way every other repair path in this codebase does.
            if conn.execute("SELECT 1 FROM coin_ledger WHERE user_id=? AND reason=? LIMIT 1",
                            (uid, key)).fetchone():
                return {"why": "already on the coin ledger (idempotency key)"}
            # NOT `core.add_coins`: it commits the balance in one transaction and
            # then best-effort-writes the ledger row in another, and its YAML
            # fallback writes no key at all. A path built to be retried cannot
            # use either. A DB failure here raises, the op is marked failed, and
            # a staff task carries the figures — which is the honest outcome.
            _c, _p, applied = d.adjust_balance_tx(
                conn, uid, amount,
                counts_as_principal=bool(op.get("principal", False)), reason=key)
            _note_effect(conn, action_id, idx, f"{applied:+d} coins")
        short = amount - applied
        if amount < 0 and short:
            # Clawback came up short — they spent it. Say so, with figures.
            return {"why": f"clawed back {abs(applied):,} of {abs(amount):,}",
                    "task": (f"Short clawback on action #{action_id}",
                             f"Tried to take {abs(amount):,} coins back from "
                             f"{_who(uid, guild=guild)}; only {abs(applied):,} were there. "
                             f"**{abs(short):,} coins still outstanding.** "
                             f"Ledger tag `{key}`.")}
        return {"why": f"{applied:+,} coins"}

    if t == "platform":
        amount = int(op["amount"])
        month = str(op.get("month") or "")
        market_id = str(op.get("market_id") or "")
        note = key
        # One transaction: claim, move the balance, write the log line. It used
        # to be a read (`platform_fee_exists`, inside `try/except: pass`, so a
        # missing or raising helper silently meant "not compensated yet"), then a
        # read-modify-write of the balance, then a log INSERT ALSO inside
        # `except: pass` — so the balance could move with no record at all, and
        # the retry had nothing to match on. Now the log row IS the key.
        with d.db() as conn:
            if not _claim_effect(conn, action_id, idx, "platform"):
                return {"why": f"already applied (idempotency key `{key}`)"}
            if conn.execute(
                    "SELECT 1 FROM platform_balance_log WHERE month=? AND market_id=? "
                    "AND note=? LIMIT 1", (month, market_id, note)).fetchone():
                return {"why": "already compensated (idempotency key)"}
            # `balance = balance + ?` rather than read-then-set: no window, and
            # rowcount tells us whether the singleton row is actually there
            # instead of silently updating nothing the way set_platform_balance
            # does when the table has never been seeded.
            cur = conn.execute(
                "UPDATE platform_balance SET balance = balance + ? WHERE id=1", (amount,))
            if (cur.rowcount or 0) != 1:
                raise _Unapplied({
                    "manual": True,
                    "title": f"No platform balance row for action #{action_id}",
                    "why": f"`platform_balance` has no id=1 row, so the {amount:+,} "
                           f"coin correction was NOT applied and nothing was logged."})
            conn.execute(
                "INSERT INTO platform_balance_log (month, market_id, amount, note) "
                "VALUES (?,?,?,?)", (month, market_id, float(amount), note))
            bal = int(conn.execute(
                "SELECT balance FROM platform_balance WHERE id=1").fetchone()["balance"] or 0)
            _note_effect(conn, action_id, idx, f"platform {amount:+d} -> {bal}")
        # The YAML mirror too. `_credit_platform_balance` (Restocker_main.py:9827)
        # documents itself as the one path "so the two stores can never drift
        # apart again" — it only credits, so a clawback has to mirror by hand or
        # every rolled-back commission leaves the legacy readers reporting a fee
        # the DB no longer holds.
        core = _core()
        if core is not None and hasattr(core, "_add_platform_fee"):
            try:
                core._add_platform_fee(amount, market_id=market_id,
                                       month=month or "", note=note)
            except Exception as e:  # noqa: BLE001
                return {"why": f"platform {amount:+,} (DB only)",
                        "task": (f"Platform YAML mirror out of step on action #{action_id}",
                                 f"The DB platform balance moved {amount:+,} but the YAML "
                                 f"mirror write failed: {type(e).__name__}: {e}. The two "
                                 f"stores are now {abs(amount):,} coins apart.")}
        return {"why": f"platform {amount:+,}"}

    if t == "treasury":
        delta = int(op["delta"])
        mid = str(op.get("market_id") or "")
        # Resolved BEFORE the transaction opens. `_market_name` runs its own
        # `with db()`, and on this thread-local connection that COMMITS whatever
        # is open — including the idempotency claim of an op we are about to
        # refuse. Measured: the refused op came back reading "already applied".
        mname = _market_name(mid)
        with d.db() as conn:
            if not _claim_effect(conn, action_id, idx, "treasury"):
                return {"why": f"already applied (idempotency key `{key}`)"}
            trow = conn.execute("SELECT treasury_coins FROM market_shares WHERE market_id=?",
                                (mid,)).fetchone()
            before = int(float((trow["treasury_coins"] if trow else 0) or 0))
            # adjust_treasury returns 0.0 when the market_shares row is gone
            # (delisted since the action ran). Say so and open a task rather than
            # logging a restore that did not happen — and roll the claim back
            # with it, so the correction is still retryable if it gets relisted.
            applied = int(round(float(d.adjust_treasury(mid, float(delta),
                                                        allow_negative=True, conn=conn) or 0)))
            if delta and not applied:
                raise _Unapplied({
                    "manual": True,
                    "title": f"Treasury row gone for action #{action_id}",
                    "why": f"{mname} is no longer listed on the exchange; "
                           f"the {delta:+,} coin treasury correction was NOT applied. "
                           f"Treasury read {before:,} before the attempt."})
            _note_effect(conn, action_id, idx, f"treasury {applied:+d}")
        return {"why": f"treasury {applied:+,} ({before:,} → {before + applied:,})"}

    if t == "stock":
        # Same reason as the treasury branch: resolve the display name before
        # opening the transaction, never inside it.
        mname = _market_name(op.get("market_id"))
        with d.db() as conn:
            if not _claim_effect(conn, action_id, idx, "stock"):
                return {"why": f"already applied (idempotency key `{key}`)"}
            cur = conn.execute(
                "UPDATE market_stock SET stock = MAX(0, stock + ?), updated_at=datetime('now') "
                "WHERE market_id=? AND item=?",
                (int(op["delta"]), str(op["market_id"]), str(op["item"])))
            if cur.rowcount != 1:
                raise _Unapplied({
                    "manual": True,
                    "title": f"Stock row gone for action #{action_id}",
                    "why": f"`{op['item']}` no longer exists in {mname}; the "
                           f"{int(op['delta']):+,} stock correction was not applied."})
            _note_effect(conn, action_id, idx, f"stock {int(op['delta']):+d}")
        return {"why": f"stock {int(op['delta']):+,}"}

    if t == "loyalty":
        pts = float(op["points"])
        mid = op.get("market_id")
        with d.db() as conn:
            if not _claim_effect(conn, action_id, idx, "loyalty"):
                return {"why": f"already applied (idempotency key `{key}`)"}
            if mid:
                d.add_market_loyalty_points(str(op["user_id"]), str(mid), pts,
                                            update_activity=False, conn=conn)
            else:
                d.add_loyalty_points(str(op["user_id"]), pts, update_activity=False,
                                     conn=conn)
            _note_effect(conn, action_id, idx, f"loyalty {pts:+.1f}")
        return {"why": f"loyalty {pts:+.1f}"}

    if t in ("setfields", "insrow", "delrow"):
        return _apply_table_op(action_id, idx, op)

    if t == "manual":
        return {"manual": True,
                "title": op.get("what") or f"Manual step for action #{action_id}",
                "why": op.get("hint") or op.get("what") or
                       "This part of the action cannot be reversed automatically."}

    return {"manual": True, "title": f"Unknown op on action #{action_id}",
            "why": f"Op type {t!r} is not understood by this build."}


def _cols(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _apply_table_op(action_id: int, idx: int, op: dict) -> dict:
    """The three table ops, each with its key inside its own transaction.

    Every refusal in here `raise _Unapplied` rather than returning, because a
    `return` inside `with d.db()` COMMITS — which would leave the idempotency key
    on record for an op that did nothing. `insrow` in particular needed the key:
    `INSERT OR IGNORE` is only idempotent when a UNIQUE constraint happens to
    cover the row it is putting back, and for a table with a surrogate `id`
    whose stored row carries no `id`, a Retry made a SECOND row.
    """
    table = str(op["table"])
    keys = ROLLBACKABLE_TABLES.get(table)
    if not keys:
        return {"manual": True, "title": f"Table {table} is not rollbackable",
                "why": f"Action #{action_id} stored an op against `{table}`, which is "
                       f"not on the allowlist. Apply it by hand."}
    d = _db()
    with d.db() as conn:
        if not _claim_effect(conn, action_id, idx, str(op["t"])):
            return {"why": f"already applied (idempotency key "
                           f"`{idem_key(action_id, idx)}`)"}
        cols = _cols(conn, table)
        if op["t"] == "setfields":
            where = {k: v for k, v in (op.get("where") or {}).items() if k in cols}
            fields = {k: v for k, v in (op.get("fields") or {}).items() if k in cols}
            if not where or set(where) != set(keys) or not fields:
                raise _Unapplied({
                    "manual": True, "title": f"Bad restore op on action #{action_id}",
                    "why": f"`{table}` needs keys {keys}; got {sorted(where)}."})
            sql = (f"UPDATE {table} SET " + ", ".join(f"{k}=?" for k in fields)
                   + " WHERE " + " AND ".join(f"{k}=?" for k in where))
            cur = conn.execute(sql, [*fields.values(), *where.values()])
            if cur.rowcount != 1:
                raise _Unapplied({
                    "manual": True, "title": f"Row gone on action #{action_id}",
                    "why": f"No `{table}` row matched {where}; the field restore "
                           f"{fields} was not applied."})
            _note_effect(conn, action_id, idx, f"setfields {table} {sorted(fields)}")
            return {"why": f"restored {len(fields)} field(s) on {table}"}

        if op["t"] == "insrow":
            row = {k: v for k, v in (op.get("row") or {}).items() if k in cols}
            if not row:
                raise _Unapplied({
                    "manual": True, "title": f"Empty re-insert on action #{action_id}",
                    "why": f"Nothing usable to put back into `{table}`."})
            sql = (f"INSERT OR IGNORE INTO {table} (" + ",".join(row)
                   + ") VALUES (" + ",".join("?" for _ in row) + ")")
            cur = conn.execute(sql, list(row.values()))
            _note_effect(conn, action_id, idx,
                         f"insrow {table} rowcount={cur.rowcount}")
            return {"why": f"re-created the {table} row"}

        where = {k: v for k, v in (op.get("where") or {}).items() if k in cols}
        if not where or set(where) != set(keys):
            raise _Unapplied({
                "manual": True, "title": f"Bad delete op on action #{action_id}",
                "why": f"`{table}` needs keys {keys}; got {sorted(where)}."})
        cur = conn.execute(f"DELETE FROM {table} WHERE "
                           + " AND ".join(f"{k}=?" for k in where), list(where.values()))
        _note_effect(conn, action_id, idx, f"delrow {table} rowcount={cur.rowcount}")
        return {"why": f"removed the {table} row"}


# ── Staff tasks ─────────────────────────────────────────────────────────────
def open_task(title: str, body: str, *, action_id=None, op_index=None,
              opened_by=None, idem: Optional[str] = None) -> int:
    """Record something a human must finish. Idempotent per (action, op).

    This is the "cannot be safely automated" exit. It is never silence.
    """
    ensure_schema()
    key = idem or (f"task:{action_id}#{op_index}" if action_id is not None else None)
    if key:
        with _db().db() as conn:
            r = conn.execute("SELECT id FROM staff_tasks WHERE idem_key=?", (key,)).fetchone()
            if r:
                return int(r["id"])
    try:
        with _db().db() as conn:
            cur = conn.execute(
                "INSERT INTO staff_tasks (idem_key, action_id, op_index, title, body, opened_by) "
                "VALUES (?,?,?,?,?,?)",
                (key, action_id, op_index, str(title)[:200], str(body)[:1800],
                 str(opened_by) if opened_by else None))
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        with _db().db() as conn:
            r = conn.execute("SELECT id FROM staff_tasks WHERE idem_key=?", (key,)).fetchone()
        return int(r["id"]) if r else -1


def get_task(task_id: int) -> Optional[dict]:
    ensure_schema()
    with _db().db() as conn:
        r = conn.execute("SELECT * FROM staff_tasks WHERE id=?", (int(task_id),)).fetchone()
    return dict(r) if r else None


def list_tasks(status: str = "open", limit: int = 25) -> list[dict]:
    ensure_schema()
    with _db().db() as conn:
        rows = conn.execute(
            "SELECT * FROM staff_tasks WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, int(limit))).fetchall()
    return [dict(r) for r in rows]


def close_task(task_id: int, staff_id) -> bool:
    """Claim-first here too — two staff cannot both 'complete' the same task."""
    ensure_schema()
    with _db().db() as conn:
        cur = conn.execute(
            "UPDATE staff_tasks SET status='done', closed_by=?, closed_at=datetime('now') "
            "WHERE id=? AND status='open'", (str(staff_id), int(task_id)))
        return cur.rowcount == 1


# `recent(limit, kind=…)` — a `SELECT * FROM sys_actions ORDER BY id DESC` — was
# DELETED here, deliberately, and this note is the reason so it does not get
# rebuilt by the next person who wants a list of actions.
#
# It had no caller, in the bot or the web app or a test, and no docstring: there
# was no surface that listed actions and nothing said what the list was for. That
# is the shape this project has now hit four times — a mechanism built, never
# wired, and read later as if it were load-bearing. There is no rollback list
# command; `handle_rollback_click` resolves ONE action by id from the button's
# custom_id, and `open_tasks()` above is what a human actually browses.
#
# If a list surface is wanted, build the surface and the query together in the
# same change, so the query has a reader on the day it is written.
