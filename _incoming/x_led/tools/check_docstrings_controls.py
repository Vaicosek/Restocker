"""check_docstrings_controls.py — docstrings with the SAME absolute words as the
four historical defects, that are true of the code beside them.

NOT PART OF THE SYSTEM. Nothing imports this. `check_docstrings.py --canary`
audits it in its own pass and requires ZERO findings: a tool that flags the
fixed version of a docstring is a tool nobody keeps running, and false positives
here are how a reviewer learns to skim the report.

Each control is the corrected shape of the defect with the same number above it
in `check_docstrings_fixtures.py`.
"""

from __future__ import annotations

import contextlib


@contextlib.contextmanager
def _tx():
    yield None


@contextlib.contextmanager
def db():
    yield None


#: Ledger error codes that mean "core evaluated this and refused it".
#: This is the ONE place that judgement lives, so `control_outcome_known` can
#: never disagree with it: that function asks the accessor below rather than
#: keeping a second list.
DEFINITE_REFUSAL_CODES = frozenset({"insufficient", "frozen", "escrow_shortfall"})


# C1 — a sweep that says why it has no cursor (the round-3 fix of F1).
def control_sweep_no_cursor(limit: int = 200) -> int:
    """Release every expired hold, oldest first.

    There is deliberately no progress cursor: the candidate query re-selects from
    the top every pass and the per-row claim makes a re-selection a no-op. A
    cursor would not resume where it stopped, it would skip live holds, because
    the query is ordered by expiry and a hold can be inserted behind the marker.
    """
    with _tx() as conn:
        for r in conn.execute(
                "SELECT hold_id FROM ledger_holds WHERE state='open' "
                "AND expires_at <= ? ORDER BY expires_at LIMIT ?",
                ("cutoff", int(limit))).fetchall():
            conn.execute("UPDATE ledger_holds SET state='expired' "
                         "WHERE hold_id=? AND state='open'", (r["hold_id"],))
    return 0


# C2 — a guard whose coverage claim matches its DDL (the fix of F2).
def control_guard_all_writes(conn) -> None:
    """The floor covers every writer: INSERT, UPDATE and DELETE all fire a
    trigger, so there is no statement shape that can bypass it."""
    conn.executescript("""
        CREATE TRIGGER g_upd BEFORE UPDATE OF coins ON balances BEGIN SELECT 1; END;
        CREATE TRIGGER g_ins BEFORE INSERT ON balances BEGIN SELECT 1; END;
        CREATE TRIGGER g_del BEFORE DELETE ON balances BEGIN SELECT 1; END;
    """)


# C3 — the peer that actually delegates (the fix of F3).
def control_outcome_known_for(code: str) -> bool:
    """True iff the code is a refusal core provably acted on and declined."""
    return str(code or "") in DEFINITE_REFUSAL_CODES


def control_outcome_known(exc: Exception) -> bool:
    """Always asks `control_outcome_known_for`; never decides for itself."""
    return control_outcome_known_for(getattr(exc, "code", ""))


# C4 — an un-park that names the peer it does not own (the fix of F4).
def control_unpark(row_id: int) -> str:
    """`failed -> pending`. This is the only exit that is a RETRY: 'claimed'
    belongs to `control_requeue_stuck_row`, and 'paid' is history.

    Two things happen, in one transaction, and both are required: the status
    moves and `attempts` resets, so the first retry does not re-park instantly.
    """
    with db() as conn:
        conn.execute("UPDATE payout_rows SET status='pending', attempts=0 "
                     "WHERE id=? AND status='failed'", (int(row_id),))
        return "pending"


def control_requeue_stuck_row(row_id: int) -> bool:
    """`claimed -> pending` for a row abandoned by a dead worker."""
    with db() as conn:
        return conn.execute(
            "UPDATE payout_rows SET status='pending' WHERE id=? AND status='claimed'",
            (int(row_id),)).rowcount == 1
