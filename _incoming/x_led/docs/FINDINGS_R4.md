# Round 4 review

## (a) Scorecard — round‑3's surviving items

All verdicts below were run against real temp DBs unless marked *(read only)*. **No files in `/home/claude/build` were edited**; `python3 -m pyflakes` is clean (exit 0) on all five modules. Harnesses: `/tmp/r4/h1.py`…`/tmp/r4/h6.py`, `/tmp/r4/m1.py`.

| # | Verdict | Proof |
|---|---|---|
| **money N1 (CRITICAL)** | **VERIFIED FIXED** | `ledger_v2.py:2084-2096` — success sets `slot["body"]` and returns; `_resolve_out_of_band` is now called on the refusal path only. Both round‑3 variants reproduced as *closed* (`/tmp/r4/m1.py`, cases B and C): with `_complete_idempotency` raising `database is locked`, the row stays `in_progress, applied_unknown=1`, ageing it 100 000 s and retrying returns **409 `idempotency_unresolved`** + the ERROR log, and `exec_stock_trade` ran **once**. With `get_balance` raising after the trade committed, the immediate retry is likewise refused and the trade ran **once**. Case D confirms the refusal path still deletes the claim, so a corrected retry is not blocked for 30 days. The accepted cost in §6 is exactly what the code now does. |
| **money N4** | **VERIFIED FIXED (both halves) — with one hole, see R4‑2** | Second half now exists *and is called*: `estates_db.unpark_payout_row` (`estates_db.py:3003-3085`) ← `UnparkConfirmView.go` (`estates_main.py:3853-3882`) ← `run_progress_embed`'s "Action needed" (`estates_main.py:1704-1714`). Reproduced (`h3.py`): a row parked with `attempts=5` un‑parks to `pending`, `attempts` resets to 0, `failed_rows` decrements, the run reopens from `failed`→`pending`, `execute_run` pays it, run reaches `done`, and every attempt carried the identical key `estates:market:1:payout:a1:user:u1`. |
| **money N6** | **VERIFIED FIXED** | `estates_main.py:368` is now `return edb.outcome_known_for(getattr(e, "code", ""))`, with an import‑time truth‑table assert at `:371-393`. Reproduced (`h1.py`): a frozen punter's 5 000 stake goes `held(refusals 1) → held(2) → capture_refused(3)`; the ping‑pong stops after three HTTP calls instead of running forever; it lands in `refused_hold_rows()` and `market_pools['refused_stakes']=1 / refused_amount=5000`. |
| **money N7** | **VERIFIED FIXED** | `replay_placements` (`estates_main.py:1978-2088`) is wired into `recovery_tick` (`:4050-4056`). Reproduced (`h6.py`): a `place_unknown` stake is replayed with the identical key `estates:market:1:stake:1`, core returns hold `H-1`, `GET /hold` says `open`, `reconcile_stake_placement` lands it `held`, and the 5 000 joins the pool. The definite‑refusal site now passes `outcome_known=True` (`estates_main.py:1047-1049`, `:1309-1311`) and lands in `failed`, so "nothing was taken" is finally true when it is said. |
| **Discord N5** | **VERIFIED FIXED** *(read only)* | `_send_picker` (`estates_main.py:2348-2400`) takes a zero‑arg callable re‑run per page, pages with Next/Previous, and computes `real_total` from the full list — the "Showing 25 of 50" lie is gone. Guards the now‑reachable empty page at `:2367-2375`. |
| **R3‑1** (`_resolve_out_of_band` ordering) | **VERIFIED FIXED** | As money N1. |
| **R3‑2** (`capture_refused` winner) | **VERIFIED FIXED** | The winner is re‑read *above* the loser‑release loop, `estates_main.py:1826-1852`. Reproduced (`h2.py`): passes 1‑3 refuse Alice's 50 000 capture; pass 4 finds `capture_refused`, logs `winning bid 1 is 'capture_refused', not 'captured'`, returns `skipped/parked`, leaves the lot in `closing` with **both** bids still held (Bob's 40 000 is *not* released) and `changed=False` so nothing is announced. Staff release → Alice `released` → next sweep closes on Bob at 40 000 and pays the seller 39 000. Round‑3's "lot stolen and unsettleable forever" is gone and the recovery route actually works. |
| **R3‑3** (refused stake invisible) | **VERIFIED FIXED** | `_stakes_left_out` returns a third bucket (`estates_main.py:3193-3223`), `_unknown_warning` renders it first (`:3271-3282`), `_LIVE_STAKE_STATUSES` now includes `edb.REFUSED_STATUSES` (`:426`) so the punter's own screens say "DID NOT GO THROUGH", `release_market_holds` uses `RELEASABLE_STATUSES` (`:1410-1411`) so a void does free them, and there is a real staff exit (`ReconcileView.release_parked` → `release_parked_holds`, `:3070-3143`). Reproduced in `h1.py`/`h2.py`. |
| **R3‑4** (`_resolution_block` deadlock) | **VERIFIED FIXED** | `h1.py`: with one stake parked in `capture_refused`, `_resolution_block(mid)` → `None`, `_confirm_guard` → `None`, pool = 3 000. Non‑blocking + rendered, as prescribed. |
| **R3‑5** (head‑of‑line blocking) | **NOT FIXED — reproduced** | `estates_main.py:1595-1603` is unchanged; `pending_payout_rows` still has no caller in `estates_main.py`. See R4‑1. |
| **R3‑6** (v1 aliases build `_Idem` without `endpoint`) | **NOT FIXED** *(read only)* | `ledger_v2.py:2222` and `:2251` still call `_Idem(key, claim_ts, body_fn=…)`; `endpoint` defaults to `""` (`:552`), so `_finalize_idempotency`'s declaration guard (`:589`) short‑circuits on 2 of 7 money paths. No money bug today (both claim as `adjust`/`transfer`, both in‑band). The separate parenthetical is also unchanged: `transfer()` (`ledger_v2.py:1553`) still lacks `h_transfer`'s `forbidden_source` rule (`:1861-1870`) and `_v1_transfer` calls it directly — still osentar‑only, still UNPROVEN. |
| **R3‑7** (AFTER INSERT absolute floor) | **NOT FIXED** | `ledger_migrate.py` was not touched this round (mtime 20:28 vs 21:27 for the estates files). `HOLD_GUARD_INSERT_DDL:314-324` still uses `NEW.coins < open_total` with no `settling` term, so the two guards still disagree about the same invariant. Still unreachable in the uploaded code. |
| **R3‑5 (Discord, seven dead loops)** | **NOT FIXED** | Seven `for child in self.children: child.disabled = True` after a `_defer` remain at `estates_main.py:2666, 2872, 3424, 3538, 3661, 3706, 3898`, next to `_lock_view` (`:682-710`) which explains why they do nothing. Cosmetic; all seven are protected by something real. |
| **R3‑6 (Discord, silent dead end)** | **NOT FIXED** | `estates_main.py:1036-1038` and `:1298-1300` still `return` with no reply after a lost claim, and `_lock_view` still pushes no "Placing…" footer. |

---

## (b) Defects round 4 introduced

### R4‑1 — HIGH, reproduced. One retryable row still stalls the entire payout run, and now nothing on any screen says so
**`estates_main.py:1595-1603`** with **`:1546-1550`**.

Round 3 named this and gave the two‑line fix (`edb.pending_payout_rows` snapshot). It was not applied; `pending_payout_rows` (`estates_db.py:2798`) still has zero callers in `estates_main.py`. Reproduced (`h3.py`, 5‑row run, row 1 fails `LedgerUnavailable`):

```
pass 1..7: finish=running paid=0 pending=5 failed=0   transfers=1,2,3,…,7 — ALL to u1
rows: [(1,'u1','pending',7,'unavailable'), (2,'u2','pending',0,None), … (5,'u5','pending',0,None)]
```

Rows 2‑5 are never attempted, on any pass, for as long as row 1 keeps failing. Round 4 made this *worse* to detect, not better: because `_park_or_requeue` correctly refuses to park a retryable row, `failed_rows` stays 0 — so `run_progress_embed`'s "Action needed" block (`:1704`) never renders, `UnparkConfirmView` has nothing to offer, and the market sits in `paying` indefinitely with a panel reading "0 failed". 199 winners wait on one, invisibly. **Minimal fix is still round‑3's:** iterate `edb.pending_payout_rows(run_id, limit=500)` and `continue` past `attempted`, keeping the per‑row claim.

### R4‑2 — HIGH, reproduced. `unpark_payout_row`'s reversal guard also blocks the reversal's own rows, freezing the market permanently
**`estates_db.py:2996-3001`** (`_UNPAYABLE_RESOLUTION_STATES`) against **`:3037-3043`**, with **`build_market_reverse_run:3171-3174`** (which sets `resolution_id` on the reverse run itself).

A clawback row parks by design — `build_market_reverse_run`'s own docstring says it "fails loudly per row if the coins are gone", and `InsufficientFunds` is `_permanent` (`estates_main.py:412`). The row's run *is* the reversal, so its resolution is in `reversing`, so the un‑park guard refuses it. Reproduced (`h4.py`):

```
reverse run: failed   rows: [(3,'u1','in','failed','insufficient: they spent it'), (4,'u2','in','paid')]
unpark row 3 -> refused:resolution_reversing
execute again: None            res state: reversing   market: paid
re-propose refused: market 1 is paid; close it before resolving
```

u1 keeps 5 550 coins that the reversal exists to recover; the resolution can never reach `reversed`, so the market can never return to `closed`, so the corrected resolution the UI tells staff to make ("re‑resolve as a new attempt instead, which mints new keys", `estates_main.py:3866-3870`) is refused by `propose_resolution` (`estates_db.py:2445-2446`). Before round 4 there was no un‑park at all, so this is the new part: the only exit now exists, is offered, and explicitly declines the one run kind where parking is expected. **Minimal fix:** exempt the reversal from its own guard —
```python
if (run is not None and run["resolution_id"]
        and str(run["kind"]) != "market_reverse"):
```
(direction‑`in` rows recover money; the guard exists to stop paying money *out* under a withdrawn decision).

### R4‑3 — MEDIUM, reproduced. `release_refused` is newly reachable, has no exit, and is missing from the one panel that names parked rows
**`estates_db.py:146`** (`RELEASABLE_STATUSES` excludes it, correctly) vs **`estates_main.py:3001-3002, 3111-3112, 3149-3150`** (all three staff surfaces filter `refused_hold_rows()` down to `RELEASABLE_STATUSES`).

`_outcome_known` returning True for `frozen` is what makes this state reachable at all, so it is a round‑4 consequence. Reproduced (`h5.py`), frozen punter, voided market:

```
1 (0,0) held/refusals=1 → 2 held/2 → 3 release_refused/3
refused_hold_rows(): [(1,'release_refused',5000)]
panel 'parked' population: []        release_parked_holds: released 0
```

5 000 coins stay reserved at core until the hold's TTL, and the row appears on **no** admin surface. It does surface on the market screen — but under text that names a remedy which skips it: `_unknown_warning` says "core refused these **captures** … Free them with `/admin → Reconcile holds → Release the refused holds`" (`estates_main.py:3271-3282`), and that button's population excludes `release_refused`. **Minimal fix:** list `release_refused` in the Reconcile panel as its own field ("core refuses to release these; the coins free themselves when the hold expires at *&lt;hold_expires_at&gt;* — unfreeze the wallet to fix it sooner") and split the wording in `_unknown_warning` by status.

### R4‑4 — MEDIUM, reproduced, PLAUSIBLE in production. `_stock_trade` infers "definite refusal" from a falsy `ok`, including from no answer at all
**`ledger_v2.py:2074`** (`r = r or {}`) and **`:2084-2096`**.

The flag‑clear is now the *only* thing that lets a stock key be released, which makes the test that guards it load‑bearing — and the test is "the answer I could not validate did not have `ok` truthy". Reproduced (`m1.py`, case E) with `exec_stock_trade` returning `None` after recording the trade:

```
status 200 ok False   idem: []            <- claim DELETED
retry -> 200 ok True | exec_stock_trade ran 2 times
```

`None` is not a refusal; it is silence, which is the exact thing the `applied_unknown` flag exists to represent. `Restocker_main` is not in this repo so I cannot prove `exec_stock_trade` ever returns it — but `run_on_bot_loop` has a 20 s timeout and, per `LAND_EXCHANGE_AUDIT.md:295`, "the sync core, once started, cannot be cancelled and runs to completion", which is precisely the shape that produces a non‑answer over a committed trade. Any `ok=False` return after a partial application does the same. **Minimal fix (one condition):**
```python
refused = isinstance(r, dict) and r.get("code") in DEFINITE_STOCK_REFUSALS
if refused:
    _resolve_out_of_band(slot["idem"])
```
leaving anything else `applied_unknown=1` → `idempotency_unresolved` + a human. Same trade the module already accepts everywhere else.

### R4‑5 — LOW, reproduced. A refused trade whose flag‑clear fails locks its key for 30 days
`m1.py` case F: `_resolve_out_of_band` raising leaves `state=in_progress, applied_unknown=1`; the retry gets 409 forever until someone deletes the key. Loud (the UNRESOLVED log fires on every retry) and strictly safer than the alternative, so acceptable — but it means a *refused* trade, where nothing moved, now needs a human. Worth one sentence in §6.

### R4‑6 — LOW. The "ONE place that judgement lives" is still two places
`estates_db.py:180-182` claims `DEFINITE_REFUSAL_CODES` is the single home for that judgement. `_outcome_known` now delegates, but `estates_main.py:1047` and `:1309` still hand‑code `(InsufficientFunds, AccountFrozen, BadRequest, IdempotencyConflict)` — a 4‑class subset omitting `EscrowShortfall`, the three `Forbidden*`, `Unauthorized` and `HoldNotFound`. Effect is benign (those land in `place_unknown` and `replay_placements` resolves them to `failed` on the next tick, verified in `h6.py`), but the invariant the docstring asserts is still not mechanically true. **Fix:** `except LedgerError as e: … if edb.outcome_known_for(_code(e)): fail_stake(..., outcome_known=True)`.

### Checks that came back clean
- **Refused trade locking its key forever / successful trade stranding one:** only R4‑5 and the documented accepted cost. Case D proves the refusal path releases the claim.
- **Lot stolen by the second bidder:** no. `h2.py` — the runner‑up is only promoted after a human releases the refused hold, and then at his own price. Losing bids are *not* released while parked, which is what preserves the runner‑up.
- **Lot parking forever:** it parks until staff act, which is the intended trade; it is listed on the Reconcile panel with the exact verb and the lot's `_LOT_RESWEEP` backoff means zero REST cost per pass.
- **`unpark_payout_row` resurrecting a correctly‑parked row / double‑paying:** no. `attempts=0`, `failed_rows` decrement, run reopen and `proof_message_id=NULL` are all correct and all necessary; the reversal guard is right in intent (too broad in scope — R4‑2); the key is untouched, so every retry is a replay (`h3.py`: 8 sends of one key, one payment). The one path where a market could be both refunded and paid is closed upstream: `propose_resolution` refuses any market not in `closed`/`resolving` (`estates_db.py:2445`), so a void cannot be proposed over a half‑paid payout run.
- **`outcome_known_for` reclassifying something into a coin strand:** the widened set is `insufficient / frozen / escrow_shortfall / forbidden_* / unauthorized / hold_not_found / bad_* / idempotency_conflict`. Each is contractually "the transaction rolled back" (§5.1, §7), so returning the row to `held` is correct, and the three‑strike park bounds the retry loop. The one new landing state is R4‑3. One cosmetic wart: `refusals` is shared between the capture and release counters (`estates_db.py:1340-1345`), so a row with 2 capture refusals parks on its *first* release refusal.
- **`placing`/`place_unknown` race with a live handler:** none. `placements_needing_replay` returns `place_unknown` at any age but `placing` only past `older_than_seconds` (900 s), well beyond `_hold_with_retry`'s ~90 s worst case (`estates_db.py:1592-1594`).

---

## (c) Is there still a sequence in which a user is paid twice, charged twice, or loses coins?

**One, and it is a double *charge*, and it is conditional on a module I cannot read: `ledger_v2.py:2074` (R4‑4).** If `exec_stock_trade`/`run_on_bot_loop` ever answers `None` — or any dict without a truthy `ok` — *after* the trade has committed on the bot loop, `_stock_trade` reads that as a definite refusal, clears `applied_unknown`, `_idempotent` deletes the claim, and the retry buys the same shares again. Reproduced in `m1.py` (case E: two executions, no wait). The `run_on_bot_loop` 20 s timeout over an uncancellable sync call is the plausible production trigger. Everything else about the flag is now correct; this is the one place its clearing is decided by a value rather than by a protocol.

**Everything else I could construct is a stall, a park, or a bounded reservation — no second money movement.** What I checked:

- **Paid twice.** Every credit goes through `execute_run` → `client.transfer` with the durable per‑row key, and every hand‑back path (`fail_payout_row:2868`, `requeue_stuck_row:2977`, `unpark_payout_row:3054`) touches `status`/`attempts` only, never `idem_key` — verified by running 8 attempts on one row and seeing one key (`h3.py`). Attempt‑scoped keys make a corrected resolution a genuinely new run. `build_market_reverse_run` reads back `status='paid'` rows rather than recomputing. The outer bound is core's 30‑day key TTL: a row left `claimed` for over 30 days and then requeued would re‑send a key core has swept — unreachable while `recovery_tick` runs every 120 s.
- **Charged twice — escrow.** `capture_hold:1228`, `release_hold:1295`, `sweep_expired_holds:1396` are claim‑first inside `BEGIN IMMEDIATE` with the `CHECK (captured+released <= amount)` net behind them; `claim_stake_capture`/`claim_bid_capture` are claim‑first on `status='held'`; the new `win_now` re‑read closes the only path that let a second bid be captured for one lot. `capture_refused` provably means three definite refusals, i.e. three rolled‑back transactions.
- **Charged twice — placement replay.** `replay_placements` re‑sends `POST /hold` with `p["idem_key"]`, and `edb.mint_key` is a pure function of `(domain, entity_id, action, seq)` with no clock and no uuid (`estates_db.py:295`) — verified in `h6.py` that the replay returns the original hold rather than a second one. This is the single assumption the whole sweep rests on and it holds today.
- **Losing coins.** `estates` cannot mint; every payout is bounded by `paid + treasury == pool`. The four ways a user is separated from money without an immediate return are all **bounded by hold expiry, not permanent**: `place_unknown` (now swept), `capture_refused` (now released by a staff button), `release_refused` (R4‑3 — no software exit, core's sweeper frees it), and a `held` stake stranded on a market that reached `paid` (only reachable through R4‑1's stall). The one genuine *value* loss is R4‑2: 5 550 coins that a confirmed‑and‑reversed resolution can never recover, with the market frozen in `paid`.

---

## (d) Do the docstrings and `LEDGER_API_v2.md` match the code now?

**Much closer than any prior round, and there is still one docstring that claims a guarantee the code does not provide — plus one button label that does the same.**

Matching, and checked line by line:
- §6's "the flag comes off on exactly two statements… clearing it anywhere else — including 'the bot loop returned, so we know' — re‑opens the double‑charge" is now literally what `ledger_v2.py:828-874` and `:2084-2096` do, and the "accepted cost" paragraph describes the behaviour I reproduced in cases B and C. This is the first round where §6 and the code agree.
- §5.1's trigger snippet matches `HOLD_GUARD_DDL:293-305` term for term, and the coverage table matches the three installed triggers.
- `estates_db.py:180-182`'s "ONE place that judgement lives" is now true of `_outcome_known` (`estates_main.py:368`) and enforced at import (`:371-393`) — subject to R4‑6's remaining hand‑written tuple.
- `_stakes_left_out`, `_resolution_block` and `_unknown_warning` docstrings describe exactly what they now do, including the deliberate non‑blocking of `refused`.

Not matching:
1. **`unpark_payout_row`'s docstring (`estates_db.py:3005-3040`) calls itself "the ONLY exit from a parked payout row" and explains the reversal refusal as "that payment has been withdrawn as a domain decision".** For a `market_reverse` row that reasoning is inverted — the row *is* the withdrawal — and the function silently refuses the one run kind whose parking is designed in (R4‑2). Fourth round, fourth over‑claiming docstring.
2. **`ReconcileView.release_parked`'s label "Release the refused holds" and `_unknown_warning`'s "Free them with /admin → Reconcile holds → Release the refused holds"** are false for `release_refused` rows, which that sweep filters out on purpose (R4‑3). The button's own docstring is accurate ("parked in `capture_refused`"); the user‑facing strings are not.
3. **`LEDGER_API_v2.md` §10.4** — "one `transfer` per winner… per‑row, not after the loop — a half‑finished payout run resumes exactly where it stopped" — is true of the row statuses and false of the loop that reads them: R4‑1 means the run resumes at row 1 every pass and never reaches row 2.
4. **Interface drift** persists: `ESTATES_DB_INTERFACE.md` still predates `refused_hold_rows`, `placements_needing_replay`, `reconcile_*_placement`, `outcome_known_for`, `unpark_payout_row`, `failed_payout_rows`, `RELEASABLE_STATUSES`, `REFUSED_STATUSES`, `PLACEMENT_IN_DOUBT_STATUSES`, `DEFINITE_REFUSAL_CODES`, `MAX_HOLD_REFUSALS`. Unlike round 3, `estates_main` now uses all of them — the doc is the only thing left stale, and it should be regenerated so the next round's "frozen interface" check means something.

**Files:** `/home/claude/build/ledger_v2.py`, `/home/claude/build/estates_db.py`, `/home/claude/build/estates_main.py`, `/home/claude/build/ledger_migrate.py` (untouched this round), `/home/claude/build/ledger_client.py` (untouched this round). **Harnesses:** `/tmp/r4/h1.py` (N6/R3‑1/R3‑3/R3‑4), `/tmp/r4/h2.py` (R3‑2 lot), `/tmp/r4/h3.py` (R4‑1 + N4 un‑park), `/tmp/r4/h4.py` (R4‑2 reversal), `/tmp/r4/h5.py` (R4‑3 `release_refused`), `/tmp/r4/h6.py` (N7 replay), `/tmp/r4/m1.py` (money N1 cases A–F).
---

# Wiring checker report

`/home/claude/build/check_wiring.py` (1,336 lines, pyflakes clean, exit 0/1/2, `--root --json --quiet --strict --canary`). It is configured by a CONFIG block at the top (providers, API classes, guarantee/extension/directive/safety word lists, duplicate thresholds), not hard-coded to this tree.

## What it checks

1. **Forward** — resolves `import estates_db as edb`, `from ledger_client import …`, and instance aliases (including the `ledger: LedgerClient | None = (LedgerClient(...) if URL else None)` shape and `client = ledger` propagation to a fixed point). Every provider attribute must exist on the *imported* module; every call site must `inspect.signature().bind()`; every coroutine must be awaited or handed to a supervisor.
2. **Reverse** — every public name of `estates_db`/`ledger_client` with no caller anywhere. Method-granular, transitive reachability graph (`self.x` inside class `C` becomes node `C.x`), so a helper whose only callers are themselves uncalled is uncalled. Classes: `DEAD_SAFETY_MACHINERY` / `TEST_ONLY_NO_PRODUCTION_CALLER` / `UNUSED_API_BINDING` / `EXTENSION_POINT` / `UNCLASSIFIED`.
3. **Constants** — every module-level constant no other module reads, with `INTERNAL_ONLY` (live internal reader) vs `DEAD_BY_TRANSITIVITY` (only readers are dead or self-test-only).
4. **Duplicate judgement** — cross-module predicate pairs (name similarity + string-literal Jaccard + return shape) and same-name cross-module definitions. A pair where one delegates to the other reports as `RESOLVED_DELEGATION`, not a defect.

Two design decisions that mattered: **self-test roots are excluded from production reachability** (`_n6_refusal_regression` asserts `outcome_known_for` in both directions, which makes a naive graph call it live), and **classes are not single nodes** (`EXPECTED_API_VERSION` is read only by `LedgerClient.check_version`; class granularity called it live).

## Proof the tool bites

`--canary` copies the tree to a temp dir, plants five defects, requires all five, deletes the copy. Real files untouched (`grep -c canary estates_db.py estates_main.py` → 0).

```
planted dead safety function (reverse)     caught: True
planted duplicate judgement (duplicate)    caught: True
planted missing name (forward)             caught: True
planted arity failure (forward)            caught: True
planted un-awaited coroutine (forward)     caught: True
```

**Retro-test against round 3's actual defects** (`/tmp/r3retro`: this tree with estates_main's calls to the six new names neutered, i.e. the round-3 state). 11 defects, including exactly the machinery R3 shipped dead:

```
DEFECT reverse  DEAD_SAFETY_MACHINERY  estates_db.outcome_known_for
DEFECT reverse  DEAD_SAFETY_MACHINERY  estates_db.placements_needing_replay
DEFECT reverse  DEAD_SAFETY_MACHINERY  estates_db.reconcile_stake_placement
DEFECT reverse  DEAD_SAFETY_MACHINERY  estates_db.reconcile_bid_placement
DEFECT reverse  DEAD_SAFETY_MACHINERY  estates_db.refused_hold_rows
DEFECT constant DEAD_BY_TRANSITIVITY   estates_db.DEFINITE_REFUSAL_CODES
DEFECT constant DEAD_BY_TRANSITIVITY   estates_db.PLACEMENT_IN_DOUBT_STATUSES
```

## Findings on the current build

Forward: **117 provider names referenced, 0 missing; 307 call sites bound, 0 arity/keyword failures; 0 un-awaited coroutines**; 1 bind skipped (`edb.BadState(...)`, no introspectable signature). Round 4's wiring holds in that direction.

**DEFECT (4)** — all in `ledger_client.py`, all confirmed by hand:

- **`ledger_client.py:700` `LedgerClient.require_version`** — docstring says *"Call once at boot"*; nothing calls it. `setup_hook` (`estates_main.py:4092`) calls `edb.migrate()` and nothing else. **The version handshake never happens** — estates discovers a v1/v3 core on its first money call instead of at boot.
- **`ledger_client.py:687` `LedgerClient.check_version`** — reachable only from `require_version`. This is the one a one-hop check would have called live.
- **`ledger_client.py:95` `EXPECTED_API_VERSION`** — dead by transitivity, readers `require_version` and `_version_ok`. Same root cause; listed separately because it is the constant the whole handshake is written around.
- **`ledger_client.py:404` `mint_key`** — no caller, in `__all__`, and `estates_db.py:295` defines a second `mint_key` that *is* the live one. They disagree: different argument order (`service` first vs `SERVICE` pinned), different validation (`ledger_client` rejects empty/space; `estates_db` also rejects `::` and a trailing `:`), different exception (`BadKeyShape` vs `EstatesDBError`). Idempotency-key derivation is the mechanism that makes a retry a replay instead of a second charge, and there are two implementations of it with nothing forcing agreement.

**REVIEW (27)** — highlights:

- **`estates_db.py:2798` `pending_payout_rows` — TEST_ONLY_NO_PRODUCTION_CALLER.** Its only reader is `estates_db.py:3639`, a regression function. This is the function FINDINGS_R3 finding 5 named as the fix for head-of-line blocking, and **the fix was not applied**: `estates_main.py:1595` still runs `while (nxt := edb.next_payout_row(run_id))` with the `break` at `:1596-1602`, so one persistently-retryable row still stalls rows 2..200. R3-5 is open, and the machinery for it is in the tree, published in the frozen interface, exercised only by an assertion.
- **`estates_db.py:319` `next_rent_period`** — test-only, same shape, no money consequence today.
- **10 uncalled estates_db functions published in `ESTATES_DB_INTERFACE.md`**: `create_parcel:984`, `get_parcel:1005`, `get_parcel_by_slug:1011`, `set_parcel_owner:1037`, `start_lease:1053`, `end_lease:1073`, `ledger_for:965`, `ledger_for_user:972`, `get_run_by_key:2750`, `init_db:810` (alias for `migrate`, self-documented as "bank_db.py calls it"). Parcels/leases are unbuilt domain scaffolding, not safety machinery — `UNCLASSIFIED`, human call.
- **9 `UNUSED_API_BINDING`** on `LedgerClient` (`adjust`, `hold_extend`, `stocks`, `stock_buy`, `stock_sell`, `portfolio`, `ping`, `health`, plus `held_total` as `UNCLASSIFIED`) — endpoints estates does not use. Deliberately *not* defects.
- **Duplicates for human judgement**: `mint_key` (above), `utcnow` (`estates_db.py:260` / `estates_main.py:190` — two time sources), `migrate` (`estates_db.py:781` / `ledger_migrate.py:463`), `table_exists`/`_table_exists` (`land_money_migrate.py:275` / `ledger_migrate.py:401`, identical SQL literal).
- **`outcome_known_for` / `_outcome_known` → `RESOLVED_DELEGATION` (INFO)**: `estates_main.py:341` now calls `edb.outcome_known_for`, so the tool reports the R3-1 shape as resolved rather than as a defect. That is the check working in the positive direction.

**Constants**: of the four named in the task, `MAX_HOLD_REFUSALS` is now externally read (with `MAX_PAYOUT_ATTEMPTS`, `REFUSED_STATUSES`, `RELEASABLE_STATUSES`, `SCHEMA_VERSION`, `TREASURY`) so it is not reported. `DEFINITE_REFUSAL_CODES`, `PLACEMENT_IN_DOUBT_STATUSES`, `IN_DOUBT_STATUSES`, `UNKNOWN_STATUSES`, `HOLD_STATE_RESULT`, `SERVICE`, `DB_PATH` and the three `DEFAULT_*` are `INTERNAL_ONLY` with live internal readers — INFO, not defects.

**Known limitations, stated so a clean run is not over-read**: dynamic dispatch through `getattr` is only caught when the name appears as a string literal (reported as evidence, severity downgraded); an attribute whose base the tool cannot resolve is recorded as "POSSIBLE unresolved use" and downgrades a DEFECT to REVIEW; the duplicate check is a heuristic and is meant to be noisy; classification reads docstrings, so a function with no docstring can only ever be `UNCLASSIFIED`.