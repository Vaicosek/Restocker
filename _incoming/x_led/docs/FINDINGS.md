# V Tech build — adversarial review findings

Two independent reviewers, each told to REFUTE rather than approve. 12 money-path
findings and 14 Discord/product findings. Nothing here is shippable yet.

---

# PART 1 — Money paths (ledger_v2.py, ledger_client.py, estates_db.py)

I read the spec, then `ledger_v2.py`, `ledger_client.py`, `estates_db.py` in full, plus the live call sites in `estates_main.py` and the legacy `Restocker_db.adjust_balance` that shares the `balances` table.

**The claim that these are safe does not survive.** Twelve findings; four of them lose or strand real coins on sequences that will occur in normal operation. Ranked by severity.

---

## S1 — CRITICAL. Escrow is advisory: the legacy `adjust_balance` spends held coins, so a capture can fail on an already-won auction

- `/home/claude/build/ledger_v2.py:576-587` (`_debit`), `:768` (`respect_holds=False`), `:695-704` (`place_hold`)
- `/mnt/user-data/uploads/RestockerLocal/Restocker_db.py:982-1021`, deduction at `:1015`

`ledger_v2` enforces `available = balance - held` on every path it owns. Nothing enforces it on the paths it does *not* own. `Restocker_db.adjust_balance` — the mutator every shop, hive and payout in Restocker goes through — writes `coins = MAX(0, coins - ?)` with no reference to `ledger_holds` and no failure mode. The v2 module's header documents that *it* won't call `adjust_balance`; the converse is never addressed.

Sequence:
1. User has 10,000. Bids 10,000 on lot 412 → `place_hold` passes (`available` 10,000 ≥ 10,000), hold open, available now 0.
2. User buys anything in a V Tech shop. `adjust_balance(uid, -10000)` checks nothing, clamps nothing (there is nothing to clamp), commits. `coins = 0`, hold still open for 10,000.
3. Lot closes. `capture_hold` marks the hold `captured` (line 745), then `_debit(conn, uid, 10000, respect_holds=False)` → `WHERE ... CAST(coins AS INTEGER) - 0 >= 10000` → `0 >= 10000` false → `rowcount 0` → `insufficient` → **the whole transaction rolls back**, hold returns to `open`.
4. The capture can never succeed. The hold expires, releases nothing (there is nothing to release), the seller is never paid, and the winner has both the goods and the coins.

**Minimal fix:** one trigger closes every legacy path at once, rather than auditing every caller —
```sql
CREATE TRIGGER balances_respect_holds BEFORE UPDATE OF coins ON balances
WHEN NEW.coins < OLD.coins AND CAST(NEW.coins AS INTEGER) <
     (SELECT COALESCE(SUM(amount-captured_amount-released_amount),0)
        FROM ledger_holds WHERE user_id = NEW.user_id AND state='open')
BEGIN SELECT RAISE(ABORT, 'insufficient: would spend held coins'); END;
```
Add it in `ledger_migrate.py`. `adjust_balance` then raises instead of silently eating escrow.

---

## S2 — CRITICAL. A corrected resolution pays nobody: the payout run key omits the attempt

- `/home/claude/build/estates_db.py:1888` (`run_key = mint_key(domain, int(subject_id), action)`), `:1897-1900` (returns the existing run untouched), `:1919` (row key), `:1688-1690` (`attempt` exists but is never used in a key)

This is the exact Stoshi scenario the whole two-step design exists to prevent, and the two-step design does not survive it.

1. Market 77 confirmed to outcome A. `build_market_payout_run` → `create_payout_run` mints `estates:market:77:payout`, run #1, rows `estates:market:77:payout:user:<uid>`. Winners paid. `finish_run` → `done`.
2. Wrong outcome. `claim_resolution_reverse` → `build_market_reverse_run` (key `…:reverse`, distinct) claws back → `resolution_reversed` → market back to `closed`.
3. `propose_resolution` attempt 2, outcome B. Hold-down elapses. `claim_resolution_confirm` wins.
4. `build_market_payout_run` → `create_payout_run("market_payout", "market", 77, …)` mints **the same** `estates:market:77:payout` → line 1897 finds run #1 → line 1900 `return dict(existing)` **untouched, with zero pending rows**.
5. `resolution_confirmed(rid2, run1.id)` binds the corrected resolution to the completed wrong run. Market → `paying`. `run_progress` reports 100% paid. `finish_run` → `done`. **Outcome-B backers are never paid and the panel says the payout completed.**

Note that fixing only the run key is not enough — attempt 2's *row* keys would still be `…:payout:user:<uid>`, which core replays from attempt 1, so previously-paid users get nothing and new winners get paid: a silently half-correct payout.

**Minimal fix:** thread the resolution attempt through both keys.
```python
# create_payout_run(..., attempt: int = 1)
run_key = mint_key(domain, int(subject_id), action, f"a{attempt}")
... mint_key(domain, int(subject_id), action, f"a{attempt}:user:{user_id}")
```
and pass `attempt=int(res["attempt"])` from `build_market_payout_run` / `build_market_reverse_run`.

---

## S3 — CRITICAL. Money commits, the idempotency key stays `in_progress` forever, and the manual fix double-pays

- `/home/claude/build/ledger_v2.py:333-386` (`_claim_idempotency`), `:389-405` (`_complete_idempotency`), `:1118-1138` (`_idempotent`), `:420-425` (`sweep_idempotency` — 30-day TTL is the only reaper)
- `/home/claude/build/ledger_client.py:204-215` (`_CODE_MAP` has no entry for it)

The claim, the money move and the completion are **three separate transactions** (`_tx()` is explicitly non-re-entrant, so they cannot be composed). There is a window where the coins have moved and the key is not marked `done`, and nothing ever resolves a stale `in_progress` row inside 30 days.

1. Estates pays winner 123: key `estates:market:77:payout:user:123`. `_claim_idempotency` commits (tx 1).
2. `transfer()` commits — **coins have moved** (tx 2).
3. Before tx 3: the process is killed, the host reboots, or `_complete_idempotency`'s `_tx()` hits SQLITE_BUSY past the 10s `busy_timeout` and raises out of the context manager's `__exit__`. Row stays `state='in_progress'`.
4. `requeue_stuck_row` (estates_db.py:2155) puts the row back to `pending` after 900s. The retry sends the identical key. `_claim_idempotency`: INSERT conflicts → service matches → fingerprint matches → state is not `done` → **`raise LedgerError("idempotency_in_progress", 409)`** — line 385. Forever, for 30 days.
5. `idempotency_in_progress` is not in `_CODE_MAP` and not in §7's error list, so the client raises a bare `LedgerError`. `fail_payout_row` retries it 5 times, parks the row `failed`, `finish_run` → `failed`.
6. Staff see one failed winner in a run of 200 and pay them by hand. **The user is paid twice**, and the audit trail says they were paid once.

**Minimal fix,** in this order (the first alone is unsafe):
1. Record completion *inside* the money transaction — pass `key`/`response` into `transfer` / `capture_hold` / `adjust` and have them `UPDATE ledger_idempotency SET state='done', …` in their own `_tx()`. Then `in_progress` provably means "not applied".
2. Only then make a stale claim reclaimable: `... ON CONFLICT(key) DO UPDATE SET created_at=? WHERE state='in_progress' AND created_at < ?`.
3. Add `"idempotency_in_progress": IdempotencyInProgress` to `_CODE_MAP` as a distinct *retryable* class, so `fail_payout_row` never parks on it.

---

## S4 — HIGH. `settle_lot` captures the hammer price and then can never pay the seller

- `/home/claude/build/estates_main.py:1984-2014`

The docstring says "same keys on every retry, so a crash between the two steps replays instead of double-moving". The keys are fine; the *gate* is not.

1. `if not edb.claim_lot_close(lot_id): return` (line 1985) — the lot leaves `open`.
2. `hold_capture` succeeds. 50,000 lands in `treasury:estates`.
3. `transfer(TREASURY, seller, net)` raises (core restarting, rate limit — see the `_rate_limit_mw` note at ledger_v2.py:1615-1618, `/api/v1/ledger/` is **not** exempt from the 120 req/min limiter) → `except LedgerError: … return` (line 2011-2013), or the process simply dies.
4. Next tick: `claim_lot_close(lot_id)` returns falsy because the lot is no longer `open` → `return` at line 1986, **before the capture/transfer block**. Every subsequent tick does the same.
5. 50,000 sits in the treasury permanently. The seller is never paid. `mark_lot_settled` never runs, so the lot is stuck in `closing` and no alert fires.

**Minimal fix:** settlement must resume from *state*, not from winning the close race — `if not edb.claim_lot_close(lot_id) and (lot.get("state") not in ("closing","closed") or lot.get("settled_at")): return`. Better: delete this hand-rolled two-step and use `build_auction_settle_run` + `claim_payout_row`/`settle_payout_row` (estates_db.py:2230-2246), which were built for exactly this and are currently unreachable.

---

## S5 — HIGH. `estates_main.py` and `estates_db.py` are two incompatible generations — the reviewed safety machinery is dead code

- `/home/claude/build/estates_main.py:104` `import estates_db as edb`

`estates_main` calls at least: `open_stakes`, `mark_stake_captured`, `mark_stake_capture_failed`, `mark_stake_failed`, `attach_stake_hold`, `claim_high_bid`, `attach_bid_hold`, `mark_bid_failed`, `mark_bid_released`, `get_lot`, `lot_by_message`, `claim_lot_close`, `mark_lot_settled`, `list_lots`, `market_outcomes`, `market_totals`, `market_by_message`, `quote_stake`, `payout_progress`, `mark_payout_paid`, `mark_payout_failed`, and `claim_payout_row(market_id, uid)`. **None of these exist** in `estates_db.py` (whose `claim_payout_row` takes one argument). The bot `AttributeError`s on the first stake placed.

The consequence for this review: every property under S2, S6, S10 lives in code with no caller, while the code that *does* run (`close_market`, `run_payouts`, `run_void`, `settle_lot`) is a weaker second implementation with S4's bug in it. Any statement of the form "estates_db is claim-first, therefore estates is safe" is unfalsifiable rather than true. Reconcile the two before reviewing either further.

---

## S6 — HIGH. A stake captured-but-unacked becomes permanently unrefundable on a void

- `/home/claude/build/estates_db.py:1503-1506` (`unclaim_stake_capture` → `held`), `:1509-1517` (`claim_stake_release`), `:1533-1536` (`unclaim_stake_release`); same shape at `:1183-1189` and `:1223-1226` for bids

1. Market closes. `claim_stake_capture(s)` → `capturing`. `hold_capture` **commits at core** — the punter's 5,000 is now in `treasury:estates` — but the response is lost.
2. `unclaim_stake_capture` moves the row **backwards** to `held`.
3. Staff void the market. The void path takes `held` stakes: `claim_stake_release` → `releasing` → `hold_release(hold_id)` → core: the hold is `captured`, terminal → `409 hold_not_open` (ledger_v2.py:811-813) → `unclaim_stake_release` → back to `held`. Loop forever.
4. The stake is never `captured`, so `market_pools(captured_only=True)` (`:1556`) and `build_market_payout_run` (`:2182`) exclude it from the refund run. **The punter's 5,000 is in the treasury and no code path returns it.** The void reports 100% refunded.

**Minimal fix:** never move a row backwards past a terminal core state. On `hold_not_open`, re-read `GET /hold/{id}`; if `state == 'captured'`, reconcile *forward* — `stake_captured(id, ledger_ref=…)` — and let the refund run pick it up. Apply to `unclaim_stake_capture`, `unclaim_stake_release`, `unclaim_bid_capture`, `unclaim_bid_release`.

---

## S7 — HIGH. Every user-sourced transfer 403s: the client never sends `acting_user`. Clawback and rent cannot work at all

- `/home/claude/build/ledger_client.py:561-580` (body has no `acting_user`) vs `/home/claude/build/ledger_v2.py:1204, 1209-1213`

`h_transfer` requires `src == SERVICE_TREASURY[service]` **or** `src == body["acting_user"]`. `LedgerClient.transfer` sends exactly `from_user, to_user, amount, reason, idempotency_key`, so `acting` is `""` and the check reduces to "source must be the treasury". Any transfer whose source is a user returns `403 forbidden_source` on the first call, in production:

- `build_market_reverse_run` (estates_db.py:2225-2227) creates `direction="in"` rows — winner → treasury. Every row 403s, 5 attempts each, run parks `failed`. **The compensating clawback that the entire reversible-resolution design rests on has never been able to move a coin.**
- Rent, `estates:parcel:<id>:rent:<period>` tenant → owner (spec §10, estates_db.py:886-958). Same 403. The rent charge cycles `claimed → pending` until `MAX_PAYOUT_ATTEMPTS` parks it.

**Minimal fix:** `async def transfer(self, from_user, to_user, amount, *, idempotency_key, reason="", acting_user=None)` and include `"acting_user": str(acting_user)` when set; pass the debited user in the reverse and rent paths. **Do not** "fix" it by making the client always send `acting_user=from_user` — see U3 below, that converts a 403 into an unrestricted wallet-drain primitive.

---

## S8 — MEDIUM. A failed COMMIT poisons the thread's connection; the web thread stops moving money until restart

- `/home/claude/build/ledger_v2.py:254-264`

```python
    else:
        conn.execute("COMMIT")        # outside the try
```
If COMMIT raises (SQLITE_BUSY past `busy_timeout`, `SQLITE_FULL`, disk I/O error), the exception escapes with the transaction still open. `_conn()` caches per thread (`:225-236`) and never discards. Every subsequent `_tx()` hits `:249-253` and raises `RuntimeError("… not re-entrant …")`, which `_require` maps to `500 internal_error` (`:1052-1055`). Every balance-moving endpoint on that thread is dead until the process bounces, and the reported cause is "internal error", not a lock problem.

**Minimal fix:**
```python
    else:
        try:
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except sqlite3.Error: pass
            try: conn.close()
            finally: _local.conn = None
            raise
```

---

## S9 — MEDIUM. A hold retry conflicts on a clock-derived `expires_in`, stranding the coins it already reserved

- `/home/claude/build/ledger_v2.py:312-321` (`_fingerprint` hashes the whole body), `:371-376` (mismatch → hard 409)
- `/home/claude/build/estates_main.py:796-797`, `:971`: `expires_in = max(600, int((closes - utcnow()).total_seconds()) + GRACE)`

The **key** is stable; the **payload** is not. The client's own connection retry re-sends the identical dict and is safe — the failure is any retry one layer up, which `estates_db`'s `claim_stake`/`attempts`/`unclaim` design explicitly anticipates.

1. `create_stake` writes seq=5, key `estates:market:77:stake:5`. `claim_stake` → `placing`.
2. `POST /hold` with `expires_in=86402`. Core claims the key, **commits the hold**, response lost past the 15s timeout; all client retries exhausted → `LedgerUnavailable`. Row is `placing` with no `hold_id`.
3. A resume pass re-attempts 60s later. `expires_in` is now `86342` → different fingerprint → `409 idempotency_conflict` — whose own client docstring says "do NOT retry, fix the key" (ledger_client.py:185-187).
4. The hold exists at core, nothing in `estates.db` names it, the punter's coins are reserved and invisible for the full TTL, and the stake is dead.

`reason` strings are the next landmine in the same shape.

**Minimal fix:** send an absolute `expires_at` (stored on the row at creation), not a clock-derived duration. Or narrow the fingerprint to the money-identifying fields only — `user_id, amount, to_user, from_user, hold_id` — so a volatile field cannot fail a legitimate replay.

---

## S10 — MEDIUM. Confirm pays a figure staff never saw (Rule 4)

- `/home/claude/build/estates_db.py:1691-1698` (preview frozen at propose) vs `:2182-2205` (recomputed at confirm). Nothing compares them.

Propose at T: pool 40,000, paid 37,000 — one 10,000 stake still `capturing` after a lost ack. During the 300s hold-down a resume pass settles it → pool 50,000. Staff click Confirm on a screen reading "40,000 / 37,000". The run pays 46,250. Rule 4 is that users confirm *numbers*.

**Minimal fix:** in `claim_resolution_confirm`, recompute `market_pools(captured_only=True)["total_pool"]` and return `None` (with the delta, for the UI) when it differs from `preview_pool`, forcing a re-propose against the new figures.

---

## S11 — LOW. The sweep's progress marker is write-only, and the docstring credits it for the safety

- `/home/claude/build/ledger_v2.py:869-874` (docstring), `:878-883` (candidate query), `:902-908` (cursor write)

The cursor is written per row inside the row's own transaction — correct in form. It is never read: the candidate query re-selects `state='open' AND expires_at <= cutoff` from the top every pass. That is *safe* (the per-row claim-first UPDATE makes re-selection a no-op), but the safety comes from the claim, not the marker. Leaving a marker that looks load-bearing invites a later "optimisation" that adds `AND hold_id > cursor` to a query ordered by `expires_at`, which would silently skip holds. Either delete the cursor and say the claim is the guard, or make it a real `(expires_at, hold_id)` tuple cursor.

---

## S12 — LOW. The treasury cannot go negative, contrary to §3's stated guarantee

- `/home/claude/build/ledger_v2.py:578-594`; LEDGER_API_v2.md §3

§3's argument is that an estates bug "misallocates money but can never create it — the treasury goes negative and screams". `_debit`'s WHERE applies to every account including `treasury:estates`, so it cannot go negative: the payout row just returns `insufficient`, `fail_payout_row` retries 5× and parks it `failed`. The invariant holds, but insolvency is indistinguishable from a punter having no coins. Emit a distinct `treasury_insolvent` code when `user_id.startswith("treasury:")`, so the scream is audible.

---

## Checked and could NOT refute — stated as such rather than padded

- **Double capture / capture-after-release.** `capture_hold:745-751`, `release_hold:804-810` and `sweep_expired_holds:890-895` each carry `AND state='open'` in the UPDATE itself and check `rowcount`, all inside `BEGIN IMMEDIATE`. I could not construct a sequence producing two captures or a capture after release. `CHECK (captured_amount + released_amount <= amount)` (ledger_migrate.py:78) is a second net.
- **Expiry sweep racing an in-flight capture.** Both take `BEGIN IMMEDIATE` on the same file; they serialise, and the loser gets `rowcount 0`. No loss found. The domain-level consequence (a hold expiring before estates captures it) refunds the punter — the safe direction.
- **Float arithmetic / rounding.** `pari_mutuel_payouts:678-690`, `auction_split:733`, `indicative_odds:721-723` are integer floor throughout; `paid + remainder == distributable` holds by construction and the remainder's destination is documented. `_int_field:1069-1093` and `_coins:245-257` reject non-integers rather than rounding. The only `float()` is `pe` in the stocks passthrough (`:1347`), not a money path. **No coin can be created or destroyed by rounding anywhere I can find.** This part is genuinely good.
- **Progress marker after a loop.** `settle_payout_row:2038-2041` advances `done_rows`/`cursor_seq`/audit in the same transaction as `claimed → paid`, and `next_payout_row:1981-1984` is state-driven rather than cursor-driven, so the cursor is decorative but harmless. Correct per Rule 2.
- **available-vs-balance.** Every debit path checks `available` except the deliberate `respect_holds=False` at capture, which is sound *in isolation*; S1 is about what happens outside this module, not a defect in `_debit`.
- **UNPROVEN — unverified `acting_user` (`ledger_v2.py:1204, 1209-1211`).** A satellite asserts who pressed the button and core trusts it, so an `estates` token could debit any wallet by echoing the victim's id. I cannot construct a live loss, because no caller sends `acting_user` at all (S7) — the primitive is currently unreachable. Flagged specifically so S7 is not fixed by having the client blindly set `acting_user = from_user`, which would make it reachable and unrestricted. Fix S7 by passing the *interaction's* user id, and have core cross-check it against a signed interaction context if that ever becomes available.
---

# PART 2 — Discord reality and product rules (estates_main.py)

Read the contract, `estates_main.py` in full, and cross-checked every `edb.*` / `ledger.*` call against `estates_db.py` and `ledger_client.py`.

## Platform-impossibility checklist — clean

No modal contains a select or autocomplete (`StakeModal`, `BidModal`, `NewMarketModal` 4 inputs, `NewLotModal` 5 inputs — all `TextInput`, ≤5). No View is fed free text. No flow asks for an ID or an exact name — seller is `UserSelect`, channel is `ChannelSelect`, market/lot/outcome are all `Select` pickers; modals ask only for amounts, hours, and authored copy. Component budgets hold (`MarketView` = 20 outcome buttons capped at 10 by `NewMarketModal` + 1 info button; all selects ≤25 options). Every `send`/`edit` that renders user text carries `NO_PINGS`. `_reply` correctly switches to `followup.send` after a defer. The DynamicItem handlers do re-resolve from `message_id` (`_resolve_market`/`_resolve_lot`, lines 487–512) before touching money.

The defects are elsewhere.

---

### 1. `/home/claude/build/estates_main.py:2146` — the bot cannot boot; 33 of the 44 `edb.*` functions it calls do not exist

`main()` calls `edb.init_db()`; `estates_db.py` has no `init_db` (it is `migrate()`). AttributeError before `bot.run`. Behind that, the module's header docstring (lines 25–86) describes an `estates_db` API that was never built. Missing entirely: `market_outcomes`, `market_totals`, `market_by_message`, `quote_stake`, `attach_stake_hold`, `mark_stake_failed`, `mark_stake_captured`, `mark_stake_capture_failed`, `open_stakes`, `build_payout_rows`, `build_refund_rows`, `claim_confirm_resolution`, `clear_proposal`, `claim_void`, `payout_progress`, `mark_payout_paid`, `mark_payout_failed`, `markets_with_unfinished_payouts`, `create_lot`, `get_lot`, `list_lots`, `lot_by_message`, `set_lot_message`, `claim_high_bid`, `attach_bid_hold`, `mark_bid_failed`, `mark_bid_released`, `claim_lot_close`, `mark_lot_settled`, `user_bids`.

The ones that do exist have different signatures, and two are silently wrong rather than loudly wrong:

- `estates_main.py:787` — `edb.create_stake(market_id, interaction.user.id, idx, coins)` vs `estates_db.py:1403 create_stake(market_id, outcome_id, user_id, amount)`. Arguments 2 and 3 are swapped: the Discord user id is passed as `outcome_id` and the outcome index as `user_id`. Also `outcome_id` is the outcomes-table row id, not `idx`.
- `estates_main.py:825, 1061` — `edb.user_stakes(user_id, market_id)` vs `estates_db.py:1581 user_stakes(market_id, user_id)`, also swapped; the one-arg call at 1061 is a TypeError.
- `estates_main.py:1031, 1022` — `list_markets(state=...)` / `list_lots(...)`; db has `list_markets(status=...)` and `list_auctions(status=...)`. The whole auction domain is named `auction`/`bid` in the db, `lot`/`bid` in main.
- `estates_main.py:1329` — `edb.set_market_message(market["id"], ch.id, msg.id)` vs `estates_db.py:1381 set_market_message(market_id, guild_id, channel_id, message_id)`: channel id lands in `guild_id`, message id in `channel_id`. Every persistent-view resolution by message id then fails.
- `estates_main.py:1914, 1921` — `pending_payout_rows(market_id)` / `claim_payout_row(market_id, uid)` vs `estates_db.py:1987 pending_payout_rows(run_id, limit=100)` / `1994 claim_payout_row(row_id)`. The db models payouts as **runs** (`build_market_payout_run` → `run_id` → `payout_rows`); main models them as a flat per-market set.

Fix: this is not a patch, it is a rewrite of every `edb` call site against the real module. Do it against `estates_db.py`'s actual names (`get_outcomes`, `market_pools`, `get_market_by_message`, `create_auction`/`create_bid`/`claim_bid`/`bid_held`, `preview_resolution`, `build_market_payout_run`, `claim_run`/`next_payout_row`/`settle_payout_row`/`fail_payout_row`/`run_progress`/`finish_run`, `unfinished_runs`) and delete the fictional API from the header docstring so it stops looking authoritative.

### 2. `estates_main.py:1711` — "Confirm & pay out" never materializes a payout run, so winners are never paid and nothing screams

`confirm` calls `claim_confirm_resolution(...)` then `asyncio.create_task(run_payouts(market_id))`, and `run_payouts` (line 1914) reads `pending_payout_rows(market_id)`. Nothing in the file ever *writes* payout rows — `build_payout_rows` is called only for the preview embed at line 1605, and its result is discarded. In the real db, `build_market_payout_run(market_id, resolution_id, ...)` (estates_db.py:2168) is the function that creates the run and its rows, and `propose_resolution`'s return value (which carries `resolution_id`) is thrown away at line 1654.

Failure: staff confirms a 60k market. `run_payouts` finds zero rows, posts a proof embed reading "Paid 0 · Coins 0 · 0/0 rows", the market shows resolved, `_status_embed` (line 1791) can't flag it because `total == 0`. Nobody is paid and the only surface that would notice reports nothing wrong.

Fix: in `confirm`, after winning the claim, call `build_market_payout_run(...)` and pass the returned `run_id` into `run_payouts`; make `_status_embed`/`payout_progress` treat `total == 0` on a resolved market as an error state, not "complete".

### 3. `estates_main.py:145, 398, 694, 697, 844, 1605` — the rake is read from live config instead of the market's snapshot

`estates_db.create_market` snapshots the rake into `markets.rake_bps` (schema line 377) with an explicit comment that "Every surface renders `market['rake_bps']`, never a literal". `estates_main` renders and *computes* with the module-level `RAKE_BPS` env value at eight sites: the odds line (398), the stake preview's `gross` (697), `quote_stake` (694), the position embed (844), and the payout plan (1605).

Failure: a market opens at 5%. Owner sets `ESTATES_RAKE_BPS=750` and restarts. Every already-open market's embed now says 7.5%, the payout arithmetic uses 7.5%, and punters who staked against a 5% embed get less than the number they read. This is the exact Stoshi failure the contract calls out — one config value, two effective numbers — arriving through the back door.

Fix: `rake_pct(market)` and every payout/odds computation take `int(market["rake_bps"])`. Keep the env value as the default for `create_market` only.

### 4. `estates_main.py:1663–1675` (and the entry at `1566`) — the irreversible money click shows zero figures

`_proposal_embed` contains only: the proposed outcome label, who proposed it, and when it becomes confirmable. `ConfirmResolutionView`'s "Confirm & pay out" is rendered on top of that embed. Worse, `_on_pick_resolve` line 1566 routes any already-`proposed` market straight to `ConfirmResolutionView(_proposal_embed(...))` — so a second staff member, who never saw `show_resolution_preview`, can pay out the whole pool having been shown no pool, no winner count, no total, no remainder.

Rule 4 is "users confirm numbers, not intentions", and this is the one button in the file where that matters most. Fix: have `_proposal_embed` recompute and render the same figure block as `show_resolution_preview` (pool, winning side, rake, winners, total to pay, remainder) from the DB at render time.

### 5. `estates_main.py:1881 / 1985` vs `2038 / 2045` — a half-finished close or settle is never resumed, contradicting the comments

`close_market` claims the state transition first (`claim_market_close` moves `open → closing`, estates_db.py:1588), then loops captures. `lifecycle_tick` line 2038 only scans `list_markets(state="open")`. So if the process dies mid-sweep, the market is `closing`, its remaining stakes are still `open`, and **no tick ever revisits it** — the docstring at 1871 ("a crash mid-loop resumes here on the next tick") is false. Same shape at `settle_lot`: line 1999 logs "will retry next tick" and returns, but `claim_lot_close` already moved the lot out of `open`, and line 2045 only scans open lots.

Failure: 300-stake market, redeploy at stake 140. 160 stakes stay held until expiry and are absent from the pool; the resolution then pays out from a pool 53% of its real size. The auction case is worse: the winning hold is captured to treasury and the seller transfer is never retried, so the treasury keeps the hammer price.

Fix: add a sweep over `status='closing'` markets and `status='closing'/'closed'` unsettled auctions to `lifecycle_tick`, and use the db's existing `unclaim_market_close` / `unclaim_auction_close` on failure paths.

### 6. `estates_main.py:1349–1356` — closing a market moves coins with no preview and no figures

`_CloseMarketSelect.callback` goes select → `await close_market(...)` → done. That call captures every open stake out of every punter's wallet into `treasury:estates`. The only thing shown beforehand is the sentence at 1270 — an intention, not figures. Every other money path in the file (stake, bid, resolve, void) has a preview + separate confirm; this one does not.

Fix: select → preview embed (stakes to capture, coin total, per-outcome split) → `CloseConfirmView`, mirroring `VoidConfirmView`.

### 7. `estates_main.py:1351` and `1814` — long HTTP loops awaited inline on a deferred interaction

Both `_CloseMarketSelect.callback` (`await close_market(...)`) and `PayoutStatusView.resume` (`await run_payouts(...)`) defer and then `await` a loop of one core HTTP round-trip per row. An interaction token is dead 15 minutes after the defer; a 300-row payout at ~200ms plus retries blows through it, the followup 404s, the staff member sees a spinner that never resolves, and — because the exception surfaces inside the component callback — nothing tells them the run is still going. `ConfirmResolutionView.confirm` at 1715 already does this correctly with `asyncio.create_task` + immediate reply.

Fix: both become `asyncio.create_task(...)` + "Run started — watch the proof channel", same as line 1714–1715.

### 8. `estates_main.py:1849` — unguarded ledger call leaves a permanently hung ephemeral

```python
held = await client.list_holds(member.id, state="open")
```
is outside the `try/except LedgerError` that wraps `client.balance` at 1837–1841. If core is up for the balance read and errors or times out on the holds read, `LedgerUnavailable` escapes the component callback. discord.py's default `View.on_error` only logs, so after `_defer(thinking=True)` at 1832 the staff member is left with a "thinking" ephemeral forever and no error text.

Fix: move line 1849 inside the same `try`, or wrap it in its own `try` that degrades to omitting the holds field.

### 9. Module-wide — no `on_error` on any View or Modal, so every failure presents as a hung spinner

Every `on_submit`/`callback` calls `_defer(thinking=True)` first (654, 872, 1123, 1181, 1292, 1350, 1429, 1557, 1594, 1648, 1698, 1721, 1762, 1811, 1832). `@bot.tree.error` (2129) only covers app commands. Given finding 1, *every one* of these paths currently raises `AttributeError`, and the user-visible result is an ephemeral that thinks forever with no message. Fix: a shared `async def on_error(self, interaction, error, item)` mixin on the views and `on_error(self, interaction, error)` on the modals that logs and calls `_reply(..., "That failed and nothing was charged.", error=True)`.

### 10. `estates_main.py:1739–1747` vs `1963–1975` — the void preview describes a different operation than the void performs

`_on_pick_void` shows only `build_refund_rows(market_id)`. `AdminView.void` (1504) offers `open` markets, whose stakes are held, not captured — for those the refund set is empty, so staff clicks "Void and refund everyone" under an embed reading "Stakes refunded 0 · Coins returned 0" while `run_void` proceeds to release N holds. If instead `build_refund_rows` includes open stakes, `run_void` both releases the hold (line 1967) *and* transfers the stake out of treasury (line 1926) — coins that were never captured into it. `estates` cannot mint (contract §3), so that path drives `treasury:estates` negative.

Fix: the void preview shows two figure blocks — "open holds to release: N / X coins" and "captured stakes to refund: M / Y coins" — and `run_void` builds its refund run strictly from `status='captured'` stakes.

### 11. `estates_main.py:843` — silent fallback inflates a displayed payout

```python
side = int(per.get(idx, 0)) or amount
```
When the per-outcome lookup misses (wrong key type) or the side legitimately totals 0, `side` becomes the user's own stake, and line 845 then renders `would = pool * (1-rake) / their_stake` — i.e. "you'd win the entire pool". Failure: user stakes 100 into a 50,000 pool, sees "→ ~🪙 47,500 if it wins", gets 190. That is the reading-it-as-theft failure mode. Fix: no `or` fallback — if `side <= 0`, render "—" and log; the same applies to `pool_after`/`side_after` at 695–696.

### 12. `estates_main.py:1617` and `1628` — duplicate embed field name

`show_resolution_preview` adds `name="Winners"` at 1617 and, when `rows` is empty, a *second* field also named `"Winners"` at 1628. Resolving to an outcome nobody backed renders two contradictory "Winners" fields ("0" and a sentence). Fix: rename 1628 to `"No winners"`.

### 13. `estates_main.py:2084–2093` — command tree synced on every `on_ready`

`on_ready` fires on every fresh IDENTIFY, not just the first. `edb.init_db()` + `tree.sync()` re-run on each reconnect, which is what the global-command rate limit exists to punish. Fix: move migrate + sync into `setup_hook`; keep the `is_running()`-guarded loop starts where they are.

### 14. `estates_main.py:1917–1919` — the one row that skips claim-first

```python
if amount <= 0:
    edb.mark_payout_paid(market_id, uid, ref="zero")
    continue
```
runs *before* `claim_payout_row`. Two concurrent runners (manual resume + `payout_resume_tick`) both mark it. Zero coins, so no money is lost today, but it is the exact act-then-mark shape the rest of the file is built to avoid, and it will get copied. Fix: claim first, then mark, or filter zero-amount rows out at row-build time.