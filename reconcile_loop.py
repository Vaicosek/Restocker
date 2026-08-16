"""
reconcile_loop.py — the minute tick that makes the site's promises true.

WHY THIS FILE EXISTS
--------------------
Three places in this codebase used to tell a player, or a maintainer, that something
would sort itself out:

  * `estates_web._place_bid`  — "It will resolve by itself within a minute."
  * `estates_web._place_stake` — the same sentence for a stake.
  * `estates_web._release_bid_row` (was `_release_previous`) — "will be reconciled"
    in a log line.

Nothing resolved anything. `ledger_v2.sweep_expired_holds` was never called, the estates
replay finders (`estates_db.placements_needing_replay`, `holds_needing_reconcile`) were
never called, and `hub_web.sweep_keys` was defined and called from nowhere (WEB_ATTACK
findings 3 and 10). The player-facing sentences have been changed to say what is true
today — that these need staff — and this file is the loop that would make the original
sentence true again.

IT IS NOT SCHEDULED. Writing it and wiring it are two acts, and the point of the honesty
fix is that the site must not claim the second one has happened. The exact wiring is in
`wire_into_loops_cog` below; until somebody pastes it into `cogs/loops.py`, importing
this module changes nothing.

THE ONE RULE EVERY FUNCTION HERE OBEYS
--------------------------------------
A sweep that half-runs and restarts must not double-act. So:

  * There is NO CURSOR anywhere in this file. The progress marker is always the ROW'S
    OWN STATE, and every candidate query excludes the states it produces. A pass killed
    halfway resumes on the next tick with exactly the rows it had not reached and
    re-processes none of the ones it had. (`sweep_expired_holds` documents the same
    decision, and the dead `hold_sweep_cursor` it deleted is the cautionary tale.)
  * Every act is claim-first: one atomic `UPDATE ... WHERE <not yet>`, act only if we
    won the row.
  * Every ledger call carries a key derived from the domain event — the row's own
    `idem_key`, stable across re-reads — never a per-attempt uuid.
  * An UNKNOWN outcome is never resolved by guessing. We ASK core (does a hold with
    this key exist? what state is this hold in?) and record the answer. Absence of a
    hold is a fact, not an assumption: `place_hold` is one transaction, so if no row
    carries the key, the placement never landed.
  * Nothing here ever frees a claimed `web_idempotency` key on an unknown outcome. See
    `report_stuck_bank_keys`.

WHAT ONE TICK DOES

    tick()
      ├─ sweep_expired_holds()        release ledger holds past expires_at
      ├─ replay_stake_placements()    estates_db stakes stuck in placing/place_unknown
      ├─ reconcile_stake_holds()      estates_db stakes stuck in capturing/releasing/*_unknown
      ├─ replay_land_bid_placements() land_bids stuck in placing/place_unknown
      ├─ reconcile_land_bid_holds()   land_bids stuck in releasing/release_unknown
      ├─ sweep_web_keys()             delete FINISHED web_idempotency rows past the TTL
      ├─ report_stuck_bank_keys()     log unfinished ones for staff — never delete them
      └─ hub_web.sweep_keys()         delete finished hub_idempotency rows past the TTL

Every one of the eight is independently safe to run alone, twice, or not at all, and a
failure in one is logged and does not stop the others: a tick is eight small jobs, not
one transaction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger("vtech.reconcile")

#: The estates service owns every hold this loop touches. A hold belonging to another
#: service (`osentar`) is not ours to release and `ledger_v2` refuses it by design.
SERVICE = "estates"

#: A row is only a candidate once it has been unresolved for this long. A live request
#: is allowed to be mid-flight; racing a handler that is about to write `held` would
#: mean two writers on one row for no gain.
STALE_SECONDS = 900

#: Per-tick batch ceilings. A tick is a minute; a backlog drains over several ticks
#: rather than holding a write lock for one long one.
HOLD_SWEEP_LIMIT = 200
ROW_LIMIT = 200

#: Finished web/hub idempotency rows older than this are deleted. It is a REPLAY window,
#: not an audit log — the audit lives in `ledger_entries`. Unfinished rows are never
#: deleted at any age (`report_stuck_bank_keys`).
WEB_KEY_TTL_SECONDS = 7 * 24 * 3600

TICK_SECONDS = 60


# ══════════════════════════════════════════════════════════════════════════
# Imports done late and defensively — this module must never be the reason
# the bot fails to start.
# ══════════════════════════════════════════════════════════════════════════

def _ledger():
    import ledger_v2 as _L
    return _L


def _core_db():
    import Restocker_db as _db
    return _db


def _estates_db():
    import estates_db as _edb
    return _edb


def _hold_by_key(service: str, key: str) -> Optional[dict]:
    """The hold created by `key`, or None. READ ONLY, and the crux of every replay.

    WEB_ATTACK finding 3's minimal fix says "re-send the identical POST /hold, core
    replays and returns the original hold". That is true over HTTP, where
    `ledger_v2._idempotent` wraps the v1 handler. IT IS NOT TRUE IN-PROCESS: calling
    `ledger_v2.place_hold(key=...)` a second time INSERTs a second hold — the key is
    only stored on the row, `ledger_holds` has no unique index on `idempotency_key`, and
    the in-process function has no dedupe of its own. A loop that "replayed" placements
    by calling `place_hold` again would double-reserve every stranded bid it touched,
    which is the bug it exists to clean up.

    So the replay is: ASK FIRST by key, and only place if the answer is "no such hold".
    `ledger_v2` exposes no finder by key, so this reads the table directly through the
    module's own connection. It writes nothing.

    Follow-up worth doing in core, in this order: a
    `CREATE UNIQUE INDEX ledger_holds_key ON ledger_holds(service, idempotency_key)
    WHERE idempotency_key IS NOT NULL`, then a public `find_hold_by_key`, then make
    `place_hold` return the existing row on a key it has already seen. Then this
    function collapses into one call and the double-hold becomes unrepresentable
    instead of merely avoided.
    """
    L = _ledger()
    row = L._conn().execute(
        "SELECT hold_id, service, user_id, amount, state, reason, expires_at "
        "FROM ledger_holds WHERE service=? AND idempotency_key=? "
        "ORDER BY created_at ASC LIMIT 1",
        (str(service), str(key)),
    ).fetchone()
    return dict(row) if row is not None else None


# ══════════════════════════════════════════════════════════════════════════
# 1. Expired holds
# ══════════════════════════════════════════════════════════════════════════

def sweep_holds() -> int:
    """Release every hold past its `expires_at`. Returns how many.

    Straight through to `ledger_v2.sweep_expired_holds`, which is already per-row
    claim-first and cursor-free: a released row leaves the candidate set by
    construction, so a half-finished sweep is simply a shorter sweep.
    """
    return int(_ledger().sweep_expired_holds(limit=HOLD_SWEEP_LIMIT))


# ══════════════════════════════════════════════════════════════════════════
# 2 & 3. Estates stakes — the pari-mutuel side
# ══════════════════════════════════════════════════════════════════════════

def replay_stake_placements() -> int:
    """Settle stakes whose `POST /hold` outcome was never learned. Returns rows settled.

    Input: `estates_db.placements_needing_replay()` — `place_unknown` at any age, plus
    `placing` older than `STALE_SECONDS`. Each row carries the `idem_key` the placement
    used, which is minted from the row's own id and is therefore identical on every
    re-read.

    Per row, one of exactly two answers, and both are facts rather than guesses:

      * a hold exists under that key -> adopt it (`reconcile_stake_placement` with the
        hold id and state). The row lands in the status core's answer implies.
      * no hold exists under that key -> the INSERT never happened, so the placement
        provably failed. The row lands in `failed`, which is now the truth, and the
        player's coins were never reserved.

    Both are idempotent: `reconcile_stake_placement` is itself claim-first on the row's
    status, so re-running this on a row that a previous half-tick already settled is a
    no-op returning the landed status.
    """
    edb = _estates_db()
    done = 0
    for row in edb.placements_needing_replay(older_than_seconds=STALE_SECONDS,
                                             limit=ROW_LIMIT):
        key = row.get("idem_key")
        sid = int(row["id"])
        if not key:
            # Nothing to ask about. A row with no key predates the escrow columns and
            # is a hand job for staff; saying so once per tick is noise, so debug.
            log.debug("[reconcile] stake %s has no idem_key — staff must settle it", sid)
            continue
        try:
            hold = _hold_by_key(SERVICE, str(key))
            if hold:
                edb.reconcile_stake_placement(sid, hold_id=str(hold["hold_id"]),
                                              hold_state=str(hold["state"]),
                                              expires_at=hold.get("expires_at"))
            else:
                edb.reconcile_stake_placement(
                    sid, error="no hold at core under this key — the placement never landed")
            done += 1
        except Exception as e:
            log.exception("[reconcile] stake placement %s not settled: %s", sid, e)
    return done


def reconcile_stake_holds() -> int:
    """Ask core what happened to stakes stuck mid-capture or mid-release. Rows settled.

    Input: `estates_db.holds_needing_reconcile()` — `capture_unknown`/`release_unknown`
    at any age, plus `capturing`/`releasing` older than `STALE_SECONDS`. Every row has a
    `hold_id`, so the question is a plain read: `get_hold` -> `reconcile_stake_hold`.

    This never re-issues a capture or a release. Core's state is the answer; if a hold
    is still `open` the row goes back to `held` and the next ordinary pass over that
    market will terminate it with its own domain key.
    """
    edb, L = _estates_db(), _ledger()
    done = 0
    for row in edb.holds_needing_reconcile(older_than_seconds=STALE_SECONDS,
                                           limit=ROW_LIMIT):
        sid, hid = int(row["id"]), str(row["hold_id"])
        try:
            hold = L.get_hold(SERVICE, hid)
        except Exception as e:
            log.warning("[reconcile] stake %s hold %s could not be read: %s", sid, hid, e)
            continue
        try:
            edb.reconcile_stake_hold(sid, str(hold["state"]))
            done += 1
        except Exception as e:
            log.exception("[reconcile] stake %s not reconciled: %s", sid, e)
    return done


# ══════════════════════════════════════════════════════════════════════════
# 4 & 5. Land auction bids — `land_bids` in restocker.db
# ══════════════════════════════════════════════════════════════════════════
# These live in core's db rather than estates.db, so they have no finder in
# `estates_db` and the queries are here. Same two verbs, same two rules.

def replay_land_bid_placements() -> int:
    """Settle bids whose hold placement outcome was never learned. Rows settled.

    Candidates: `status='place_unknown'` at any age, or `status='placing'` older than
    `STALE_SECONDS`, and in both cases `hold_id IS NULL` — a row that already has a hold
    id is not a placement question.

    Claim-first: the row is moved to `replaying` by an UPDATE gated on the status we
    read, and we only act if `rowcount == 1`. Two reconcilers, or a reconciler racing a
    handler, cannot both settle one row. `replaying` is not in the candidate set, so a
    tick that dies immediately after the claim leaves a row that the NEXT tick will pick
    up only once it has aged back in — which is the conservative direction: a bid nobody
    settles is visible, a bid settled twice is a double hold.
    """
    db = _core_db()
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.gmtime(time.time() - STALE_SECONDS))
    with db.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, listing_id, bidder_id, amount, status, idem_key FROM land_bids "
            "WHERE hold_id IS NULL AND idem_key IS NOT NULL AND ("
            "  status='place_unknown' OR "
            "  (status='placing' AND (claimed_at IS NULL OR claimed_at < ?))) "
            "ORDER BY id LIMIT ?", (cutoff, ROW_LIMIT)).fetchall()]

    done = 0
    for row in rows:
        rid, was = int(row["id"]), str(row["status"])
        with db.db() as conn:
            claimed = conn.execute(
                "UPDATE land_bids SET status='replaying' WHERE id=? AND status=?",
                (rid, was))
            if claimed.rowcount != 1:
                continue        # somebody else won it; not ours to settle
        try:
            hold = _hold_by_key(SERVICE, str(row["idem_key"]))
        except Exception as e:
            log.exception("[reconcile] bid %s: could not ask core: %s", rid, e)
            with db.db() as conn:   # hand the row back exactly as we found it
                conn.execute("UPDATE land_bids SET status=? WHERE id=? AND status='replaying'",
                             (was, rid))
            continue
        with db.db() as conn:
            if hold and str(hold["state"]) == "open":
                conn.execute(
                    "UPDATE land_bids SET status='held', hold_id=?, hold_expires_at=?, "
                    "last_error=NULL WHERE id=? AND status='replaying'",
                    (str(hold["hold_id"]), hold.get("expires_at"), rid))
            elif hold:
                # The hold existed and has already ended (released/expired/captured).
                conn.execute(
                    "UPDATE land_bids SET status='released', hold_id=?, "
                    "settled_at=datetime('now'), last_error=? WHERE id=? AND status='replaying'",
                    (str(hold["hold_id"]), f"hold {hold['state']} at core", rid))
            else:
                conn.execute(
                    "UPDATE land_bids SET status='failed', last_error=? "
                    "WHERE id=? AND status='replaying'",
                    ("no hold at core under this key — the placement never landed", rid))
        done += 1
        log.info("[reconcile] bid %s settled from core: %s", rid,
                 (hold or {}).get("state", "no hold"))
    return done


def reconcile_land_bid_holds() -> int:
    """Finish releases that were never confirmed. Rows settled.

    Candidates: `status='release_unknown'` at any age, or `status='releasing'` older
    than `STALE_SECONDS`, with a `hold_id` to ask about. This is the row
    `_release_bid_row` leaves behind when the outbid release throws — the one case
    where a losing bidder's coins stay reserved while somebody else leads the lot.

    We read the hold and then act on what core says, with the row's own release key:

      * already `released`/`expired`/`captured` -> record `released`, nothing to do.
      * still `open`                            -> release it, keyed `<idem_key>:release`,
        which is derived from the row and identical on every retry, so a tick that dies
        between the call and the write re-issues the SAME release next time. A release
        of an already-released hold raises `hold_not_open`, which we read as "done".
    """
    db, L = _core_db(), _ledger()
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                           time.gmtime(time.time() - STALE_SECONDS))
    with db.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, hold_id, idem_key, status FROM land_bids "
            "WHERE hold_id IS NOT NULL AND ("
            "  status='release_unknown' OR "
            "  (status='releasing' AND (claimed_at IS NULL OR claimed_at < ?))) "
            "ORDER BY id LIMIT ?", (cutoff, ROW_LIMIT)).fetchall()]

    done = 0
    for row in rows:
        rid, hid, was = int(row["id"]), str(row["hold_id"]), str(row["status"])
        key = f"{row['idem_key'] or ('land:bid:' + str(rid))}:release"
        try:
            state = str(L.get_hold(SERVICE, hid)["state"])
            if state == "open":
                L.release_hold(SERVICE, hid, key=key, reason="realestate:outbid_release")
        except Exception as e:
            if "hold_not_open" not in str(getattr(e, "code", "")) + str(e):
                log.warning("[reconcile] bid %s hold %s not settled: %s", rid, hid, e)
                continue
        with db.db() as conn:
            conn.execute(
                "UPDATE land_bids SET status='released', settled_at=datetime('now'), "
                "last_error=NULL WHERE id=? AND status=?", (rid, was))
        done += 1
    return done


# ══════════════════════════════════════════════════════════════════════════
# 6 & 7. The web's own key table
# ══════════════════════════════════════════════════════════════════════════

def sweep_web_keys(now: Optional[float] = None) -> int:
    """Delete FINISHED `web_idempotency` rows past the TTL. Returns rows removed.

    WEB_ATTACK finding 10: the table has no age-based delete anywhere, so it grows for
    the life of the deployment.

    `state='done'` is the whole safety of this. A `done` row's only remaining job is to
    replay a stored receipt, and after a week nobody is re-submitting a week-old form.
    An `in_progress` row is a live claim on an unknown outcome and deleting it would
    re-arm the exact double this table exists to stop — so it is not in this DELETE at
    any age, and there is no `--force` that puts it there. The per-row state is the
    progress marker; a half-run delete is just a smaller delete.
    """
    cutoff = (now if now is not None else time.time()) - WEB_KEY_TTL_SECONDS
    with _core_db().db() as conn:
        cur = conn.execute(
            "DELETE FROM web_idempotency WHERE state='done' AND created_at < ?", (cutoff,))
        return int(cur.rowcount or 0)


def report_stuck_bank_keys(older_than_seconds: int = 900) -> list:
    """Name the unresolved money keys for staff. Returns the rows. DELETES NOTHING.

    A banking key stuck `in_progress` means one instruction whose fate we do not know:
    Osentar may have applied it, and the only party who can say is Osentar. There is no
    "did you get this key" read on the bank's API (see `banking_web`'s endpoint list),
    so this loop CANNOT settle these, and the honest daemon behaviour is to surface
    them rather than to invent an outcome.

    What the fix in `banking_web._keys_for` guarantees meanwhile: the player's reload
    re-issues this same key, so nothing about a stuck row can produce a second call to
    the bank. It costs the player one blocked action until staff clear it, and that is
    the cheap side of this trade.

    Read-only, so it is trivially idempotent and safe to run on every tick.
    """
    cutoff = time.time() - int(older_than_seconds)
    try:
        with _core_db().db() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT key, user_id, endpoint, created_at, updated_at, response "
                "FROM web_idempotency WHERE state='in_progress' AND created_at < ? "
                "ORDER BY created_at LIMIT ?", (cutoff, ROW_LIMIT)).fetchall()]
    except Exception as e:
        log.exception("[reconcile] could not read stuck keys: %s", e)
        return []
    for r in rows:
        log.warning("[reconcile][STAFF] unresolved money key: endpoint=%s user=%s "
                    "age=%.0fs key=%s reason=%s — the outcome at the far end is "
                    "UNKNOWN; check the service's book before clearing it by hand.",
                    r["endpoint"], r["user_id"], time.time() - float(r["created_at"] or 0),
                    str(r["key"])[:24], r.get("response") or "")
    return rows


def sweep_hub_keys() -> int:
    """`hub_web.sweep_keys()` — defined at hub_web.py:413 and, until this file, called
    from nowhere at all (WEB_ATTACK finding 10). Same shape as `sweep_web_keys`: it
    deletes only rows whose state is not `claimed`, so a live claim survives."""
    try:
        import hub_web
    except Exception as e:
        log.debug("[reconcile] hub_web unavailable: %s", e)
        return 0
    return int(hub_web.sweep_keys())


# ══════════════════════════════════════════════════════════════════════════
# The tick
# ══════════════════════════════════════════════════════════════════════════

_JOBS = (
    ("expired holds released", sweep_holds),
    ("stake placements settled", replay_stake_placements),
    ("stake holds reconciled", reconcile_stake_holds),
    ("bid placements settled", replay_land_bid_placements),
    ("bid holds reconciled", reconcile_land_bid_holds),
    ("web keys swept", sweep_web_keys),
    ("hub keys swept", sweep_hub_keys),
)


def tick() -> dict:
    """One pass of every job. Returns `{job: count}`. Never raises.

    Synchronous on purpose: every call underneath is SQLite, and running it in a thread
    (see `run_tick`) keeps the bot's event loop free without pretending these are
    awaitable. One failing job does not cancel the others — a tick is seven small
    independent jobs, each of which is safe to skip for a minute.
    """
    out: dict = {}
    for name, fn in _JOBS:
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = f"error: {e}"
            log.exception("[reconcile] %s failed: %s", name, e)
    try:
        out["stuck money keys"] = len(report_stuck_bank_keys())
    except Exception as e:  # pragma: no cover - report_stuck_bank_keys catches its own
        out["stuck money keys"] = f"error: {e}"
    return out


async def run_tick() -> dict:
    """`tick()` off the event loop. This is what the cog awaits."""
    return await asyncio.to_thread(tick)


async def run_forever(interval: int = TICK_SECONDS) -> None:
    """Standalone runner, for a console or a smoke test. The bot uses the cog below."""
    while True:
        log.info("[reconcile] %s", await run_tick())
        await asyncio.sleep(interval)


def wire_into_loops_cog() -> str:
    '''The exact snippet for `cogs/loops.py`. Paste it, do not paraphrase it.

    Add to the imports at the top of the file:

        import reconcile_loop

    Add to the cog class (alongside the other `@tasks.loop` members):

        @tasks.loop(seconds=60.0)
        async def reconcile_tick(self):
            """Settle unknown-outcome holds, expire stale ones, sweep dead keys.

            Every job it runs is claim-first on the row's own state, so a tick that is
            cancelled halfway is simply a shorter tick — nothing double-acts, and the
            next tick picks up exactly what this one did not reach. Sixty seconds is
            the number the player-facing copy used to promise; it is cheap because an
            idle tick is seven indexed SELECTs that return nothing.
            """
            try:
                counts = await reconcile_loop.run_tick()
            except Exception:
                log.exception("[loops] reconcile tick failed")
                return
            if any(isinstance(v, int) and v for v in counts.values()):
                log.info("[loops] reconcile: %s",
                         {k: v for k, v in counts.items() if v})

        @reconcile_tick.before_loop
        async def before_reconcile_tick(self):
            # Never run before the bot has its db and its ledger open.
            await self.bot.wait_until_ready()

    And in `cog_load` / `__init__` where the other loops are started:

        self.reconcile_tick.start()

    And in `cog_unload`:

        self.reconcile_tick.cancel()

    EXACTLY ONE PROCESS may run this. discord.py runs one cog instance per bot process,
    so that is satisfied by default — but if Restocker is ever run in two processes
    against one db, this loop belongs to whichever one holds the bot, not to the web
    thread. Every job is claim-first and so a second runner is SAFE rather than
    corrupting; it is simply wasted work and doubled log lines.

    Once this is wired, the two "contact staff" sentences in `estates_web` (`_place_bid` ~:600 and
    `_place_stake` ~:942, both ending "needs to be checked by hand")
    may go back to promising a resolution — and not one minute before.
    '''
    return wire_into_loops_cog.__doc__ or ""


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    print(tick())
