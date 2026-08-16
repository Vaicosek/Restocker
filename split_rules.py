"""Standing split rules — declarative distribution of an income event, in integers.

WHAT THIS IS
------------
A **standing rule** says: "when `treasury:estates` books commission, 40% of it goes
to `treasury:vtech`, 25% is shared evenly among the holders of the market-owner
role, and whatever floor division leaves behind stays where it was." The rule is a
durable row, not a runtime decision, and executing it is also a durable row, so the
same trigger can be re-offered any number of times and pay exactly once.

The design is read off Ironcrest's `The Banker 0.1/src/services/AutoSplitService.ts`
(+ `src/models/AutoSplit.ts`). Their stack is GPL-3.0 and this file is a
from-scratch implementation: go and read theirs if you want the shape, nothing here
is transliterated from it. What IS taken from them, deliberately, is the list of
things not to do, because every one of the nine defects below is a real way to mint
or destroy coins:

  * they credit every beneficiary first and debit the source last, from a document
    read before the loop — a crash in between is net new coins (`:159-160`,
    `:186-187`, `:198-199` vs `:109-111`);
  * the source wallet is `min: 0`, so a concurrently-drained wallet throws on the
    final save *after* everyone has been paid (`models/Wallet.ts:81-84`);
  * an empty role returns `{success: true, recipientCount: 0}` and the caller still
    debits the full leg (`:174-177` + `:92` + `:110`) — the wallet is debited and
    nobody is credited;
  * `floor(amount / size)` is credited `size` times but the FULL leg is debited, so
    up to `size - 1` coins are destroyed per rule per run (`:179`);
  * a role member with no user document is skipped and their share is debited
    anyway (`:184-189`);
  * the source document is loaded, then the loop awaits a members fetch for
    seconds, then the whole document is saved — concurrent credits in that window
    are silently overwritten (`:59`);
  * there is no run record and no idempotency key at all, so a re-fired interaction
    splits again (`:92`).

Against that, the four structural facts of this file:

  1. **One transaction.** The debit of the source, every beneficiary credit, every
     `split_legs` row and `split_runs.state='applied'` commit together in a single
     `ledger_v2._tx()`. There is no instant at which a beneficiary is paid and the
     source is not. That single fact kills their entire bug class; the rest is
     bookkeeping so that the operator can see what happened.
  2. **Integers and basis points, end to end.** No float touches a coin. Percentages
     are `bps` (10000 = 100%).
  3. **One income event, one run.** The run id is derived from durable rows — the
     trigger's identity and the pinned ruleset version — never from `uuid4()` and
     never from the clock, so two attempts at the same trigger compute the same
     `run_id` and the second is a replay. The version in that hash is also why the
     id is not the whole guarantee: phase 1 looks the trigger up at EVERY version
     (`_run_for_trigger`) before it mints, so a rule edited between two offers of
     one event cannot mint a second run and pay the same coins out again. The
     first run pinned for an event owns it; new rules govern the next event.
  4. **Three outcomes.** `applied` / `refused` / `unknown`. `unknown` parks with the
     plan intact and is resolved by EVIDENCE (the per-leg idempotency keys this
     module writes into `ledger_entries`), not by guessing.

CONSERVATION, stated as the invariant the probes check
------------------------------------------------------
    coins debited from the source  ==  sum of coins credited to beneficiaries
and both are `allocated`, which is `<=` `amount_in`. A split never mints and never
overdraws. The crumb that floor division leaves behind is not "lost" — it is simply
never debited, so it stays in the source account. See `plan_split` for the two
remainder rules and why they differ.

COUPLING TO `ledger_v2`
-----------------------
This module calls six private names in `ledger_v2` (`_tx`, `_debit`, `_credit`,
`_record`, `_read_balance`, `_ensure_wallet`) plus `LedgerError`. That is the same
narrow, deliberate coupling `land_escrow.LedgerV2InProcess` documents and for the
same reason: the alternative is a second implementation of the claim-first debit,
and two implementations of one money rule drift. It fails loudly with
`AttributeError` on the first call if ledger v2 renames anything.

The reason it is `_debit`/`_credit` and NOT `place_hold` + `capture_hold` per
beneficiary, which is what the brief suggested: `capture_hold` opens its own
`_tx()`, and `_tx()` is explicitly not re-entrant. A hold/capture split is
therefore N+1 separate commits with a window between each one, and every window is
a state where the source is committed short and a beneficiary is not yet paid.
Holds exist to reserve coins ACROSS TIME while a human decides. A split decides
nothing — it is arithmetic — so it wants atomicity, not reservation. One
transaction is strictly stronger than "hold first", not a weaker substitute for it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from typing import Any, Callable, Optional

log = logging.getLogger("restocker.split")

#: How many coins a split may move in one run before it refuses on its face. A
#: standing rule firing on a mistyped `amount_in` is the one input this module
#: cannot sanity-check from inside, so it has a ceiling instead of a hope.
MAX_SPLIT_AMOUNT = 1_000_000_000

#: Short-source policies. See `plan_split` and the module README block.
POLICIES = ("strict", "prorate", "defer")

#: Terminal + non-terminal run states.
STATES = ("pending", "claimed", "applied", "refused", "unknown", "pending_funds")

#: Beneficiary kinds. `account` is any ledger wallet id — a Discord user id or a
#: `treasury:*` account. `role` is a Discord role id, expanded at plan time.
KINDS = ("account", "role")


class SplitError(Exception):
    """A rule-management or planning failure. Never raised past a money move."""


# ══════════════════════════════════════════════════════════════════════════
# Schema
# ══════════════════════════════════════════════════════════════════════════

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS split_rulesets (
    source_account TEXT PRIMARY KEY,
    short_policy   TEXT NOT NULL DEFAULT 'strict',
    version        INTEGER NOT NULL DEFAULT 1,
    -- Bumped by EVERY rule write. It is pinned into the run id, so a run that is
    -- mid-flight when an operator edits the rules keeps executing the rules it
    -- was planned against, and a NEW trigger gets a different run id rather than
    -- replaying the old plan.
    note           TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (short_policy IN ('strict','prorate','defer')),
    CHECK (version > 0)
);

CREATE TABLE IF NOT EXISTS split_rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_account   TEXT NOT NULL,
    seq              INTEGER NOT NULL DEFAULT 0,   -- order; ties broken by id
    beneficiary_kind TEXT NOT NULL,
    beneficiary_ref  TEXT NOT NULL,
    bps              INTEGER NOT NULL,
    floor_coins      INTEGER NOT NULL DEFAULT 0,   -- leg does not fire below this
    active           INTEGER NOT NULL DEFAULT 1,
    label            TEXT NOT NULL DEFAULT '',
    created_by       TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (beneficiary_kind IN ('account','role')),
    CHECK (bps > 0 AND bps <= 10000),
    CHECK (floor_coins >= 0),
    CHECK (active IN (0,1))
);

CREATE INDEX IF NOT EXISTS idx_split_rules_src
    ON split_rules(source_account, active, seq, id);

CREATE TABLE IF NOT EXISTS split_runs (
    run_id          TEXT PRIMARY KEY,
    trigger_kind    TEXT NOT NULL,
    trigger_row_id  TEXT NOT NULL,
    source_account  TEXT NOT NULL,
    amount_in       INTEGER NOT NULL,
    ruleset_version INTEGER NOT NULL,
    short_policy    TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'pending',
    allocated       INTEGER NOT NULL DEFAULT 0,
    shortfall_coins INTEGER NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT '',
    plan_json       TEXT NOT NULL DEFAULT '',
    attempts        INTEGER NOT NULL DEFAULT 0,
    service         TEXT NOT NULL DEFAULT 'core',
    created_at      REAL NOT NULL,
    claimed_at      REAL,
    settled_at      REAL,
    CHECK (state IN ('pending','claimed','applied','refused','unknown','pending_funds')),
    CHECK (amount_in >= 0),
    CHECK (allocated >= 0),
    -- This key is idempotency PER TRIGGER PER VERSION, and that is deliberately
    -- NOT the whole guarantee: a run planned under v3 and a run planned under v4
    -- are genuinely different plans and must be able to coexist as rows (a
    -- mid-flight run keeps executing the rules it was planned against). What
    -- must NOT happen is two of them being minted for ONE income event, which is
    -- a second payment out of the same coins. The triple below — without the
    -- version — is the real unit of idempotency, and `_run_for_trigger` enforces
    -- it inside phase 1's `BEGIN IMMEDIATE`, before anything is minted. It is not
    -- a UNIQUE index because legacy rows predating that check may already
    -- violate it, and a migration that cannot apply protects nothing.
    UNIQUE (trigger_kind, trigger_row_id, source_account, ruleset_version)
);

CREATE INDEX IF NOT EXISTS idx_split_runs_trigger
    ON split_runs(trigger_kind, trigger_row_id, source_account);

CREATE INDEX IF NOT EXISTS idx_split_runs_state
    ON split_runs(state, created_at);

CREATE TABLE IF NOT EXISTS split_legs (
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    rule_id    INTEGER,
    kind       TEXT NOT NULL,
    to_account TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    state      TEXT NOT NULL DEFAULT 'planned',   -- planned | applied
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, seq),
    CHECK (amount > 0),
    CHECK (state IN ('planned','applied'))
);
"""


def _statements(script: str) -> list[str]:
    """Split DDL on `;`, ignoring semicolons inside `--` comments.

    A naive `script.split(";")` cut this file's own DDL in half at the word
    "order; ties broken by id" and every table after it silently failed to be
    created — which is `ledger_migrate._split_statements`' note (line 437) landing
    on a second file. Comments are stripped for EXECUTION only; the SCHEMA
    constant keeps them, because they are half of why the columns are what they
    are.
    """
    out, cur = [], []
    for line in script.splitlines():
        code = line.split("--", 1)[0]
        cur.append(code)
        while ";" in code:
            head, code = code.split(";", 1)
            stmt = "\n".join(cur[:-1] + [head]).strip()
            if stmt:
                out.append(stmt)
            cur = [code]
    tail = "\n".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the four tables if they are absent. Safe to call repeatedly."""
    for stmt in _statements(SCHEMA):
        conn.execute(stmt)


def _lv():
    """The ledger module. Imported lazily so this file can be read without it."""
    import ledger_v2
    return ledger_v2


def _ensure_schema_ready(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('split_rules','split_runs','split_legs','split_rulesets')").fetchone()
    if int(row[0]) != 4:
        ensure_schema(conn)


def available() -> bool:
    """True iff a split could execute right now: ledger present, tables in."""
    try:
        lv = _lv()
        with lv._tx() as conn:
            _ensure_schema_ready(conn)
        return hasattr(lv, "_debit") and hasattr(lv, "_credit")
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════
# Rule management
# ══════════════════════════════════════════════════════════════════════════

def _ruleset(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT source_account, short_policy, version, note FROM split_rulesets "
        "WHERE source_account=?", (str(source),)).fetchone()
    if row is None:
        return {"source_account": str(source), "short_policy": "strict",
                "version": 1, "note": ""}
    return dict(row)


def _bump_version(conn: sqlite3.Connection, source: str) -> int:
    """Create-or-bump the ruleset row. Returns the NEW version."""
    conn.execute(
        "INSERT INTO split_rulesets (source_account, short_policy, version) "
        "VALUES (?, 'strict', 1) ON CONFLICT(source_account) DO UPDATE SET "
        "version = version + 1, updated_at = datetime('now')", (str(source),))
    return int(conn.execute(
        "SELECT version FROM split_rulesets WHERE source_account=?",
        (str(source),)).fetchone()[0])


def _active_rules(conn: sqlite3.Connection, source: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM split_rules WHERE source_account=? AND active=1 "
        "ORDER BY seq ASC, id ASC", (str(source),)).fetchall()]


def add_rule(source_account: str, beneficiary_kind: str, beneficiary_ref: str,
             bps: int, *, seq: int = 0, floor_coins: int = 0, label: str = "",
             created_by: str = "") -> dict[str, Any]:
    """Add one standing rule. Refuses if it would take the active total past 100%.

    The over-allocation guard is checked INSIDE the write transaction against a
    re-read of the active rules, not against a list read before it. Their version
    reads the rules, then loads the wallet, then compares — so two admins adding
    30% each concurrently both pass a check against the pre-existing 50%. Here the
    insert and the check share one `BEGIN IMMEDIATE`, so the second one loses.
    """
    kind = str(beneficiary_kind).strip().lower()
    if kind not in KINDS:
        raise SplitError(f"beneficiary_kind must be one of {KINDS}, not {kind!r}")
    ref = str(beneficiary_ref).strip()
    if not ref:
        raise SplitError("beneficiary_ref is required")
    b = int(bps)
    if b <= 0 or b > 10000:
        raise SplitError("bps must be 1..10000 (10000 = 100%)")
    fl = int(floor_coins)
    if fl < 0:
        raise SplitError("floor_coins may not be negative")
    src = str(source_account).strip()
    if not src:
        raise SplitError("source_account is required")

    lv = _lv()
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        cur = conn.execute(
            "INSERT INTO split_rules (source_account, seq, beneficiary_kind, "
            " beneficiary_ref, bps, floor_coins, label, created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (src, int(seq), kind, ref, b, fl, str(label)[:80], str(created_by)))
        rule_id = int(cur.lastrowid)
        total = sum(int(r["bps"]) for r in _active_rules(conn, src))
        if total > 10000:
            # Raising rolls the whole transaction back — the insert included.
            raise SplitError(
                f"{src}: active rules would total {total} bps (>100%). "
                f"Deactivate or reduce another rule first.")
        version = _bump_version(conn, src)
        return {"rule_id": rule_id, "source_account": src, "bps": b,
                "total_bps": total, "ruleset_version": version}


def deactivate_rule(rule_id: int, *, by: str = "") -> bool:
    """Retire a rule. Claim-first on `active=1`; the rowcount is the answer.

    Never a DELETE. A rule that paid coins last week is part of the audit trail of
    why those coins moved, and a deleted row cannot explain a past run.
    """
    lv = _lv()
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        row = conn.execute("SELECT source_account FROM split_rules WHERE id=?",
                           (int(rule_id),)).fetchone()
        if row is None:
            return False
        won = conn.execute("UPDATE split_rules SET active=0 WHERE id=? AND active=1",
                           (int(rule_id),)).rowcount == 1
        if won:
            _bump_version(conn, row["source_account"])
        return won


def set_short_policy(source_account: str, policy: str, *, note: str = "") -> str:
    """Set what happens when the source cannot fund the whole plan. Per account.

    This is a per-account column and NOT a global, because the right answer differs
    by what the money is:

      `strict`  (default) — refuse the whole run, move nothing. A commission split
                that pays 3 of 5 beneficiaries is worse than one that pays none,
                because the three who were paid now have to be un-paid by hand.
      `prorate` — scale every leg by the coins that are actually there, last leg
                absorbs the remainder, and the difference is written to
                `split_runs.shortfall_coins` so the operator can see exactly who
                was underpaid and by how much. For wages, where paying 90% now and
                10% later is better than paying nothing.
      `defer`   — park in `pending_funds` and let the sweep retry. Only for an
                account that is EXPECTED to be topped up (a hive float). On any
                other account this is a run that never completes and nobody sees.
    """
    p = str(policy).strip().lower()
    if p not in POLICIES:
        raise SplitError(f"policy must be one of {POLICIES}")
    lv = _lv()
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        conn.execute(
            "INSERT INTO split_rulesets (source_account, short_policy, version, note) "
            "VALUES (?,?,1,?) ON CONFLICT(source_account) DO UPDATE SET "
            "short_policy=excluded.short_policy, note=excluded.note, "
            "version = version + 1, updated_at=datetime('now')",
            (str(source_account), p, str(note)[:200]))
        return p


def reorder_rules(source_account: str, ordered_rule_ids: list,
                  *, by: str = "") -> dict[str, Any]:
    """Re-sequence ALL the active rules on one account, atomically.

    Order is not decoration. `_active_rules` sorts by `(seq, id)`, and that order
    survives into the pinned plan, where two things read it:

      * `_scale_pinned` (the `prorate` policy) gives the LAST contributing rule
        the remainder of `cap`, so who is last decides who absorbs a shortfall's
        odd coins;
      * `split_legs.seq` — and therefore each leg's idempotency key — is assigned
        in this order, so the order is part of what a run pins.

    It is deliberately a whole-list operation and not `set_rule_seq(id, seq)`.
    Moving one rule by writing one `seq` leaves every other rule on whatever
    number it happened to have, so "move this to the front" can silently land it
    in the middle of a tie broken by id. The caller passes the complete order it
    was just shown, and this refuses anything else:

      * an id that is not an ACTIVE rule on `source_account` -> `SplitError`;
      * an active rule left out of the list -> `SplitError` (naming the missing
        ids), because a partial order is the ambiguity above;
      * a duplicate id -> `SplitError`.

    Every `UPDATE` re-states `source_account` and `active=1` in its WHERE clause
    and its **rowcount is read**: a rule retired by another operator between the
    read and this write loses the race and rolls the whole re-order back, rather
    than half-applying an order the operator never saw.

    Bumps the ruleset version ONCE, for the same reason `add_rule` does — the new
    order governs the next income event, and any event already offered keeps the
    plan (and the order) it was pinned with. See `run_split` point 3.
    """
    src = str(source_account).strip()
    if not src:
        raise SplitError("source_account is required")
    try:
        ids = [int(x) for x in ordered_rule_ids]
    except (TypeError, ValueError) as e:
        raise SplitError(f"rule ids must be integers: {e}") from e
    if not ids:
        raise SplitError("give the complete order — an empty list reorders nothing")
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise SplitError(f"rule id(s) {dupes} appear more than once in the order")

    lv = _lv()
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        live = [int(r["id"]) for r in _active_rules(conn, src)]
        unknown = [i for i in ids if i not in live]
        if unknown:
            raise SplitError(
                f"rule(s) {unknown} are not active rules on {src} — "
                f"the active rules are {live}")
        missing = [i for i in live if i not in ids]
        if missing:
            raise SplitError(
                f"the order must list every active rule on {src}; "
                f"{missing} is missing. Re-read the rules and send the whole order.")
        for position, rid in enumerate(ids, start=1):
            n = conn.execute(
                "UPDATE split_rules SET seq=? WHERE id=? AND source_account=? "
                "AND active=1", (position, rid, src)).rowcount
            if n != 1:
                raise SplitError(
                    f"rule {rid} changed underneath this re-order (rowcount {n}) — "
                    f"nothing has been re-ordered. Re-read the rules and try again.")
        version = _bump_version(conn, src)
        log.info("[split] %s rules re-ordered to %s by %s (ruleset v%d)",
                 src, ids, by or "?", version)
        return {"source_account": src, "order": ids, "ruleset_version": version}


def list_rules(source_account: str) -> dict[str, Any]:
    """Everything an operator surface needs to render one account's rules."""
    lv = _lv()
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        rs = _ruleset(conn, source_account)
        rules = _active_rules(conn, source_account)
        rs["rules"] = rules
        rs["total_bps"] = sum(int(r["bps"]) for r in rules)
        rs["retained_bps"] = 10000 - rs["total_bps"]
        return rs


def has_rules(source_account: str) -> bool:
    """Cheap gate for a caller that should not build a run for nothing."""
    lv = _lv()
    try:
        with lv._tx() as conn:
            _ensure_schema_ready(conn)
            return bool(_active_rules(conn, source_account))
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════
# The planner — integers only, and it conserves by construction
# ══════════════════════════════════════════════════════════════════════════

#: A member resolver answers "who holds this role right now".
#:   list[str]  -> these accounts, in any order (this module sorts them)
#:   []         -> the role is KNOWN to be empty
#:   None       -> CANNOT SAY (members intent off, gateway down, role deleted)
#: The distinction is the whole point. `== 0` is a fact about the world; `None` is
#: an absence of one, and they must never be collapsed into `if not members`.
#: See DECISIONS.md NEW-5: the bank paid for this exact conflation once already.
MemberResolver = Callable[[str], Optional[list]]


class MembersUnknown(SplitError):
    """A role leg could not be enumerated. NO money has moved and none will."""


#: The process-wide default resolver, registered once by the bot at startup. A
#: caller like `land_settle` has no Discord client and should not grow one, so it
#: passes no resolver and gets this. It stays `None` under test and in any process
#: without a gateway — which is exactly right: a `role` rule then refuses safely
#: instead of guessing at a membership.
#:
#: THE IMPLEMENTATION THE BOT REGISTERS MUST RETURN `None`, NOT `[]`, WHEN IT
#: CANNOT SAY. With the privileged members intent off (John's call, see
#: DECISIONS.md NEW-5) `guild.get_role(id).members` is a partial cache, not an
#: answer, and returning it as a fact would pay a role's whole share to whichever
#: two members happened to be cached. `None` there; `[]` only for a role that
#: really exists and really has nobody in it.
_DEFAULT_RESOLVER: Optional[MemberResolver] = None


def set_member_resolver(fn: Optional[MemberResolver]) -> Optional[MemberResolver]:
    """Register the process-wide role resolver. Returns the previous one."""
    global _DEFAULT_RESOLVER
    prev, _DEFAULT_RESOLVER = _DEFAULT_RESOLVER, fn
    return prev


def _expand(rule: dict[str, Any], gross: int,
            resolver: Optional[MemberResolver]) -> tuple[list[tuple[str, int]], str]:
    """One rule's gross amount -> [(account, coins)], plus a note.

    The two remainder rules, which are different on purpose:

      BETWEEN RULES the crumb stays with the source. `gross = amount_in * bps //
      10000` is floored, so `sum(gross) <= amount_in`, and the source is debited
      `sum(gross)` — never `amount_in`. The coins floor division leaves behind were
      never moved, so there is nothing to lose. (This is the same shape as
      `land_settle.commission_split`, where the seller keeps the crumb, generalised
      to N legs.)

      WITHIN A ROLE the crumb must go to somebody, because this rule's gross IS
      being debited. `per = gross // n`, and the first `gross - per*n` members by
      ascending account id get one extra coin each, so the legs sum to `gross`
      EXACTLY. Stable sort, so a re-plan of the same run gives the same member the
      same coin. Their version credits `per * n` and debits `gross`, destroying up
      to `n-1` coins every run, forever.
    """
    if gross <= 0:
        return [], "zero"
    kind = rule["beneficiary_kind"]
    if kind == "account":
        return [(str(rule["beneficiary_ref"]), gross)], ""

    # kind == 'role'
    if resolver is None:
        raise MembersUnknown(
            f"rule {rule['id']} pays role {rule['beneficiary_ref']} and no member "
            f"resolver was supplied — the plan cannot be computed")
    members = resolver(str(rule["beneficiary_ref"]))
    if members is None:
        raise MembersUnknown(
            f"rule {rule['id']}: the holders of role {rule['beneficiary_ref']} "
            f"could not be determined (members intent off, or the gateway is "
            f"unreachable). This is NOT the same as the role being empty.")
    accounts = sorted({str(m) for m in members if str(m).strip()})
    if len(accounts) == 0:
        # KNOWN empty. The rule contributes ZERO to the allocation — the source is
        # not debited for it. Their `:174-177` returns success here and the caller
        # at `:92`/`:110` debits the full leg into nowhere, which deletes coins.
        return [], "role_empty"
    n = len(accounts)
    per = gross // n
    rem = gross - per * n
    out: list[tuple[str, int]] = []
    for i, acct in enumerate(accounts):
        amt = per + (1 if i < rem else 0)
        if amt > 0:
            out.append((acct, amt))
    return out, ""


def plan_split(rules: list[dict[str, Any]], amount_in: int,
               resolver: Optional[MemberResolver] = None) -> dict[str, Any]:
    """Turn rules + an amount into a pinned list of legs. Pure; touches no DB.

    Raises `MembersUnknown` if a role leg cannot be enumerated. It raises rather
    than skipping, because a plan that silently drops a beneficiary is the defect
    this whole module exists to remove.
    """
    amt = int(amount_in)
    if amt < 0:
        raise SplitError("amount_in may not be negative")
    if amt > MAX_SPLIT_AMOUNT:
        raise SplitError(f"amount_in {amt} exceeds MAX_SPLIT_AMOUNT")

    contributing: list[tuple[dict, int]] = []
    notes: list[dict[str, Any]] = []

    for r in rules:
        gross = (amt * int(r["bps"])) // 10000
        if gross < int(r["floor_coins"] or 0):
            notes.append({"rule_id": r["id"], "skipped": "below_floor",
                          "would_be": gross, "floor": int(r["floor_coins"] or 0)})
            continue
        if gross <= 0:
            notes.append({"rule_id": r["id"], "skipped": "rounds_to_zero"})
            continue
        contributing.append((r, gross))

    legs: list[dict[str, Any]] = []
    rules_used: list[dict[str, Any]] = []
    allocated = 0
    for r, gross in contributing:
        pairs, note = _expand(r, gross, resolver)
        if note == "role_empty":
            notes.append({"rule_id": r["id"], "skipped": "role_empty",
                          "would_be": gross})
            continue
        if not pairs:
            notes.append({"rule_id": r["id"], "skipped": "expanded_to_nothing"})
            continue
        got = sum(a for _, a in pairs)
        # Not a comment: an expansion that does not total its own gross is a coin
        # leak, and it is exactly the leak their `floor(amount/size)` has.
        if got != gross:
            # NOT an `assert`: `python -O` strips those, and a money invariant
            # that is only checked when somebody remembered not to pass -O is
            # not an invariant. This is the exact leak at their `:179`.
            raise SplitError(f"rule {r['id']}: legs total {got} but the rule's "
                             f"gross is {gross} — refusing to lose {gross - got} coin(s)")
        for acct, amount in pairs:
            legs.append({"seq": len(legs), "rule_id": int(r["id"]),
                         "kind": r["beneficiary_kind"], "to_account": acct,
                         "amount": int(amount)})
        allocated += gross
        rules_used.append({"id": int(r["id"]), "bps": int(r["bps"]),
                           "kind": r["beneficiary_kind"],
                           "ref": r["beneficiary_ref"], "gross": gross,
                           "label": r.get("label") or ""})

    total = sum(int(l["amount"]) for l in legs)
    if total != allocated:
        raise SplitError(f"plan legs total {total} != allocated {allocated}")
    if allocated > amt:
        raise SplitError(f"plan allocated {allocated} of {amt} — refusing to mint")

    return {"amount_in": amt, "allocated": allocated, "retained": amt - allocated,
            "legs": legs, "rules_used": rules_used, "notes": notes}


def _scale_pinned(plan: dict[str, Any], cap: int) -> dict[str, Any]:
    """The `prorate` policy: shrink an ALREADY-PINNED plan to fit `cap` coins.

    Works entirely off `plan_json` — it never re-reads a rule and never re-resolves
    a role, so it is safe to call inside the money transaction and gives the same
    answer on every retry of the same run.

    Two levels of remainder, the same two the planner has:
      * between rules — each rule's share of `cap` is `cap * bps // total_bps`, and
        the LAST contributing rule absorbs what is left, so the rules total exactly
        `cap` (not `cap` minus a coin per rule);
      * within a rule — the pinned beneficiary list keeps its order (already sorted
        ascending by the planner) and the earliest members take the extra coins.
    """
    avail = max(0, int(cap))
    by_rule: dict[Any, list[dict]] = {}
    for leg in plan.get("legs", []):
        by_rule.setdefault(leg["rule_id"], []).append(leg)
    used = [r for r in plan.get("rules_used", []) if r["id"] in by_rule]
    total_bps = sum(int(r["bps"]) for r in used)
    out_legs: list[dict[str, Any]] = []
    rules_used: list[dict[str, Any]] = []
    if total_bps <= 0 or avail <= 0:
        return {"allocated": 0, "legs": [], "rules_used": []}

    running = 0
    for i, r in enumerate(used):
        if i == len(used) - 1:
            gross = avail - running
        else:
            gross = (avail * int(r["bps"])) // total_bps
            running += gross
        if gross <= 0:
            continue
        members = by_rule[r["id"]]
        n = len(members)
        per, rem = gross // n, gross - (gross // n) * n
        got = 0
        for j, leg in enumerate(members):
            amount = per + (1 if j < rem else 0)
            if amount > 0:
                out_legs.append({"seq": len(out_legs), "rule_id": r["id"],
                                 "kind": leg["kind"], "to_account": leg["to_account"],
                                 "amount": int(amount)})
                got += amount
        if got != gross:
            raise SplitError(f"prorate rule {r['id']}: legs total {got} != {gross}")
        rules_used.append({**r, "gross": gross})

    allocated = sum(int(l["amount"]) for l in out_legs)
    if allocated > avail:
        raise SplitError(f"prorate allocated {allocated} > available {avail}")
    return {"allocated": allocated, "legs": out_legs, "rules_used": rules_used}


# ══════════════════════════════════════════════════════════════════════════
# Run identity
# ══════════════════════════════════════════════════════════════════════════

def run_id_for(trigger_kind: str, trigger_row_id: Any, source_account: str,
               ruleset_version: int) -> str:
    """The idempotency key of a whole split, derived ONLY from durable facts.

    Nothing here comes from the clock, from `uuid4()`, or from any runtime value
    that could differ between two attempts at the same trigger. Two callers who
    read the same durable rows compute the same id, so the second one replays.
    The ruleset version is in the hash on purpose: editing the rules produces a
    genuinely different distribution, and it must not be able to replay the
    answer stored for the old one.

    THIS ID IS NOT, BY ITSELF, THE IDEMPOTENCY GUARANTEE, and reading it as one
    was a double payment (F1). It says "same trigger, same rules → same answer".
    It says nothing about the same trigger under DIFFERENT rules, and an operator
    editing a rule between two offers of one income event is exactly that case.
    What makes a split idempotent BY THE TRIGGER is `_run_for_trigger`, which
    phase 1 consults before it mints anything: the first run minted for a
    `(trigger_kind, trigger_row_id, source_account)` owns that trigger, at the
    version it was planned against, whatever the rules say later.
    """
    raw = "\x1f".join([str(trigger_kind), str(trigger_row_id),
                       str(source_account), str(int(ruleset_version))])
    return "split:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def leg_key(run_id: str, seq: int) -> str:
    """The per-leg key stamped into `ledger_entries`. This is the EVIDENCE an
    `unknown` run is resolved against — see `_resolve_unknown`."""
    return f"{run_id}:leg:{int(seq)}"


#: Terminal states: the run has an answer and that answer is the answer.
#:
#: `refused` IS NOT ONE OF THEM, and that is N1. A refusal is an answer about one
#: ATTEMPT, not about the income event: the role was momentarily empty, the source
#: was momentarily short. Treating it as terminal made the first refusal own the
#: trigger for ever, so the commission could never be routed by any operator
#: action — and `refused` is in neither `stuck_runs` nor `parked_runs`, so
#: `/splits runs` said "nothing to see" over it. See `_run_for_trigger`.
#:
#: This is safe in a way re-planning `applied` or a live run is not, and the
#: reason is load-bearing: EVERY site that writes `state='refused'` does so
#: before any money moves — 979 is phase 1 (which never moves coins); 1035 and
#: 1083 are both above the single `lv._debit` in this module; 1241 is a plan with
#: no legs; and `_refuse_definite` runs only after `_tx()` has rolled the whole
#: money block back. A refused run PROVABLY MOVED NO COINS, so re-planning it
#: cannot un-pay anybody. Nothing here touches the `applied`-and-live pin, which
#: is what F1 was about.
_TERMINAL = ("applied",)


def _run_for_trigger(conn: sqlite3.Connection, trigger_kind: str,
                     trigger_row_id: Any, source_account: str
                     ) -> Optional[dict[str, Any]]:
    """The run that ALREADY owns this trigger, at any ruleset version.

    ONE INCOME EVENT, ONE RUN. The `split_runs` UNIQUE key carries
    `ruleset_version`, so it cannot express this on its own: at a new version the
    same trigger is a new row, a new plan and a second payment out of the same
    coins. This lookup is the missing half — it asks the question the unique key
    cannot, on the triple that identifies the EVENT rather than the plan.

    Ordering, when a legacy database already holds more than one row for a
    trigger (rows minted before this lookup existed):
      1. an `applied` run — money moved, and that is the answer, full stop;
      2. otherwise the earliest live run, so a resume finishes the plan that was
         pinned first rather than starting a competing one.

    A `refused` run is NOT an owner and is never returned (N1). It moved no coins
    — see `_TERMINAL` for the proof, site by site — so it has no claim on the
    event, and adopting one stranded the commission for ever. Returning None here
    lets the next offer plan the event afresh. The pin this function exists for
    is over `applied` and live runs, and that is untouched: if either exists it is
    still returned ahead of anything, at any ruleset version, which is F1.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM split_runs WHERE trigger_kind=? AND trigger_row_id=? "
        "  AND source_account=? ORDER BY created_at ASC, run_id ASC",
        (str(trigger_kind), str(trigger_row_id), str(source_account))).fetchall()]
    if not rows:
        return None
    for r in rows:
        if r["state"] == "applied":
            return r
    for r in rows:
        if r["state"] != "refused":
            return r
    return None


def find_run(trigger_kind: str, trigger_row_id: Any,
             source_account: str) -> Optional[dict[str, Any]]:
    """Public read of `_run_for_trigger` — "did this event ever get a run row?".

    A caller that has just seen `run_split` raise needs to know whether anything
    was minted, because that is the difference between "the sweep will finish it"
    and "nothing exists and nothing ever will". `land_settle` asks exactly that.
    """
    lv = _lv()
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        return _run_for_trigger(conn, trigger_kind, trigger_row_id, source_account)


# ══════════════════════════════════════════════════════════════════════════
# Execution
# ══════════════════════════════════════════════════════════════════════════

def _row(conn: sqlite3.Connection, run_id: str) -> Optional[dict[str, Any]]:
    r = conn.execute("SELECT * FROM split_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(r) if r else None


def _result(row: Optional[dict[str, Any]], *, replayed: bool = False) -> dict[str, Any]:
    if not row:
        # Every caller reads a row it has just written in the same transaction, so
        # this is unreachable — and if it ever is reached it must say so, not be
        # swallowed by the outer handler and reported to the operator as an
        # ambiguous money outcome that never happened.
        raise SplitError("split run row vanished between the write and the read")
    state = row["state"]
    outcome = {"applied": "applied", "refused": "refused", "unknown": "unknown",
               "pending_funds": "refused", "claimed": "unknown",
               "pending": "refused"}[state]
    return {
        "outcome": outcome,
        "state": state,
        # `retryable` is the fourth fact, and it is NOT a fourth outcome. It says
        # whether the sweep will come back to this run. A `refused` with
        # retryable=False is final; with True it is "not this attempt".
        "retryable": state in ("pending", "pending_funds", "claimed", "unknown"),
        "run_id": row["run_id"],
        "source_account": row["source_account"],
        "amount_in": int(row["amount_in"]),
        "allocated": int(row["allocated"]),
        "shortfall_coins": int(row["shortfall_coins"]),
        "reason": row["reason"],
        "replayed": replayed,
        "legs": json.loads(row["plan_json"] or "{}").get("legs", []),
    }


def run_split(trigger_kind: str, trigger_row_id: Any, source_account: str,
              amount_in: int, *, resolver: Optional[MemberResolver] = None,
              service: str = "core", reason: str = "") -> dict[str, Any]:
    """Execute the standing rules on `source_account` for one income event.

    IDEMPOTENT BY THE TRIGGER, ACROSS RULE EDITS. The unit of idempotency is the
    triple `(trigger_kind, trigger_row_id, source_account)` — the EVENT — and not
    the run id, which also carries the ruleset version. The first offer of an
    event mints one run row and pins one plan; every later offer of that same
    event finds that row (`_run_for_trigger`) and either replays its terminal
    answer or resumes it, **whatever anybody did to the rules in between**.

    That is a change of behaviour, and it is the fix for a real double payment:
    the run id alone is idempotent per trigger PER VERSION, so a single
    `add_rule` / `deactivate_rule` / `set_short_policy` between two offers of one
    commission minted a second run row and paid the same coins out again.

    WHICH RULESET WINS when an edit lands between two offers: **the pinned
    original**, and deliberately so —
      * money may already have moved under it, and a plan that half-executed
        cannot be re-planned without un-paying somebody;
      * a `pending_funds` run is precisely the state that provokes the edit
        ("this parked, switch it to prorate"), so re-planning on the new rules is
        the case most likely to double-pay;
      * an income event belongs to the moment it was booked. New rules govern the
        next sale, not one already settled.
    The operator's escape, if they truly want the new ruleset applied, is the
    run's own audit trail plus a manual transfer — never a second automatic run.

    Two phases, and the seam between them is deliberate.

      PHASE 1 moves no money. It mints the run row, computes the plan and pins it
      into `plan_json` + `split_legs`, and claims the run with one conditional
      UPDATE whose rowcount is read. If the process dies anywhere in here, the
      worst state is a `pending` or `claimed` row with a pinned plan and no coin
      moved, which the sweep finishes.

      PHASE 2 is the money, and it is ONE transaction: re-read the balance, apply
      the short policy, debit the source, credit every leg, mark every leg row,
      mark the run `applied`. A failure before COMMIT rolls all of it back and
      leaves the run `claimed` for the sweep — with the plan still pinned, so the
      retry pays the same people the same coins. A failure OF the commit is the
      only genuinely ambiguous case and parks the run in `unknown`.

    Returns a dict with `outcome` in {applied, refused, unknown} — never a bare
    boolean, and never `success: true` over a swallowed failure, which is what
    theirs returns when every single beneficiary failed.
    """
    lv = _lv()
    src = str(source_account).strip()
    amt = int(amount_in)
    if amt < 0:
        raise SplitError("amount_in may not be negative")

    # ── phase 1: plan, mint, claim ───────────────────────────────────────
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        rs = _ruleset(conn, src)
        version = int(rs["version"])
        policy = rs["short_policy"]
        rid = run_id_for(trigger_kind, trigger_row_id, src, version)

        existing = _row(conn, rid)
        replanning = existing is not None and existing["state"] == "refused"
        if existing is None or replanning:
            # THE VERSION-CROSSING LOOKUP. `rid` only finds a run planned against
            # TODAY's ruleset; this finds one planned against any version of it.
            # Without it, one `set_short_policy` between two offers of the same
            # income event minted a second run and paid the same commission twice.
            #
            # A `refused` row at `rid` is asked the same question, and asked it
            # HERE rather than short-circuiting on the row we happen to have:
            # a refusal does not own the event (N1), but an applied or live run
            # at another version still does, and it must win. Resetting to None
            # on the refused row alone would re-plan an event that already has a
            # run in flight — F1, straight back open.
            owner = _run_for_trigger(conn, trigger_kind, trigger_row_id, src)
            existing = owner
            if existing is not None:
                # THE PINNED RUN WINS. Adopt its id, its version and its policy —
                # we are not planning an event, we are continuing one.
                rid = existing["run_id"]
                version = int(existing["ruleset_version"])
                policy = existing["short_policy"]
                if int(existing["amount_in"]) != amt:
                    # Same trigger row, two different figures. The pinned one is
                    # the one the plan was computed from, so it is the one that
                    # executes; say so rather than silently using either.
                    log.warning(
                        "[split] %s/%s on %s re-offered as %d but run %s is pinned "
                        "at %d (ruleset v%d) — executing the pinned figure.",
                        trigger_kind, trigger_row_id, src, amt, rid,
                        int(existing["amount_in"]), version)
                    amt = int(existing["amount_in"])

        if existing and existing["state"] in _TERMINAL:
            # Terminal. Replay the stored answer; move nothing. This is the thing
            # their version has no way to do at all.
            return _result(existing, replayed=True)

        if existing is None:
            rules = _active_rules(conn, src)
            if not rules:
                return {"outcome": "refused", "state": "refused", "retryable": False,
                        "run_id": rid, "source_account": src, "amount_in": amt,
                        "allocated": 0, "shortfall_coins": 0, "legs": [],
                        "reason": "no_rules", "replayed": False}

            try:
                plan = plan_split(rules, amt,
                                  resolver if resolver is not None
                                  else _DEFAULT_RESOLVER)
            except MembersUnknown as e:
                # Definitely-refused-for-now. Nothing is written, nothing moved,
                # and the caller can retry when the gateway can answer. Writing a
                # run row here would pin a plan computed from an unknown, which is
                # the opposite of fail-safe.
                log.warning("[split] %s/%s on %s: %s", trigger_kind,
                            trigger_row_id, src, e)
                return {"outcome": "refused", "state": "pending", "retryable": True,
                        "run_id": rid, "source_account": src, "amount_in": amt,
                        "allocated": 0, "shortfall_coins": 0, "legs": [],
                        "reason": f"members_unknown: {e}", "replayed": False}
            # MINT, OR RE-PLAN A REFUSED ROW IN PLACE. `rid` carries the ruleset
            # version, so a re-plan after a version bump is a genuinely new row
            # and inserts cleanly; a re-plan at the SAME version has to reuse the
            # refused row, because `run_id` is the primary key and
            # `(trigger, source, version)` is UNIQUE. The `WHERE` on the conflict
            # arm is the claim: it fires only over a row that provably moved no
            # coins, and the rowcount below is read rather than assumed.
            wrote = conn.execute(
                "INSERT INTO split_runs (run_id, trigger_kind, trigger_row_id, "
                " source_account, amount_in, ruleset_version, short_policy, state, "
                " allocated, plan_json, service, created_at) "
                "VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET "
                "  amount_in=excluded.amount_in, short_policy=excluded.short_policy, "
                "  state='pending', allocated=excluded.allocated, "
                "  plan_json=excluded.plan_json, service=excluded.service, "
                "  shortfall_coins=0, reason='', settled_at=NULL, claimed_at=NULL "
                " WHERE split_runs.state='refused'",
                (rid, str(trigger_kind), str(trigger_row_id), src, amt, version,
                 policy, int(plan["allocated"]), json.dumps(plan), str(service),
                 time.time())).rowcount
            if wrote != 1:
                # The conflict arm did not fire, so the row at `rid` is not the
                # refused row we planned over. Somebody else owns it now. Say so
                # rather than executing a plan against a row in an unknown state.
                raise SplitError(
                    f"{rid}: could not mint or re-plan the run — the row changed "
                    f"underneath this transaction")
            if replanning:
                # The old plan's legs are not this plan's legs.
                conn.execute("DELETE FROM split_legs WHERE run_id=?", (rid,))
                log.info("[split] %s: re-planning a refused run (%s/%s on %s) — it "
                         "moved no coins, so the event is still unrouted.",
                         rid, trigger_kind, trigger_row_id, src)
            for leg in plan["legs"]:
                conn.execute(
                    "INSERT INTO split_legs (run_id, seq, rule_id, kind, to_account, "
                    " amount) VALUES (?,?,?,?,?,?)",
                    (rid, int(leg["seq"]), leg["rule_id"], leg["kind"],
                     leg["to_account"], int(leg["amount"])))
        else:
            # A run already exists for this event — at this version or another.
            # It executes THE PLAN IT PINNED, and the active rules are not
            # re-read here on purpose: the legs, the beneficiaries and the
            # figures were decided when the event was first offered, and a rule
            # written or retired since then applies to the NEXT income event, not
            # to this one. (Same pin semantics as a `pending_funds` run that pays
            # a since-retired beneficiary — see `set_short_policy`.)
            plan = json.loads(existing["plan_json"] or "{}")

        if not plan.get("legs"):
            conn.execute(
                "UPDATE split_runs SET state='refused', reason='nothing_to_pay', "
                " settled_at=? WHERE run_id=? AND state IN ('pending','pending_funds')",
                (time.time(), rid))
            return _result(_row(conn, rid))

        # CLAIM-FIRST. One UPDATE gated on the state we believe, and the rowcount
        # is the answer. A second caller on the same trigger loses here.
        won = conn.execute(
            "UPDATE split_runs SET state='claimed', claimed_at=?, attempts=attempts+1 "
            " WHERE run_id=? AND state IN ('pending','pending_funds','claimed')",
            (time.time(), rid)).rowcount == 1
        if not won:
            return _result(_row(conn, rid))

    # ── phase 2: the money, in one transaction ───────────────────────────
    return _execute_claimed(rid, reason=reason)


def _execute_claimed(run_id: str, *, reason: str = "") -> dict[str, Any]:
    """The money transaction for a run already in `claimed`.

    Everything from `_debit` to `state='applied'` is inside one `_tx()`. There is
    no ordering hazard to reason about because there is no ordering: either all of
    it committed or none of it did. The source is still debited FIRST inside the
    block, so that on any in-transaction failure the exception comes from the
    source's own guard (insufficient / frozen / escrow shortfall) rather than from
    the last credit, which is a better error and costs nothing.
    """
    lv = _lv()
    try:
        with lv._tx() as conn:
            row = _row(conn, run_id)
            if row is None:
                raise SplitError(f"{run_id}: no such run")
            if row["state"] in ("applied", "refused"):
                return _result(row, replayed=True)
            if row["state"] != "claimed":
                return _result(row)

            src = row["source_account"]
            plan = json.loads(row["plan_json"] or "{}")
            legs = [dict(l) for l in plan.get("legs", [])]
            allocated = sum(int(l["amount"]) for l in legs)
            shortfall = 0
            policy = row["short_policy"]

            # THE FIGURE IS RE-READ HERE, INSIDE THE TRANSACTION, and never taken
            # from whatever the trigger passed in. `_read_balance` is what
            # `_debit` will be judged against, holds included: coins reserved by
            # an open hold are somebody else's and a split may not spend them.
            snap = lv._read_balance(conn, src)
            avail = int(snap["available"])

            if avail < allocated:
                if policy == "strict":
                    conn.execute(
                        "UPDATE split_runs SET state='refused', settled_at=?, "
                        " reason=? WHERE run_id=? AND state='claimed'",
                        (time.time(),
                         f"insufficient: {src} has {avail} available, plan needs "
                         f"{allocated}", run_id))
                    log.warning("[split] %s REFUSED: %s has %d available, plan "
                                "needs %d (policy=strict)", run_id, src, avail,
                                allocated)
                    return _result(_row(conn, run_id))
                if policy == "defer":
                    conn.execute(
                        "UPDATE split_runs SET state='pending_funds', reason=? "
                        " WHERE run_id=? AND state='claimed'",
                        (f"waiting for funds: {avail} of {allocated}", run_id))
                    return _result(_row(conn, run_id))
                # prorate
                if avail <= 0:
                    conn.execute(
                        "UPDATE split_runs SET state='pending_funds', reason=? "
                        " WHERE run_id=? AND state='claimed'",
                        (f"waiting for funds: 0 of {allocated}", run_id))
                    return _result(_row(conn, run_id))
                # SCALE THE PINNED PLAN. Not a re-plan: the role expansion was
                # done once, at plan time, and it stays done. Re-resolving here
                # would (a) call the Discord gateway from inside an open
                # `BEGIN IMMEDIATE`, blocking every other writer on the database
                # for as long as a members fetch takes — which is the shape of
                # their `:59` bug, a slow await in the middle of a money path —
                # and (b) let a member who joined the role since the plan was
                # pinned change who gets paid on a RETRY of an older run.
                scaled = _scale_pinned(plan, avail)
                shortfall = allocated - int(scaled["allocated"])
                legs = [dict(l) for l in scaled["legs"]]
                allocated = int(scaled["allocated"])
                plan = {**plan, "legs": legs, "rules_used": scaled["rules_used"],
                        "prorated_from": int(row["allocated"])}
                conn.execute("DELETE FROM split_legs WHERE run_id=?", (run_id,))
                for leg in legs:
                    conn.execute(
                        "INSERT INTO split_legs (run_id, seq, rule_id, kind, "
                        " to_account, amount) VALUES (?,?,?,?,?,?)",
                        (run_id, int(leg["seq"]), leg["rule_id"], leg["kind"],
                         leg["to_account"], int(leg["amount"])))
                conn.execute("UPDATE split_runs SET plan_json=? WHERE run_id=?",
                             (json.dumps(plan), run_id))

            if allocated <= 0:
                conn.execute(
                    "UPDATE split_runs SET state='refused', settled_at=?, "
                    " reason='nothing_to_pay' WHERE run_id=? AND state='claimed'",
                    (time.time(), run_id))
                return _result(_row(conn, run_id))

            # THE ASSERTION, not the hope. A split may never move more than the
            # income event brought in, and never more than the source holds.
            if allocated > int(row["amount_in"]):
                raise SplitError(f"{run_id}: allocated {allocated} > amount_in "
                                 f"{row['amount_in']} — refusing to mint")
            if allocated > avail:
                raise SplitError(f"{run_id}: allocated {allocated} > available "
                                 f"{avail} — refusing to overdraw")

            note = reason or f"split:{row['trigger_kind']}:{row['trigger_row_id']}"
            src_after = lv._debit(conn, src, allocated)
            lv._record(conn, service=row["service"], action="split_out",
                       user_id=src, delta=-allocated, balance_after=src_after,
                       reason=note, key=run_id)

            paid = 0
            for leg in legs:
                acct, amount = str(leg["to_account"]), int(leg["amount"])
                lv._ensure_wallet(conn, acct)
                after = lv._credit(conn, acct, amount, counts_as_principal=False)
                # PER-LEG MARKER, written with the credit it describes and NOT
                # after the loop. Inside one transaction it cannot disagree with
                # the money; the key it stamps into `ledger_entries` is what makes
                # an `unknown` run resolvable from evidence afterwards.
                lv._record(conn, service=row["service"], action="split_in",
                           user_id=acct, delta=amount, balance_after=after,
                           counterparty=src, reason=note,
                           key=leg_key(run_id, int(leg["seq"])))
                conn.execute(
                    "UPDATE split_legs SET state='applied', updated_at=datetime('now') "
                    " WHERE run_id=? AND seq=? AND state='planned'",
                    (run_id, int(leg["seq"])))
                paid += amount

            if paid != allocated:
                raise SplitError(f"{run_id}: credited {paid} against a debit of "
                                 f"{allocated} — rolling back")

            conn.execute(
                "UPDATE split_runs SET state='applied', allocated=?, "
                " shortfall_coins=?, settled_at=?, reason=? "
                " WHERE run_id=? AND state='claimed'",
                (allocated, shortfall, time.time(),
                 "prorated" if shortfall else "", run_id))
            out = _result(_row(conn, run_id))
        log.info("[split] %s applied: %s -%d across %d leg(s)%s", run_id,
                 out["source_account"], out["allocated"], len(out["legs"]),
                 f", shortfall {out['shortfall_coins']}" if out["shortfall_coins"] else "")
        return out

    except SplitError:
        raise
    except Exception as e:
        code = getattr(e, "code", None)
        if code:
            # A `LedgerError` is raised BY a money primitive, INSIDE the `_tx()`
            # block, so `_tx()` has already rolled the whole thing back: the
            # source is not debited and no beneficiary is credited. That is a
            # DEFINITE refusal and collapsing it into UNKNOWN would be the same
            # mistake as collapsing UNKNOWN into refused, pointing the other way —
            # it parks a run the sweep can never resolve, because there will never
            # be any ledger evidence to find.
            return _refuse_definite(run_id, code, str(e))

        # Did the transaction commit? `_tx()` rolls back and re-raises on a failed
        # COMMIT, so ALMOST every path here means nothing moved — but "almost" is
        # not "provably", and this module does not get to collapse UNKNOWN into
        # refused. Park it and let the sweep decide from the evidence in
        # `ledger_entries`. The plan stays pinned so the answer is the same either
        # way.
        _park_unknown(run_id, e)
        return {"outcome": "unknown", "state": "unknown", "retryable": True,
                "run_id": run_id, "reason": f"{type(e).__name__}: {e}",
                "source_account": "", "amount_in": 0, "allocated": 0,
                "shortfall_coins": 0, "legs": [], "replayed": False}


#: Ledger refusals that describe a state of the WORLD rather than of this run, so
#: they will stop being true without anybody editing the run. These park in
#: `pending_funds` (retryable) instead of refusing terminally, because a run whose
#: only problem is a freeze that gets lifted an hour later should complete itself.
#: Anything not in here is a refusal that will still be a refusal next time.
TRANSIENT_LEDGER_CODES = frozenset({
    "frozen", "insufficient", "escrow_shortfall", "treasury_insolvent",
})


def _refuse_definite(run_id: str, code: str, detail: str) -> dict[str, Any]:
    """A ledger primitive refused, inside the transaction, so nothing moved."""
    lv = _lv()
    transient = code in TRANSIENT_LEDGER_CODES
    state = "pending_funds" if transient else "refused"
    with lv._tx() as conn:
        conn.execute(
            "UPDATE split_runs SET state=?, reason=?, settled_at=? "
            " WHERE run_id=? AND state='claimed'",
            (state, f"{code}: {detail}"[:200],
             None if transient else time.time(), run_id))
        row = _row(conn, run_id)
    log.warning("[split] %s: ledger refused with '%s' — nothing moved, run is '%s'. %s",
                run_id, code, state, detail)
    return _result(row)


def _park_unknown(run_id: str, exc: Exception) -> None:
    """Record the ambiguity durably. Best-effort by necessity, loud on failure.

    Uses a fresh connection: the one that raised may be the one `_tx()` discarded.
    """
    lv = _lv()
    try:
        lv._discard_conn()
    except Exception:
        pass
    try:
        with lv._tx() as conn:
            conn.execute(
                "UPDATE split_runs SET state='unknown', reason=? "
                " WHERE run_id=? AND state='claimed'",
                (f"unknown after {type(exc).__name__}: {exc}"[:200], run_id))
    except Exception as e2:
        log.error("[split] %s: the money outcome is UNKNOWN (%s) and the run row "
                  "could not be marked (%s). The pinned plan is still in "
                  "split_runs; resolve it with resume_pending().", run_id, exc, e2)
        return
    log.error("[split] %s: UNKNOWN outcome (%s). Plan pinned, run parked; the "
              "sweep resolves it against ledger_entries.", run_id, exc)


# ══════════════════════════════════════════════════════════════════════════
# Resume
# ══════════════════════════════════════════════════════════════════════════

def _resolve_unknown(run_id: str) -> Optional[str]:
    """Decide an `unknown` run from EVIDENCE, never from a guess.

    Every leg credit stamps `split:<hash>:leg:<n>` into `ledger_entries` in the
    same transaction as the credit itself. So the presence of leg 0's key is proof
    the transaction committed, and its absence is proof it did not — there is no
    third possibility, because the entry and the balance write are the same
    commit. That is what `_record` living inside the money transaction buys, and
    it is why this function can exist at all.

    Returns 'applied', 'claimed' (i.e. definitely not applied, retry it), or None
    if the run cannot be read.
    """
    lv = _lv()
    with lv._tx() as conn:
        row = _row(conn, run_id)
        if row is None:
            return None
        legs = json.loads(row["plan_json"] or "{}").get("legs", [])
        if not legs:
            conn.execute("UPDATE split_runs SET state='refused', reason="
                         "'nothing_to_pay', settled_at=? WHERE run_id=? AND "
                         "state='unknown'", (time.time(), run_id))
            return "refused"
        keys = [leg_key(run_id, int(l["seq"])) for l in legs]
        marks = conn.execute(
            "SELECT COUNT(*) FROM ledger_entries WHERE idempotency_key IN "
            "(" + ",".join("?" * len(keys)) + ")", keys).fetchone()[0]
        if int(marks) == 0:
            conn.execute("UPDATE split_runs SET state='claimed', reason=? "
                         " WHERE run_id=? AND state='unknown'",
                         ("resolved: no ledger evidence, nothing moved", run_id))
            return "claimed"
        if int(marks) != len(keys):
            # Impossible if the credits shared one transaction, so if it happens,
            # somebody has changed that and this is the tripwire. Never guess past
            # it: leave it unknown and shout.
            log.error("[split] %s: %d of %d leg entries present — the legs did NOT "
                      "commit together. NOT resolving; a human must reconcile.",
                      run_id, int(marks), len(keys))
            return None
        allocated = sum(int(l["amount"]) for l in legs)
        conn.execute("UPDATE split_runs SET state='applied', allocated=?, "
                     " settled_at=?, reason='resolved: ledger evidence found' "
                     " WHERE run_id=? AND state='unknown'",
                     (allocated, time.time(), run_id))
        conn.execute("UPDATE split_legs SET state='applied' WHERE run_id=?", (run_id,))
        return "applied"


def resume_pending(limit: int = 25) -> dict[str, Any]:
    """Finish every run that was interrupted. Safe to call on a loop.

    Candidate set, and why each is in it:
      `unknown`       — resolve from ledger evidence, then re-run if it did not land
      `claimed`       — a phase-2 that rolled back; the plan is pinned, re-run it
      `pending_funds` — a `defer` run waiting for a top-up; re-run and see
      `pending`       — a run minted and never claimed (a crash between two
                        statements of phase 1)

    There is no cursor and no marker table: the candidate query IS the progress
    marker, exactly as the hold sweep does it (`ledger_migrate` S11). A run that
    finishes leaves the set; a sweep killed half way resumes with precisely the
    runs it never reached.
    """
    lv = _lv()
    out = {"resolved": 0, "applied": 0, "refused": 0, "still_unknown": 0,
           "waiting": 0, "errors": 0}
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT run_id, state FROM split_runs WHERE state IN "
            "('unknown','claimed','pending_funds','pending') "
            "ORDER BY created_at ASC LIMIT ?", (int(limit),)).fetchall()]

    for r in rows:
        rid = r["run_id"]
        try:
            if r["state"] == "unknown":
                verdict = _resolve_unknown(rid)
                out["resolved"] += 1
                if verdict == "applied":
                    out["applied"] += 1
                    continue
                if verdict in (None, "refused"):
                    out["still_unknown"] += 1 if verdict is None else 0
                    continue
            if r["state"] in ("pending", "pending_funds"):
                # Re-claim it, then execute. Claim-first, rowcount read.
                with lv._tx() as conn:
                    won = conn.execute(
                        "UPDATE split_runs SET state='claimed', claimed_at=?, "
                        " attempts=attempts+1 WHERE run_id=? AND state IN "
                        "('pending','pending_funds')",
                        (time.time(), rid)).rowcount == 1
                if not won:
                    continue
            res = _execute_claimed(rid)
            if res["outcome"] == "applied":
                out["applied"] += 1
            elif res["outcome"] == "unknown":
                out["still_unknown"] += 1
            elif res["state"] == "pending_funds":
                out["waiting"] += 1
            else:
                out["refused"] += 1
        except Exception as e:  # noqa: BLE001 — one bad run must not stop the sweep
            out["errors"] += 1
            log.warning("[split] resume of %s failed: %s", rid, e)
    return out


# ══════════════════════════════════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════════════════════════════════

def get_run(run_id: str) -> Optional[dict[str, Any]]:
    lv = _lv()
    with lv._tx() as conn:
        row = _row(conn, run_id)
        if row is None:
            return None
        row["leg_rows"] = [dict(x) for x in conn.execute(
            "SELECT * FROM split_legs WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()]
        return row


def parked_runs(older_than_seconds: float = 0.0) -> list[dict[str, Any]]:
    """Runs waiting for coins: `pending_funds`, oldest first.

    Deliberately NOT folded into `stuck_runs`, and the difference is the whole
    reason there are two functions. `stuck_runs` means "the sweep cannot resolve
    this, a human must" — `cogs/loops.py` turns every row it returns into a
    `log.error` every five minutes, which is right for an ambiguous commit and
    wrong for a run that is doing exactly what `defer` told it to do. A parked
    run is not broken; it is a promise waiting on a top-up.

    It still has to be VISIBLE, because `set_short_policy`'s own docstring says
    what `defer` costs on an account nobody tops up: "a run that never completes
    and nobody sees". `/splits runs` lists these under their own heading, with
    their age, so the operator can tell a run that is waiting from a run that is
    stuck without reading a log.

    `older_than_seconds=0` (the default) lists every parked run, because for this
    state age is context and not a filter.
    """
    lv = _lv()
    cutoff = time.time() - float(older_than_seconds)
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        return [dict(r) for r in conn.execute(
            "SELECT run_id, state, source_account, amount_in, allocated, "
            "       shortfall_coins, reason, attempts, created_at, "
            "       trigger_kind, trigger_row_id FROM split_runs "
            " WHERE state='pending_funds' AND created_at <= ? "
            " ORDER BY created_at ASC LIMIT 50", (cutoff,)).fetchall()]


def unrouted_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Income events whose ONLY run refused: the coins are still sitting in the
    source and nobody has been told (N1).

    The third list, and it exists for the same reason `parked_runs` does. A
    `refused` run is not in `stuck_runs` (the sweep is not going to resolve it —
    there is nothing ambiguous about it) and not in `parked_runs` (it is not
    waiting on funds), so before this function the operator's only view of a
    commission that never got routed was `SELECT * FROM split_runs WHERE
    state='refused'` typed by hand. A state that is neither an answer nor a job
    in anyone's queue is invisible, and invisible is how this project's worst
    findings survived.

    The next offer of the event re-plans it (see `_TERMINAL`), so this list is
    "events nothing has re-offered" — for `land_commission` that is most of them,
    because a settled lot is offered once. Fix the configuration, then use the
    audit trail: these runs moved no coins, so nothing has to be un-paid.

    A refusal on a trigger that ALSO has an applied or live run is not listed:
    that event was routed by a later attempt and the refused row is history, not
    an open job.
    """
    lv = _lv()
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        return [dict(r) for r in conn.execute(
            "SELECT run_id, state, source_account, amount_in, allocated, reason, "
            "       attempts, created_at, settled_at, ruleset_version, "
            "       trigger_kind, trigger_row_id FROM split_runs r "
            " WHERE r.state='refused' AND NOT EXISTS ("
            "   SELECT 1 FROM split_runs o WHERE o.trigger_kind=r.trigger_kind "
            "     AND o.trigger_row_id=r.trigger_row_id "
            "     AND o.source_account=r.source_account AND o.state<>'refused') "
            " ORDER BY r.created_at ASC LIMIT ?", (int(limit),)).fetchall()]


def stuck_runs(older_than_seconds: float = 900.0) -> list[dict[str, Any]]:
    """Runs a human should look at: unknown, or claimed for too long.

    A mechanism that can park a run in `unknown` and has no surface naming the
    parked runs is a silent failure with extra steps.

    `pending_funds` is NOT here on purpose — see `parked_runs`, which is where it
    is surfaced. A run waiting for a top-up is not a run nobody can finish, and
    `cogs/loops.py` logs everything this returns at ERROR level.
    """
    lv = _lv()
    cutoff = time.time() - float(older_than_seconds)
    with lv._tx() as conn:
        _ensure_schema_ready(conn)
        return [dict(r) for r in conn.execute(
            "SELECT run_id, state, source_account, amount_in, allocated, reason, "
            "       attempts, created_at FROM split_runs "
            " WHERE state='unknown' OR (state IN ('claimed','pending') AND created_at < ?) "
            " ORDER BY created_at ASC LIMIT 50", (cutoff,)).fetchall()]
