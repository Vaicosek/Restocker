"""The markets engine's money paths must move coins INSIDE ONE TRANSACTION.

THE SENTENCE THIS FILE MAKES CHECKABLE
--------------------------------------
The markets verification round closed with one mechanically-checkable claim:

    No money path in the markets engine ever passes a `conn`: `Restocker_main.py`
    uses `adjust_balance_tx` zero times and all 14 of its `adjust_treasury` calls
    omit `conn=`, so every coin movement in the engine is two or more
    independently-committing statements with no durable marker between them.

Four separate findings were that sentence seen from four angles — an unfunded
sell that MINTED the proceeds, a crashed dividend that paid 60 of 200 holders
twice and minted 300,000 coins, a process death between a holding write and a
credit that DESTROYED 9,653 coins with nothing recording a credit was owed, and a
partial dividend recorded as complete. Each had a per-bug patch. None of the
patches could close the class, because every one of them was compensation — a
further write, in a further transaction, for the same crash to land in — and each
patch only moved the seam somewhere else in the same function.

So the fix was structural, and this file is what stops it eroding. Compensation
code is easy to add back one line at a time and it always looks locally correct;
what it cannot do is survive `os._exit(9)`. This test does not care whether the
code looks careful. It walks the AST and asserts that inside the functions that
move real coins, every money call is joined to the caller's transaction.

The project has done exactly this twice before and both caught real regressions
the round they landed: `test_conditional_settlers.py` for the bank, and the AST
contract test for the land exchange. This is the markets engine's copy.

WHAT IT CHECKS
--------------
1. `adjust_balance_tx` is used at all (the sentence's headline: "zero times").
2. Inside every CLOSED function, calls to `adjust_treasury`, `adjust_holding`,
   `adjust_bond_holding`, `adjust_etf_units`, `log_stock_trade`,
   `dividend_leg_claim` and `dividend_leg_settle` pass `conn=`.
3. Inside every CLOSED function, wallet movement goes through
   `adjust_balance_tx(conn, ...)` — never bare `add_coins` / `deduct_coins`,
   which are two commits by construction.
4. Every `adjust_balance_tx` call passes a non-empty `reason=`. A ledger row that
   commits with the money is worth nothing if it says nothing; the engine had 31
   unlabelled rows, 15 of them withdrawals totalling 855,605 coins.
5. The `mk:` reasons are derived from a durable row (they interpolate one), so
   the partial UNIQUE index can actually cover them.
6. OPEN functions are listed explicitly, each with a reason. That list is the
   frontier, in the file, rather than in someone's memory — and a money path that
   is neither closed nor listed fails the test, so a NEW money path cannot be
   added silently to either state.

7. THE PRIMITIVES UNDERNEATH ARE CLAIM-FIRST, CHECKED ON SQL SHAPE. Sections
   1-6 check ARGUMENT PRESENCE, and that is not the property. They read 60/60
   over an `adjust_treasury` that was still `SELECT` -> compute -> absolute
   `UPDATE`: two concurrent sells both read "fully funded", both were paid,
   9,653 coins minted, no error. So section 7 derives the class of money
   primitives from the CLOSED and OPEN lists above, derives the vocabulary of
   accumulator columns from the module's own SQL, and asks of every one of them
   whether the new value is computed FROM THE ROW or from a SELECT somebody
   already ran. Section 7b asks the other half — is the conditional write's own
   `rowcount` read, on its own path.
8. THE CALLER READS THE ANSWER. A debit can apply LESS than it was asked for.
   Ignoring the figure it returns and then crediting the full amount on the
   other side is a mint; this found three live ones.
9. ONE DEFINITION PER NAME, because a shadowed definition is the one defect
   where reading the code actively misleads you.

Sections 7-9 exist because the verifier graded the first version of this file:
"it checks argument presence, not SQL shape... its enforcement test measures the
thing that is easy to measure rather than the thing that matters." Every check
in them is mutation-proven ONE AT A TIME, and each mutation must produce the
failure that NAMES it, so no guard can be credited for another's red:

    python3 tests/test_money_tx_contract.py
    python3 ../../build/mutate_money_contract.py    # the test of the test
"""
from __future__ import annotations

import ast
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MAIN = os.path.join(ROOT, "Restocker_main.py")

# ── Calls that MOVE VALUE. Inside a closed function these must ALWAYS carry the
#: caller's transaction, wherever they appear — a value move outside the `with`
#: block is exactly the seam this contract exists to forbid.
MUST_BE_IN_TX = {
    "adjust_treasury",
    "adjust_holding",
    "adjust_bond_holding",
    "adjust_etf_units",
    "adjust_balance",       # the non-transactional form; must not appear at all
    "log_stock_trade",
}

#: Calls that RECORD an outcome. These must carry the transaction when they are
#: lexically inside a `with db()` block — a marker that commits separately from
#: the money it describes is the original defect. They are allowed BARE outside
#: one, because that is how a rolled-back attempt writes down what it learned
#: ("this holder already had the credit", "the outcome is unreadable"), and that
#: write is correctly its own transaction: the first one no longer exists.
MARKER_CALLS = {
    "dividend_leg_claim",
    "dividend_leg_settle",
    "investor_leg_claim",
    "investor_leg_settle",
    "add_investor_payout",
    "update_bond",
}

#: Wallet movement that is TWO COMMITS BY CONSTRUCTION. `add_coins` calls
#: `adjust_balance` and then `record_coin_ledger` in a second `with db()` block
#: documented "best-effort: never raises" and wrapped in `except Exception: pass`.
#: There is no argument you can pass to make it atomic; inside a closed function
#: it must be `adjust_balance_tx`.
BANNED_IN_CLOSED = {"add_coins", "deduct_coins"}

#: Functions whose money is closed. Adding a money call here without a `conn`
#: fails this test.
CLOSED = {
    "_do_stock_trade": "buy and sell: treasury, holding, wallet, trade log",
    "_execute_dividend_run": "per-leg: claim marker, treasury, credit, receipt",
    "_pay_bondholder": "bond coupon and principal: treasury then credit",
    "_do_bond_buy": "bond purchase: debit, treasury, units",
    "_pay_manager_override": "override: treasury then credit",
    "_pay_manager_sales_override": "sales override: treasury then credit",
    "_etf_invest": "investor -> fund transfer leg (the basket buys are separate trades)",
    "_etf_redeem": "fund -> investor payout and the unit burn",
    "_liquidate_holdings": "admin force-sale: the holder -> recipient transfer leg",
    "_transfer_coins_tx":
        "wallet -> wallet, both legs in one transaction. This is what "
        "`bank_api.h_transfer` now calls instead of `deduct_coins` on one trip to "
        "the bot loop and `add_coins` on a second with a compensating refund.",
    "_distribute_investor_profit":
        "GEX.PR investor profit share, per investor: claim marker, credit, receipt. "
        "NOTE: closed for ATOMICITY only. This path still credits investors with no "
        "debit anywhere — whether that is intended issuance or should come out of the "
        "treasury is the owner's open question, and it is not what this file checks.",
}

#: Money paths deliberately NOT closed this round, each with the reason. This is
#: the frontier, written down. A money-moving function that is in neither dict
#: fails the test — so the next person cannot add one and leave it unclassified.
OPEN = {
    "_skim_insurance":
        "treasury -> a config-key insurance pot, not a wallet. Coin-conserving "
        "between two non-wallet pots; a failure leaves the coins in the treasury, "
        "which is the safe direction. Worth closing when the insurance fund stops "
        "being a config key.",
    "_pay_honey_from_export":
        "OWNED BY THE STANDING-SPLIT WORKFLOW (vtech-ironcrest-r2). Not touched, "
        "by agreement, to avoid two agents reverting each other.",
    "_pay_honey_harvesters":
        "OWNED BY THE STANDING-SPLIT WORKFLOW (vtech-ironcrest-r2). Same.",
    "apply_weekly_interest":
        "whole-table wallet interest sweep, outside the markets engine's money "
        "paths; it mints by design and has its own week marker.",
    "_drip_reinvest":
        "moves no coins itself — it calls `_do_stock_trade`, which is closed.",
    "_charge_futures_upfront":
        "consignment futures. Adjacent to the standing-split workflow's territory "
        "(vtech-ironcrest-r2 is wiring split paths in this tree) — left alone this "
        "round rather than risk two agents editing the same seam.",
    "_settle_project":
        "project settlement. Same reason as `_charge_futures_upfront`: consignment "
        "money, and the round that closes it should close both legs at once.",
    "_refund_project":
        "the refund leg of the same pair. Closing one leg and not the other is how "
        "a settlement and its reversal end up disagreeing.",
    "_ai_tool_bill_customer":
        "the AI assistant's billing tool. Small amounts, staff-triggered, and it "
        "has no second write to be atomic WITH — it is a single credit. Worth "
        "converting for the ledger row alone, not for atomicity.",
    "add_coins":
        "THIS IS THE NON-TRANSACTIONAL PRIMITIVE ITSELF, not a caller of it. It is "
        "deliberately kept: plenty of callers outside the markets engine only want "
        "an audit line, and `record_coin_ledger`'s best-effort behaviour is right "
        "for them. What is forbidden is reaching for it inside a CLOSED function.",
    "deduct_coins":
        "the debit half of the same primitive, kept for the same reason.",
}


#: ── SECTIONS 7-9's SUBJECT: the PRIMITIVES the sections above stand on. ────
#:
#: Sections 1-6 check that money calls carry a `conn`. That is ARGUMENT
#: PRESENCE. It is easy to check, and it certified "the sentence holds — 60/60"
#: over an `adjust_treasury` that was still `SELECT` -> compute -> absolute
#: `UPDATE`: two concurrent sells both read "fully funded", both were paid,
#: 9,653 coins minted, no error. Atomicity of a group of statements is worth
#: nothing if one of the statements is a lost update. A test that measures the
#: easy thing instead of the thing that matters reads as evidence, so it is
#: worse than no test.
#:
#: THE FIRST ANSWER TO THAT WAS ITSELF THE EASY THING, one level down. It was a
#: hand-written dict of nine `(module, function) -> (money_column, rowcount)`
#: entries. Three ways that could not fail, all of them the shapes the bank's
#: `test_conditional_settlers.py` was caught by twice and fixed:
#:
#:   * THE CLASS WAS TYPED IN. A primitive added next month is checked by
#:     nobody, and one that leaves the money paths sits in the list for ever
#:     looking like coverage. `adjust_config_number` had to be hand-added the
#:     day it was written, and `upsert_market_shares` — which writes
#:     `treasury_coins` absolutely, from a caller's stale read — was never in
#:     the list at all. Now the class is DERIVED: the db functions the CLOSED
#:     and OPEN money paths call, transitively, that write durable state.
#:   * THE COLUMN WAS TYPED IN, one per function, so a primitive that writes
#:     two quantity columns was checked on one of them (`claim_holding_tx`
#:     writes `shares` AND `cost_basis`). The vocabulary is now DERIVED from the
#:     module: a column that is anywhere written in terms of itself with
#:     arithmetic (`coins = coins + ?`, `MAX(0, coins - ?)`) is an accumulator,
#:     and an accumulator written absolutely is a lost update wherever it
#:     appears. 18 columns, from `treasury_coins` to `units_sold`, none typed in.
#:   * `"rowcount" in ast.dump(fn)` — the bank's W1 mistake, VERBATIM. A
#:     substring over a dumped tree is satisfied by a docstring, by a variable
#:     called `rowcount_note`, by a read on a DIFFERENT statement's cursor, and
#:     by a read in the `if` arm answering for a claim taken in the `else`. It
#:     is one boolean for a whole function against a per-STATEMENT question.
#:     `_analyse` below replaces it, ported from the bank's: a claim is PENDING
#:     against the cursor name it was bound to and is only answered when an
#:     `ast.Attribute` named `rowcount` on THAT cursor is evaluated on THAT path.
#:
#: The one thing NOT borrowed is the bank's rule itself. Its writers take a row
#: by state (`WHERE status='claimed'`); ours move a QUANTITY, and the shape that
#: conserves coins is the relative UPDATE. So section 7 asks the question this
#: component's money actually turns on — is the new value computed from the row
#: or from a SELECT this function already ran — and section 7b asks the bank's
#: question about the conditional writes we do have.

#: Money paths in `Restocker_main.py` are the roots of the class: `CLOSED` and
#: `OPEN` above are exactly "the functions that move coins", already argued over
#: and already audited in both directions by section 6. Deriving from them means
#: a primitive JOINS the class the moment a money path calls it, and LEAVES when
#: the last one stops — neither transition needs anyone to remember this file.
#: Expanded transitively on BOTH sides: through `Restocker_main`'s own helpers
#: (`_skim_insurance` -> `_add_insurance_fund` -> `adjust_config_number`, which
#: a one-hop rule misses) and through `Restocker_db`'s (`add_coins` ->
#: `adjust_balance` -> `adjust_balance_tx`).
#:
#: Deliberately over-inclusive, the same trade the bank's `CORE_AWAIT` makes:
#: widening the class costs an entry in `ABSOLUTE_OK` or `ANSWER_DISCARDED`,
#: with a reason someone had to write. Narrowing it loses a defect silently.

#: An absolute write to an accumulator column, allowed by name, with the reason.
#: `x = excluded.x` in an upsert is an ABSOLUTE WRITE and gets no free pass here:
#: it stores a value the caller computed elsewhere, which is the definition of
#: the shape that mints. Audited in both directions (7c/7d) AND BACKWARDS (7e).
#:
#: ── WHY EACH ENTRY NOW CARRIES A LIST OF CALLERS ─────────────────────────────
#:
#: Because the previous round's entry was FREE TEXT AND WRONG, and nothing
#: checked it. `("upsert_market_shares", "treasury_coins")` was waived on the
#: written grounds that *"the trading paths never come through here, they come
#: through `adjust_treasury`, which is relative"* — while `_persist_price`, on
#: the success path of every buy AND every sell, came through there twice a
#: round trip. Section 7a found the write, named it by line and column, and was
#: silenced by a sentence. Measured cost of that sentence: eight concurrent
#: sells MINTED +29,625 against a 30,000-coin treasury, eight concurrent buys
#: DESTROYED 60,261 of the 80,824 those buyers paid in, a dividend racing four
#: sells minted +27,653 — zero errors on every run.
#:
#: That is this project's recurring defect in its third instance: the MECHANISM
#: is right and the LIST ABOUT the mechanism goes stale. `CLAIM_PRIMITIVES` was
#: the first, `_do_stock_trade`'s residual paragraph the second, `ABSOLUTE_OK`
#: the third. A list about a mechanism goes stale; the mechanism does not. So
#: the fix is not a better sentence — it is to stop the sentence being the thing
#: that is trusted.
#:
#: The value is now `(callers, reason)`, where `callers` is the set of money-path
#: functions that reach this db function — derived from the SAME transitive
#: closure section 7 already computes, just run BACKWARDS (7e). An EMPTY tuple is
#: the strong form and the one to aim for: "nothing that moves coins can reach
#: this at all". A non-empty tuple is a standing, audited admission, and it goes
#: red the moment a money path that is not in it starts calling the function —
#: which is exactly the transition nobody noticed last round.
ABSOLUTE_OK = {
    ("set_balance", "coins"): ((
        "_save_balances",
    ),
        "the legacy YAML mirror's writer. `_save_balances` re-states a whole "
        "balances file, so the value IS the caller's — there is no delta to "
        "apply. It is not a money path; the money paths use `adjust_balance_tx`, "
        "and section 3 forbids reaching for anything else inside a CLOSED "
        "function. Worth deleting with the YAML mirror, not worth a claim. "
        "THE ONE MONEY PATH THAT REACHES IT is `_save_balances`, which is the "
        "mirror's own writer and stores the whole file at once."),
    ("set_balance", "principal"): ((
        "_save_balances",
    ),
        "same statement, same reason, same single reaching caller."),
    ("set_config", "value"): ((
        "_market_quality", "_pay_honey_harvesters", "_queue_dividend_post",
    ),
        "the generic `bot_config` setter — 'store this string under this key'. "
        "`value` is in the accumulator vocabulary only because ONE key in that "
        "table is a coin pot (`exchange_insurance_fund`, incremented by "
        "`adjust_config_number`), and the deriver works on columns, not on keys. "
        "THE RESIDUAL, STATED: `set_config` and `adjust_config_number` write the "
        "same column, so a `set_config` on a numeric pot key IS a lost update "
        "against a concurrent skim. No caller does that today — the pot is only "
        "ever read (`get_config`) or incremented — and the check will say so "
        "again the moment one is added. THE THREE MONEY PATHS THAT REACH IT do "
        "so for non-coin keys (a quality score, a harvester's ledger marker, a "
        "queued post), which is why they are named here rather than closed: 7e "
        "will name a fourth the day one appears, and a fourth is worth reading."),
    ("set_market_treasury_absolute", "treasury_coins"): ((
        # EMPTY, AND THAT IS THE ENTIRE POINT. Checked, not asserted: 7e derives
        # the money-path closure and demands this set match.
    ),
        "THE STAFF OVERRIDE — `/market treasury set` reads the old figure, shows "
        "`old -> new` on screen and stores exactly what was typed. An absolute "
        "write is what was asked for; there is no delta. It is genuinely a lost "
        "update if it races a trade (measured: 9,653-19,306 coins created, 3/3 "
        "runs) and that is accepted, because a human is choosing the number with "
        "the market in front of them — the command now says so on screen instead "
        "of leaving it in this file. "
        "THIS ENTRY REPLACES `('upsert_market_shares', 'treasury_coins')`, which "
        "made the identical claim and was FALSE: `_persist_price` ran that upsert "
        "on the success path of every buy and every sell. The write was moved "
        "into a function of its own precisely so the claim could be checked, "
        "because 'no money path reaches it' is a property of a function and not "
        "of a keyword argument. `upsert_market_shares` now REFUSES a "
        "`treasury_coins` kwarg outright.",
    ),
}

#: Bare calls to a DEBIT primitive whose returned `applied` figure is discarded,
#: allowed by name, with the reason. See section 8: a debit can apply LESS than
#: asked, so ignoring the answer is how the credit on the other side becomes a
#: mint. Audited in both directions (8b/8c).
ANSWER_DISCARDED = {
    ("_etf_invest", "adjust_etf_units"):
        "the UNIT BURN on the return leg, not a coin move. `adjust_etf_units` is "
        "relative (`units = units + excluded.units`) so it cannot lose an "
        "update, and the figure it returns is the holder's new total, not 'what "
        "was applied' — there is no short answer to read. NEW FINDING, recorded "
        "here rather than hidden: the burn has no floor, so two concurrent "
        "redemptions that both pass the `units > held` check ABOVE the "
        "transaction both burn and both get paid, driving units negative. That "
        "is a check-then-act at the CALLER, not a lost update at the primitive, "
        "and closing it means giving the burn a claim of its own.",
    ("_etf_redeem", "adjust_etf_units"):
        "the same burn on the redemption leg, same reason, same open finding.",
}

#: Columns that are accumulators WHETHER OR NOT the module still increments one.
#:
#: `_quantity_columns` derives its vocabulary from the same file it then checks,
#: and that is circular in exactly one direction: delete the last relative write
#: to a column and the column stops being an accumulator, so the absolute write
#: you just introduced is not a defect any more. Proven, not theorised — mutation
#: M1 turns `treasury_coins = treasury_coins + ?` into `treasury_coins = ?`, the
#: mint the whole round was about, and the derived vocabulary loses
#: `treasury_coins` in the same edit.
#:
#: So the coin columns are a FLOOR, not a list. Everything else is still derived
#: (13 more on this tree, none typed in); these eight cannot be argued away by
#: editing the code that names them.
ACCUMULATOR_FLOOR = {"coins", "principal", "treasury_coins", "shares",
                     "cost_basis", "units", "total_received", "value"}

#: Named anchors, so a regression is reported by NAME and not only as a count.
#: These are the primitives the four closed findings rest on.
ANCHOR_PRIMITIVES = ["adjust_treasury", "adjust_balance_tx", "claim_holding_tx",
                     "adjust_holding", "adjust_config_number",
                     "dividend_leg_claim", "investor_leg_claim"]


# ── SQL shape: parsing ──────────────────────────────────────────────────────
import re as _re                                             # noqa: E402

#: `UPDATE t SET`, and the upsert's `... DO UPDATE SET`, which is the same write.
_UPDATE_SET = _re.compile(r"\bUPDATE\b\s+(?:\w+\s+)?SET\b", _re.I)
_WHERE_KW = _re.compile(r"\bWHERE\b", _re.I)
_WRITE_SQL = _re.compile(r"^\s*(UPDATE|INSERT|DELETE|REPLACE)\b", _re.I)


def _strip_sql_literals(s):
    """`detail='claimed by an attempt - unknown'` -> `detail=''`.

    Only used to decide whether an assignment's right-hand side does ARITHMETIC.
    A hyphen inside an English sentence in a SQL string is not a minus sign, and
    it is exactly what would promote `detail` to an accumulator column and start
    producing false reds on every `SET detail=?`."""
    return _re.sub(r"'[^']*'", "''", s)


def _split_assignments(set_clause):
    """`a = f(x, y), b = c + 1` -> ['a = f(x, y)', 'b = c + 1'].

    Splits on TOP-LEVEL commas only. Naively splitting on every comma is how this
    check first reported `coins = MAX(0, coins - ?)` as an absolute write: the
    comma inside `MAX(...)` cut the column off its own right-hand side. A checker
    that cries wolf about the correct shape gets switched off, which costs more
    than not having it."""
    parts, depth, cur = [], 0, ""
    for ch in set_clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def _sql_of(call):
    """The SQL text of an `.execute(...)`: every string constant inside ARGS[0],
    whitespace-collapsed.

    Args[0] only, and that is the point. Walking every argument also picks up the
    PARAMETER tuple's strings — `(..., 'claimed', 'planned')` — which then land
    inside whatever clause the parser happened to be in and let a parameter
    vouch for a predicate that is not in the statement at all. (The bank hit
    this and split `_sql_strings` from `_sql_text` for it.)"""
    if not getattr(call, "args", None):
        return ""
    return " ".join(" ".join(n.value.split()) for n in ast.walk(call.args[0])
                    if isinstance(n, ast.Constant) and isinstance(n.value, str))


def _sql_updates(sql):
    """Every UPDATE in one statement as (set_clause, where_tail).

    Split on the LAST `WHERE` in each UPDATE's body, for both halves: that is
    the OUTER one, so `SET x=(SELECT .. WHERE q) WHERE id=?` keeps its whole
    right-hand side in the set clause AND reads `id=?` as the tail. Splitting on
    the first would cut the assignment in half and hand the subquery's own
    predicate — which guards the subquery's row, not this one — to the guard
    check. That is the bank's V6 mistake (`STATE_PREDICATE` matched against the
    whole statement, so `SET status='active'` scored as a guard on itself) in
    the shape this file could have inherited."""
    out, ms = [], list(_UPDATE_SET.finditer(sql))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(sql)
        body = sql[m.end():end]
        ws = list(_WHERE_KW.finditer(body))
        out.append((body[:ws[-1].start()], body[ws[-1].end():]) if ws else (body, ""))
    return out


def _is_execute(n):
    return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("execute", "executescript"))


def _is_write_call(n):
    return _is_execute(n) and bool(_WRITE_SQL.match(_sql_of(n)))


def _top_funcs(tree):
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(n.name, []).append(n)
    return out


def _callee_names(fn):
    """Every name called anywhere inside fn (dotted calls by their attribute)."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute)
                    else f.id if isinstance(f, ast.Name) else None)
    out.discard(None)
    return out


def _quantity_columns(tree):
    """DERIVED vocabulary: columns this module ever writes in terms of themselves,
    with arithmetic. `coins = coins + ?`, `treasury_coins = treasury_coins + ?`,
    `shares = shares - ?`, `value = CAST(... COALESCE(value,'0') ... + ?)`.

    You only write a column against itself when you are ACCUMULATING into it, and
    an accumulator stored absolutely is a lost update by construction — the value
    can only have come from a SELECT the function already ran. So the shape of
    the correct write derives the class of column that needs it, and a new money
    column is in scope the first time anybody increments it.

    The arithmetic requirement is what keeps the vocabulary honest: without it
    `note = COALESCE(?, note)` and `detail = CASE WHEN detail='' ...` join, and
    every plain `SET note=?` elsewhere turns red. Measured on this tree: with it,
    18 columns and no false member; without it, 20 and two."""
    cols = set()
    for n in ast.walk(tree):
        if not _is_execute(n):
            continue
        for setc, _tail in _sql_updates(_sql_of(n)):
            for a in _split_assignments(setc):
                if "=" not in a:
                    continue
                col, rhs = a.split("=", 1)
                col = col.strip()
                if not _re.fullmatch(r"[A-Za-z_]\w*", col):
                    continue
                rhs = _strip_sql_literals(rhs)
                if _re.search(rf"\b{col}\b", rhs, _re.I) and _re.search(r"[-+]", rhs):
                    cols.add(col.lower())
    return cols


def _is_claim_call(n, quant):
    """A CANDIDATE claim: an UPDATE whose WHERE TAIL tests the row's own state —
    a column compared to a literal (`state='planned'`), or an accumulator column
    named at all (`treasury_coins + ? >= 0`, `treasury_coins = ?`).

    Candidate, not claim. It becomes one only when its rowcount is read on the
    same path (7b). Half a claim is an UPDATE that may have changed nothing while
    the caller carries on as though it had — which is the entire defect this
    component's dividend, sell and investor paths were built out of."""
    if not _is_execute(n):
        return False
    for _setc, tail in _sql_updates(_sql_of(n)):
        if not tail:
            continue
        if _re.search(r"\b[a-z_]+\s*(=|!=|<>|>=|<=|>|<)\s*('[^']*'|\d+)", tail, _re.I):
            return True
        if any(_re.search(rf"\b{c}\b", tail, _re.I) for c in quant):
            return True
        if _re.search(r"\bIS\s+(NOT\s+)?NULL\b", tail, _re.I):
            return True
    return False


def _is_rowcount_read(n):
    return isinstance(n, ast.Attribute) and n.attr == "rowcount"


def _analyse(fn, quant):
    """(claims whose rowcount is never read on their own path, claims that are).

    PORTED FROM THE BANK'S `_analyse`, AND FOR ITS REASONS. The version this
    replaces was `"rowcount" in ast.dump(fn)`: one boolean, per function, over a
    dumped tree. Every way that can be wrong was already proven in the bank —

      * a read on a DIFFERENT statement's cursor answers for this one;
      * a read in the `if` arm answers for a claim taken in the `else` (a
        per-FUNCTION answer to a per-WRITE question);
      * a name or a docstring containing the word answers for nothing at all.

    — and all three are reachable here. So a candidate claim is carried PENDING
    against the cursor name it was bound to, and is answered only when an
    `ast.Attribute` named `rowcount` on THAT cursor is evaluated on THAT path.
    Branches fork the pending set and rejoin by UNION, so a claim taken in one
    arm can still be answered after the join (it is on that path) but is NOT
    answered by the other arm's read (it is not).

    `cur.rowcount` bound to a name and then ignored still counts as read. "Read"
    versus "acted upon" is a dataflow question; the shape in `Restocker_db.py`
    is uniform — `if not cur.rowcount: return 0`, `return cur.rowcount > 0`."""
    answered, bound = [], {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    bound[id(n.value)] = t.id

    def key(call):
        return bound.get(id(call)) or f"\0{id(call)}"     # inline `.execute(...).rowcount`

    def read_key(attr):
        v = attr.value
        if isinstance(v, ast.Name):
            return v.id
        if isinstance(v, ast.Call):
            return key(v)
        return None                       # an owner we cannot name reads nothing

    def expr(nodes, pending):
        pending = dict(pending)
        # (lineno, col, end_col) puts a Call BEFORE the Attribute wrapping it, so
        # `conn.execute(...).rowcount` registers the claim and then its answer.
        items = sorted((n for node in nodes if node is not None
                        for n in ast.walk(node)
                        if isinstance(n, ast.Call) or _is_rowcount_read(n)),
                       key=lambda n: (n.lineno, n.col_offset, n.end_col_offset or 0))
        for n in items:
            if _is_rowcount_read(n):
                k = read_key(n)
                if k is not None and k in pending:
                    answered.append(pending.pop(k))
            elif _is_claim_call(n, quant):
                pending[key(n)] = n.lineno
        return pending

    def body(stmts, pending):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue                                   # a separate scope
            if isinstance(st, ast.If):
                pending = expr([st.test], pending)
                a = body(st.body, pending)
                b = body(st.orelse, pending) if st.orelse else pending
                merged = dict(b)
                merged.update(a)          # union: still pending on EITHER path
                pending = merged
            elif isinstance(st, (ast.While, ast.For, ast.AsyncFor)):
                pending = expr([getattr(st, "test", None) or getattr(st, "iter", None)],
                               pending)
                pending = body(st.body, pending)
                pending = body(st.orelse, pending)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                pending = expr([i.context_expr for i in st.items], pending)
                pending = body(st.body, pending)
            elif isinstance(st, ast.Try):
                after = body(st.body, pending)
                after = body(st.orelse, after)
                for h in st.handlers:
                    body(h.body, pending)
                pending = body(st.finalbody, after)
            else:
                pending = expr([st], pending)
        return pending

    left = body(getattr(fn, "body", []), {})
    return sorted(left.values()), sorted(answered)


def _money_path_reach(main_tree, roots):
    """The MAIN-side transitive closure of the money paths: every top-level
    function in `Restocker_main.py` that a `CLOSED` or `OPEN` money path can
    reach through its own helpers.

    Split out of `_derive_class` because section 7e runs it BACKWARDS. Forwards
    it answers "which db primitives does the money stand on"; backwards it
    answers "which money paths reach THIS primitive", which is the question an
    `ABSOLUTE_OK` waiver makes a claim about in prose and nothing checked."""
    mainf = _top_funcs(main_tree)
    reach = set(roots) & set(mainf)
    changed = True
    while changed:
        changed = False
        for nm in list(reach):
            for f in mainf[nm]:
                new = (_callee_names(f) & set(mainf)) - reach
                if new:
                    reach |= new
                    changed = True
    return reach


def _derive_class(db_tree, main_tree, roots):
    """The primitives the money paths stand on: db functions called (transitively,
    through main's helpers and through each other) by a `CLOSED` or `OPEN`
    money path, that WRITE durable state. Read-only helpers drop out by
    themselves — nothing to lose an update in."""
    dbf = {n.name: n for n in db_tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    mainf = _top_funcs(main_tree)

    reach = _money_path_reach(main_tree, roots)
    seed = set()
    for nm in reach:
        for f in mainf[nm]:
            seed |= _callee_names(f) & set(dbf)
    seed |= set(roots) & set(dbf)                   # `add_coins`/`deduct_coins`
    cls = set(seed)
    changed = True
    while changed:                                  # db-side fixed point
        changed = False
        for nm in list(cls):
            new = (_callee_names(dbf[nm]) & set(dbf)) - cls
            if new:
                cls |= new
                changed = True
    return {nm: dbf[nm] for nm in sorted(cls)
            if any(_is_write_call(n) for n in ast.walk(dbf[nm]))}


def _lines_inside_db_with(fn):
    """Line numbers covered by a `with <...>.db() as X:` block inside fn."""
    lines = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.With):
            continue
        opens_tx = False
        for item in n.items:
            c = item.context_expr
            if isinstance(c, ast.Call) and _fname(c) == "db":
                opens_tx = True
        if not opens_tx:
            continue
        for sub in ast.walk(n):
            ln = getattr(sub, "lineno", None)
            if ln is not None:
                lines.add(ln)
    return lines


def _fname(node):
    """Dotted call name -> its last attribute (`_db.adjust_treasury` -> the name)."""
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _has_kw(node, kw):
    return any(k.arg == kw for k in node.keywords)


def _reason_of(node):
    for k in node.keywords:
        if k.arg == "reason":
            return k.value
    return None


def _const_prefix(value):
    """Best-effort leading literal of a str/JoinedStr, for namespace checks."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.JoinedStr) and value.values:
        first = value.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def _interpolates(value):
    """True if the reason mixes in a runtime value — i.e. it is derived from a row."""
    if isinstance(value, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in value.values)
    return False


def main() -> int:
    src = io.open(MAIN, encoding="utf-8").read()
    tree = ast.parse(src)

    fails, checks = [], 0

    def fail(msg):
        fails.append(msg)

    def check(cond, msg):
        nonlocal checks
        checks += 1
        if not cond:
            fail(msg)

    # ── 1. the headline: is the transactional API used at all? ───────────────
    all_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    n_tx = sum(1 for c in all_calls if _fname(c) == "adjust_balance_tx")
    check(n_tx > 0,
          "Restocker_main.py calls `adjust_balance_tx` ZERO times — the "
          "transactional ledger API is unused by the component that needs it "
          "most. This is the sentence the whole round was about.")

    # ── walk every top-level function/method body once ───────────────────────
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, []).append(node)

    def calls_in(fn):
        """Calls lexically inside fn, excluding nested function definitions."""
        out = []
        for child in ast.iter_child_nodes(fn):
            stack = [child]
            while stack:
                n = stack.pop()
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                if isinstance(n, ast.Call):
                    out.append(n)
                stack.extend(ast.iter_child_nodes(n))
        return out

    # ── 2 + 3. closed functions carry the transaction ────────────────────────
    for name in sorted(CLOSED):
        defs = funcs.get(name)
        check(bool(defs), f"CLOSED function `{name}` is not in Restocker_main.py "
                          f"any more — the contract is describing code that is gone.")
        for fn in defs or []:
            in_tx = _lines_inside_db_with(fn)
            for call in calls_in(fn):
                cname = _fname(call)
                if cname in MUST_BE_IN_TX:
                    check(_has_kw(call, "conn"),
                          f"{name}:{call.lineno} calls `{cname}(...)` with no `conn=` — "
                          f"that is a separate commit inside a money path that is "
                          f"supposed to be one transaction. ({CLOSED[name]})")
                if cname in MARKER_CALLS and call.lineno in in_tx:
                    check(_has_kw(call, "conn"),
                          f"{name}:{call.lineno} calls `{cname}(...)` with no `conn=` from "
                          f"INSIDE a `with db()` block — `db()` is not re-entrant, so this "
                          f"commits the caller's half-written money early. That is the "
                          f"exact split the transactional API exists to close.")
                if cname in BANNED_IN_CLOSED:
                    check(False,
                          f"{name}:{call.lineno} calls `{cname}(...)`, which commits the "
                          f"balance and the ledger row in TWO transactions and cannot be "
                          f"made atomic by any argument. Use "
                          f"`adjust_balance_tx(conn, ..., reason=...)`. ({CLOSED[name]})")

    # ── 4 + 5. every transactional credit says why, in a stable way ──────────
    for call in all_calls:
        if _fname(call) != "adjust_balance_tx":
            continue
        reason = _reason_of(call)
        check(reason is not None,
              f"Restocker_main.py:{call.lineno}: `adjust_balance_tx` with no `reason=` — "
              f"the money commits and the record of it does not exist. Passing a reason "
              f"is the entire difference between this call and `adjust_balance`.")
        if reason is None:
            continue
        prefix = _const_prefix(reason)
        literal_empty = (isinstance(reason, ast.Constant)
                         and isinstance(reason.value, str)
                         and not reason.value.strip())
        check(not literal_empty,
              f"Restocker_main.py:{call.lineno}: `adjust_balance_tx` reason is the empty "
              f"string. `add_coins` fell back to a FRAME NAME for these; a transactional "
              f"call has no such fallback and will write a blank ledger row.")
        if prefix.startswith("mk:"):
            check(_interpolates(reason),
                  f"Restocker_main.py:{call.lineno}: an `mk:` reason must be derived from "
                  f"a DURABLE ROW (a run id, a bond id, a month) or the partial UNIQUE "
                  f"index on `coin_ledger(user_id, reason) WHERE reason LIKE 'mk:%'` "
                  f"would collapse unrelated payments into one. This one is a constant.")

    # ── 6. no money path is left unclassified ────────────────────────────────
    for name, defs in sorted(funcs.items()):
        if name in CLOSED or name in OPEN:
            continue
        for fn in defs:
            moved = sorted({_fname(c) for c in calls_in(fn)}
                           & (BANNED_IN_CLOSED | {"adjust_balance", "adjust_balance_tx",
                                                  "adjust_treasury"}))
            if not moved:
                continue
            check(False,
                  f"`{name}` (line {fn.lineno}) moves coins ({', '.join(moved)}) and is in "
                  f"neither CLOSED nor OPEN in this file. Close it, or list it in OPEN with "
                  f"the reason it stays open — an unclassified money path is how the "
                  f"previous rounds' fixes drifted apart from each other.")

    # ── 7. the primitives underneath are CLAIM-FIRST, checked on SQL SHAPE ───
    # Not "does it pass conn=". Does the write compute the new value FROM THE ROW,
    # or from a SELECT this function already ran? An accumulator column written
    # absolutely is a lost update: the delta of any concurrent writer is silently
    # discarded, with no error and no exception — a wrong number, not a failure.
    # That is the exact shape that minted 9,653 coins while sections 1-6 read
    # 60/60, and NEITHER of the two functions it is now caught in
    # (`upsert_market_shares`, `set_balance`) was in the hand-written list that
    # replaced it.
    db_path = os.path.join(ROOT, "Restocker_db.py")
    db_src = io.open(db_path, encoding="utf-8").read()
    db_tree = ast.parse(db_src)

    DERIVED = _quantity_columns(db_tree)
    QUANT = DERIVED | ACCUMULATOR_FLOOR
    check(len(DERIVED) >= 10 and {"coins", "treasury_coins", "shares"} <= DERIVED,
          f"the accumulator vocabulary derived from Restocker_db.py is {sorted(DERIVED)} — "
          f"it does not contain the columns the whole engine's money is kept in. Either "
          f"the deriver has stopped resolving the module's SQL (so section 7 is checking "
          f"nothing while printing a number), or the LAST relative write to a coin column "
          f"has just been deleted — which is the defect itself, seen from the vocabulary. "
          f"ACCUMULATOR_FLOOR keeps the column in scope either way; this says so out loud.")

    CLASS = _derive_class(db_tree, tree, set(CLOSED) | set(OPEN))
    check(len(CLASS) >= 20,
          f"the money-primitive class derived from CLOSED+OPEN resolved to {len(CLASS)} "
          f"writers. It should be dozens. A deriver that resolves to nothing reports "
          f"zero defects for ever — the project has shipped that twice "
          f"(`check_wiring.py` scanning zero cogs) and it reads as evidence.")
    for anchor in ANCHOR_PRIMITIVES:
        check(anchor in CLASS,
              f"`{anchor}` is not in the derived money-primitive class. It is one of the "
              f"primitives the four closed findings rest on, so either it stopped being "
              f"called from a money path (and the money moved somewhere this file cannot "
              f"see) or the deriver stopped reaching it.")

    # A waived function need NOT be in CLASS — the fix for NEW-M5 deliberately
    # moved the treasury's absolute write into `set_market_treasury_absolute`,
    # which no money path reaches, so the class no longer contains it. Audit the
    # waived functions anyway, or 7c/7d would report "not an absolute write any
    # more" about a write that is still sitting there. AUDIT is CLASS plus them;
    # 7b's claim analysis stays on CLASS, which is what the claim rule is about.
    ALL_DBF = {n.name: n for n in db_tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    AUDIT = dict(CLASS)
    for wname, _wcol in ABSOLUTE_OK:
        if wname not in AUDIT and wname in ALL_DBF:
            AUDIT[wname] = ALL_DBF[wname]

    seen_absolute = set()
    claims_found = 0
    for name in sorted(AUDIT):
        fn = AUDIT[name]
        # 7a. no accumulator is written absolutely.
        for n in ast.walk(fn):
            if not _is_execute(n):
                continue
            for setc, tail in _sql_updates(_sql_of(n)):
                for a in _split_assignments(setc):
                    if "=" not in a:
                        continue
                    col, rhs = a.split("=", 1)
                    col = col.strip().lower()
                    if col not in QUANT:
                        continue
                    # `excluded.coins` IS NOT `coins`. It is the value the caller
                    # passed to the INSERT, so `SET coins=excluded.coins` is an
                    # absolute write in an upsert's clothing — and `\bcoins\b`
                    # matches inside it, because `.` is a word boundary. That one
                    # regex silently waved through both of this file's real
                    # absolute writers (`set_balance`, `upsert_market_shares`)
                    # on the first run. A qualified reference is stripped before
                    # the test; `units = units + excluded.units` still reads as
                    # relative on its own bare `units`, which is the only term
                    # that makes it so.
                    bare = _re.sub(r"\b\w+\s*\.\s*\w+", " ", rhs)
                    relative = _re.search(rf"\b{col}\b", bare, _re.I)
                    # A COMPARE-AND-SWAP PINS THE VALUE. `WHERE col = ?` says
                    # "only if the row still holds the number I computed this
                    # from"; `WHERE col >= ?` does not — the row may have moved
                    # and still match, which is the stale-read write with a
                    # sanity check bolted on. Mutation M4 is the proof: turning
                    # `claim_holding_tx`'s `SET shares = shares - ?` into
                    # `SET shares = ?` left a green board under the looser rule,
                    # because its `AND shares >= ?` scored as a claim — while the
                    # mutant lets two concurrent sells of the same 100 shares
                    # both write 100.
                    cas = _re.search(rf"\b{col}\b\s*(=|IS)\b|\b{col}\b\s*=",
                                     _re.sub(r"\b\w+\s*\.\s*\w+", " ", tail or ""), _re.I)
                    if relative or cas:
                        continue
                    # `x = excluded.x` reaches here on purpose: it is an ABSOLUTE
                    # write wearing an upsert's clothes.
                    seen_absolute.add((name, col))
                    check((name, col) in ABSOLUTE_OK,
                          f"Restocker_db.py:{n.lineno} `{name}` writes the accumulator "
                          f"column `{col}` with neither the column on the right-hand side "
                          f"(RELATIVE) nor the column in the WHERE tail "
                          f"(COMPARE-AND-SWAP): `SET {a.strip()}"
                          f"{(' WHERE ' + tail.strip()) if tail.strip() else ''}`. The "
                          f"value stored can only have come from a SELECT somebody "
                          f"already ran, so a concurrent writer's delta is discarded "
                          f"silently. Measured on the shape this check was written for: "
                          f"9,653 coins minted by two concurrent sells, 17,000 destroyed "
                          f"by twenty concurrent credits, zero errors either way. Make it "
                          f"relative, or add ('{name}', '{col}') to ABSOLUTE_OK with the "
                          f"reason it is allowed to be a lost update.")
        # 7b. every conditional claim has its OWN rowcount read on its OWN path.
        if name not in CLASS:
            continue
        unread, read = _analyse(fn, QUANT)
        claims_found += len(read) + len(unread)
        for ln in unread:
            check(False,
                  f"Restocker_db.py:{ln} in `{name}`: a conditional UPDATE whose "
                  f"`rowcount` is never read on the path that runs it. A claim nobody "
                  f"reads the answer to is not a claim, it is a hope — the statement may "
                  f"have changed nothing while the caller carries on as though it had. "
                  f"CLAIM-FIRST THEN ACT, AND READ THE ROWCOUNT.")

    check(claims_found >= 10,
          f"the claim finder located {claims_found} conditional UPDATEs across "
          f"{len(CLASS)} money primitives. The engine has more than that; a finder that "
          f"locates none passes every function trivially.")

    # 7c/7d. ABSOLUTE_OK is audited in BOTH directions. The bank's V8: a list
    # audited one way lets a NEW member in with nobody having looked at it.
    for entry in sorted(ABSOLUTE_OK):
        check(entry in seen_absolute,
              f"ABSOLUTE_OK names {entry[0]}.{entry[1]} as an allowed absolute write and "
              f"it is not one any more — either the write became relative (delete the "
              f"entry) or the function was deleted (delete the entry too, and check what "
              f"else left with it). A stale waiver is a waiver nobody re-reads.")

    # ── 7e. THE WAIVER'S CLAIM, CHECKED — the closure run BACKWARDS ──────────
    # Sections 7a-7d ask "is this write absolute" and "is the waiver stale". They
    # do NOT ask the only question a waiver actually turns on: CAN ANYTHING THAT
    # MOVES COINS REACH IT? An absolute write to an accumulator is a lost update
    # whenever two writers race, so a waiver is a bet that there is only ever one
    # writer, and that bet is about REACHABILITY.
    #
    # Last round that bet was written in prose and lost. `upsert_market_shares`
    # was waived because "the trading paths never come through here" while
    # `_persist_price` came through it on the success path of every buy and every
    # sell — +29,625 minted, -60,261 destroyed, nothing logged. Section 7a had
    # already found the write and named it by line and column; a sentence turned
    # it green. That is the third round running that a LIST ABOUT a mechanism
    # went stale (`CLAIM_PRIMITIVES`, `_do_stock_trade`'s residual paragraph,
    # `ABSOLUTE_OK`) while the mechanism itself held.
    #
    # So the claim stops being prose. `_money_path_reach` is the SAME closure
    # section 7 already computes to derive CLASS, run the other way: for each
    # waived function, the money-path callers are DERIVED and the waiver must
    # name exactly them. An empty tuple is the strong form — "nothing that moves
    # coins reaches this" — and it is now a measured fact rather than a belief.
    # A new money path calling a waived writer turns the board red on the day it
    # is written, which is the transition nobody caught last time.
    REACH = _money_path_reach(tree, set(CLOSED) | set(OPEN))
    check(len(REACH) >= len(set(CLOSED) | set(OPEN)),
          f"the money-path closure run backwards resolved to {len(REACH)} functions, "
          f"fewer than the {len(set(CLOSED) | set(OPEN))} roots it started from. The "
          f"closure has stopped resolving, so 7e would wave every waiver through while "
          f"printing a number — the failure mode this project has shipped twice.")

    callers_of = {}
    for nm, defs in sorted(_top_funcs(tree).items()):
        for f in defs:
            for callee in _callee_names(f):
                callers_of.setdefault(callee, set()).add(nm)

    for entry in sorted(ABSOLUTE_OK):
        fname_, col_ = entry
        declared = set(ABSOLUTE_OK[entry][0])
        actual = callers_of.get(fname_, set()) & REACH
        check(actual == declared,
              f"ABSOLUTE_OK's waiver for `{fname_}.{col_}` makes a claim about WHO "
              f"reaches it, and the claim does not match the code. It names "
              f"{sorted(declared) or '[] (nothing that moves coins)'}; the money-path "
              f"closure says {sorted(actual) or '[] (nothing that moves coins)'}. "
              + (f"NEW REACHING MONEY PATHS: {sorted(actual - declared)}. Each of those "
                 f"can race any other writer of `{col_}` and lose an update silently — "
                 f"measured on the last waiver that got this wrong: +29,625 minted by "
                 f"eight concurrent sells, -60,261 destroyed by eight concurrent buys, "
                 f"zero errors. Either stop that path reaching an absolute write (make "
                 f"it relative, or move the absolute write into a function the money "
                 f"cannot reach, which is what `set_market_treasury_absolute` is), or "
                 f"add it to this entry's caller list with the reason it is safe. "
                 if actual - declared else "")
              + (f"NAMED BUT NO LONGER REACHING: {sorted(declared - actual)} — delete "
                 f"them from the entry, and check what else moved when they did."
                 if declared - actual else ""))

    # ── 7f. THE RESIDUAL PARAGRAPH IS DERIVED, NOT WRITTEN ──────────────────
    # `_do_stock_trade`'s docstring carries a list of what is still check-then-act
    # under it. That list has now been wrong TWICE, in consecutive rounds, and
    # the second time it was wrong in the paragraph added to stop it being wrong
    # the first time: it named `_skim_insurance` and two reads, omitted
    # `_persist_price`, and concluded "None of those can mint or destroy a coin"
    # over the function that was minting +29,625 per eight concurrent sells.
    #
    # A written list goes stale silently; a derived one cannot. The call graph is
    # already here — CLASS is built from it — so the residual is DERIVED and the
    # docstring is required to name all of it. Adding a helper to the trade path
    # that touches a quantity column now turns this red on the day it is written,
    # and the fix is to say so in the docstring, which is the point.
    _all_dbf = {n.name: n for n in db_tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _writes_quantity(fn):
        for n in ast.walk(fn):
            if not _is_execute(n):
                continue
            sql = _sql_of(n)
            for setc, _tail in _sql_updates(sql):
                for a in _split_assignments(setc):
                    if "=" in a and a.split("=", 1)[0].strip().lower() in QUANT:
                        return True
            m = _re.match(r"\s*INSERT\s+INTO\s+\w+\s*\(([^)]*)\)", sql, _re.I)
            if m and any(c.strip().lower() in QUANT for c in m.group(1).split(",")):
                return True
        return False

    _qwriters = {nm for nm, f in _all_dbf.items() if _writes_quantity(f)}
    check(len(_qwriters) >= 5,
          f"only {len(_qwriters)} db functions were found to write a quantity column. "
          f"The SQL resolver has stopped resolving, so 7f would certify any docstring "
          f"at all — including the two that were already wrong.")

    _mainf = _top_funcs(tree)
    _trade_reach = {"_do_stock_trade"}
    _ch = True
    while _ch:
        _ch = False
        for nm in list(_trade_reach):
            for f in _mainf.get(nm, []):
                new = (_callee_names(f) & set(_mainf)) - _trade_reach
                if new:
                    _trade_reach |= new
                    _ch = True
    _residual = sorted(nm for nm in _trade_reach - {"_do_stock_trade"}
                       if any(_callee_names(f) & _qwriters for f in _mainf[nm]))
    check(len(_residual) >= 1,
          "the trade path was derived to reach NO helper that writes a quantity column. "
          "It reaches at least `_skim_insurance`; a deriver resolving to nothing passes "
          "every docstring trivially.")

    # Read the DERIVED PARAGRAPH, not the whole docstring. Anywhere-in-the-text
    # would be satisfied by the paragraph BELOW it, which recounts the wreck and
    # names `_persist_price` while explaining how it was missed — so a docstring
    # that dropped the name from its actual list would still have scored green
    # off its own post-mortem. The list has to be a list.
    _trade_doc = ast.get_docstring(_mainf["_do_stock_trade"][0]) or ""
    _MARK = "DERIVED, AND ENFORCED"
    check(_MARK in _trade_doc,
          f"`_do_stock_trade`'s docstring has no {_MARK!r} paragraph. That paragraph is "
          f"the derived residual list this check reads; without it the check has nothing "
          f"to read and would pass trivially, which is how the previous two versions of "
          f"this docstring were wrong in the first place.")
    _seg = _trade_doc.split(_MARK, 1)[-1].split("\n\n", 1)[0]
    _missing = [nm for nm in _residual if nm not in _seg]
    check(not _missing,
          f"`_do_stock_trade`'s docstring does not name {_missing}. Those are helpers the "
          f"trade path reaches that write a quantity column, derived from the call graph "
          f"— so the docstring's account of what is still check-then-act underneath it is "
          f"incomplete, which is how it came to certify 'None of those can mint or destroy "
          f"a coin' over `_persist_price` while eight concurrent sells minted +29,625. "
          f"The full derived residual is {_residual}. Name them, and say what each one "
          f"can and cannot do — do not delete this check to make the sentence true.")

    check(any(not ABSOLUTE_OK[e][0] for e in ABSOLUTE_OK),
          "not one entry in ABSOLUTE_OK claims the strong form — an EMPTY caller list, "
          "meaning nothing that moves coins reaches the absolute write at all. If every "
          "entry is a standing admission then 7e is only checking that the admissions "
          "are current, and the shape it exists to make possible — an absolute write "
          "the money cannot reach — is not being used by anything.")

    # ── 8. THE CALLER READS THE ANSWER ───────────────────────────────────────
    # Half the guarantee is at the primitive; the other half is the call site. A
    # DEBIT can apply LESS than it was asked for — `adjust_treasury` draws down to
    # zero and says so, `adjust_balance_tx` clamps at zero and says so — so a call
    # that discards the answer and then credits the full figure on the other side
    # MINTS. This is the bank's check 2 pointed at quantities instead of states.
    # A debit is derived from the call, not listed: a negated amount argument, or
    # `allow_negative=False`.
    debit_prims = {n for n in CLASS if _analyse(CLASS[n], QUANT)[1]} | {
        "adjust_balance_tx", "adjust_treasury", "adjust_config_number",
        "adjust_etf_units", "adjust_holding", "adjust_bond_holding"}
    debit_prims &= set(CLASS)

    def _is_debit(c):
        for a in c.args:
            if isinstance(a, ast.UnaryOp) and isinstance(a.op, ast.USub):
                return True
        for k in c.keywords:
            if k.arg == "allow_negative" and isinstance(k.value, ast.Constant) \
                    and k.value.value is False:
                return True
            if isinstance(k.value, ast.UnaryOp) and isinstance(k.value.op, ast.USub):
                return True
        return False

    enclosing = {}
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(f):
                enclosing.setdefault(id(n), f.name)

    seen_discarded, n_debit_sites = set(), 0
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)):
            continue
        c = n.value
        cn = _fname(c)
        if cn not in debit_prims or not _is_debit(c):
            continue
        n_debit_sites += 1
        where = enclosing.get(id(n), "<module>")
        seen_discarded.add((where, cn))
        check((where, cn) in ANSWER_DISCARDED,
              f"Restocker_main.py:{n.lineno} in `{where}`: `{cn}(...)` takes coins OUT "
              f"and its return value is thrown away. That figure is what was ACTUALLY "
              f"applied — a short treasury draws down to what it holds, a short wallet "
              f"clamps at zero — and the credit on the other side of this debit is for "
              f"the full amount. Read it and refuse when it comes up short, or add "
              f"('{where}', '{cn}') to ANSWER_DISCARDED with the reason.")

    check(n_debit_sites >= 2,
          f"section 8 found {n_debit_sites} bare debit call sites to inspect. The "
          f"detector has stopped recognising a debit, so it now passes every call site "
          f"in the file — which is what a green board over an unmeasured property looks "
          f"like from the inside.")
    for entry in sorted(ANSWER_DISCARDED):
        check(entry in seen_discarded,
              f"ANSWER_DISCARDED names {entry[0]} -> {entry[1]} and there is no such "
              f"bare debit call any more. Either it now reads its answer (delete the "
              f"entry) or it moved (find it). A waiver for a call site that does not "
              f"exist is coverage this file is not providing.")

    # ── 9. ONE DEFINITION PER NAME ───────────────────────────────────────────
    # `Restocker_db.py` carried two live `list_futures_orders`: the second shadowed
    # the first and silently dropped its `user_id` and `limit` parameters, so a
    # caller that passed either got a TypeError and a caller that passed neither
    # got a different query. Two live `get_config_prefix` as well. A shadowed
    # definition is the one defect class where reading the code is actively
    # misleading — you find the definition, and it is not the one that runs.
    for modname in ("Restocker_db.py", "Restocker_main.py"):
        mtree = ast.parse(io.open(os.path.join(ROOT, modname), encoding="utf-8").read())
        seen = {}
        for node in mtree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen.setdefault(node.name, []).append(node.lineno)
        for nm, lns in sorted(seen.items()):
            check(len(lns) == 1,
                  f"{modname} defines `{nm}` {len(lns)} times at module level (lines "
                  f"{lns}) — the last one wins and every earlier one is dead code that "
                  f"still reads like the truth. Delete the shadow, and check its "
                  f"signature against the survivor's before you do: that is how "
                  f"`list_futures_orders` lost `user_id` and `limit`.")


    # ── the guard index the `mk:` namespace depends on must exist ────────────
    al = io.open(os.path.join(ROOT, "action_log.py"), encoding="utf-8").read()
    check("uq_coin_ledger_mk" in al and "reason LIKE 'mk:%'" in al,
          "the partial UNIQUE index on `coin_ledger(user_id, reason) WHERE reason LIKE "
          "'mk:%'` is not in action_log._GUARD_INDEXES — the `mk:` reasons are then just "
          "strings and nothing enforces that a bond coupon is paid once.")

    print(f"\n{'=' * 72}")
    for f in fails:
        print("FAIL:", f)
    ok = checks - len(fails)
    print(f"{'=' * 72}")
    print(f"money-transaction contract: {ok}/{checks} checks passed"
          + ("" if fails else "  — the sentence holds"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
