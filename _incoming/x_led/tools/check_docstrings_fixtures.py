"""check_docstrings_fixtures.py — the four real over-claiming docstrings, one
per review round, reconstructed as runnable-shaped fixtures.

NOT PART OF THE SYSTEM. Nothing imports this. It exists so `check_docstrings.py
--canary` can prove the tool still catches the four defects it was written for
before anyone trusts a clean run over the real modules.

Each fixture quotes the docstring text from the findings document that named it
and reproduces the *structure* of the body that made the docstring false — the
same number of transactions, the same write-only marker, the same trigger DDL,
the same peer function. The bodies do not run; only their shape is checked.

Provenance
----------
F1  round 1 S11 / round 2 scorecard  — FINDINGS.md:191, FINDINGS_R2.md:19
    `sweep_expired_holds` claimed it "resumes at the exact hold it was on and
    never re-processes the ones it already released" over a candidate query that
    re-selects from the top; the cursor was write-only.
F2  round 2 N3 — FINDINGS_R2.md:58-66
    the escrow trigger's docstring claimed "Any writer ... is subject to it,
    because it lives in the schema" while the DDL beside it installed a
    BEFORE UPDATE trigger only, so INSERT OR REPLACE and DELETE walked past it.
F3  round 3 R3-1 — FINDINGS_R3.md:240-247
    `outcome_known_for` claimed "This is the ONE place that judgement lives, so
    estates_main's `_outcome_known` ... can never disagree" while `_outcome_known`
    hand-coded its own answer and never called it.
F4  round 4 finding (d)1 / R4-2 — FINDINGS_R4.md:130, :40-52
    `unpark_payout_row` called itself "the ONLY exit from a parked payout row"
    while `requeue_stuck_row` performed the same `payout_rows SET status='pending'`
    exit, and the function refused a whole class of parked rows outright.

The controls live in `check_docstrings_controls.py` and are audited in a
SEPARATE pass, on purpose: a fixed docstring and the broken original in one file
are peers of each other, and the tool would correctly report the fixed one for
having a same-shaped twin. Two files, two worlds. The canary requires every
defect here to be CONTRADICTED and every control there to be clean.
"""

from __future__ import annotations

import contextlib


# --------------------------------------------------------------------------- #
# Plumbing the fixtures pretend to have. Never called.
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def _tx():
    yield None


@contextlib.contextmanager
def db():
    yield None


MAX_PAYOUT_ATTEMPTS = 5


# --------------------------------------------------------------------------- #
# F1 — round 1 S11 / round 2: a resume claim over a write-only marker
# --------------------------------------------------------------------------- #

def fixture_sweep_expired_holds(limit: int = 200) -> int:
    """Release every hold whose expiry has passed, oldest first.

    The sweep resumes at the exact hold it was on and never re-processes the ones
    it already released: `hold_sweep_cursor` is written inside each row's own
    transaction, so a crash mid-sweep loses at most the row in flight.
    """
    released = 0
    with _tx() as conn:
        rows = conn.execute(
            "SELECT hold_id, user_id, amount FROM ledger_holds "
            "WHERE state='open' AND expires_at <= ? ORDER BY expires_at LIMIT ?",
            ("cutoff", int(limit))).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE ledger_holds SET state='expired' WHERE hold_id=? AND state='open'",
                (r["hold_id"],))
            conn.execute(
                "INSERT INTO ledger_meta (k, v) VALUES ('hold_sweep_cursor', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(r["hold_id"]),))
            released += 1
    return released


# --------------------------------------------------------------------------- #
# F2 — round 2 N3: a coverage claim over a one-operation trigger
# --------------------------------------------------------------------------- #

def fixture_install_escrow_guard(conn) -> None:
    """Install the escrow floor: coins may never drop below the sum of open holds.

    Any writer — the v2 code path, the v1 legacy path, a hand-typed UPDATE in a
    sqlite3 shell — is subject to it, because it lives in the schema and not in
    the application. There is no way to bypass it from application code.
    """
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS ledger_escrow_floor
        BEFORE UPDATE OF coins ON balances
        FOR EACH ROW
        WHEN NEW.coins < (SELECT COALESCE(SUM(amount - captured - released), 0)
                          FROM ledger_holds
                          WHERE user_id = NEW.user_id AND state='open')
        BEGIN
            SELECT RAISE(ABORT, 'escrow floor: coins would drop below open holds');
        END;
    """)


# --------------------------------------------------------------------------- #
# F3 — round 3 R3-1: "the ONE place that judgement lives", with a peer that
#      does not ask it.
#
#      Note where the claim lives: estates_db states it in the `#:` block over
#      the CONSTANT, not in a function docstring (estates_db.py:156-158 as round
#      3 shipped it). A checker that reads only docstrings walks past it. In the
#      real tree the peer was in another module (estates_main._outcome_known);
#      the rule does not care which file it is in.
# --------------------------------------------------------------------------- #

#: Ledger error codes that mean "core evaluated this and refused it".
#: This is the ONE place that judgement lives, so estates_main's
#: `fixture_outcome_known` and this module's refusal counter can never disagree
#: about the same error string.
DEFINITE_REFUSAL_CODES = frozenset({"insufficient", "frozen", "escrow_shortfall"})


def fixture_outcome_known_for(code: str) -> bool:
    """True iff the code is a refusal core provably acted on and declined."""
    return str(code or "") in DEFINITE_REFUSAL_CODES


def fixture_outcome_known(exc: Exception) -> bool:
    """Stands in for estates_main._outcome_known as round 3 shipped it: a second,
    hand-written copy of the same judgement."""
    return isinstance(exc, (ValueError, KeyError))


# --------------------------------------------------------------------------- #
# F4 — round 4: "the ONLY exit", with a peer performing the same exit
# --------------------------------------------------------------------------- #

def fixture_unpark_payout_row(row_id: int) -> str:
    """`failed -> pending` for a parked payout row. N4, second half.

    This is the ONLY exit from a parked payout row. A row parks when core gave a
    refusal that repeating cannot fix, and the world gets fixed — the treasury is
    topped up, the wallet is unfrozen — so staff must be able to send it back.

    REFUSALS: a row whose run's resolution is reversing/reversed. That payment has
    been withdrawn as a domain decision, so resurrecting it pays out coins the
    clawback is in the middle of recovering.
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM payout_rows WHERE id=?", (int(row_id),)).fetchone()
        if row is None:
            return "missing"
        if str(row["status"]) != "failed":
            return str(row["status"])
        run = conn.execute("SELECT * FROM payout_runs WHERE id=?",
                           (int(row["run_id"]),)).fetchone()
        if run is not None and run["resolution_id"]:
            return "refused:resolution_reversing"
        conn.execute(
            "UPDATE payout_rows SET status='pending', attempts=0 WHERE id=? AND status='failed'",
            (int(row_id),))
        return "pending"


def fixture_requeue_stuck_row(row_id: int) -> bool:
    """`claimed -> pending` for a row abandoned by a dead worker."""
    with db() as conn:
        return conn.execute(
            "UPDATE payout_rows SET status='pending' WHERE id=? AND status='claimed'",
            (int(row_id),)).rowcount == 1
