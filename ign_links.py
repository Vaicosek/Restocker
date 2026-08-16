"""ign_links.py — evidence for who an in-game name belongs to, and an audit
trail for every binding that ever routed money.

WHY THIS EXISTS
═══════════════
`ign_registry` is a money-routing table. `get_user_id_by_ign()` decides who
`add_coins()` pays for a hive harvest (`Restocker_main.py:3771`), who a CSN sale
is credited to, and who a team roster shows. It is filled by exactly one write
path — a manager typing a name into `manage_team` (`Restocker_main.py:15877`) —
and emptied by `remove_ign` / `delete_ign`, which are hard DELETEs with **no
callers at all**. So today the registry has:

  * no record of who bound an IGN, or when, or why;
  * no record that an IGN was ever bound to someone else;
  * no evidence behind any binding beyond "a manager typed it";
  * and one blocked feature that names the cost out loud —
    `Restocker_main.py:4385`: *"the ownership check is unusable because no
    scanning IGN is in ign_registry, so every source reads as unattributable,
    including the honest ones."* That comment sits on the third recurrence of
    the same fault (a shop's scan delivered into another market's webhook).

THE IDEA, AND WHERE IT CAME FROM
════════════════════════════════
CorpNode OS mints a link row *empty* and binds the Minecraft name on first
contact from the in-game client, which reads the name off the session object —
so the value that lands in the row is one the user structurally cannot type
(`CorpNode OS 0.1/db/mc_links.py`, read for the shape; nothing copied).

**John's stack has no in-game client.** The CSN mod is parked by owner decision
and the transport is a CSV dropped in a channel. So the strong form of that idea
is not buildable here and this module does not pretend to build it. What the
transport *does* already carry is the mod's own `# SHOP,<ign>` stamp
(`_extract_shop_name`, `Restocker_main.py:7905`) — a name written by the mod
from its local config, not typed by the person posting the file, at the moment
of upload.

That is **evidence, not proof.** A CSV is editable; a session object is not.
The whole design of this module follows from taking that sentence seriously:

  ┌─ ign_observations ─────────────────────────────────────────────────────┐
  │  What we saw. Never routes money. Grows on its own, from ingest.       │
  └────────────────────────────────────────────────────────────────────────┘
                    │  confirm(), by a named human
                    ▼
  ┌─ ign_registry (existing) ──────────────────────────────────────────────┐
  │  What we pay. One owner per IGN. Unchanged shape, unchanged callers.   │
  └────────────────────────────────────────────────────────────────────────┘
                    │  every transition, both directions
                    ▼
  ┌─ ign_registry_log ─────────────────────────────────────────────────────┐
  │  Who was ever bound. Append-only. Nothing deletes from it.             │
  └────────────────────────────────────────────────────────────────────────┘

An observation NEVER writes `ign_registry`. Auto-binding a money-routing row
from an editable file would be a strictly worse bug than the manual step it
replaced: today an attacker needs a manager to type a name, and after an
auto-bind they would need a text editor.

WHY REVOCATION IS A LOG ROW AND NOT A `revoked` FLAG
════════════════════════════════════════════════════
CorpNode never deletes a link; it flips `revoked` and keeps the row, so "who was
ever bound" survives. That is right, and the reason it is right is the audit
trail — not the flag. But their primary key is a link id and many links coexist;
John's primary key is the **IGN itself**, and "one IGN pays exactly one Discord
user" is an invariant `add_ign` enforces and money depends on. A `revoked`
column on `ign_registry` would either break that PK or need a partial index and
a `WHERE revoked=0` added to `get_user_id_by_ign`, `get_ign`, `get_igns`,
`count_igns` and every ad-hoc SELECT in `Restocker_main.py` and
`Restocker_web.py` — five call sites where forgetting the predicate pays a
revoked user. So: the routing table keeps its shape, and the durable fact moves
to an append-only log that nothing can lose by forgetting a predicate.

HOUSE RULES OBSERVED
════════════════════
* **Claim-first, and the rowcount is read.** `confirm()` and `revoke()` are one
  `INSERT`/`DELETE` guarded on the believed state; the log row is only written
  by the caller that won. `observe()` is an INSERT-then-catch upsert.
* **The binding and the record of it commit in ONE transaction.** `confirm()`
  writes `ign_registry`, the observation's decision and `ign_registry_log`
  inside a single `db_in(conn)`. Two commits would allow a wage-routing row with
  no audit row.
* **No money here.** This module never calls `add_coins`, never touches a
  balance, and holds no amounts. It changes *who* a later payment resolves to,
  which is why it is audited, not why it is a ledger.
* **Three outcomes.** `check_attribution()` returns `ok` / `foreign` / `unknown`
  and callers must handle `unknown` as its own case — "we cannot say" is not
  "it is fine", and it is not "reject" either.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
-- What the transport told us. Evidence only: no money path reads this table.
CREATE TABLE IF NOT EXISTS ign_observations (
    ign          TEXT NOT NULL COLLATE NOCASE,  -- from the mod's `# SHOP` stamp
    source       TEXT NOT NULL,                 -- how we saw it, e.g. 'csn_shop_stamp'
    source_ref   TEXT NOT NULL,                 -- who delivered it: poster id or webhook id
    market_id    TEXT,                          -- market the file landed in
    hits         INTEGER NOT NULL DEFAULT 1,    -- how many uploads carried this pair
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'observed',   -- observed|confirmed|rejected
    decided_by   TEXT,
    decided_at   TEXT,
    bound_user   TEXT,                          -- who confirm() bound it to
    PRIMARY KEY (ign, source, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_ign_obs_state  ON ign_observations(state, last_seen);
CREATE INDEX IF NOT EXISTS idx_ign_obs_market ON ign_observations(market_id);

-- Append-only. Every bind and every unbind of a money-routing name, forever.
CREATE TABLE IF NOT EXISTS ign_registry_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ign     TEXT NOT NULL COLLATE NOCASE,
    user_id TEXT NOT NULL,
    event   TEXT NOT NULL,          -- 'bound' | 'revoked'
    actor   TEXT NOT NULL DEFAULT '',
    reason  TEXT NOT NULL DEFAULT '',
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ign_log_ign  ON ign_registry_log(ign, id);
CREATE INDEX IF NOT EXISTS idx_ign_log_user ON ign_registry_log(user_id, id);
"""

SOURCE_SHOP_STAMP = "csn_shop_stamp"

_schema_ready = False


def _db():
    import Restocker_db as _d
    return _d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema() -> None:
    """Idempotent and cheap after the first call. Called from every entry point
    so an existing restocker.db picks the feature up with no migration step —
    same convention as panel_skus.ensure_schema()."""
    global _schema_ready
    if _schema_ready:
        return
    with _db().db() as conn:
        conn.executescript(_SCHEMA)
    _schema_ready = True


def _norm(ign) -> str:
    return str(ign or "").strip()


# ── Evidence ────────────────────────────────────────────────────────────────
def observe(ign: str, source_ref, *, source: str = SOURCE_SHOP_STAMP,
            market_id: Optional[str] = None, conn=None) -> dict:
    """Record that `source_ref` delivered a file stamped with `ign`.

    Returns the observation row as a dict. NEVER writes ign_registry — see the
    module docstring; that separation is the whole point of the design.

    Upsert is INSERT-then-catch on the primary key, not SELECT-then-branch: two
    uploads arriving on the same gateway event race here, and a read-then-write
    would either double-count `hits` or lose one. The `hits` bump is done as an
    UPDATE relative to the stored value (`hits = hits + 1`), never as a write of
    a count read before the insert.
    """
    ensure_schema()
    ign = _norm(ign)
    ref = str(source_ref or "").strip()
    if not ign or not ref:
        return {}
    now = _now()
    with _db().db_in(conn) as c:
        try:
            c.execute(
                "INSERT INTO ign_observations "
                "(ign, source, source_ref, market_id, hits, first_seen, last_seen, state) "
                "VALUES (?,?,?,?,1,?,?,'observed')",
                (ign, source, ref, (str(market_id) if market_id else None), now, now))
        except sqlite3.IntegrityError:
            # Already seen this exact pair. Bump the evidence count and refresh
            # the market it was last seen serving; do NOT touch `state` — a
            # rejected pair that shows up again stays rejected and simply
            # accumulates hits, which is exactly the signal an operator wants.
            c.execute(
                "UPDATE ign_observations SET hits = hits + 1, last_seen = ?, "
                "market_id = COALESCE(?, market_id) "
                "WHERE ign = ? AND source = ? AND source_ref = ?",
                (now, (str(market_id) if market_id else None), ign, source, ref))
        row = c.execute(
            "SELECT * FROM ign_observations WHERE ign=? AND source=? AND source_ref=?",
            (ign, source, ref)).fetchone()
    return dict(row) if row else {}


def pending(limit: int = 25) -> list:
    """Observations nobody has decided yet, strongest evidence first.

    An observation whose IGN is ALREADY in ign_registry is not pending — there
    is nothing to decide — but it is not silently dropped either: it is returned
    with `registry_user` filled in so the caller can show "already bound to
    <@x>" rather than an empty list that reads like "no evidence exists".
    """
    ensure_schema()
    with _db().db() as conn:
        rows = conn.execute(
            "SELECT * FROM ign_observations WHERE state='observed' "
            "ORDER BY hits DESC, last_seen DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            owner = conn.execute(
                "SELECT user_id FROM ign_registry WHERE ign=? COLLATE NOCASE",
                (d["ign"],)).fetchone()
            d["registry_user"] = owner["user_id"] if owner else None
            out.append(d)
    return out


def history(ign: str) -> list:
    """Every bind/unbind this IGN has ever had, oldest first. This is the
    question `remove_ign`'s hard DELETE made unanswerable."""
    ensure_schema()
    with _db().db() as conn:
        rows = conn.execute(
            "SELECT * FROM ign_registry_log WHERE ign=? COLLATE NOCASE ORDER BY id",
            (_norm(ign),)).fetchall()
    return [dict(r) for r in rows]


# ── Decisions ───────────────────────────────────────────────────────────────
def confirm(ign: str, user_id, actor, *, reason: str = "",
            source: str = SOURCE_SHOP_STAMP, source_ref=None) -> str:
    """Bind `ign` to `user_id` in ign_registry, audited. The ONLY promotion path.

    Returns one of:
        'bound'  — newly bound to this user (registry row created, log appended)
        'exists' — this user already held it; idempotent, no second log row
        'taken'  — a DIFFERENT user holds it; nothing changed, caller must refuse

    CLAIM-FIRST. The authority is the INSERT against `ign_registry`'s primary
    key on `ign`, and the rowcount is read. `add_ign()` does the opposite —
    SELECT the owner, compare in Python, then INSERT — so two managers
    confirming different users for the same IGN in the same moment both pass the
    check and the second one's INSERT raises IntegrityError out of a code path
    whose docstring promises 'taken'. This does not read then write.

    ONE TRANSACTION. Registry row, observation decision and audit row commit
    together. A binding that routes wages must never exist without the row
    saying who created it.
    """
    ensure_schema()
    ign = _norm(ign)
    uid = str(user_id or "").strip()
    if not ign or not uid:
        return "taken"
    now = _now()
    with _db().db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO ign_registry (ign, user_id, registered_at) VALUES (?,?,?)",
                (ign, uid, now))
            won = cur.rowcount == 1
        except sqlite3.IntegrityError:
            won = False
        if not won:
            owner = conn.execute(
                "SELECT user_id FROM ign_registry WHERE ign=? COLLATE NOCASE",
                (ign,)).fetchone()
            held = str(owner["user_id"]) if owner else ""
            if held != uid:
                return "taken"
            status = "exists"
        else:
            status = "bound"
            conn.execute(
                "INSERT INTO ign_registry_log (ign, user_id, event, actor, reason, at) "
                "VALUES (?,?,'bound',?,?,?)",
                (ign, uid, str(actor or ""), str(reason or ""), now))
        # The observation is marked decided either way: 'exists' means somebody
        # already did this by hand, and leaving the evidence 'observed' would
        # re-offer a decision that has been made.
        if source_ref:
            conn.execute(
                "UPDATE ign_observations SET state='confirmed', decided_by=?, "
                "decided_at=?, bound_user=? WHERE ign=? AND source=? AND source_ref=?",
                (str(actor or ""), now, uid, ign, source, str(source_ref)))
        else:
            conn.execute(
                "UPDATE ign_observations SET state='confirmed', decided_by=?, "
                "decided_at=?, bound_user=? WHERE ign=? AND state='observed'",
                (str(actor or ""), now, uid, ign))
    return status


def reject(ign: str, source_ref, actor, *, reason: str = "",
           source: str = SOURCE_SHOP_STAMP) -> bool:
    """Mark one observation as not-a-binding. Touches no registry row.

    Kept distinct from 'nothing happened': a rejected pair that keeps arriving
    keeps incrementing `hits`, so 'we decided this is not them' stays tellable
    apart from 'we have not looked yet' — the same distinction csn_settle draws
    between 'skip' and 'done'.
    """
    ensure_schema()
    with _db().db() as conn:
        cur = conn.execute(
            "UPDATE ign_observations SET state='rejected', decided_by=?, decided_at=? "
            "WHERE ign=? AND source=? AND source_ref=? AND state='observed'",
            (str(actor or ""), _now(), _norm(ign), source, str(source_ref)))
        return cur.rowcount > 0


def revoke(ign: str, actor, *, reason: str = "") -> Optional[str]:
    """Unbind an IGN from whoever holds it. Returns the freed user id, or None
    if nobody held it.

    This is the unbind surface the tree does not otherwise have: `remove_ign`
    and `delete_ign` exist in Restocker_db and have **zero callers**, so today
    a mistyped IGN can only be fixed by editing the database. The difference
    from those two is the log row — which is why this exists as a separate
    function rather than a caller of them.

    CLAIM-FIRST: the DELETE carries the believed holder in its WHERE clause when
    one was read, and the rowcount decides whether the log row is written. No
    holder read, no delete, no log entry claiming an unbind that did not happen.
    """
    ensure_schema()
    ign = _norm(ign)
    with _db().db() as conn:
        row = conn.execute(
            "SELECT user_id FROM ign_registry WHERE ign=? COLLATE NOCASE", (ign,)).fetchone()
        if not row:
            return None
        held = str(row["user_id"])
        cur = conn.execute(
            "DELETE FROM ign_registry WHERE ign=? COLLATE NOCASE AND user_id=?", (ign, held))
        if cur.rowcount != 1:
            # Someone else moved it between the read and the delete. Say nothing
            # happened rather than log an unbind of a binding we did not remove.
            return None
        conn.execute(
            "INSERT INTO ign_registry_log (ign, user_id, event, actor, reason, at) "
            "VALUES (?,?,'revoked',?,?,?)",
            (ign, held, str(actor or ""), str(reason or ""), _now()))
    return held


# ── The check the 4385 comment says is unusable ─────────────────────────────
def check_attribution(market_id: str, shop_ign: str) -> dict:
    """Does this file's shop plausibly belong to this market?

    Returns {'verdict', 'shop_ign', 'market_id', 'owner_id', 'reason'} where
    verdict is one of:

        'ok'      — the IGN is bound to the market's owner or one of its managers
        'foreign' — the IGN is bound to somebody who is neither
        'unknown' — the IGN is not bound, or the market is not registered

    THREE OUTCOMES, NOT TWO. 'unknown' is the common case on day one (an empty
    registry means every honest file is unattributable, which is exactly why the
    check was shelved) and it must not read as either approval or rejection. The
    caller warns on 'foreign' and stays silent on 'unknown'; nothing here
    rejects an upload, because the evidence behind a binding is a text file.
    """
    ensure_schema()
    mid = str(market_id or "").strip()
    ign = _norm(shop_ign)
    out = {"verdict": "unknown", "shop_ign": ign, "market_id": mid,
           "owner_id": None, "reason": ""}
    if not mid or not ign:
        out["reason"] = "no market id or no shop stamp"
        return out
    with _db().db() as conn:
        m = conn.execute(
            "SELECT owner_id, leader_discord_id, manager_ids FROM markets WHERE market_id=?",
            (mid,)).fetchone()
        if not m:
            out["reason"] = f"market `{mid}` is not registered"
            return out
        owner = conn.execute(
            "SELECT user_id FROM ign_registry WHERE ign=? COLLATE NOCASE", (ign,)).fetchone()
    if not owner:
        out["reason"] = f"`{ign}` is not bound to a Discord account"
        return out
    uid = str(owner["user_id"])
    out["owner_id"] = uid
    allowed = {str(m["owner_id"] or ""), str(m["leader_discord_id"] or "")}
    try:
        allowed |= {str(x) for x in (json.loads(m["manager_ids"] or "[]") or [])}
    except Exception:
        pass
    allowed.discard("")
    allowed.discard("None")
    if not allowed:
        out["reason"] = f"market `{mid}` names no owner, leader or manager"
        return out
    if uid in allowed:
        out["verdict"] = "ok"
        out["reason"] = f"`{ign}` is <@{uid}>, who runs `{mid}`"
    else:
        out["verdict"] = "foreign"
        out["reason"] = (f"`{ign}` is <@{uid}>, who is not an owner, leader or manager "
                         f"of `{mid}`")
    return out
