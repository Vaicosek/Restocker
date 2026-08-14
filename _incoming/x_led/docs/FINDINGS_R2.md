# Round 2 review — scorecard and new findings

## Money paths

## (a) Round-1 findings S1–S12 — re-verified against the code

| # | Verdict | Proof |
|---|---|---|
| **S1** | **PARTIALLY FIXED** | Trigger exists and works: `ledger_migrate.py:217-233`, recreated every run (`:429-432`). Verified empirically: legacy `UPDATE balances SET coins=MAX(0,coins-?)` and `set_balance`'s upsert both raise `IntegrityError`; `capture_hold` still succeeds because the claim UPDATE (`ledger_v2.py:1082-1088`) precedes `_debit` (`:1105`). But the guard is **not** schema-wide as `ledger_migrate.py:174-178` claims — see N2/N3. |
| **S2** | **VERIFIED FIXED** | `estates_db.py:2275` (`run_key = mint_key(domain, id, action, f"a{attempt}")`) and `:2306` (row key `a{attempt}:user:{uid}`); `attempt` threaded from `res["attempt"]` at `:2608`, `:2619`, `:2648`. Ran it: run1 `estates:market:1:payout:a1` / rows `…:a1:user:u1`; after reverse + re-propose, run2 `estates:market:1:payout:a2` / rows `…:a2:user:u2`, distinct run id. |
| **S3** | **PARTIALLY FIXED** | Fix 1 real: `_finalize_idempotency` runs inside the money `_tx()` (`ledger_v2.py:527-564`, called at `:1045, 1133, 1160, 1343, 1372`) and is itself claim-first (`WHERE … created_at=?`, `:554`). Fix 2 at `:600-605`. Fix 3 at `ledger_client.py:357` + `estates_main.py:356`. **But** (i) the takeover at `:600` is applied to `stock.buy`/`stock.sell`, which do *not* complete inside the money transaction → **N1, a double-charge**; (ii) `fail_payout_row`'s attempt counter defeats `_permanent=False` → **N4**, so S3's "staff pay the winner by hand" ending is still reachable. |
| **S4** | **VERIFIED FIXED** | The hand-rolled capture-then-transfer is gone. `close_auction` resumes from state, not from winning the race (`estates_main.py:1427-1435`, resume point `:1511-1529`), and settlement goes through `build_auction_settle_run` → `execute_run` (`:1535-1546`). *However* the resume path has a new hole — **N5**. |
| **S5** | **VERIFIED FIXED** | AST check: all 98 distinct `edb.*` attributes used by `estates_main.py` exist in `estates_db.py`, and every call site binds against the real signature (0 arity errors). Client: `get_hold/hold/hold_capture/hold_release/transfer` all present. |
| **S6** | **VERIFIED FIXED** | `_unclaim_hold_row` lands on `capture_unknown`/`release_unknown` unless `outcome_known` (`estates_db.py:1167-1201`); forward-only reconcile `_reconcile_hold_row` (`:1209-1263`, `_RECONCILABLE` `:1206`, `HOLD_STATE_RESULT` `:107-112`); `HoldNotOpen` deliberately excluded from `_outcome_known` (`estates_main.py:347`); sweeper wired at `estates_main.py:1549-1586` / `2896`. |
| **S7** | **VERIFIED FIXED, correctly** | `ledger_client.py:728-784` — `acting_user` is an explicit parameter, sent only when set (`:782-783`), and the client *refuses* a user-sourced transfer without it (`:764-770`). No default of `acting_user=from_user` anywhere. Call sites pass the debited user only on inbound clawback rows (`estates_main.py:1313-1319`) and rent (`:2830`, gated off by `RENT_ENABLED`). |
| **S8** | **VERIFIED FIXED** | `ledger_v2.py:361-376`: COMMIT is inside the `try`; on failure `_unwind()` + `_discard_conn()` before re-raise. `_discard_conn` at `:310-327` clears `_local.conn` first, so a broken `close()` cannot leave the poisoned handle installed. |
| **S9** | **VERIFIED FIXED** | `FINGERPRINT_FIELDS` (`ledger_v2.py:440-448`) narrows `hold` to `(user_id, amount)` — `expires_in` and `reason` excluded; `_canon` (`:451-464`) normalises `100` vs `100.0`. Caller also derives TTL from stored timestamps (`estates_main.py:365-372`) rather than the clock. |
| **S10** | **VERIFIED FIXED (moved to the caller)** | `estates_db.claim_resolution_confirm` (`:2095-2118`) still does **not** compare, exactly as the finding described — but `estates_main.py:2486-2503` recomputes `market_pools(captured_only=True)`, refuses on `unknown_stakes`, and `unresolve`s on a pool delta, and `proposal_embed` (`:2432-2459`) recomputes at render time. No `await` between the check (`:2493`) and `build_market_payout_run` (`:2521`), and this is the only caller, so there is no TOCTOU today. Note the guarantee now lives in one Discord handler, not in the DB layer — a second entry point would silently skip it. |
| **S11** | **NOT FIXED / mildly REGRESSED** | `ledger_v2.py:1244-1250` still writes `hold_sweep_cursor`; `grep` shows it is read **nowhere** in either file, and the candidate query `:1220-1225` re-selects from the top. The docstring now claims *more* than before — `:1211-1213` "resumes at the exact hold it was on and never re-processes the ones it already released" — which is false; `ledger_migrate.py:152-154` repeats it. This is precisely the "marker that looks load-bearing" S11 warned about, now with a stronger comment inviting the `AND hold_id > cursor` optimisation. (estates_db's half *is* fixed: `cursor_seq` removed, `SCHEMA_VERSION = 2`, `estates_db.py:2416-2418`.) |
| **S12** | **VERIFIED FIXED** | `TREASURY_MAX_DEFICIT` (`ledger_v2.py:167-169`), distinct `treasury_insolvent` code (`:902-905`), ERROR log + durable audit row inside the debit transaction (`:917-923`), `insolvent` on every balance read (`:751`), typed client class `TreasuryInsolvent` (`ledger_client.py:273`). Verified: a treasury payout with no funds lands at `-5000` with `insolvent: true` and the ERROR line. |

---

## (b) New defects introduced by the fixes

### N1 — CRITICAL. The S3 stale-claim takeover double-charges the one endpoint S3's fix 1 does not cover
**`ledger_v2.py:600-605`** (takeover) vs **`:1760-1779`** (`_stock_trade`).

`_claim_idempotency`'s own docstring (`:583-586`) states the rule: takeover "is only safe on top of fix 1. Without completion inside the money transaction, `in_progress` would not mean 'not applied' and a takeover would double-pay." `_stock_trade` is exactly that case — the trade happens in `Restocker_main.exec_stock_trade` via `run_on_bot_loop` (`:1762`) and the key is completed afterwards by `_complete_idempotency` in a **separate** `_tx()` (`:1501`). The WHERE clause at `:601-605` has no endpoint exclusion.

Sequence:
1. `POST /stock/buy` key `osentar:stock:buy:2026-08-13:u123:7`. Claim inserted, `in_progress`.
2. `exec_stock_trade` commits — coins deducted, 7 shares credited.
3. Process is killed / `_complete_idempotency`'s COMMIT hits SQLITE_BUSY (which S8 now turns into a discarded connection and a propagating exception) before the row is marked `done`.
4. ≥900s later (`IDEMPOTENCY_STALE_SECONDS`, `:141`) the bank retries the identical key. Same service, same endpoint, same fingerprint `(user_id, market_id, shares)`, `state='in_progress'`, `created_at < stale_before` → **`rowcount == 1`, claim granted**, handler re-runs `exec_stock_trade`. User pays twice, gets 14 shares.

Verified directly: seeding a stale `stock.buy` row and calling `_claim_idempotency` with the same fingerprint returns a fresh claim token instead of raising.
Before the S3 fix this returned `409 idempotency_in_progress` forever — a stranding bug. The fix converted it into a double-spend on the only path it was not designed for.
**Minimal fix:** `AND ledger_idempotency.endpoint NOT IN ('stock.buy','stock.sell')` in the ON CONFLICT WHERE (or, better, a `state='applied_unknown'` row written before `run_on_bot_loop` so a takeover is never possible for out-of-band money).

---

### N2 — HIGH. Once a wallet is over-committed the trigger blocks *every* capture, including affordable ones
**`ledger_migrate.py:217-233`**, surfacing at **`ledger_v2.py:876-889`**.

The trigger's floor is the sum of the user's *other* open holds, so it is not per-hold. Verified:

- u2: balance 10 000, hold A = 3 000, hold B = 6 000 (both legitimately placed).
- A writer that is not `UPDATE OF coins` drops the balance to 8 000 (see N3).
- `capture_hold(A, 3000)`: claim moves A out of `open`; `_debit` passes its own WHERE (`8000 - 0 - 3000 >= 0`) but the trigger sees `NEW.coins = 5000 < 6000` (hold B) → **ABORT** → `escrow_shortfall`, whole tx rolled back, A back to `open`.
- `capture_hold(B, 6000)` → same, blocked by A.

**Neither hold can ever be captured.** Before the trigger, A's 3 000 would have been collected correctly and only B would have failed. Now the entire market/lot is stuck and the only exit is manual DB surgery. Compounding it, the error text is wrong: `ledger_v2.py:886-888` interpolates `held`, which on the capture path is always `0` because `_debit` was called with `respect_holds=False` — the operator is told "(0 held)" while 9 000 is held.
**Minimal fix:** on the capture path, exempt the hold being captured explicitly rather than relying on total ordering — or, simpler, make `capture_hold` retry-safe by checking `_read_balance(conn, uid)["balance"] >= want` first and raising `escrow_shortfall` with the real `held` figure and the offending hold ids.

---

### N3 — MEDIUM. The escrow guard is not schema-wide: `INSERT OR REPLACE` and `DELETE` on `balances` bypass it
**`ledger_migrate.py:219`** (`BEFORE UPDATE OF coins`) vs the claim at **`:174-178`** ("Any writer — … a hand-typed UPDATE in a sqlite3 shell — is subject to it, because it lives in the schema").

Verified against a migrated DB with a live 6 000 hold on a 10 000 wallet:
- `INSERT OR REPLACE INTO balances (user_id, coins, principal, lp) VALUES ('u1',0,0,0)` → **allowed**, coins → 0, hold still open.
- `DELETE FROM balances WHERE user_id='u1'` → **allowed**.

`INSERT OR REPLACE` is a DELETE+INSERT and never fires an UPDATE trigger; SQLite's `INSERT … ON CONFLICT DO UPDATE` (which `Restocker_db.set_balance:962` uses) *does*, and is covered. Nothing in the uploaded Restocker files uses REPLACE/DELETE on `balances`, so I cannot construct a live loss today — **the loss is UNPROVEN**, but the docstring's absolute claim is false and this is the only route I found into N2's state.
**Minimal fix:** add matching `BEFORE DELETE ON balances` and `BEFORE INSERT ON balances` triggers (the INSERT one guarding the REPLACE case via `WHEN EXISTS(SELECT 1 FROM ledger_holds …)`), and soften the docstring.

---

### N4 — HIGH. `MAX_PAYOUT_ATTEMPTS` overrides `_permanent=False`, so S3's retryable code still parks a row — permanently
**`estates_db.py:2458-2459`**, **`estates_main.py:356-357`**, **`estates_main.py:2732-2744`**.

`fail_payout_row` computes `status = "failed" if (permanent or exhausted)`. `exhausted` is `attempts >= 5`, and `claim_payout_row` increments `attempts` on every claim (`:2399`). So `_permanent(IdempotencyInProgress) == False` protects only the first four attempts.

Sequence:
1. Payout row for winner 123, key `estates:market:77:payout:a1:user:123`. Attempt 1 claims the key at core, then that worker dies (or its response is lost). Core row stays `in_progress`; coins did **not** move (S3 fix 1).
2. `recovery_tick` runs every 120 s (`estates_main.py:2887`) and re-spawns `execute_run`; each pass claims the row, gets `409 idempotency_in_progress`, and `fail_payout_row` returns it to `pending` with `attempts+1`.
3. Attempts 2–5 all land inside **600 s**, which is less than `IDEMPOTENCY_STALE_SECONDS = 900` (`ledger_v2.py:141`). The takeover window never opens.
4. Attempt 5 → `exhausted` → row `failed`, `finish_run` → `failed`.
5. Nothing in `estates_db.py` ever moves a row out of `'failed'` (only `payout_rows SET status` writes are `:2399, 2425, 2461, 2558`, and `requeue_stuck_row` matches `status='claimed'`). `PayoutStatusView.resume` requeues only `stuck_rows` (`status='claimed'`), and `next_payout_row` selects `status='pending'` — the failed row is invisible to Resume forever.
6. The proof embed says "Action needed … press Resume", Resume does nothing, and the only remaining route is paying by hand — the exact ending S3 was written to remove.

**Minimal fix:** don't count a non-permanent classification against `attempts` (`exhausted = attempts >= MAX and permanent_class`), or set `MAX_PAYOUT_ATTEMPTS × recovery interval > IDEMPOTENCY_STALE_SECONDS`; and add an `unpark_payout_row(row_id)` (`failed → pending`) wired to the Resume button, since a parked row is currently terminal in software.

---

### N5 — CRITICAL. `capturing` is not treated as "in doubt", so a crash mid-capture promotes the second-highest bidder and drops a captured stake out of the pool
**`estates_db.py:102`** (`UNKNOWN_STATUSES`), **`:1130-1136`** (`highest_bid` filters `status='held'`), **`:1327-1353`** (`unreconciled_*` use `UNKNOWN_STATUSES` only), **`:1898`** (`market_pools`), **`estates_main.py:1442`, `:1449`, `:1232`, `:2590-2596`**.

`holds_needing_reconcile` explicitly treats a stale `capturing`/`releasing` row as in-doubt (`estates_db.py:1315-1316`) — but only after `RECONCILE_AGE_S = 900 s` (`estates_main.py:138`). For those 15 minutes every other consumer treats such a row as if it did not exist.

**Auction case — coins lost and the wrong bidder wins:**
1. Lot 412 closes. Bids: Alice 50 000 (seq 3, high), Bob 40 000 (seq 2). `close_auction` claims the close, `claim_bid_capture(alice)` → `capturing`, `POST /hold/capture` **commits at core** (50 000 → `treasury:estates`).
2. The bot is killed (deploy/OOM) before `bid_captured` runs. Alice's row is left `capturing` — *not* `capture_unknown`.
3. Restart inside 900 s. `lifecycle_tick:2879-2882` sees the lot `closing` → `close_auction`.
   - `unreconciled_bids` (`estates_main.py:1442`) is **empty** — `capturing` is not in `UNKNOWN_STATUSES`. Gate passes.
   - `highest_bid` (`:1449`) returns **Bob**, because Alice is not `held`.
   - Bob clears the reserve → `claim_bid_capture(bob)` captures another 40 000.
   - `losing_held_bids(auction, bob.id)` selects `status='held'` → Alice is not released either.
   - `auction_closed(winner=Bob, hammer=40 000)`; `settle_auction` → `build_auction_settle_run` pays the seller 40 000 − fee from the treasury. Nothing anywhere checks that the winning bid is `captured`.
4. At T+900 s the reconciler flips Alice to `captured`. **Alice has paid 50 000, has no lot, and there is no refund path** — `void_auction` (`estates_db.py:1605`) only accepts `draft|open|closing|closed`, and the auction is `settled`.

**Market case — a captured stake is never paid:**
1. Market 77 closing; `close_market` claims stake #40 (10 000) → `capturing`; core commits the capture; bot dies.
2. Restart: `close_market` resumes but iterates `status='held'` (`estates_main.py:1232`) so #40 is skipped, then calls `market_closed()` unconditionally (`:1252`). The `closed`-with-held-stakes re-sweep (`:2865-2874`) also only looks at `held`.
3. Staff resolve. At confirm, `pools["unknown_stakes"] == 0` and `total_pool == preview_pool` (both exclude #40), so the S10 guard passes; `build_market_payout_run`'s `unreconciled_stakes` gate (`estates_db.py:2590`) passes too. The run is built from `captured` stakes only — #40's owner is absent.
4. Run completes, market → `paid`. At T+900 s #40 is reconciled to `captured`. The punter's 10 000 is in `treasury:estates`, they backed the winning outcome, and they are in no run. Nothing alerts; the proof embed reads 100 % paid.

**Minimal fix:** introduce `IN_DOUBT_STATUSES = UNKNOWN_STATUSES + ('capturing','releasing')` and use it in `unreconciled_stakes`, `unreconciled_bids` and `market_pools`'s `unknown_*`; make `highest_bid` refuse to answer (or include `capturing`/`captured`) while any bid on the lot is mid-flight; and have `build_auction_settle_run` assert the winning bid row is `captured` before quoting proceeds.

---

### N6 — MEDIUM. A definite refusal on capture is filed as "unknown", producing an endless reconcile ping-pong
**`estates_main.py:339-347`** (`_outcome_known`), with `estates_main.py:1241-1245` and `estates_db.py:1175`.

`_outcome_known` is true only for `HoldNotFound` and `BadRequest`. `InsufficientFunds`, `AccountFrozen`, `EscrowShortfall`, `ForbiddenScope` and `ForbiddenHold` are all *definite* refusals in which core provably moved nothing (`ledger_v2.py:891-907` — `rowcount != 1` means the UPDATE did not apply; the trigger's ABORT rolls the whole tx back, verified in N2 with the hold returning to `open`). They nonetheless land in `capture_unknown`.

Sequence: user's wallet is frozen (or over-committed per N2) → `close_market` capture → `frozen`/`escrow_shortfall` → `capture_unknown` → 120 s later `reconcile_holds` asks core, gets `open` → row back to `held` → next `lifecycle_tick` re-sweeps → same refusal → `capture_unknown` → … one `GET /hold` plus one `POST /hold/capture` per stake per cycle, forever. Whenever the row happens to be in `capture_unknown`, `build_market_payout_run` refuses (`estates_db.py:2591-2596`) and the confirm button rejects (`estates_main.py:2487-2492`), so resolution succeeds or fails depending on which side of a 120 s race staff click.
No coins are lost (the punter keeps them), but the market resolves against a pool that silently omits the stake, and the HTTP loop never terminates.
**Minimal fix:** add `InsufficientFunds`, `AccountFrozen`, `EscrowShortfall`, `ForbiddenScope`, `ForbiddenHold` to `_outcome_known`, and give a stake that core has definitively refused N times a terminal status so it stops re-entering the sweep.

---

### N7 — MEDIUM. An unconfirmed `POST /hold` is filed as `failed`, stranding the punter's coins for the full TTL under a message saying nothing was taken
**`estates_main.py:898-914`** (stakes) and **`:1106-1114`** (bids).

`_hold_with_retry` exhausts its retries (`LedgerUnavailable`), the hold may well have committed at core, and the handler calls `fail_stake` / `fail_bid` — a terminal state with `hold_id IS NULL`. `holds_needing_reconcile` (`estates_db.py:1313`) requires `hold_id IS NOT NULL`, so nothing ever revisits it; the code comment at `:899-901` concedes this. The reserved coins are invisible until `expires_in` elapses — `WAGER_HOLD_GRACE_S` defaults to **7 days past market close** (`estates_main.py:129`), `LOT_HOLD_GRACE_S` to 24 h past lot close.

Sequence: punter stakes 10 000; core commits the hold; the response times out twice; the row is `failed`; the ephemeral says "nothing was taken"; their available balance is 10 000 short for a week; a second attempt at the same size returns `insufficient` with no explanation. The stake is also absent from the pool.
This is the same shape S6 was fixed for, one step earlier in the lifecycle, and the machinery to fix it already exists and is unused: the row's `idem_key` is durable and `place_hold` is replayable, so re-sending the identical key returns the original `hold_id`.
**Minimal fix:** add a `place_unknown` status (row keeps `idem_key`, no `hold_id`) and a resume pass that re-sends `hold` with the stored key; core replays and hands back the `hold_id`. Only a definite refusal (`insufficient`/`frozen`/`bad_*`) should reach `fail_stake`/`fail_bid`.

---

### Checked and could not refute (stated, not padded)

- **Double capture / capture-after-release** — `capture_hold:1082-1088`, `release_hold:1143-1149`, `sweep_expired_holds:1231-1237` are each a claim-first UPDATE gated on `state='open'` with a `rowcount` check inside `BEGIN IMMEDIATE`; `CHECK (captured_amount + released_amount <= amount)` (`ledger_migrate.py:98`) is a second net. No sequence found.
- **Sweep racing an in-flight capture** — both take `BEGIN IMMEDIATE` on the same file and serialise; the loser gets `rowcount 0`. The trigger adds no new lock and cannot deadlock: it is a `BEFORE` trigger reading `ledger_holds` on the connection that already holds the write lock, with no nested write.
- **Float money** — `pari_mutuel_payouts:757-771` and `auction_split:812` are integer floor throughout, `paid + remainder == distributable` by construction, `_int_field:1421-1445` and `_coins:406-418` reject fractions rather than rounding. Ran `estates_db.py`'s self-test: all assertions pass.
- **Progress marker after a loop (estates)** — `settle_payout_row:2424-2442` flips the row and advances counters in one transaction; `next_payout_row:2364-2373` is state-driven; the run-level cursor was deleted. Correct per Rule 2. (Core's sweep cursor is the S11 exception above.)
- **available-vs-balance** — every debit path checks `available` except the deliberate `respect_holds=False` at capture; verified that an over-available `transfer` from a wallet with a live hold returns `insufficient` and an affordable one succeeds.
- **UNPROVEN — `claim_bid_capture` mints an auction-scoped capture key**, `estates:lot:<id>:capture` (`estates_db.py:1496`), where stakes use a per-seq key (`:1734`). Two different bids on one lot would collide; core's `hold.capture` fingerprint `(hold_id, amount, to_user)` would answer `409 idempotency_conflict` rather than replay, so this fails loudly rather than double-paying, and I could not construct a flow that capture-claims two bids on one lot. Worth aligning to `…:capture:<seq>` anyway.
- **UNPROVEN — `ReverseConfirmView.go` (`estates_main.py:2794-2799`) leaves the resolution in `reversing` with no `unclaim` if `build_market_reverse_run` raises**, which would strand the correction path. `claim_resolution_reverse` only fires from `confirmed`, which implies a non-null `payout_run_id`, so the `BadState` at `estates_db.py:2640` looks unreachable. Add the `unclaim` regardless — it is one line and the alternative is a market nobody can correct.
- **UNPROVEN — `_v1_transfer` (`ledger_v2.py:1914-1941`) does not apply `h_transfer`'s `src == treasury or src == acting_user` rule**, so the v1 alias can move coins between arbitrary wallets while the v2 endpoint refuses. Not an escalation today because the alias is osentar-only and osentar holds `wallet.mint` anyway, but the two surfaces disagree about the same guarantee.
- **Cosmetic** — `resolution_reversed` (`estates_db.py:2181-2184`) clears `winning_outcome_id` and `resolution_id` but leaves `markets.payout_run_id` pointing at the reversed run, so `/admin → Payout status` shows the clawed-back run until the next resolution is confirmed.
---

## Discord / product

I read all four docs, then `estates_main.py` (2996 lines) in full, cross-checked every `edb.*` call against `estates_db` by AST + `inspect.signature`, and ran the resolve/payout path end-to-end against a real temp DB.

---

# (a) FINDINGS.md Part 2, findings 1–14

**1 — VERIFIED FIXED. The number is 0.**
Mechanical check (AST walk of every `ast.Attribute` on `Name('edb')`, matched against `dir(estates_db)`): **98 distinct `edb.*` names referenced, 0 missing.** I also bound every one of the ~180 `edb.*` call sites to the real signature with `inspect.signature(...).bind(...)`: **0 arity failures**, and the five argument-order bugs from round 1 are all correct now — `create_stake(market_id, outcome_id, user_id, amount)` at `estates_main.py:875`, `user_stakes(market_id, user_id)` at `:952` and `:1673`, `set_market_message(id, guild, channel, message)` at `:1920`, `set_auction_message` at `:2116`, `claim_payout_row(row_id)` at `:1309`, `pending_payout_rows`/runs model at `:1299`. Boot: `edb.migrate()` at `:2942` (not `init_db`). The fictional API is gone from the header docstring. Live proof: I created a market, staked, closed, captured, previewed, proposed, built the run and rendered every embed — no AttributeError anywhere.

**2 — VERIFIED FIXED.** `estates_main.py:2521` `edb.build_market_payout_run(...)` inside the won claim, `:2530` `resolution_confirmed(rid, run["id"])`, `:2532` `spawn(run:…, execute_run(run_id))`. `total_rows == 0` is a loud state in both places that could hide it: `:2540-2544` and `:1400-1408`.

**3 — VERIFIED FIXED.** No module-level `RAKE_BPS` exists (`:112-114` says why). Every rake read is `int(market["rake_bps"])` / `int(auction["fee_bps"])`: `:454`, `:476`, `:515`, `:811`, `:823`, `:948`, `:977`, `:1623`, `:2143`, `:2319`.

**4 — VERIFIED FIXED.** `_figures_fields` (`:2311-2330`) is shared by the preview (`:2401`) and `proposal_embed` (`:2447`), and `proposal_embed` recomputes via `edb.preview_resolution` at render time (`:2435`). The second-staff-member path now routes to it: `:2361-2365`.

**5 — VERIFIED FIXED.** `close_market:1212-1223` resumes from state (`closing`/`closed`), `close_auction:1427-1435` + `:1511-1514` resumes from `closing`/`closed`/`settling`+unsettled, and `lifecycle_tick:2854-2882` sweeps open, `closing`, `closed`-with-held-stakes (backoff `_RESWEEP`), and unsettled lots. See N7 for a hazard this fix introduces.

**6 — VERIFIED FIXED.** `show_close_preview:1933-1960` renders stakes-to-capture, coins, pool-after, per-outcome split, unreconciled; `CloseConfirmView:1963`.

**7 — PARTIALLY FIXED.** Every payout/close/void/reverse loop is now `spawn(...)` (`:1123, 1975, 2177, 2532, 2634, 2687, 2740, 2800`). One inline loop survives — see **N4** (`ReconcileView.go:2299`).

**8 — VERIFIED FIXED.** `positions_embed:1654-1667` — balance and `list_holds` each in their own `try/except LedgerError`, degrading to a field rather than escaping.

**9 — VERIFIED FIXED.** `SafeView.on_error:576`, `SafeModal.on_error:584`, `_dynamic_guard:590`. All 22 Views subclass `SafeView`, all 4 Modals subclass `SafeModal`, and both non-`SafeView` children (`_PickSelect:1702`, `_EphemeralOutcomeButton:1736`) live inside `SafeView` parents.

**10 — VERIFIED FIXED.** `on_pick_void:2566-2618` shows three separate blocks (holds to release / captured to refund / unknown) and branches to `VoidReleaseView` (holds only, refuses if captured stakes exist, `:2596-2602`) or `VoidRefundView` (refund through propose→hold-down→confirm).

**11 — VERIFIED FIXED.** No `or` fallback. `odds_text:200-205` returns `—` on (0,0); `:826-832` and `:965-971` render `—` and log.

**12 — VERIFIED FIXED.** `_field:240-251` refuses a duplicate name, logs, renames to `(cont.)`. The old collision site is now `"Nobody is paid"` (`:2328`).

**13 — VERIFIED FIXED.** `setup_hook:2934-2964` does migrate + `add_dynamic_items` + one `tree.sync`; `on_ready:2967-2969` only logs.

**14 — VERIFIED FIXED.** `execute_run:1299-1334` has no pre-claim branch at all; zero-coin rows are written `status='skipped'` at build time (`estates_db.create_payout_run`, the `"skipped" if amount == 0 else "pending"` INSERT) and are invisible to `next_payout_row`.

---

# (b) New defects

## N1 — HIGH. The confirm buttons are never actually disabled, so a double-click places two real stakes / two real bids
`estates_main.py:851` (`StakeConfirmView.confirm`), `:1065` (`BidConfirmView.confirm`)

```python
for child in self.children:
    child.disabled = True
```
This mutates a local Python object. Nothing pushes it to Discord — there is no `edit_message`, no `followup.edit_message`, no `self.stop()` anywhere in the file (grep: 9 `disabled = True`, 0 of each). The buttons stay live in the client.

The seven staff instances are cosmetic (the second click loses an atomic claim and gets a clean refusal). These two are not: each click calls `edb.create_stake` / `edb.create_bid`, which mints a **new seq and a new key**. Verified:

```
two identical stakes -> estates:market:1:stake:1 | estates:market:1:stake:2   distinct: True
two identical bids   -> estates:lot:1:bid:1      | estates:lot:1:bid:2
```

Failure: punter types 5,000, clicks "Confirm stake" twice while the first is deferring. Two holds of 5,000, 10,000 reserved, two "Stake placed" embeds, and at close 10,000 is captured. They intended one stake. Nothing in the file dedupes it.

Minimal fix, in both classes:
```python
def __init__(...):
    ...
    self._used = False

async def confirm(self, interaction, button):
    if self._used:
        await _defer(interaction); await _reply(interaction, "already submitted."); return
    self._used = True
    await _defer(interaction)
    self.stop()
    ...
```

## N2 — HIGH. `_unknown_warning` computes the "punters left out of the market" signal and both callers throw it away; confirm doesn't check it at all
`estates_main.py:2333-2352` (returns `dirty`), discarded at `:2402` and `:2457`; the confirm guard at `:2486-2492` tests only `pools["unknown_stakes"]`

`unknown_stakes` counts only `('capture_unknown','release_unknown')` (`estates_db.UNKNOWN_STATUSES`). A stake whose capture was *refused* (`insufficient` — precisely S1's sequence, the punter spent held coins through the legacy `adjust_balance`) lands back in `held`, which is invisible to both that guard and to `build_market_payout_run`'s refusal. Reproduced against a real DB:

```
sB status: ['captured', 'held']
captured pool: 1000   unknown_stakes: 0   unreconciled_stakes: 0
preview_pool frozen: 1000   pool now == preview? True
run built despite held stake: 1 rows 925 coins; u2's 5000 excluded
```

So: 6,000 staked, market pays out on a 1,000 pool, u2 is silently absent from the market they backed, and the S10 pool-moved guard passes because the pool never moved. Worse, once `propose_resolution` flips the market to `resolving`, `lifecycle_tick`'s re-sweep (`:2865`, which only scans `closed`) never touches it again — the stake stays held to expiry. The one thing that would have told a human is the "Not captured" field `_unknown_warning` builds at `:2347-2351`, and its return value is dropped on both render paths.

Minimal fix: use the return — `dirty = _unknown_warning(...)` and withhold `ProposeResolutionView`/`ConfirmResolutionView` when true — and add the same test to the confirm guard:
```python
stranded = [s for s in edb.stakes_for_market(self.market_id)
            if str(s["status"]) in ("held", "capturing")]
if stranded:
    await _reply(interaction, f"{len(stranded)} stake(s) never reached the treasury...", error=True)
    return
```

## N3 — MEDIUM. Rapid bidding silently drops the outbid release; "released the moment you are outbid" becomes false for days
`estates_main.py:1123` (`spawn(f"outbid:{auction_id}", ...)`), `:1152-1175` (`release_outbid`), `:2876-2878` (lifecycle_tick's open-auction scan has no outbid sweep)

`spawn` (`:422-434`) returns `False` and `coro.close()`s when a job with that key is already running — correct for money loops, wrong here, because `release_outbid` is the *only* thing that ever releases a losing hold before close. `losing_held_bids` is evaluated once at `:1161`, so bids that arrive mid-loop are not in the running pass either.

Failure: A bids 100 (held). B bids 200 → `outbid:7` starts, releases A. C bids 300 while that task is mid-HTTP → `spawn` returns False → **B's 200 is never released**. Nothing re-runs it: `lifecycle_tick` sweeps open markets, `closing` markets, `closed` markets and unsettled lots, but never open lots for outbid holds. B's coins stay reserved until the lot closes — hours or days — while `show_bid_preview:1048-1050` promised "released automatically the moment you are outbid".

Minimal fix: add to `lifecycle_tick`'s open-auction loop —
```python
for a in edb.list_auctions(status="open", limit=100):
    if _is_past(a.get("closes_at")):
        spawn(f"lot:{a['id']}", close_auction(int(a["id"])))
    else:
        spawn(f"outbid:{a['id']}", release_outbid(int(a["id"])))
```

## N4 — MEDIUM. The one surviving inline HTTP loop: "Reconcile holds" awaits up to 200 core round-trips on a deferred interaction (P2#7 residue)
`estates_main.py:2295-2299`

```python
await _defer(interaction)
if not await ensure_staff(interaction): return
result = await reconcile_holds(limit=200)
```
`reconcile_holds:1560-1585` is one `GET /hold/{id}` per row, awaited serially. `LedgerClient` is `timeout=15.0, retries=2` with backoff (`ledger_client.py:483`), and `reconcile_holds` catches `LedgerError` per hold and **continues** — so a degraded core makes this loop 200 × ~30s. The interaction token dies 15 minutes after the defer; the followup at `:2306` 404s and the staff member is left with the hung spinner that finding 9 exists to prevent. This is the same shape as `PayoutStatusView.resume`, which *was* fixed (`:2740`).

Minimal fix, mirroring `:2740`:
```python
started = spawn("reconcile", _reconcile_and_prove(interaction.user.id))
await _reply(interaction, f"Asking core about {len(pending)} hold(s) in the background; "
                          "the result posts to the proof channel." if started else
                          "A reconcile is already running.")
```

## N5 — MEDIUM. Staff pickers silently drop everything past the 25th option, and the panel that manages in-flight payouts is the worst hit
`estates_main.py:1707` (`options[:25]`, no warning), fed by `:2239-2241`, `:2255-2257`, `:1846-1847`

`AdminView.status:2255-2257` concatenates six statuses at `limit=10` in the order `paying, paid, voiding, void, reversing, resolving` → up to 60 options, sliced to 25. `paid` and `void` are **terminal and accumulate forever**. After ten finished payouts and ten finished voids, the 25 slots are consumed by history and the `reversing` / `resolving` markets — the ones this panel exists to manage — are cut off entirely, with no message saying so. `AdminView.void:2239-2241` (40 → 25) and `MarketAdminView.close_market_btn:1846-1847` (50 → 25) have the same shape: with 30 open markets, five of them cannot be closed from the panel at all and nothing says why. That is Rule 5 arriving sideways — the subject is unreachable and the user has no way in.

Minimal fix: build the candidate list, and if `len(options) > 25`, prefer in-flight statuses first and append a `_reply` line — `f"Showing 25 of {len(options)}; finished markets are hidden."` Simplest correct version for `status`: drop `paid`/`void` from the list entirely (they are already proved in the proof channel).

## N6 — MEDIUM. `place_bid` never re-checks the minimum next bid, so a stale confirm reserves coins on a bid that cannot win — and says "Bid placed"
`estates_main.py:1074-1093` vs the check at `:1033-1037`

`show_bid_preview` validates `amount >= edb.min_next_bid(...)`. `place_bid` re-reads the auction (`:1080`) but never re-reads the floor, and `estates_db.create_bid` does not check it either (it validates only status and amount > 0). Failure: A previews 500 against a floor of 500; B bids 900; A clicks Confirm 20 seconds later. `create_bid(500)` succeeds, a hold for 500 is placed, and A is told **"Bid placed — 500 coins reserved"** on a bid that is already losing. It only unwinds if `release_outbid` runs — which N3 shows it may not.

Minimal fix, immediately before `edb.create_bid` at `:1085`:
```python
floor = edb.min_next_bid(int(auction_id))
if amount < floor:
    await _reply(interaction, f"somebody bid while you were confirming — the minimum "
                              f"is now {fmt(floor)} coins. Nothing was reserved.", error=True)
    return
```

## N7 — MEDIUM (multi-worker only). The claim-free resume path in `close_auction` lets a second worker pick a different winner and capture a second bidder
`estates_main.py:1427-1448`, specifically the guard at `:1432` and the unconditional `if str(a["status"]) == "closing":` at `:1448`

The P2#5/S4 fix is right in shape but the auction case differs from the market case: closing a market is a set of independently-claimed per-stake captures (safe to run twice), whereas closing a lot **chooses a winner**, and the resume path re-enters that choice with no claim of its own.

Sequence: worker A claims the close, `claim_bid_capture(bid#5, 50,000)` → status `capturing`, HTTP in flight. Worker B (`lifecycle_tick` in a second process) enters: `claim_auction_close` returns None, status is `closing`, and `edb.unreconciled_bids` is **empty** — `UNKNOWN_STATUSES` is `('capture_unknown','release_unknown')` only, so a `capturing` row does not block (verified). B falls into `:1448`, `highest_bid` returns only `status='held'` rows so it sees bid#4 at 40,000, captures it, releases the rest, and calls `auction_closed(winner=#4, hammer=40,000)`. A's capture then lands and `bid_captured(#5)` succeeds. Two bidders charged, the lower one declared winner, the seller paid on 40,000, and #5's 50,000 sits in the treasury with no refund path (`void_auction` releases open holds only).

Within one process `spawn`'s key `lot:{id}` prevents this — but `spawn`'s own docstring at `:424-425` says "correctness never depends on it (every row is claimed atomically)", and here it does. The file is otherwise built for multiple workers (`WORKER_ID`, run leases, `stuck_rows`).

Minimal fix: make the resume path claim too, e.g. add an `open|closing -> closing` guarded transition to claim re-entry, or gate `:1448` on there being no bid in `('capturing','releasing')` on this lot:
```python
if any(str(b["status"]) in ("capturing", "releasing")
       for b in edb.bids_for_auction(int(auction_id))):
    out["skipped"] = True
    return out
```

## N8 — LOW. A lot with a permanently-failed settle run burns two REST calls a minute, forever
`estates_main.py:2879-2882` → `close_auction` → `:1531`

The unsettled-lot sweep has no `_RESWEEP`-style backoff (markets got one at `:2868-2871`, lots did not). A lot in `closed` with `settled_at IS NULL` and a settle run parked `failed` re-enters every 60s: `build_auction_settle_run` returns the existing run, `claim_run` refuses a `failed` run (its WHERE takes only `pending|running`), `execute_run` returns `None` — and then `:1531` `refresh_auction_message` does `fetch_message` + `edit` unconditionally. Two Discord REST calls per minute per stuck lot, indefinitely, editing identical content.

Minimal fix: give lots the same `_RESWEEP` backoff, and make `refresh_auction_message` a no-op when nothing changed (or move it inside the branches that changed something).

## N9 — LOW. The stake preview renders a full figure block for an amount the market will refuse
`estates_main.py:798-837`

`show_stake_preview` never reads `market["min_stake"]` / `max_stake`; only `edb.create_stake` enforces them, at confirm time. A market with `min_stake=1000`: the punter types 500, is shown "Pool after your stake", "This side after your stake" and "Indicative return per 100 → about N coins back", clicks Confirm, and gets "Refused: minimum stake is 1000 coins". No money moves, but Rule 4 is that the figures on the screen are the deal being offered. Minimal fix: check both bounds at the top of `show_stake_preview` and reply with the limit instead of rendering the block.

---

## Checked and clean — stated rather than padded

- **Modals**: 4, all `TextInput`-only, ≤5 inputs (`NewMarketModal`/`NewLotModal` are exactly 5). No select, no autocomplete anywhere in the file.
- **Views holding free text**: none. AST scan for `TextInput` inside a `View` subclass: zero hits.
- **Typed IDs / exact names**: none. Markets, lots, outcomes and runs are all `_PickSelect`; seller is `UserSelect` (`:2019`); channel is `ChannelSelect` (`:1820`, `:2027`). Modals ask only for amounts, hours and authored copy. The bid floor is printed on the modal's own label (`:1014`) so nobody has to guess it.
- **Persistent-view placeholder state**: the four `DynamicItem`s all re-resolve from `interaction.message.id` before touching anything — `_resolve_market:608-617`, `_resolve_auction:620-629`, called from `:654, 673, 692, 711`. `OutcomeButton` carries an *index*, not an outcome row id, and looks the id up at click time (`:766-771`).
- **Defer within 3s**: every handler that touches sqlite or core defers first. The five that don't defer are the five that *must not* — `send_modal` is itself the initial response (`:770`, `:1004`, `:1752`, `:1776`, `:1838`, `:2044`), and each does only local sqlite reads before it.
- **`response.send_message` after a defer**: impossible; the only two call sites are inside `_reply`, behind `interaction.response.is_done()` (`:276-279`).
- **`allowed_mentions`**: every `send`/`edit` in the file carries `NO_PINGS` — `:277/279` (via `_reply`), `:403`, `:548`, `:562`, `:1917`, `:1926`, `:2115`, `:2122`. Grep confirms no bare `.send(`/`.edit(`.
- **Component budgets**: `MarketView` = 10 outcomes (capped at creation, `:1882`) + 1 = 11 buttons over 3 rows; `MarketAdminView` 1 select + 2 buttons; `AuctionAdminView` 2 selects + 2 buttons (rows 0/1/2); all selects ≤25 options. Largest embed is 13 fields. Nothing over 5×5 or 25 — the only 25-cap problem is the *silent truncation* in N5, not an API violation.
- **Money click with no figures**: none found. Stake, bid, close-market, close-lot, propose, confirm, void-release, void-refund, reverse and payout-resume are each rendered on an embed carrying recomputed numbers.
- **Decorated empty state**: none. Empty pools say "No stakes yet." (`:464`), empty positions say "None." (`:1679`), an empty run refuses to read as complete (`:1400-1408`, `:2540-2544`).
- `python3 -m pyflakes estates_main.py` → clean, exit 0.
---

## Import + interface smoke test

MISSING NAMES: **none.** Zero `edb.*` and zero `ledger.*` references in `estates_main.py` fail to resolve.

## Results

**Pyflakes** — clean, no edits needed, on all 7 `.py` files in `/home/claude/build/`: `estates_db.py`, `estates_main.py`, `ledger_client.py`, `ledger_migrate.py`, `ledger_v2.py`, `_throwaway_estates_main_test.py`, `_throwaway_ledger_test.py`. discord.py 2.7.1 / aiohttp 3.14.3 were already installed.

**Smoke test** — `/tmp/smoke.py`, exit 0. Run: `python3 /tmp/smoke.py --canary`

| check | result |
|---|---|
| `ledger_migrate.migrate()` on temp restocker-like db | ok, and idempotent on re-run (8 tables after) |
| `estates_db.migrate()` on temp estates.db | ok, and idempotent on re-run |
| `import estates_db` / `ledger_client` / `estates_main` | all ok with **no** token/URL env set |
| 98 distinct `edb.*` names → `getattr(estates_db, ...)` | **all resolve** |
| 8 distinct `ledger.*` names → `getattr(LedgerClient, ...)` | **all resolve** |
| 244 `edb.*` call sites bound against real signatures | all bind, 0 skipped |
| 14 `ledger.*` call sites bound against real signatures | all bind, 0 skipped |
| 14 coroutine call sites awaited | all awaited |
| 18 `from estates_db/ledger_client import ...` names | all resolve |
| `edb.*` names vs. frozen `ESTATES_DB_INTERFACE.md` | all 98 inside the contract, no reaching past it |

**Canary passed** — the checker flags 9 injected errors across 5 classes (missing `edb` name, missing client method, constant used in call position, wrong arity, un-awaited coroutine) and correctly leaves a valid call alone. A clean run is therefore meaningful.

## Notes on the checker

- Client aliases are discovered transitively from the module global `ledger`, not hardcoded: it found `client = ledger` at 9 sites (lines 863, 1076, 1154, 1180, 1209, 1288, 1424, 1556, 2814). A rename in `estates_main` cannot silently shrink coverage.
- `ledger.*` is checked against the `LedgerClient` **class** (the global `ledger` is `None` when unconfigured), with a placeholder bound for `self`.
- The temp restocker db is laid down with the real pre-v2 schema from `CORE_MONEY_PRIMITIVES.md` including `balances.coins REAL` — `ledger_migrate` hard-refuses a db with no `balances` table, which is itself a good guard and confirmed working.

## Residual gaps this does not cover

1. Only module-qualified access is checked. Attribute typos on rows/dicts (`row["hold_id"]` vs `row["holdid"]`) and on discord objects are invisible to it — those need the functional test.
2. Types are not checked, only arity. `edb.claim_bid("7")` vs `edb.claim_bid(7)` passes.
3. `estates_main` imports but never boots a gateway connection; command-callback bodies are never executed.

The pre-existing functional flow test that was at `/tmp/smoke.py` is preserved at `/tmp/smoke_flow_prev.py` — it exercises market create/stake/close/payout against a real temp db and is worth keeping in the loop alongside this one.