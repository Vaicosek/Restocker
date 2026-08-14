# Round 5 review

## (a) Scorecard

Run against real temp DBs. **No file in `/home/claude/build` was edited.** `python3 -m pyflakes /home/claude/build/*.py` → exit 0. `python3 check_wiring.py` → exit 0, **same 4 defects as round 4, none new** (117 provider names, 0 missing; 311 call sites bound, 0 arity failures; 0 un-awaited coroutines). New harnesses: `/tmp/r5/h5b.py`, `/tmp/r5/h7.py`, `/tmp/r5/h8.py`, `/tmp/r5/m2.py`.

| # | Verdict | Proof |
|---|---|---|
| **R4‑1** (one row stalls the run) | **VERIFIED FIXED** — residual R5‑1 below | `estates_main.py:1660-1665` reads a snapshot and `continue`s. `/tmp/r4/h3.py` pass 1: `paid=4 pending=1` — u2..u5 paid while u1 fails `unavailable` for 7 passes with one key. Stall is now *said*: ERROR at `:1706`, `NOT PROGRESSING` panel field at `:1859`, throttled proof post `_announce_stall:1739`. `check_wiring` now reports `next_payout_row` as TEST_ONLY and `pending_payout_rows` as live — the roles swapped, which is the fix. |
| **R4‑2** (unpark refuses the reversal's own rows) | **VERIFIED FIXED** | `estates_db.py:3096-3101` tests direction before the resolution state; rationale at `:3013-3026`. `/tmp/r4/h4.py`: `unpark row 9 -> pending`, run reopens, `execute again: done`, `res state: reversed market: closed`, `re-propose: OK`. The retry re-sent the identical `estates:market:2:reverse:a1:user:u1`. |
| **R4‑3** (`release_refused` had no exit, on no panel) | **VERIFIED FIXED** | `_ask_core_about_refused_release` (`estates_main.py:3295-3336`) + `release_parked_holds:3379-3381`; own panel field with hold expiry at `:3208-3222`; `_unknown_warning` split into two fields at `:3569-3596`; button relabelled "Free the parked holds". `/tmp/r5/h5b.py`: still-frozen → `asked=1, failed=1`, refusals 3→4 (re-parks, bounded); unfrozen → `released=1, coins=5000`. **`/tmp/r4/h5.py` now crashes** — its fake ledger has no `get_hold`; harness limitation, not a product defect. |
| **R4‑4** (falsy `ok` read as a refusal → double charge) | **VERIFIED FIXED** | `DEFINITE_STOCK_REFUSALS:2059`, `_classify_stock_result:2071-2107`, unknown branch `ledger_v2.py:2209-2231`, `BaseException` guard `:2189`. `/tmp/r4/m1.py` case E: `status 409 ... exec_stock_trade ran 1 times` (was 2). 16-shape probe: `None`, `{}`, `{"ok":False}`, `code:"error"`, `code:"deduped"`, `code:409`, a str, a list, and `{"code":"ok"}` with no `ok` key all → `unknown`; only a dict with falsy `ok` and a recognised code → `refused`. |
| **R4‑5** (refusal whose flag-clear fails locks its key) | **NOT FIXED — unchanged, still acceptable** | `m1.py` case F: `_resolve_out_of_band` raising leaves `in_progress, applied_unknown=1`; retry 409 forever. R4 asked for one sentence in §6; §6 (`LEDGER_API_v2.md:400-415`) documents the *unknown-code* lock but **not** this one. |
| **R4‑6** (judgement still in two places) | **NOT FIXED** | `estates_main.py:1070` and `:1354` still hand-code `(InsufficientFunds, AccountFrozen, BadRequest, IdempotencyConflict)`; `edb.outcome_known_for` is used at `:381` only. Effect still benign (`h6.py`: the omitted codes land `place_unknown` and `replay_placements` resolves them). |
| **R3‑5** (head-of-line) | **VERIFIED FIXED** | = R4‑1. |
| **R3‑6** (`_Idem` without `endpoint`) | **FIXED for the main half; parenthetical NOT FIXED** | `_Idem.__init__:582` has no default for `endpoint`; both v1 aliases pass it (`:2384` `"adjust"`, `:2415` `"transfer"`); `_finalize_idempotency`'s guard is now unconditional (`:619-633`). **Parenthetical unchanged:** `transfer():1571` still lacks `h_transfer`'s `forbidden_source` rule (`:1885-1890`) and `_v1_transfer:2395` calls it directly — a wallet-sweep primitive for anyone holding the osentar token on the v1 alias. Osentar-only, still UNPROVEN. |
| **R3‑7** (AFTER INSERT floor) | **VERIFIED FIXED for the named disagreement** | `ledger_migrate.py:333-343` now `NEW.coins < open − settling`, rationale `:307-332`. Trigger test on a migrated DB: `settling=6000` → `REPLACE→0 ALLOWED, REPLACE→-1 REFUSED`; `settling=0` → `REPLACE→6000 ALLOWED, →5999 REFUSED`, identical to the UPDATE guard. One over-reach in the comment: the guards do **not** agree in general — with `OLD.coins=10000, settling=6000`, `UPDATE→0` is REFUSED while `REPLACE→0` is ALLOWED. Unreachable (SQLite single writer, `settling` non-zero only inside `capture_hold`). |
| `check_wiring` DEFECTs ×4 | **NOT FIXED** | `require_version`/`check_version`/`EXPECTED_API_VERSION` still dead — the boot version handshake never happens; `ledger_client.mint_key:410` still a second, disagreeing key derivation next to the live `estates_db.mint_key:295`. |

## (b) What round 5 broke

**R5‑1 — LOW at the default, MEDIUM if the knob moves, reproduced. R4‑1's head-of-line blocking survives at the batch boundary, and no docstring names it.** `estates_main.py:1660-1665`: the snapshot is `pending_payout_rows(run_id, limit=RUN_ROW_BATCH)` (`RUN_ROW_BATCH=500`, env `ESTATES_RUN_ROW_BATCH`). The query always returns the *first* N pending rows by seq, so if every row in one batch fails, the next iteration's batch is entirely `attempted` → `break`, and rows N+1.. are never touched on that pass or any later one. `/tmp/r5/h8.py` with `ESTATES_RUN_ROW_BATCH=3`, 6 rows, first 3 failing: `u4/u5/u6 ever attempted: False` across 3 passes; attempts on u1‑u3 climb 1→3. At 500 this needs 500 simultaneously-failing rows (core down for everyone), so it is not today's bug — but it is the fifth round of the same pattern: `execute_run`'s docstring (`:1638-1650`) says "a failure is a `continue`, never a `break`", the panel says "the rest of the run is not waiting on them" (`:1862`), and `LEDGER_API_v2.md:565` says "The loop reads the whole pending set per pass". All three are true of a batch, not of a run.

**Refusal protocol in `_stock_trade` — no new lock found.** Every shape I could construct falls to `unknown`, which keeps the claim and pages a human; nothing new can release a key. The real cost, correctly chosen and named in the docstring's LIMIT (`:2160-2165`): `{"ok": false, "code": "error"}` is a **documented** `exec_stock_trade` result (`CORE_MONEY_PRIMITIVES.md:457-459`) and now strands its key until an operator deletes it. That belongs in the runbook, not just the docstring.

**Payout-loop change — cannot skip a row (except R5‑1's boundary), cannot pay one twice, keeps the resume property.** `attempted` is per-invocation, `claim_payout_row:2839` is still the atomic gate, `settle_payout_row` still flips status inside its own transaction, `finish_run` is still in the `finally`.

**Reverse-run recovery — cannot re-pay a paid row.** Reverse rows are built by reading back `status='paid'` (`estates_db.py:3213-3215`) into the disjoint `…:reverse:a<N>:user:<uid>` namespace; the R4‑2 un-park retry re-sends the identical reverse key (`h4.py` transfer list). Un-parking an inbound row can only move coins *toward* the treasury.

## (c) Is there still a sequence in which a user is paid twice, charged twice, or loses coins?

**Yes. Two. Neither was introduced this round; both survived five rounds.**

**R5‑A — DOUBLE PAY, reproduced end to end, no client involvement, two staff clicks.** Reversing a payout run that has **not finished**. `PayoutStatusView.reverse` (`estates_main.py:4131-4139`) gates only on `live_resolution().state == 'confirmed'`; `claim_resolution_reverse` (`estates_db.py:2581`) and `build_market_reverse_run` (`:3196`) never look at the run's progress. `/tmp/r5/h7.py`, 3 winners, u3's transfer failing `unavailable`:

```
pass1: running  rows: u1 paid, u2 paid, u3 pending
reverse claimed: True     reverse rows: [u1 in 7400, u2 in 4933]   reverse run: done
res: reversed  market: closed
unfinished_runs: [(1,'market_payout','running')]  -> execute 1 -> done   (u3 PAID, 6166)
attempt2 run: done                                                       (u3 PAID AGAIN, 6166)
TRANSFERS: ...payout:a1:user:u3  6166   |   ...payout:a2:user:u3  6166
```

u3 receives 12,332 for a 6,166 entitlement; the treasury pays 24,665 out of a 20,000 pool. Three things combine: the reverse run is built from rows that were `paid` *at build time*, so u3's row is not in it; `recovery_tick` → `unfinished_runs` (`estates_main.py:4339`) resumes the reversed run forever with no state check; and `resolution_reversed` puts the market back to `closed`, which is exactly what lets `propose_resolution` mint attempt 2 with a fresh key namespace. `unpark_payout_row`'s `_UNPAYABLE_RESOLUTION_STATES` guard (`estates_db.py:3096-3101`) is the right test applied in the wrong place — it protects a *parked* row and not a *pending* one. Worst part: R4‑1's new stall notice sends staff to the one panel whose second button is "Reverse this payout", and that panel's confirm embed says "Rows to claw back: 2" while three are owed. **Minimal fix:** apply the guard the module already owns to the loop — in `execute_run`, after `run = edb.get_run(...)`, refuse a `market_payout`/`market_refund` run whose `resolution_id` is in `reversing`/`reversed`; and refuse the reversal in `ReverseConfirmView.go` while `run_progress(payout_run_id)['pending_rows']` is non-zero. Those rows then need an explicit terminal status, not silence.

**R5‑B — DOUBLE CHARGE, ledger-side permitted, needs a client that mints per-attempt keys.** `UUID4_BANNED_ENDPOINTS` (`ledger_v2.py:219-221`) is `{hold.capture, transfer, adjust}` — **`stock.buy` and `stock.sell` are not in it**, and they are the one pair whose money moves outside the transaction. `/tmp/r5/m2.py`:

```
attempt 1: key=3a660c6e211d... status=200 ok=True trades_run=1
attempt 2: key=1e4cec6ff1cd... status=200 ok=True trades_run=2
transfer status 400 bad_idempotency_key   (control: banned there)
```

Every `applied_unknown` / `idempotency_unresolved` protection N1 built is voluntary: a caller that sends a fresh uuid4 per attempt takes a fresh claim every time and buys the shares again, silently, at 200. This is not hypothetical shape-chasing — `CORE_MONEY_PRIMITIVES.md` item 4 records that the existing production client already defaults `uuid.uuid4().hex` on `adjust`/`transfer` and sends **no key at all** on `stock_buy`/`stock_sell`, which under v2 now returns `400 missing_idempotency_key`; the obvious repair is to copy the uuid4 default across, and ledger_v2 will accept it. `_key_field`'s docstring (`:1713-1721`) asserts the banned set is "exactly the calls where a retry with a fresh key double-pays" — that sentence is false about the endpoint the module elsewhere calls its only out-of-band one. **Minimal fix:** add `"stock.buy", "stock.sell"` to `UUID4_BANNED_ENDPOINTS` and correct that docstring.

**Also true, and not softened:** the `release_refused` → core says `captured` branch (`estates_main.py:3327-3335`) records the punter's coins as being in the treasury on a *voided* market and creates no refund row — it logs ERROR and carries it on the proof post. That is a real "user loses coins until a human acts", deliberately chosen and clearly said, but it is a hole with a person in it.

**What I checked and found clean.** Payout: one key per row across 8 attempts (`h3.py`), attempt-scoped keys, `claim_payout_row` claim-first, `unpark` never touches `idem_key`, `proof_message_id=NULL` on reopen. Escrow: `capture_hold`/`release_hold`/`sweep_expired_holds` claim-first inside `BEGIN IMMEDIATE` behind `CHECK (captured+released <= amount)`; the `win_now` re-read still blocks a second bid capture (`h2.py`); `capture_refused` still provably means three rolled-back transactions. Placement replay: `mint_key` is a pure function of `(domain, entity, action, seq)` and the replay returns the original hold (`h6.py`). Stock: 16 return shapes classified; `BaseException` on the dispatch; the claim survives a raise, a cancel, a failed store and an unreadable answer; the refusal path still releases the claim (case D). Idempotency: takeover excludes `applied_unknown=1`; `_finalize_idempotency`'s in-band declaration guard now fires on all 7 money paths. Triggers: UPDATE/INSERT/DELETE guards installed and agreeing on the mid-capture value. Reversal keys, refund keys and payout keys are disjoint namespaces.

## (d) Deployment verdict

If this ships tomorrow, the double-charge story on the stock passthrough is genuinely closed *provided the caller sends a domain-derived key* — and nothing in core enforces that, so the most likely first incident is a punter who buys 7 shares twice during a bot-loop timeout because someone defaulted a uuid4 to clear the new `400 missing_idempotency_key`; an operator would notice only from a user complaint and a stock ledger with two identical fills seconds apart, because the ledger logs nothing on that path (both calls are clean 200s and the second is not a replay). The worse and more expensive failure is R5‑A: core wobbles, a 200-winner market stalls with 40 rows pending, the new "NOT PROGRESSING" alert does its job and puts a staff member on the payout panel, and they press the danger button one line below the one they came for — 160 winners are clawed back, the 40 stragglers are then quietly paid by the recovery tick under a resolution that no longer exists, the market re-opens for a corrected attempt, and every one of those 40 who wins again is paid twice out of a pool that only ever held the money once. An operator would see it in the proof channel as two "Payout" posts for one market with no reversal between them, and in the treasury as a balance that does not reconcile against `pool_amount − rake`; nobody would see it at the moment it happened, because each individual transfer is correct, keyed, audited and idempotent — the arithmetic is only wrong at the level of the decision. Everything else I could construct is a stall, a park, or a coin reserved by a hold that expires on its own; those are visible, named on a panel, and recoverable by a button. I would hold the release on two changes: `stock.*` into `UUID4_BANNED_ENDPOINTS`, and the `reversing`/`reversed` check moved from `unpark_payout_row` into `execute_run`.
---

# Docstring checker

Both audits are complete and clean. Final state verified: `pyflakes` 0 on every file, both tools exit 0, both canaries pass.

## 1. `check_wiring.py` — was 4 DEFECT / 27 REVIEW, now **0 DEFECT / 26 REVIEW** (exit 0)

Forward: 119 provider names, 0 missing; 313 call sites bound, 0 arity failures; 0 un-awaited coroutines.

**All four DEFECTs were one root cause each, and all four are closed:**

| Defect | Fix |
|---|---|
| `LedgerClient.require_version` DEAD_SAFETY_MACHINERY — "Call once at boot", no boot caller, so **the version handshake never happened** | Wired for real: `estates_main._version_handshake()` (`/home/claude/build/estates_main.py:4389`), called from `setup_hook` (`:4440`). Deliberately non-fatal in both directions, and the docstring says so. Proven: `/tmp/r5/h_version.py` runs all three outcomes — compatible 2.3 → INFO, core 1.1 → `ERROR LEDGER VERSION MISMATCH`, core down → WARNING |
| `check_version` — reachable only from `require_version` | Now live through it. Its docstring said *"Best-effort; never raises"*; it is now `"Raises no LedgerError"` **and names the limitation the boot caller must handle**: an unreachable core and an incompatible one both return `False`, differing only in the second element |
| `EXPECTED_API_VERSION` DEAD_BY_TRANSITIVITY | Read on a live path now |
| `ledger_client.mint_key` — no caller, in `__all__`, and a **second, disagreeing** implementation of idempotency-key derivation (the live one is `estates_db.mint_key`) | Not deleted — `OSENTAR_MIGRATION.md:129` has other satellites importing it. Instead: (a) validation aligned — it now rejects `::` and a trailing `:`, so an empty durable fact can no longer produce a well-formed key naming the wrong event; (b) `estates_main._self_check_mint_key_agreement()` asserts at import that both minters build the identical string from the same facts and both reject an empty part; (c) both docstrings name each other, name the assert, and name what it does **not** cover (arg order, exception type, `None`-dropping). `estates_db` cannot delegate — it is stdlib-only by design, and that is now stated where the mirror is |

The assert is mutation-tested: changing `":"` to `"|"` in `ledger_client.mint_key` makes `import estates_main` raise `RuntimeError: idempotency-key minters disagree: … Two keys for one business event is a second charge`.

**Remaining 26 REVIEW, unchanged in character:** `next_payout_row` is now TEST_ONLY (R4-1's fix moved the loop to `pending_payout_rows`) — its docstring already names its own trap and says nothing drives a run with it, so it is honest and left alone; `mint_key` still reports as DUPLICATE_DEFINITION because the tool recognises delegation, not import-time assertion; 10 parcel/lease names, 9 unused `LedgerClient` endpoints, `utcnow`/`migrate`/`table_exists` — all as in R4. I added the new tool + fixture files to `EXCLUDE_FILES` (they are deliberately-broken copies; leaving them in doubled every duplicate pair).

## 2. `/home/claude/build/check_docstrings.py` — new, 1,050 lines, CONFIG at top, exit 0/1/2

Flags `--root --all --json --quiet --canary --modules`. Eight rules: **atomicity, only-of-a-kind, totality, named-peer, resumption, coverage, call-shape, mechanism, interface-drift**. Verdicts CONTRADICTED (defect) / UNVERIFIABLE / CONSISTENT.

Two design points that decided whether it works:
- It reads **`#:` comment blocks over module constants**, not just docstrings. Round 3's claim ("the ONE place that judgement lives") lives over `DEFINITE_REFUSAL_CODES`, not in any docstring — a docstring-only checker walks straight past the defect it exists to find.
- A function's **own docstring is never evidence for its body**. Reading it as a string constant made `settle_payout_row` report for writing a "cursor" its docstring says it deliberately does not have.

**Canary** (`check_docstrings_fixtures.py`, quoting FINDINGS.md:191 / R2:19, R2:58-66, R3:240-247, R4:130): all four caught, plus a fifth planted interface-drift. Controls live in a **separate file** (`check_docstrings_controls.py`) with the corrected version of each — a fixed docstring and its broken twin in one file are peers of each other, and the tool is right to flag the fixed one.

**Retro-tests in situ on the real tree** (the canary alone proves nothing about this codebase):
- Round 3 reintroduced (`_outcome_known` hand-coded again) → `CONTRADICTED named-peer estates_db.DEFINITE_REFUSAL_CODES (estates_db.py:184)`, cross-module.
- Round 4's docstring restored → `CONTRADICTED totality estates_db.unpark_payout_row (estates_db.py:3037)`: *"unqualified — the only exit, full stop — but the body returns without performing the write on 3 guarded paths"*. The first draft **missed** this in situ (the real docstring names `requeue_stuck_row`, which exempted the peer rule) — the totality rule is what actually catches round 4's shape.

**Run over the five modules:** 533 functions, 289 docstrings, 130 with absolute language, 208 claims tested → **2 CONTRADICTED, both fixed**, then 0 / 202 UNVERIFIABLE / 5 CONSISTENT.

Both CONTRADICTED were the frozen contract, `ESTATES_DB_INTERFACE.md`, which says of itself *"Generated by AST from the actual source. This file is the contract"*:
- `:175` `next_payout_row` — *"Resumption is simply: call this again"*, i.e. the R4-1 head-of-line trap, advertised as the API after the source docstring was corrected to warn against exactly that.
- `:252` `unpark_payout_row` — *"the ONLY exit from a parked payout row"*: **round 4's over-claiming docstring, corrected in `estates_db.py` and left standing in the file a caller actually reads.** Both entries repaired from source.

Four false positives were found and fixed during development rather than tuned away: docstring-as-evidence (above), `"the only endpoint"` matching an unrelated same-named client wrapper, a cross-function claim (`"…never reaches here"`) judged against the wrong body, and a regex backtracking bug where `\s*(?!that)` let the qualified, true sentence *"the only exit that is a RETRY"* match as unqualified.

Regression: `/tmp/r4/h1.py h3.py h4.py h6.py` all re-run green after the edits (h3 confirms rows 2-5 pay while row 1 keeps failing).

**Files:** `/home/claude/build/check_docstrings.py`, `/home/claude/build/check_docstrings_fixtures.py`, `/home/claude/build/check_docstrings_controls.py`, `/home/claude/build/check_wiring.py`, `/home/claude/build/estates_main.py`, `/home/claude/build/estates_db.py`, `/home/claude/build/ledger_client.py`, `/home/claude/build/ESTATES_DB_INTERFACE.md`. **Proof:** `/tmp/r5/h_version.py`, `/tmp/r5/mut/` (minter mutation), `/tmp/r5retro/` (round-3 retro), `/tmp/r5retro2/` (round-4 retro).