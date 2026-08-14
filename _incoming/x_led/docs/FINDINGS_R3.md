# Round 3 review — scorecard and new findings

## Money paths

# Round-3 adversarial money re-review

Read in full: `FINDINGS_R2.md`, `LEDGER_API_v2.md`, `ESTATES_DB_INTERFACE.md`, `ESTATES_DB_USAGE.md`, then the five modules. Every verdict below was checked against the code; the ones marked *reproduced* were run against real temp databases. **No files were edited**; `python3 -m pyflakes` is clean on all five.

---

## (a) Scorecard

| # | Verdict | Proof |
|---|---|---|
| **S1** | **VERIFIED FIXED** | Three guards now, not one: `ledger_migrate.py:293-305` (UPDATE), `:314-324` (AFTER INSERT), `:329-338` (DELETE), assembled at `:342-346`, dropped+recreated every run at `:577-579`. Reproduced on a migrated DB with a live 6,000 hold on 10,000 coins: `UPDATE…coins=0`, legacy `coins=MAX(0,coins-7000)`, `INSERT OR REPLACE`, `DELETE`, and upsert `DO UPDATE` all raise `IntegrityError`; `INSERT OR IGNORE` (the `ensure_wallet` idiom), an affordable 3,000 spend and a credit all still pass. N3's two escapes are closed and `LEDGER_API_v2.md:196-203` now states the coverage honestly instead of "any writer". |
| **S3** | **VERIFIED FIXED for in-band, and now mechanically enforced** | Allowlist `IN_BAND_ENDPOINTS` `ledger_v2.py:479-486`; the takeover WHERE carries `AND applied_unknown = 0` `:662`; `_release_idempotency` scoped the same way `:775`; and `_finalize_idempotency:589-599` *refuses* to complete a key whose endpoint is not declared — I called it with `endpoint='stock.buy'` and it raised `internal_error`, so a future in-band endpoint fails loudly rather than silently arming the takeover. Residual is **N1**. |
| **S11** | **VERIFIED FIXED** | `sweep_expired_holds` `ledger_v2.py:1360-1415` writes no cursor; the docstring `:1370-1377` now explains why `AND hold_id > cursor` would have *skipped live holds* — the opposite of what it invited last round. `ledger_migrate.py:601` deletes the dead `ledger_meta` key and reports it `:634-635`. The single surviving mention of `hold_sweep_cursor` in `ledger_v2.py` is inside that docstring. |
| **N1** | **PARTIALLY FIXED — the exact stated sequence still double-charges** | See finding **1**. |
| **N2** | **VERIFIED FIXED** | `settling` column `ledger_migrate.py:367`, `_SETTLING` `:285-287`, the shortfall-may-not-grow rule `:293-305`; written in capture's claim UPDATE `ledger_v2.py:1228-1234`, cleared unconditionally `:1277`, monitored `:1418-1434` + `:1445-1450`. Reproduced on the exact wallet from N2 (8,000 coins, holds 3,000 + 6,000): capture A **succeeds** (balance 5,000), capture B fails on its own merits with `insufficient` and rolls back to `state='open', settling=0`; `escrow_settling_leaks()` empty. The "(0 held)" error text is fixed at `ledger_v2.py:1002-1021` and now names the blocking hold ids. |
| **N3** | **VERIFIED FIXED** | As S1. The AFTER-INSERT-not-BEFORE reasoning at `ledger_migrate.py:211-219` is correct and I confirmed the `ensure_wallet` no-op is unaffected. |
| **N4** | **PARTIALLY FIXED** | First half fixed: `estates_main.py:1376-1420` `_park_or_requeue` — past the attempt budget a *retryable* error goes through `requeue_stuck_row` ('claimed'→'pending'), never `fail_payout_row`, so `MAX_PAYOUT_ATTEMPTS` no longer overrides `_permanent=False`. Second half **not** fixed: no `failed → pending` transition exists anywhere — the only `payout_rows SET status` writes are `estates_db.py:2801, 2827, 2863, 2960`, and `:2960` matches `status='claimed'`. `unpark_payout_row` was not added. `estates_main.py:3161-3165` now *says* Resume cannot restart a parked row instead of implying it can, but for a genuinely permanent park (e.g. `treasury_insolvent`, after the treasury is topped up) "pay them by hand" is still the only exit. Also introduces finding **5**. |
| **N5** | **VERIFIED FIXED (both halves), with one over-reach** | `IN_DOUBT_STATUSES` `estates_db.py:127`; used by `unreconciled_stakes/bids` `:1688-1721` and `market_pools` `:2296-2306`; `highest_bid` widened `:1249-1268`; `build_auction_settle_run` now refuses unless the winning bid row is `'captured'` **and** its amount equals the hammer `:3085-3094`; `estates_main.py:1589-1593` gates the close and `:1604-1612` adds an independent `capturing/releasing` check that does not depend on estates_db's definition. Market half closed by `_stakes_left_out`/`_resolution_block` `estates_main.py:2609-2667`. Over-reach = finding **2**. |
| **N6** | **NOT FIXED in the caller — the mechanism was built and never wired** | `estates_db.py:155-193` adds `DEFINITE_REFUSAL_CODES` + `outcome_known_for()`, whose docstring says it is *"the ONE place that judgement lives, so estates_main's `_outcome_known` and this module's refusal counter can never disagree"*. `estates_main.py:340-347` `_outcome_known` is unchanged from round 2 and never calls it. Truth table I ran: `insufficient`, `frozen`, `escrow_shortfall`, `forbidden_scope`, `forbidden_hold` → main `False`, db `True`. Reproduced 5 full ping-pong cycles (`capture → capture_unknown → reconcile → held`) with `refusals` stuck at **0**, so `MAX_HOLD_REFUSALS` never trips and `capture_refused` / `refused_hold_rows()` / `market_pools['refused_*']` are dead code on the exact errors they were written for. The *money* half is closed (see N5's `_resolution_block` — both phases of the cycle now block resolution), at the cost of finding **4**. |
| **N7** | **NOT FIXED in the caller — the mechanism was built and never wired** | `estates_db.py` grew the whole apparatus: `place_unknown` `:149`, `_unclaim_placement_row` `:1524-1547`, `placements_needing_replay` `:1550-1589`, `reconcile_stake_placement`/`reconcile_bid_placement` `:1661/:1677`. `estates_main.py` contains **zero** references to `placements_needing_replay`, `reconcile_*_placement` or `PLACEMENT_IN_DOUBT_STATUSES`. `place_stake:977-993` still calls `edb.fail_stake(id, code)` on an unknown outcome and still replies "nothing was taken"; the only change is the landing status, and since nothing sweeps `place_unknown`, the punter's coins are stranded for the full TTL exactly as before. Additionally `:972-974` — the *definite*-refusal branch — also calls `fail_stake(id, code)` with the default `outcome_known=False`, so an `insufficient` lands in `place_unknown` too, i.e. the one case where `'failed'` is the truth is now also mislabelled. |

---

## (b) Defects introduced by round 3

### 1 — CRITICAL. `_resolve_out_of_band` clears the takeover guard *before* the outcome is recorded, so N1's stated sequence is still a double-charge
**`ledger_v2.py:1961`**, against **`:784-808`** and **`:1685-1695`**.

`_stock_trade` does, in order: `run_on_bot_loop` returns (coins moved) → `_resolve_out_of_band` **commits `applied_unknown = 0` in its own transaction** → builds the payload (including a `get_balance` DB read) → the `_idempotent` context manager exits and calls `_complete_idempotency`, which writes `state='done'` in *another* transaction. Between those two commits the row reads `in_progress, applied_unknown=0` over money that has already moved — which is precisely the state the flag exists to prevent.

Reproduced, both variants:
```
after _resolve_out_of_band: state=in_progress applied_unknown=0
aged past IDEMPOTENCY_STALE_SECONDS
*** RE-GRANTED: takeover token ... -> exec_stock_trade runs a SECOND time
```
```
_release_idempotency(key, ts) after _resolve_out_of_band -> row DELETED
re-claim: granted immediately (no 900s wait)
```

- **Slow variant (900 s):** process killed, or `_complete_idempotency`'s COMMIT hits SQLITE_BUSY (S8 discards the connection and re-raises out of `__exit__`, and `_idempotent`'s `except` only wraps the `yield`). This is N1's step 3 *verbatim* — the fix did not remove it.
- **Fast variant (immediate):** anything after `_resolve_out_of_band` raises — `get_balance` hitting `database is locked` is enough — and `_idempotent:1683` calls `_release_idempotency`, which now passes its `applied_unknown = 0` scope and **deletes** the claim over a committed trade. The next retry re-runs it with no wait at all.

The module docstring at `:797-799` already states the correct rule ("A raise, a cancelled task or a killed process leaves the flag set, which is the correct reading of what is known") — line 1961 contradicts it on the success path.

**Minimal fix (one line moved).** `_complete_idempotency` already writes `applied_unknown=0` in the same statement as `state='done'` (`ledger_v2.py:742`), so the success path needs no separate clear. Call `_resolve_out_of_band` only on the definite-refusal path:
```python
if payload["ok"]:
    slot["body"] = payload
    return _json(payload)
_resolve_out_of_band(slot["idem"])   # refused: known, and the claim must be releasable
return _json(payload, 409 if r.get("code") == "insufficient_funds" else 200)
```
**New failure mode this introduces:** a *successful* trade whose `_complete_idempotency` fails now leaves the row `in_progress, applied_unknown=1`, so the retry gets `idempotency_unresolved` (`:699-714`) plus the ERROR log, and an operator must check the stock ledger. That is the correct reading of what is known, it is the behaviour §6 already documents, and it trades a silent double-charge for a loud manual check.

---

### 2 — HIGH. A `capture_refused` winning bid is still top-bid-eligible, so a lot closes on a bidder who paid nothing, releases everyone else, and can never be settled
**`estates_db.py:1249-1250`** (`_TOP_BID_STATUSES` includes `"capture_refused"`) vs **`estates_main.py:1641-1676`** (`auction_closed` is called unconditionally).

`capture_refused` is deliberately *outside* `IN_DOUBT_STATUSES` (`estates_db.py:127`), so it passes the `unreconciled_bids` gate (`estates_main.py:1589`) and the explicit `capturing/releasing` check (`:1604-1612`) — but it is inside `_TOP_BID_STATUSES`, so `highest_bid` still returns it. `claim_bid_capture` then returns `None` (it requires `status='held'`), and nothing between there and `auction_closed` notices the difference between "already captured, resuming" and "never captured, refused".

Reproduced end-to-end against a real `estates.db`:
```
refusal 1: landed=held    refusal 2: landed=held    refusal 3: landed=capture_refused
unreconciled_bids gate: []          inflight gate: []
highest_bid -> alice 50000 capture_refused
claim_bid_capture(win) -> None
losers released: [('bob', 40000)]
auction_closed -> True
settle BadState: winning bid 1 is 'capture_refused', not 'captured' ...
FINAL auction: closed  winner alice  hammer 50000  settled None
bids: [('alice','capture_refused'), ('bob','released')]
```
The lot is publicly announced (`changed=True` → `refresh_auction_message`) as won by Alice at 50,000. Alice paid nothing. Bob's 40,000 is released. The seller can never be paid — `build_auction_settle_run` correctly refuses forever (that N5 assert is the only thing preventing an actual coin loss here) — and the lot re-enters `close_auction` on the `_LOT_RESWEEP` backoff indefinitely. Before round 3 (`_TOP_BID_STATUSES` = `'held'` only) `highest_bid` would have returned Bob, Bob would have been captured, and the seller would have been paid.

**Minimal fix,** at `estates_main.py:1673`, before `auction_closed` — this keeps the legitimate resume case (bid already `captured` → `claim_bid_capture` returns `None`) and rejects the refused case:
```python
win_now = next((b for b in edb.bids_for_auction(int(auction_id))
                if int(b["id"]) == int(win["id"])), None)
if win_now is None or str(win_now["status"]) != "captured":
    log.error("lot %s: winning bid %s is %r, not 'captured' — not closing on a "
              "hammer nobody paid", auction_id, win["id"],
              win_now and win_now["status"])
    out["skipped"] = True
    return out
```
**New failure mode:** the lot now stays in `closing` instead of reaching `closed`. That is strictly better — every bid stays held, and `void_auction` accepts `'closing'` (`estates_db.py:1973`), so staff have a real exit instead of a lot that is closed, unsettleable, and missing all its losing bids.

---

### 3 — MEDIUM. A `capture_refused` stake is omitted from the pool with no warning anywhere, while the figure that would have said so is computed and discarded
**`estates_main.py:2627-2628`** (`stranded` tests `status == "held"` only) vs **`estates_db.py:2289-2292, 2307-2310, 2327-2328`** (`market_pools` returns `refused_stakes` / `refused_amount`).

`grep -n "refused" estates_main.py` returns no read of `pools["refused_stakes"]`, and `refused_hold_rows()` (`estates_db.py:1360-1384`) has **zero** callers in `estates_main.py`. So a stake core has definitively refused to capture is invisible to `_resolution_block`, invisible to `_unknown_warning`, invisible to `build_market_payout_run`, and invisible to staff. The market resolves without it, the punter's coins stay reserved under an open hold until expiry, and the proof embed reads 100 % paid. This is the same shape as round-2 Discord N2, moved into the new status.

**Minimal fix:** in `_stakes_left_out`, return a third bucket from `edb.REFUSED_STATUSES` and render it in `_unknown_warning` as "N stake(s), X coins — core refused these captures; those punters are not in this market and their coins are still reserved. `/admin → Reconcile holds`." Do **not** make it blocking (see 4).
**New failure mode:** none — it is a read and a rendered field.

---

### 4 — MEDIUM. `_resolution_block` makes a market with one permanently-refused capture unresolvable forever
**`estates_main.py:2655-2666`**, in combination with N6 being unwired.

Because `_outcome_known` is `False` for `frozen`/`insufficient`, such a stake ping-pongs `capture_unknown ↔ held` every 120 s (reproduced above), and `_resolution_block` refuses *both* phases: the `capture_unknown` phase as `doubt`, the `held` phase as `stranded`. `MAX_HOLD_REFUSALS` never trips, so the row never reaches `capture_refused` where it would stop blocking. One frozen punter therefore freezes the resolution of the whole market permanently. A void is the only exit and only lands if staff click during the `held` half of the cycle (`is_void=True` skips `stranded` but not `doubt`). Round 2's version let staff confirm — badly; round 3 lets nobody resolve at all.

**Minimal fix:** wire N6, which is a one-line change and makes the whole round-3 apparatus reachable —
```python
def _outcome_known(e: Exception) -> bool:
    return edb.outcome_known_for(getattr(e, "code", ""))
```
`estates_db.outcome_known_for` (`:181-193`) already excludes `hold_not_open`, `idempotency_in_progress`, `credit_failed` and every no-answer code, so S6's exclusion is preserved. With it, three refusals park the stake in `capture_refused`, which leaves `_resolution_block` (so the market resolves) and lands in `refused_hold_rows()` and `market_pools['refused_*']` (so finding 3's fix surfaces it). **New failure mode:** the market can now resolve while one punter is excluded — which is why finding 3's warning has to land in the same change; parking silently is exactly the round-2 N2 bug.

---

### 5 — MEDIUM. Head-of-line blocking: one persistently-retryable row now stalls the entire run, unboundedly
**`estates_main.py:1441-1449`** with **`:1400-1420`**.

`next_payout_row` (`estates_db.py:2766-2775`) always returns the *first* pending row in seq order. `_park_or_requeue` now returns a past-budget retryable row to `pending` forever instead of parking it, so on every pass the loop sees the same row 1, finds it in `attempted`, and **`break`s** — rows 2..200 are never attempted. Previously the row parked after 5 attempts and the rest of the run flowed. For `idempotency_in_progress` this self-clears after the 900 s takeover; for a per-row `LedgerUnavailable` it does not, and 199 winners wait on 1 at one attempt per 120 s.

**Minimal fix,** using a function already in the frozen interface (`estates_db.py:2778`):
```python
for nxt in edb.pending_payout_rows(int(run_id), limit=500):
    if int(nxt["id"]) in attempted:
        continue
    attempted.add(int(nxt["id"]))
    row = edb.claim_payout_row(int(nxt["id"]))
    ...
```
Per-row claim-first and per-row progress are unchanged (Rule 2 intact); only the "who is next" read moves from one-at-a-time to a snapshot list.
**New failure mode:** the snapshot can go stale — a row another worker paid mid-pass. `claim_payout_row` returns `None` for it and the existing `continue` handles that, which is the same guard the current code relies on.

---

### 6 — LOW. The v1 aliases build `_Idem` without `endpoint`, so the "checked rather than trusted" declaration guard never runs on them
**`ledger_v2.py:2101-2103`** (`_v1_adjust`) and **`:2130-2132`** (`_v1_transfer`). `_Idem.__init__` (`:550`) defaults `endpoint=""`, and `_finalize_idempotency:589` short-circuits on `if idem.endpoint and …`. Both paths claim with a genuinely in-band endpoint (`"adjust"` / `"transfer"`, `:2096`, `:2128`), so there is **no money bug today** — but the mechanism `IN_BAND_ENDPOINTS:470-475` leans on ("the proof is mechanical rather than editorial") is silently absent on two of the seven money paths. **Minimal fix:** pass `"adjust"` / `"transfer"` as the third positional argument in both constructors.

*(Separately, and unchanged from round 2: `_v1_transfer:2134` calls `transfer()` directly, and `transfer()` `:1490-1528` does not carry `h_transfer`'s `src == treasury or src == acting_user` rule. Still osentar-only, still **UNPROVEN** as an escalation.)*

---

### 7 — LOW / UNPROVEN. The AFTER INSERT guard uses an absolute floor, not the shortfall-may-not-grow rule
**`ledger_migrate.py:314-324`**: `WHEN open_total > 0 AND NEW.coins < open_total`. On a wallet that is *already* over-committed, an `INSERT OR REPLACE` that **raises** the balance (a repair) is refused, because it re-introduces exactly the absolute rule N2 removed from the UPDATE guard. Nothing in the uploaded Restocker files uses `REPLACE` on `balances`, so I could not construct a live failure — but the two guards now disagree about the same invariant, which is the shape N2 started as. **Minimal fix:** mirror the UPDATE rule; there is no `OLD` on an insert, so the honest version is `NEW.coins < open_total - (settling total)`, or leave it and note the asymmetry in the comment.

---

### Answers to the three targeted questions
- **What legitimate operation does the reworked guard break?** None that I could find. `ensure_wallet` no-ops, credits, affordable spends, full captures, partial captures and treasury debits all pass; the only refusals are over-committed writes and finding 7's unreachable repair case.
- **Can it deadlock?** No. All three triggers are `BEFORE`/`AFTER` row triggers whose only statement is `RAISE(ABORT)` — no nested write, no new lock. `settling` is only ever non-zero inside `capture_hold`'s `BEGIN IMMEDIATE`, and SQLite serialises writers, so no other transaction can observe or be blocked by it. `escrow_settling_leaks()` came back empty after every run, including the rolled-back capture.
- **Does it fire during ledger_v2's own capture?** No — reproduced on the healthy path, the partial-capture path, and the over-committed path. The one capture that failed (`capture B`) failed in `_debit`'s own WHERE clause with `insufficient`, not in the trigger, and rolled the hold back to `open` with `settling=0`.
- **Can a row get stuck in an in-doubt state forever?** `capturing`/`releasing`/`capture_unknown`/`release_unknown`: **no** — `holds_needing_reconcile` (`estates_db.py:1498-1512`) covers all four and `recovery_tick:3346` calls it every 120 s. `placing`/`place_unknown`: **yes, forever** — `placements_needing_replay` has no caller (finding N7 above).

---

## (c) Is there any remaining sequence that pays a user twice, charges them twice, or loses their coins?

**Yes — exactly one, and it is a double *charge*: `ledger_v2.py:1961`.**

> A user buys 7 shares. `exec_stock_trade` commits on the bot loop. `_resolve_out_of_band` commits `applied_unknown = 0`. The process is then killed, *or* `_complete_idempotency`'s COMMIT hits SQLITE_BUSY, *or* the `get_balance` between them raises. In the first two cases the claim sits `in_progress, applied_unknown=0` and the bank's retry takes it over 900 s later; in the third `_idempotent`'s `except` branch deletes the claim and the retry is granted immediately. Either way `exec_stock_trade` runs a second time: the user pays twice and receives 14 shares.

Reproduced twice, above. Fix and its consequence in finding 1.

**Everything else I could construct is a stall, a wrong-but-refused settlement, or a temporary loss of access — not a second money movement.** What I checked to conclude that:

- **Paid twice.** Every credit to a user goes through `execute_run` → `client.transfer` with a durable per-row domain key (`estates:market:<id>:payout:a<n>:user:<uid>`), and every hand-back path preserves that key: `fail_payout_row` (`estates_db.py:2863`) and `requeue_stuck_row` (`:2960`) only touch `status`. A replay therefore hits `_claim_idempotency`'s `state='done'` branch (`ledger_v2.py:694-697`) and returns the stored bytes. `transfer` is in-band, so `_finalize_idempotency` commits `done` inside the money transaction (`:1527`) — `in_progress` provably means "not applied", and the takeover at `:660-666` re-stamps `created_at`, which the original attempt's own claim-first completion (`:604`) then fails on, rolling its money back. Attempt-scoped keys (S2) make a re-proposed resolution a genuinely new run, and `build_market_reverse_run` (`:3027-3051`) reads back `status='paid'` rows rather than recomputing. I found no path that mints a second key for one domain event.
- **Charged twice — escrow.** `capture_hold:1228-1234`, `release_hold:1295-1301` and `sweep_expired_holds:1396-1402` are each a claim-first UPDATE gated on `state='open'` with a `rowcount` check inside `BEGIN IMMEDIATE`; `CHECK (captured_amount + released_amount <= amount)` (`ledger_migrate.py:98`) is the second net. The new `settling` write lives inside that same claim UPDATE and is cleared unconditionally in the same transaction, so it adds no window. `claim_stake_capture`/`claim_bid_capture` are claim-first on `status='held'`, so a resumed close cannot re-send a capture for a row already in flight, and the N5 gates now stop a second worker re-choosing a winner.
- **Charged twice — stakes/bids at the UI.** Round-2 Discord N1 is fixed: `_lock_view` (`estates_main.py:608-635`) sets the flag, pushes it with `response.edit_message`, and calls `stop()` in `finally`; `StakeConfirmView.confirm:930` uses it as the <3 s response.
- **Losing coins.** `estates` cannot mint, and every payout is bounded by `pari_mutuel_payouts`' `paid + treasury == pool` invariant (self-test case 6 passes). The two ways a user is separated from money without getting it back are both **bounded by hold expiry, not permanent**: a `place_unknown`/`placing` row (N7, up to 7 days for a wager, 24 h for a lot) and a `capture_refused` stake (finding 3, same TTL). Core's own sweeper releases both. The two ways *value* is lost are finding 2 (seller's sale, permanently unsettleable) and finding 3 (a punter's winnings on a bet whose stake is returned) — neither moves a coin the wrong way.
---

## Discord / product

## (a) Discord findings N1–N9, re-graded

| # | Verdict | Proof |
|---|---|---|
| **N1** | **VERIFIED FIXED** | See the round-trip proof below. |
| **N2** | **VERIFIED FIXED** | `_unknown_warning` now *returns* the block (`estates_main.py:2694`) and all three consumers act on it: `show_resolution_preview:2792` withholds `ProposeResolutionView` (`:2794-2801`), `proposal_embed:2855-2858` recolours + names it, `_confirm_view_for:2863-2871` returns `None` so the button is absent. Better than the proposed fix: `_confirm_guard:2697-2722` is re-run inside `build_payout_run:2725-2737`, which raises `edb.BadState` — a second entry point cannot reach `edb.build_market_payout_run` through this module without passing it. **But `_stakes_left_out:2609-2629` is now incomplete — see R3-3.** |
| **N3** | **VERIFIED FIXED** | `lifecycle_tick:3298-3312` re-derives the work from the DB every 60 s (`highest_bid` + `losing_held_bids`) instead of trusting one dropped `spawn`. Stronger than the suggested fix — it only spawns when there is actually a losing held bid, so a quiet lot costs two sqlite reads and no HTTP. |
| **N4** | **VERIFIED FIXED** | `ReconcileView.go:2566-2582` → `spawn("reconcile", _reconcile_and_prove(...))`; `_reconcile_and_prove:1770-1798` posts to the proof channel; `reconcile_holds:1738-1742` logs every 25 rows. No inline HTTP loop is awaited on an interaction anywhere in the file. |
| **N5** | **PARTIALLY FIXED** | `_send_picker:1941-1958` + `PickerView.shown/hidden:1922-1923` now state the truncation out loud, and the three panels order in-flight subjects first (`:2093-2094`, `:2476-2477`, `:2519-2524`). Residue is real — see R3-4. |
| **N6** | **VERIFIED FIXED** | `place_bid:1167-1178` re-reads `edb.min_next_bid` immediately before `edb.create_bid:1180`, with no `await` between the check and the write, so there is no TOCTOU window; the refusal names the cause ("somebody bid while you were confirming"). |
| **N7** | **VERIFIED FIXED, twice over** | `close_auction:1604-1612` gates the claim-free resume path (`claimed is None`) on no bid being in `capturing`/`releasing`. Independently, estates_db widened `unreconciled_bids` to `IN_DOUBT_STATUSES` (`estates_db.py:127`, gate at `estates_main.py:1589`) and `highest_bid` to `_TOP_BID_STATUSES` (`estates_db.py:1249-1250`) so a resumed close re-selects the *same* winner. |
| **N8** | **VERIFIED FIXED** | `_LOT_RESWEEP:416`, backoff at `lifecycle_tick:3313-3332`, and `close_auction`'s `changed` flag (`:1614, 1676, 1697, 1700`) gates the `refresh_auction_message` at `:1705-1706`. A parked lot now costs zero REST calls per pass. |
| **N9** | **VERIFIED FIXED** | `show_stake_preview:867-882` checks `min_stake`/`max_stake` before rendering any figure, matching `estates_db.create_stake:2086-2089` exactly. |

### N1 — the headline. The guard survives the round trip; it is not a local flag.

Three independent layers, at `estates_main.py:584-599` (`SafeView._used` / `_claim_click`) and `:608-636` (`_lock_view`), used at `:921-931` and `:1141-1149`:

1. **`_claim_click()` is called synchronously at the very top of `confirm`, before any `await`.** On one event loop two clicks are two tasks; the second cannot start until the first yields, so the check-and-set is atomic. This is the layer that catches the actual double-click (the second interaction is already in flight when the first one starts).
2. **`_lock_view` pushes the disabled state with `interaction.response.edit_message` (`:629`)** — the thing round 2 was missing — and that doubles as the sub-3-s response, which is why `confirm` deliberately does not defer.
3. **`view.stop()` in `finally` (`:636`)** calls `ViewStore.remove_view`, which deletes the item from `_views[message_id]`. Verified directly against discord.py 2.7.1: registered `True` → after `stop()` `False`. `ViewStore.dispatch_view` then finds nothing and returns, so a later click never reaches the handler at all. This is the layer that needs no network.

Functional proof (two concurrent clicks, 20 ms network on the edit, 50 ms on `POST /hold`):

```
click 1 log: ['edit_message(disable)', ('followup', 'Stake placed')]
click 2 log: ['defer', ('followup', 'Refused: that stake was already submitted …')]
HOLDS sent to core: [('999', 5000, 'estates:market:1:stake:1')]
stake rows:         [(1, 'held', 5000, 'estates:market:1:stake:1')]
RESULT: ONE stake
```

Cross-process is also safe for the opposite reason: a second bot process never created this non-persistent view, so its `ViewStore` has no entry and it cannot dispatch the click.

**Residue (cosmetic):** a click that arrives *after* `stop()` is discarded silently — the user gets Discord's red "This interaction failed" rather than the friendly line. Only clicks in the window between `_claim_click` and `stop()` get the nice message.

---

## (b) Boot check — the numbers

Run against the real modules with `ast` + `inspect.signature().bind()`; client aliases discovered transitively from the module global `ledger` (found `ledger`, `client`).

| check | result |
|---|---|
| distinct `edb.*` names referenced | **99**, **0 missing** |
| `edb.*` names outside the frozen `ESTATES_DB_INTERFACE.md` | **0** — nothing reaches past the contract |
| distinct `ledger.*` names | **8** (`balance, close, get_hold, hold, hold_capture, hold_release, list_holds, transfer`), **0 missing** on `LedgerClient` |
| `edb.*` + `ledger.*` call sites bound against real signatures | **270 bound, 0 arity/keyword failures**, 1 skipped (`edb.BadState(...)` at `:2735`, an exception constructor with no introspectable signature) |
| coroutine call sites awaited | `edb` exposes 0 coroutines; `LedgerClient` exposes 26; **all 14 client call sites awaited, 0 un-awaited**. The single un-awaited local async call is `asyncio.run(_close_ledger())` at `:3440` — correct usage |
| `from estates_db / ledger_client import …` names | **18, all resolve** |
| `python3 -m pyflakes` | **clean, exit 0** on `estates_main.py`, `estates_db.py`, `ledger_client.py`. I changed nothing |

**Interface drift, worth one line:** `estates_db` has grown public API since the doc was frozen — `refused_hold_rows`, `placements_needing_replay`, `reconcile_stake_placement`, `reconcile_bid_placement`, `outcome_known_for`, plus `IN_DOUBT_STATUSES`, `REFUSED_STATUSES`, `PLACEMENT_IN_DOUBT_STATUSES`, `DEFINITE_REFUSAL_CODES`, `MAX_HOLD_REFUSALS`. Nothing was removed. `estates_main` uses **none** of them, and that is the substance of R3-1/2/3. `ESTATES_DB_INTERFACE.md` needs regenerating.

**Clean on re-check:** modals — 4, `TextInput`-only, ≤5 each (`NewMarketModal`/`NewLotModal` exactly 5), no select, no autocomplete. Views holding free text — zero (AST scan). Typed IDs/exact names — zero; every subject is a select. Persistent handlers — all four `DynamicItem`s re-resolve via `_resolve_market:664-673` / `_resolve_auction:676-685`; `OutcomeButton` carries an index and looks the id up at click time (`:822-827`). Defer-in-3s — the six handlers that don't defer are the six that send a modal (`:811, 1073, 1988, 2016, 2076, 2288`), which *is* the response, plus `confirm`, where `_lock_view`'s `edit_message` is the response. `response.send_message` after a defer — one call site (`:280`), behind `is_done()`. `allowed_mentions` — every send/edit carries `NO_PINGS`; the only bare one is `:629`, which sends neither content nor embed. Component budgets — max 4 decorated items + 2 selects; `MarketView` = 11 items over 3 rows; every select goes through `_PickSelect`, which slices to `PICKER_MAX`. Money click without figures — none. Decorated empty state — none.

---

## (c) New defects

Round 3's fixes landed almost entirely in `estates_db.py`. `estates_main.py` was not updated to match, and the three findings below are all that gap.

### R3-1 — HIGH, live today. `_outcome_known` contradicts `edb.outcome_known_for`, which estates_db states can never happen
**`estates_main.py:340-348`** vs **`estates_db.py:155-193`**

`estates_db.py:156-158` says of `DEFINITE_REFUSAL_CODES`: *"This is the ONE place that judgement lives, so estates_main's `_outcome_known` and this module's refusal counter can never disagree about the same error string."* They disagree on eight codes — `insufficient`, `frozen`, `escrow_shortfall`, `forbidden_hold`, `forbidden_scope`, `forbidden_source`, `unauthorized`, `idempotency_conflict`:

```
edb.outcome_known_for('frozen') = True   |   _outcome_known(AccountFrozen) = False
```

`_unclaim_hold_row` only increments `refusals` when the caller passes `outcome_known=True` (`estates_db.py:1330-1333`), so **the whole N6 parking mechanism is unreachable**. Verified against a real DB, a frozen wallet, three full sweeps:

```
sweep 1: status='capture_unknown' refusals=0  -> after reconcile_holds: 'held'
sweep 2: status='capture_unknown' refusals=0  -> after reconcile_holds: 'held'
sweep 3: status='capture_unknown' refusals=0  -> after reconcile_holds: 'held'
refused_hold_rows(): []
_resolution_block: "1 stake(s) — 5,000 coins — never reached the treasury…"
```

Failure: punter's wallet is frozen at close. `close_market:1336-1339` gets `AccountFrozen`, files `capture_unknown`; `recovery_tick:3346` reconciles it back to `held` 120 s later; the 600 s re-sweep refuses it again; forever. One `POST /hold/capture` + one `GET /hold` per stake per cycle, indefinitely — money-N6's ping-pong, unfixed on this side. And `_resolution_block:2649-2666` blocks on `held` *and* on `capture_unknown`, so **the market is permanently unresolvable**, with the void escape hatch (`is_void=True` tolerates `stranded` but not `doubt`, `:2650-2655`) available only in whichever half of the 120 s/600 s race the row happens to be `held`. That is verbatim the symptom `estates_db.py:160-164` claims was fixed.

**Minimal fix**, `estates_main.py:340-348` — delegate instead of duplicating:
```python
def _outcome_known(e: Exception) -> bool:
    return edb.outcome_known_for(getattr(e, "code", ""))
```
`hold_not_open`, `idempotency_in_progress`, `credit_failed`, `unavailable`, `internal_error`, `rate_limited` are already absent from `DEFINITE_REFUSAL_CODES` (`estates_db.py:166-178`), so S6's guarantee is preserved by the same edit.

**New failure mode this introduces:** it makes `capture_refused` reachable for the first time — which is exactly R3-3. **Do not ship R3-1 without R3-3.**

---

### R3-2 — HIGH, live today. N7's replay machinery exists and is wired to nothing; definite refusals are filed as `place_unknown`
**`estates_main.py:974`, `:981`, `:1198`, `:1202`** vs **`estates_db.py:1550-1589`, `:1661-1685`, `:2133-2156`**

`fail_stake`/`fail_bid` changed signature: they now take `outcome_known: bool = False` and default to `place_unknown`, not `failed`. estates_main calls them positionally at all four sites. Two consequences, both verified:

```
insufficient at POST /hold  -> estates_main lands the row in 'place_unknown'
                               (passing outcome_known=True would give 'failed')
unconfirmed hold            -> 'place_unknown'                     (correct)
placements_needing_replay() -> [(2,'place_unknown','u2',5000,'estates:market:2:stake:1'),
                                (3,'place_unknown','u3',7000,'estates:market:2:stake:2')]
holds_needing_reconcile()   -> []
```

1. **`:974` and `:1198`** are the `except (InsufficientFunds, AccountFrozen, BadRequest, IdempotencyConflict)` branches — core provably refused, no hold exists — yet the row is filed as "a hold may be sitting at core". If a replay pass is ever added it will re-send `POST /hold` for a punter who never confirmed anything and may since have topped up, creating a stake nobody asked for.
2. **Nothing calls `placements_needing_replay` / `reconcile_stake_placement` / `reconcile_bid_placement`.** So for the genuine case (`:981`, `:1202`) the *only* change from round 2 is the row's label. The punter's coins are still reserved under a hold nothing can name for `WAGER_HOLD_GRACE_S` (7 days past close, `:130`), the row is invisible to `holds_needing_reconcile` (needs `hold_id`) and to `refused_hold_rows`, and the ephemeral at `:989-992` / `:1205-1208` still says "nothing was taken" — which `estates_db.py:2141-2150` now explicitly names as the bug.

**Minimal fix:** pass the flag at the two definite-refusal sites — `edb.fail_stake(int(stake["id"]), _code(e), outcome_known=True)` at `:974`, same at `:1198` — and add a replay pass to `recovery_tick:3337-3350`, mirroring `reconcile_holds`: for each row from `edb.placements_needing_replay(older_than_seconds=RECONCILE_AGE_S)`, re-send `client.hold(user_id, amount, idempotency_key=row["idem_key"])` (core replays and returns the original `hold_id`) and record it with `edb.reconcile_stake_placement(id, hold_id=…, hold_state=…)`, or `error=` on a definite refusal.

**New failure mode:** the replay pass re-sends a `POST /hold` for a row whose original request may never have reached core. That is safe only because the key is durable and identical — if the key ever became non-deterministic this pass would double-hold. It also adds one HTTP call per stranded placement per 120 s tick, so it needs the same `older_than_seconds` gate `reconcile_holds` uses, not an immediate retry.

---

### R3-3 — HIGH, armed. `capture_refused` / `release_refused` are invisible to every estates_main surface, including the guard N2 added
**`estates_main.py:2609-2629` (`_stakes_left_out`), `:491`, `:2204`, `:3004`, `:1034`, `:1279`**

`_stakes_left_out` enumerates exactly two populations — `edb.unreconciled_stakes` (IN_DOUBT) and `status == "held"`. Its docstring explicitly reasons about `placing`/`place_unknown`, so the author tracked estates_db's N7 additions, but `capture_refused`/`release_refused` are in neither category. Forced a row into `capture_refused` on a real DB (5,000 refused, 3,000 captured):

```
stake statuses:        [(1,'u1','capture_refused',3,'hold-1'), (2,'u2','captured',0,'hold-2')]
_stakes_left_out ->    doubt: []   stranded: []
_resolution_block ->   None                       <- market resolves freely
market_pools:          total_pool 3000, unknown_stakes 0, refused_stakes 1, refused_amount 5000
market_embed fields:   Pool / Rake / Stake limits / Closes   <- no warning of any kind
_position_embed(u1):   "You have no live stakes on this market."
release_market_holds:  released 0  -> u1's status still 'capture_refused'
refused_hold_rows():   [(1,'u1',5000,'capture_refused','hold-1')]
```

Every consequence is concrete. u1's 5,000 is **still reserved under an open hold at core** (`estates_db.py:130-134`: "the hold is still open at core"), for up to 7 days past close. `release_market_holds:1279` and `run_release_void` iterate `status='held'`, so **a void does not release it**. `_position_embed:1034` and `positions_embed:1890` filter to `("held","capturing","captured")`, so the punter's own screens say they have no stake while their wallet shows coins reserved with nothing to explain them. The market message (`:491`), the close preview (`:2204`) and the void preview (`:3004`) read only `unknown_stakes`/`unknown_amount` and never `refused_stakes`/`refused_amount` — which `estates_db.py:2289-2292` computes *specifically* so "staff should be told". That is round-2 N2's exact shape ("computes the signal and both callers throw it away"), one layer down.

This is unreachable **only** because R3-1 keeps `refusals` at 0. Fix R3-1 alone and it goes live the same day.

**Minimal fix**, `_stakes_left_out:2609-2629` — return a third population and render it without blocking (core proved these coins never moved, so they must not deadlock a resolution the way `doubt` does):
```python
refused = [s for s in edb.stakes_for_market(int(market_id))
           if str(s["status"]) in edb.REFUSED_STATUSES]
```
name it in `_unknown_warning:2670-2694` ("N stake(s), X coins — core refused these captures; the punters keep the coins, they are not in this market, and their holds are still open"), add `refused_stakes` to `market_embed:491` and `on_pick_void:3004`, and add `edb.REFUSED_STATUSES` to the `live` filters at `:1034` and `:1890` so the punter sees the truth. Separately: `release_market_holds` should release the still-open hold on a refused row during a void, or `refused_hold_rows()` needs a staff surface — right now nothing in the bot ever names a parked row.

**New failure mode:** rendering `refused` non-blocking means a market *can* resolve while a punter is out of it. That is the correct trade (blocking on a permanently-refused capture is R3-1's deadlock), but only if the field is on the screen — a silent non-block is strictly worse than today.

---

### R3-4 — MEDIUM. N5 residue: "we told you" is not the same as "you can reach it", and Void has no other route
**`estates_main.py:2494-2496`**

`_send_picker` now says how many are hidden, which is honest, but a market past the 25th is still actionable from nowhere. For Close that is survivable — `lifecycle_tick:3276-3278` closes it on its clock. **Nothing ever voids a market automatically**, so with 26+ markets in `resolving`+`closed`+`closing`+`open` the 26th cannot be voided from any surface, and Rule 5 forbids the obvious workaround of typing its id. Compounding it, `total` under-reports: each `edb.list_markets(status=st, limit=25)` is itself capped at 25, so with 40 open markets the line reads "Showing 25 of 50" when the true count is higher.

**Minimal fix:** raise the per-status `limit` so `total` is truthful (`limit=100`, it is one indexed sqlite read), and give `PickerView` a "Next 25" button that re-sends the picker with an offset. **New failure mode:** paging state on a `timeout=600` view is placeholder state after a restart — the offset must be carried in the callback closure and the list re-derived from the DB on each page, never cached.

### R3-5 — LOW. Seven copies of the idiom N1 proved does nothing, sitting next to the helper that replaces it
`estates_main.py:2224, 2428, 2818, 2932, 3046, 3091, 3207` — `for child in self.children: child.disabled = True`, every one of them after an `await _defer(...)`, so none is ever pushed to Discord. All seven are genuinely protected by something real (`spawn` dedup, `claim_resolution_confirm`, `claim_resolution_reverse`, `set_market_status`), so no money is at risk — but the file now contains both the broken idiom and `_lock_view`, and the next reader cannot tell which one is load-bearing. `ProposeResolutionView.propose:2817` is the one with a visible symptom: a second click gets `edb.propose_resolution`'s raw refusal, "this market already has a live resolution; unresolve it first", which reads as staff error. **Minimal fix:** delete the seven loops; where a friendlier second-click message is wanted, use `_claim_click()` — it is already on `SafeView` and costs one line.

### R3-6 — LOW. After `_lock_view` the punter gets no progress signal for up to ~90 s, and two paths return in total silence
`estates_main.py:930` / `:1148`. `_lock_view`'s `edit_message` is the response, so there is no "thinking" indicator — deliberate and necessary, but `place_stake` then runs `_hold_with_retry` (2 attempts × `LedgerClient(timeout=15.0, retries=2)` + a 1 s sleep, `ledger_client.py:504`), ≈90 s worst case, during which the only feedback is greyed buttons. And `place_stake:962-963` / `place_bid:1187-1188` `return` after a lost claim with no reply at all — with the spinner gone, that is now a completely silent dead end. **Minimal fix:** have `_lock_view` also set the embed footer to "Placing…" in the same `edit_message`, and give those two branches a `_reply`.

---

**Files:** `/home/claude/build/estates_main.py`, `/home/claude/build/estates_db.py`. Test harnesses: `/tmp/n1/test_n1.py` (N1 double-click), `/tmp/n1/test_new.py` (R3-1, R3-2), `/tmp/n1/test_refused.py` (R3-3), `/tmp/bootcheck.py`, `/tmp/n1/ui_sweep.py`.