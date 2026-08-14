# V Tech Ledger API v2 — the cross-bot money contract

**Status:** proposed, not implemented.
**Owner of this contract:** Restocker (core). Satellites conform to it; they do not extend it.
**Supersedes:** `/api/v1/bank/*` (v1.1), which stays alive as an alias — see §9.

**Revision, post-review.** Findings S1, S3, S8, S9 and S12 all landed against sections of
this document that were true of `ledger_v2.py` and false of the database it writes to, or
silent where a satellite needed an answer. What changed here:

| § | Change |
|---|---|
| **5.1 (new)** | **Escrow is enforced by a database trigger, not by this API.** The contract previously guaranteed `available = balance - held` while the legacy `adjust_balance` could spend held coins freely — the one thing the contract exists to guarantee was advisory. Read this section before touching any code that debits `balances`. |
| 3 | The treasury can now actually go negative, and says so. `treasury_insolvent` is a distinct code. |
| 4 | `GET /balance` also returns `insolvent`. |
| 6 | Only money-identifying fields are fingerprinted; `in_progress` provably means "not applied"; stale claims are reclaimable. |
| 7 | Error codes are a table with a **retryable** column, and gained `idempotency_in_progress`, `treasury_insolvent`, `escrow_shortfall`, `forbidden_source`. |
| 9 | `/api/v1/ledger/*` is exempt from the dashboard rate limiter; migration order. |

---

## 1. The rule that everything else follows

> **Coins exist in exactly one place: `restocker.db`. Every other bot asks.**

A satellite bot owns its *domain* (loans, parcels, lots, wagers) and an audit ledger of
what it did. It never stores a coin balance, never caches one for longer than a single
interaction, and never reconciles. If core is down, the satellite refuses the action —
it does not proceed optimistically.

## 2. Services and identity

| Service id | Discord | Owns | Treasury account |
|---|---|---|---|
| `core` | V Tech | wallet, stocks, freeze state, holds | — |
| `osentar` | Osentar Bank | savings, loans, bonds, credit limits | `treasury:osentar` |
| `estates` | Lands / Auctions / Betting | parcels, auction lots, bids, prediction markets, wagers | `treasury:estates` |

- **User key is the Discord user id**, everywhere, in every service. No mapping table.
- A user does **not** need to be in the V Tech guild to have a wallet. Guild membership
  gates *which panels they can see*, never *whether they have money*.
- Treasury accounts are real wallet rows with non-numeric ids, so the books balance and
  every house position is inspectable with the same tools as a player's.

## 3. Auth and scopes

One token **per service**, not one shared secret. Header stays `X-Bank-Token` for
compatibility; `X-Service-Token` is accepted as a synonym.

```
LEDGER_TOKEN_OSENTAR=<random-32>
LEDGER_TOKEN_ESTATES=<random-32>
```

| Scope | Meaning | `osentar` | `estates` |
|---|---|---|---|
| `wallet.read` | read balance / held / available | ✅ | ✅ |
| `wallet.transfer` | move coins **from its own treasury** to a user, or user→user | ✅ | ✅ |
| `wallet.mint` | create or destroy coins (`adjust`) | ✅ | ❌ |
| `stocks.trade` | buy/sell on the exchange | ✅ | ❌ |
| `hold.*` | place/capture/release escrow | ✅ | ✅ |

**`estates` cannot mint.** Every coin it pays out must have been captured into
`treasury:estates` first. That single restriction means an auction or betting bug can
misallocate money but can never *create* it — the treasury goes negative and screams
instead of silently inflating the economy.

**And the treasury really does go negative.** Until v2.0.1 that sentence was false: the
debit guard applied the same floor to `treasury:estates` as to a person, so an overpaying
payout row came back `insufficient` — indistinguishable from a punter with an empty
wallet — the satellite retried it five times and parked it `failed`. The invariant held
and the scream was inaudible. Now:

- a `treasury:*` debit may land **below zero**, up to `LEDGER_TREASURY_MAX_DEFICIT`
  (default 10,000,000; set it to `0` to restore fail-closed behaviour);
- it logs at ERROR, writes a `treasury_insolvent` row to `ledger_entries` in the same
  transaction as the debit, and every `GET /balance` on that account returns
  `insolvent: true` until it is topped up;
- past the deficit limit the call fails with **`treasury_insolvent`**, never
  `insufficient`. A house account that cannot pay is an operational emergency, not a user
  with no money, and the two must not share an error code.

A **user** wallet is still floored at zero on every path. Only `treasury:*` ids can be
negative, so `insolvent: true` on a person's wallet is a bug in core, not a domain event.

`osentar` keeps mint because interest is genuinely new coin. Mint is rate-limited and
every mint is tagged with a reason string that must be non-empty.

Unknown scope → `403 forbidden_scope`. An empty token file disables that service
entirely (`503 disabled`), same as v1.

## 4. Balance semantics — the part that is new

```
balance    = coins in the wallet row
held       = sum of that user's OPEN holds
available  = balance - held
```

Every debit, transfer, hold and stock buy checks **`available`**, never `balance`.
`GET /balance` returns all three, plus `frozen` and `insolvent` (§3 — always `false` for
a person). A satellite that shows a user their money shows `available` and, if
`held > 0`, a line saying what's reserved.

This is enforced **in the database**, for every writer, not just for the ones that go
through this API — see §5.1.

## 5. Escrow: hold → capture → release

The state machine. A hold is in exactly one state and only moves forward:

```
        ┌─────────┐  capture(amount ≤ held)   ┌──────────┐
        │  open   │ ────────────────────────▶ │ captured │
        └─────────┘                            └──────────┘
             │  release                        (remainder auto-released)
             ├──────────────────────────────▶ ┌──────────┐
             │                                 │ released │
             │  expires_at passes              └──────────┘
             └──────────────────────────────▶ ┌──────────┐
                                               │ expired  │
                                               └──────────┘
```

- **`expires_in` is required.** A hold with no expiry is a coin leak the first time a
  satellite crashes mid-flow. Core sweeps expired holds every minute and releases them,
  each in its own transaction, resuming from hold state rather than from a cursor: the
  candidate set is `state='open' AND expires_at <= now`, so a released hold leaves it and
  an interrupted sweep neither repeats work nor skips a hold.
  Auctions set expiry to lot-close + 24h; wagers to market-resolve + 7d. A satellite may
  extend a hold it owns (`POST /hold/extend`) — it may not extend someone else's.
- **Capture may be partial.** `capture(hold, amount=X)` where `X < held` captures X and
  releases the rest atomically. This is how a losing pari-mutuel stake refunds cleanly
  on a voided outcome.
- **`capture` takes an optional `to_user`.** Omitted → coins are destroyed (only with
  `wallet.mint`). Present → coins move to that account atomically. `estates` must always
  pass `to_user`, and it must be its own treasury.
- **A hold belongs to the service that created it.** Any service touching another's hold
  gets `403 forbidden_hold`.
- Capturing or releasing an already-terminal hold returns `409 hold_not_open` **unless**
  the idempotency key matches the call that terminated it — in which case it replays the
  original result. This is what makes retries safe.

## 5.1 Escrow is enforced by the database, not by this API

*This section is the one the contract was missing, and it is about the one thing the
contract exists to guarantee.*

Everything above describes what happens when money moves **through this API**. Coins in
`restocker.db` do not only move through this API. `Restocker_db.adjust_balance` — the
mutator behind every shop purchase, hive payout and legacy admin command — writes
`coins = MAX(0, coins - ?)`: it knows nothing about `ledger_holds`, clamps instead of
failing, and reports success-ish. So "held coins cannot be spent" was a convention
observed by one module and by nothing else in the process.

The sequence that broke, and would have broken in normal operation:

1. A user with 10,000 bids 10,000 on lot 412. The hold is placed; `available` is 0.
2. The same user buys something in a V Tech shop. `adjust_balance(uid, -10000)` checks
   nothing and commits. `coins = 0`, the hold is still open for 10,000.
3. The lot closes. `capture` marks the hold captured, then debits 10,000 from a wallet
   holding 0 → `insufficient` → the whole transaction rolls back, the hold returns to
   `open`, and **the capture can never succeed**. The hold eventually expires and
   releases nothing, the seller is never paid, the winner keeps goods and coins.

### The guarantee

> **No write to `balances` may deepen the gap between a wallet's `coins` and the total its
> OPEN holds have reserved.**

`ledger_migrate.py` installs three SQLite triggers, one per write shape that can lower a
wallet. The main one, `ledger_balances_respect_holds`:

```sql
CREATE TRIGGER ledger_balances_respect_holds
BEFORE UPDATE OF coins ON balances
FOR EACH ROW
WHEN CAST(NEW.coins AS INTEGER) < CAST(OLD.coins AS INTEGER)
 AND (open total for NEW.user_id) > 0
 AND (open total) - CAST(NEW.coins AS INTEGER)                    -- shortfall after
   > MAX(0, (open total) + (settling total) - CAST(OLD.coins AS INTEGER))  -- before
BEGIN
    SELECT RAISE(ABORT,
        'insufficient: would spend coins reserved by an open hold (ledger_holds)');
END;
```

On a healthy wallet this is the same rule as before — the shortfall before is 0, so any
write that leaves `coins` below the open total is refused. The difference is a wallet
that is *already* over-committed, which is covered below.

**Exactly what is covered** (this list used to read "any writer", and the schema enforced
one statement shape):

| Write | Guard |
|---|---|
| `UPDATE … SET coins = …` | `ledger_balances_respect_holds` |
| `INSERT OR REPLACE INTO balances …` | `ledger_balances_respect_holds_ins` (AFTER INSERT) |
| a real `INSERT` that lands | same |
| `DELETE FROM balances …` | `ledger_balances_respect_holds_del` |
| `INSERT … ON CONFLICT DO UPDATE` | fires the UPDATE trigger (SQLite upsert semantics) |
| `INSERT OR IGNORE` / `ON CONFLICT DO NOTHING` that is **ignored** | **not guarded — it writes nothing** |
| an `UPDATE` that rewrites `user_id` away from its holds | **not guarded** — nothing in Restocker does this |

`INSERT OR REPLACE` is a DELETE+INSERT: it fires no UPDATE trigger, and with
`recursive_triggers` off (the default, and what production runs) no DELETE trigger
either. It was verified walking straight past the old guard and zeroing a wallet with a
live 6,000 hold. The catch has to be **AFTER INSERT**, not BEFORE: a BEFORE INSERT
trigger also fires for inserts that are about to be discarded, and cannot tell an
`ensure_wallet(uid)` no-op from a destructive replace — it would have turned every credit
to a user with an open hold into an `IntegrityError`. AFTER INSERT fires only when a row
really landed.

An AFTER INSERT trigger has no `OLD` row — a REPLACE's delete has already happened — so
`ledger_balances_respect_holds_ins` cannot express "the shortfall may not grow" and is an
absolute floor instead: `NEW.coins < (open total) - (settling total)` aborts. The
`settling` term is there so the two guards judge the same wallet identically. Without it
they disagree for a wallet mid-capture, where the UPDATE guard deliberately allows `coins`
to fall to `open - settling` (that allowance is the only reason `settling` exists) and the
INSERT guard refused the same value. `settling` is non-zero only inside `capture_hold`'s
transaction, so on every other wallet the floor is exactly `open total`.

The migration `DROP`s and re-creates all three on every run, so it is idempotent and
re-runnable, and editing the DDL is enough to update a live database.

### Why it does not fire on `capture`

A capture legitimately spends the coins its own hold reserved. It is exempted by **what
the capture does, not by an exception it claims**. `POST /hold/capture` does, inside one
transaction:

1. `UPDATE ledger_holds SET state='captured', settling=<amt> … WHERE hold_id=? AND state='open'`
2. `UPDATE balances SET coins = coins - <amt>`   ← the guard sees this
3. `UPDATE ledger_holds SET settling=0 WHERE hold_id=?`

Step 1 is the claim-first guard that already existed. Because it runs first, the hold is
no longer `open` when step 2 fires the trigger, so the floor has already dropped by
exactly the amount being debited. A partial capture behaves the same way: the released
remainder leaves the open total too.

Step 1 also **declares** the settlement, and that is the half that ordering alone could
not supply. Consider a wallet with 8,000 coins and two legitimate holds, A = 3,000 and
B = 6,000 (over-committed by 1,000 — only reachable from damage done before the guard
existed, but reachable). Capturing A writes `coins 8000 → 5000` with 6,000 still open.
Numerically that is **identical** to a shop purchase of 3,000, so a rule phrased as
"`coins` must stay above the open total" refused both: A was blocked by B, B was blocked
by A, **neither hold could ever be captured**, the market or lot froze, and the only exit
was DB surgery. The rule above instead asks whether the shortfall *grew*: a capture
retires a reservation and debits exactly that reservation, so it leaves the shortfall
unchanged and is allowed, while a shop purchase retires nothing, grows it, and is
refused. First capture wins; the hold that genuinely is not covered fails on its own
merits with `insufficient` and the real figures.

`settling` is non-zero only between steps 1 and 3 of a single transaction: a rollback
discards it and step 3 is unconditional, so it cannot survive a commit.
`ledger_v2.escrow_settling_leaks()` re-checks that once a minute and logs at ERROR rather
than trusting this paragraph.

There is deliberately **no** `WHEN service <> …` escape hatch and no session flag that
switches the guard off. An exemption is something a future bug can take by accident;
ordering and a same-transaction declaration are things the capture either did or did not
do. If anyone reorders the debit before the hold claim, or drops the `settling` write,
captures start failing loudly against their own escrow instead of silently spending
someone else's.

### What a legacy caller sees now

```
sqlite3.IntegrityError: insufficient: would spend coins reserved by an open hold (ledger_holds)
```

- `RAISE(ABORT)` undoes the offending statement and propagates the error to the
  application. It is **never** a silent no-op and **never** a partial debit — verified:
  the balance is byte-for-byte unchanged after the abort.
- `Restocker_db.adjust_balance` does not catch `IntegrityError`, so it propagates to its
  caller and that caller's `with db()` block rolls back. The shop purchase / hive payout
  fails; the escrow is intact.
- A debit that stays **above** the hold floor is unaffected — a bidder with 5,000 and
  1,000 on hold can still spend 3,000 in a shop.
- Callers that must keep working while a user has escrow (shops, stock buys) should read
  `available` from `GET /balance` and refuse early with a human sentence, so the user is
  told "1,000 of your coins are reserved by your bid on lot 412" instead of seeing a
  generic error. The trigger is the backstop, not the UX.
- Inside this API the same condition surfaces as the distinct code `escrow_shortfall`
  (409). It should be unreachable — it means a wallet was over-committed *before* the
  guard was installed — and it says "reconcile `ledger_holds` against `balances`" rather
  than blaming the user's balance. It reports the figures the operator needs: the amount,
  the balance it would leave, the real `held` total and the ids of the holds blocking it.
  (It used to interpolate the caller's local `held`, which is 0 on every capture path,
  and told the operator "(0 held)" while 9,000 was reserved.)

### Deployment order

The trigger goes in **before** the first satellite starts placing holds, and
`ledger_migrate.py` is safe to run repeatedly, so run it on core first. Legacy paths that
were quietly eating escrow before it landed will start failing at the point where they
would have caused the loss; that is the intended change.

## 6. Idempotency — keys are minted by the caller, from the domain

The v1 client mints a fresh `uuid4()` per HTTP attempt. That is safe for its own internal
connection retry and **unsafe for anything above it**: a satellite that retries an
operation produces a new key and double-charges.

**Rule: the key is derived from the domain event, so the same business action always
produces the same key no matter how many times it is attempted.**

```
<service>:<domain>:<entity_id>:<action>[:<seq>]

estates:auction:412:settle
estates:market:77:payout:user:1203738126850461738
osentar:loan:9:disburse
osentar:savings:accrue:2026-08-13
```

- Core stores `(idempotency_key → response)` for 30 days and replays byte-identical
  results. Replays are marked `"replayed": true` so a satellite can tell.
- Same key + **different** payload → `409 idempotency_conflict`. This catches key-reuse
  bugs loudly instead of silently applying the wrong amount.
- `uuid4()` is still accepted, for reads and for anything genuinely one-shot.
- It is **banned**, with `400 bad_idempotency_key`, on exactly these endpoints
  (`ledger_v2.UUID4_BANNED_ENDPOINTS`; a bare 32-hex or dashed uuid4 is what is
  detected, and a domain key always contains `:`):

  | Endpoint | Why a fresh key is a second payment |
  |---|---|
  | `/adjust` | mints or burns again |
  | `/transfer` | moves the coins again; no state makes the repeat a no-op |
  | `/hold/capture` | capture may be partial, so a repeat takes more of the hold |
  | `/stock/buy`, `/stock/sell` | **R5-B.** The trade runs outside core's transaction, so the key is the ONLY refusal there is: two fresh keys buy the shares twice, both at a clean 200, neither marked `replayed`, nothing logged |

  Not banned, and each for a mechanical reason: `/hold/release` is refused a
  second time by the hold's own `state = 'open'` guard (`409 hold_not_open`), and
  `/hold` moves no coins — a duplicate is a second *reservation* that expires on
  its own, which is a cost this rule accepts and `ledger_v2._key_field` names in
  full. `/hold/extend` takes no key at all.

  A satellite that gets `400 missing_idempotency_key` must mint a domain key, not
  default a uuid4: on `/stock/*` that default is a double charge nobody can see
  afterwards, because both attempts are ordinary successes seconds apart.

### What "different payload" means — only the money fields are compared

The key is stable across attempts; the body often is not. Estates computes a hold's
`expires_in` from the clock, so attempt 1 sends `86402`, core commits the hold, the
response is lost, and the resume pass 60 seconds later sends `86342`. Comparing whole
bodies made that a hard `409 idempotency_conflict` — an error whose own client docstring
says "do NOT retry, fix the key" — while the hold sat at core with nothing naming it and
the punter's coins reserved and invisible for the full TTL.

The comparison is therefore over the fields that identify **which money moves**:

| Endpoint | Fingerprinted fields |
|---|---|
| `/hold` | `user_id`, `amount` |
| `/hold/capture` | `hold_id`, `amount`, `to_user` |
| `/hold/release` | `hold_id` |
| `/transfer` | `from_user`, `to_user`, `amount`, `acting_user` |
| `/adjust` | `user_id`, `amount` |
| `/stock/buy` \| `/stock/sell` | `user_id`, `market_id`, `shares` |

`expires_in` and `reason` are **not** compared: neither changes which coins move, and
both are exactly the kind of field that drifts between attempts of one business event.
`acting_user` **is** compared — it is an authorisation input, and a replay under a
different asserted actor must fail loudly. `100` and `100.0` compare equal. A different
amount, account or hold id still returns `idempotency_conflict`.

### `in_progress` provably means "the coins did not move"

The claim, the money move and the completion used to be three separate transactions.
That left a window in which the coins had moved and the key still read `in_progress`, and
nothing resolved a stale `in_progress` row inside the 30-day TTL: the retry got
`409 idempotency_in_progress` forever, the payout row parked `failed`, staff saw one
failed winner in a run of 200 and paid them by hand — and the winner had already been
paid. Three changes, which are only safe **in this order**:

1. **Completion is recorded inside the money transaction.** The
   `UPDATE ledger_idempotency SET state='done', response_json=…` is one more statement in
   the same transaction as the debit and the credit; they commit together or not at all.
   `in_progress` now means "not applied", `done` means "applied, and here is the exact
   response". The stored response *is* the response the first caller received — built
   once, stored, then returned — so a replay is byte-identical by construction rather
   than by two pieces of code agreeing.
2. **A stale claim is reclaimable — but only where step 1 applies.** A later attempt with
   the same key, service, endpoint and money fingerprint may take over a claim that has
   sat `in_progress` for more than **15 minutes** (`IDEMPOTENCY_STALE_SECONDS`, matched to
   the satellites' stuck-row requeue threshold). Takeover re-stamps `created_at`, which is
   the claim token, and the completion in step 1 is itself claim-first — so a stalled
   original that wakes up after a takeover finds it no longer owns the claim and **rolls
   its own money back** instead of paying a second time. A mismatched payload is still
   `idempotency_conflict`, stale or not.

   Step 1 is the *precondition*, and it is not true of every endpoint.
   `/stock/buy` and `/stock/sell` dispatch the trade into
   `Restocker_main.exec_stock_trade` through `run_on_bot_loop` and record the key
   afterwards, in a transaction of their own; for them `in_progress` does **not** mean
   "the coins did not move". Applying the takeover to them charged a user twice for the
   same 7 shares. So the claim row now carries `applied_unknown`, written by the same
   statement that takes the claim:

   - **in-band** (`hold`, `hold.capture`, `hold.release`, `transfer`, `adjust`) →
     `applied_unknown = 0`, takeover-able, releasable by a failing handler.
   - **anything else** → `applied_unknown = 1` until the outcome is **recorded**, which
     is not the same instant as the outcome being *known*. Such a claim is never taken
     over and never deleted by an attempt that raised — an exception from the bot loop
     does not mean the trade did not commit. A retry gets `idempotency_in_progress` while
     it is fresh and `idempotency_unresolved` once it is stale, plus an ERROR log naming
     the key **and the wallet**.

     The flag comes off on exactly two statements, and neither of them leaves a gap:
     the `state='done'` write, which clears it in the same UPDATE, and a **definite
     refusal**, where core said no and the claim has to be released or a corrected retry
     is blocked for thirty days. Clearing it anywhere else — including "the bot loop
     returned, so we know" — re-opens the double-charge, because between that clear and
     the store the row reads `in_progress, applied_unknown = 0` over coins that have
     already moved.

     **And "definite refusal" is a protocol, not a value test.** It means a dict whose
     `ok` is falsy *and* whose `code` core recognises as a pre-trade rejection —
     `DEFINITE_STOCK_REFUSALS`: `insufficient_funds`, `no_shares_available`,
     `insufficient_shares`, `not_public`, `not_listed`, `bad_shares`. Everything else is
     an **unknown outcome** and keeps the flag: `None`, `{}`, a dict with no readable
     `code`, the engine's unclassified `error`, its `deduped` (which means an *earlier*
     attempt applied the trade), a non-dict, and a dispatch that raised or timed out.
     Reading a falsy return as a refusal was a second double-charge in its own right —
     `run_on_bot_loop` gives up after 20s over a synchronous core that cannot be
     cancelled, so "no answer over a committed trade" is a real shape, and round 4's
     `r = r or {}` released the key on it and let the retry buy the shares again. An
     unreadable answer now returns **409 `idempotency_unresolved`** to the caller
     immediately, rather than a `200 {"ok": false}` that reads as "nothing was taken";
     a raised dispatch keeps its own exception (500) and the same ERROR log. The cost is
     symmetric with the one below: a refusal code added to `exec_stock_trade` and not to
     that set locks its key until an operator releases it.

     **The accepted cost.** A *successful* trade whose response fails to store (a
     SQLITE_BUSY on that COMMIT, a killed process) leaves the claim unresolved forever:
     the retry is refused with `idempotency_unresolved` and an operator must check that
     wallet's stock ledger by hand and delete the key if the trade did not land. That is
     deliberate. The alternative is a silent second charge, and there is no third option
     — nothing outside `Restocker_main` can ask whether `exec_stock_trade` committed.

   The list is an **allowlist**, so an endpoint added later is takeover-unsafe until
   somebody says otherwise, and the declaration is checked rather than trusted:
   completing a key inside the money transaction is only reachable for a declared in-band
   endpoint, so a missing declaration fails loudly on the first call instead of quietly
   arming the takeover.
3. **`idempotency_in_progress` is a first-class, RETRYABLE error code** (§7), typed in
   the client as `IdempotencyInProgress`. Retry the identical call with the identical
   key: either the original finishes and you get its stored response replayed, or the
   claim goes stale and your retry takes it over. **Never park a payout row on this code
   and never pay it by hand** — the coins have not moved, and if they had, the key would
   read `done`.

## 7. Endpoints

Prefix: `/api/v1/ledger`. All responses `{ok: bool, ...}`.

| Method | Path | Scope | Notes |
|---|---|---|---|
| GET | `/health` | public | `{version:"2.0", enabled, services:[…]}` |
| GET | `/ping` | any | verifies token, echoes service id + granted scopes |
| GET | `/balance?user_id=` | `wallet.read` | `{balance, held, available, frozen}` |
| POST | `/adjust` | `wallet.mint` | signed amount, non-empty reason |
| POST | `/transfer` | `wallet.transfer` | `from_user` must be self-treasury or the acting user |
| POST | `/hold` | `hold.*` | `{user_id, amount, reason, expires_in}` → `{hold_id, expires_at}` |
| POST | `/hold/capture` | `hold.*` | `{hold_id, amount?, to_user?}` |
| POST | `/hold/release` | `hold.*` | `{hold_id}` |
| POST | `/hold/extend` | `hold.*` | `{hold_id, expires_in}` |
| GET | `/hold/{hold_id}` | `hold.*` | own holds only |
| GET | `/holds?user_id=` | `hold.*` | own service's holds for that user |
| GET | `/stocks` | `stocks.trade` | unchanged from v1 |
| GET | `/portfolio?user_id=` | `stocks.trade` | unchanged |
| POST | `/stock/buy` \| `/stock/sell` | `stocks.trade` | unchanged, now checks `available` |

### Error codes

Every failure carries a machine-readable code. **Retryable** means: send the identical
call with the identical idempotency key again. Anything else is an answer — retrying it
turns one clear error into a flood, and a payout row carrying it should be parked for a
human.

| Code | HTTP | Retryable | Means |
|---|---|---|---|
| `insufficient` | 409 | no | A **user** does not have the available funds. |
| `treasury_insolvent` | 409 | no | A `treasury:*` account cannot pay, or is past its deficit limit (§3). An operational emergency, never a user's problem. **Escalate.** |
| `escrow_shortfall` | 409 | no | The debit would spend coins reserved by an open hold — the wallet is over-committed (§5.1). Reconcile `ledger_holds` against `balances`. |
| `frozen` | 409 | no | The wallet, or a counterparty's, is frozen (§8). |
| `hold_not_found` | 404 | no | No such hold. |
| `hold_not_open` | 409 | no | The hold is already captured / released / expired. Re-read `GET /hold/{id}` and reconcile **forward**, never backward. |
| `forbidden_hold` | 403 | no | That hold belongs to another service. |
| `forbidden_scope` | 403 | no | The service lacks the scope (§3). |
| `forbidden_source` | 403 | no | `from_user` is neither this service's treasury nor `acting_user`. |
| `idempotency_conflict` | 409 | no | Same key, different money fields (§6). A key-derivation bug — fix the key, do not retry. |
| `idempotency_in_progress` | 409 | **yes** | Same key, same money, an earlier attempt is still in flight. For an in-band endpoint **the coins have not moved**. Retry; after 15 minutes the retry takes the claim over (§6). |
| `idempotency_unresolved` | 409 | no | Only from `/stock/*`. The trade was dispatched outside the ledger transaction and either never reported back, or reported something core could not identify as a refusal (§6) — so core cannot say whether it applied. Returned both on the dispatching call itself and on any later retry of that key. The key will **not** be re-granted. Check the stock ledger; if the trade did not land, delete the key (§6). |
| `bad_amount` · `bad_expiry` · `bad_idempotency_key` · `missing_*` | 400 | no | Malformed request. |
| `disabled` | 503 | yes, later | No token configured for that service. |
| `rate_limited` | 429 | yes, backed off | Core is shedding load. Ledger paths are exempt from the dashboard limiter (§9). |
| `version_mismatch` | 400 | no | Client/server major version disagreement. |
| `internal_error` | 500 | caller's call | Unhandled. Core may or may not have applied the write; only your idempotency key makes a second attempt safe — and it does. |

## 8. Freeze belongs to core

Today `/admin freeze` is local to `bank.db`, which means a frozen member can still bid on
land and place wagers. Freeze moves to core as a wallet-level flag with a reason and the
service that set it. **Every** money endpoint checks it. Satellites read it via
`/balance` and refuse before they even build the interaction.

Osentar's existing freeze rows migrate up on first boot of v2.

## 9. Compatibility and rollout

- `/api/v1/bank/*` continues to resolve to the v2 handlers, mapped to the `osentar`
  service with its v1 scope set. Nothing in the bank bot has to change on day one.
- `restocker_client.EXPECTED_API_VERSION` becomes a floor, not an equality check —
  the current `sv == "1.1"` test will hard-fail against a `2.0` server and take the
  bank offline. **Fix this before deploying core v2.**
- `doctor.py` gains a hold round-trip check: place 1 coin, read it back, release it,
  assert `available` returns to where it started.

### The rate limiter must not throttle the ledger

`Restocker_web._rate_limit_mw` sheds anything over **120 req/min/IP** and exempts the
single literal prefix `/api/bank/`. `/api/v1/ledger/*` was not exempt, so a payout run —
one HTTP call per winner, by design (§10.4) — throttled *itself*: a 200-winner run hits
the limit inside two minutes, the client does not retry a 429 (correctly: it is an
answer, not a connection failure), and rows park `failed`. This is also how an auction's
hammer price ends up captured into the treasury with the seller's transfer never sent.

`register_ledger_routes(app)` now wraps that middleware at mount time so every prefix in
`ledger_v2.RATE_LIMIT_EXEMPT_PREFIXES` skips it:

```
/api/bank/     /api/v1/bank/     /api/v1/ledger/
```

The wrap is idempotent, degrades to a loud warning if the middleware list is already
frozen or has no rate limiter in it, and leaves every non-ledger path throttled exactly
as before. Widening the check inside `Restocker_web.py` by hand is still welcome and the
two cannot conflict. Ledger traffic is authenticated per service and already serialised
by SQLite's write lock; the 120/min limiter exists for anonymous dashboard hits.

### Migration order

1. `python3 ledger_migrate.py --dry-run --db /path/restocker.db` — reports missing tables,
   columns, treasuries and whether the escrow trigger is `missing`/`outdated`/`current`.
2. `python3 ledger_migrate.py --db /path/restocker.db` — idempotent, re-runnable, and
   re-creates the trigger from the DDL in the file, so a rule change lands on re-run.
3. Only then start a satellite that places holds. §5.1 explains what legacy callers see
   from that moment on.

---

## 10. How `estates` uses this

### Auctions
1. Bid placed → `hold(user, bid, expires=close+24h, key=estates:lot:<id>:bid:<n>)`.
2. Outbid → `release` that hold. The new high bid keeps its own.
3. Close → `capture(winning_hold, to_user=treasury:estates)`, release all others,
   then `transfer(treasury:estates → seller, price - fee)`.
4. Crash anywhere in step 3 → replay with the same keys; nothing double-moves, and
   whatever wasn't captured expires and refunds on its own.

### Prediction markets & pari-mutuel betting
Same engine, two skins. A market has N outcomes; a wager is a stake on one of them.

1. Stake → `hold(user, stake, expires=resolve+7d, key=estates:market:<id>:stake:<n>)`.
2. Market closes → capture **every** stake to `treasury:estates`. The pool is now real
   coin sitting in a treasury, not a number in `estates.db`. Pool total and per-outcome
   totals are recomputed from captured holds, never from a running counter.
3. Resolution by a named oracle (staff, or an automatic feed for market-price questions).
   Payout per winning stake = `stake / winning_pool × (total_pool × (1 - rake))`,
   integer-floored, remainder to the treasury.
4. Payout → one `transfer` per winner, key
   `estates:market:<id>:payout:user:<uid>`. The row's status flips per row inside its own
   transaction, not after the loop, so a half-finished payout run resumes without
   re-paying anything it already paid.
   Note what that does **not** say: "resumes exactly where it stopped" was the claim here
   for four rounds, and it was true of the row statuses and false of the loop that read
   them (R4-1). A row that fails retryably goes back to `pending`, so a loop that asks for
   the first pending row every time spends every pass on that row and never reaches the
   rest of the run — one frozen winner stalling 199 others, with nothing parked and
   therefore nothing on any screen. The loop reads the whole pending set per pass and skips
   rows it has already attempted; a row that fails is a `continue`, never a `break`. A run
   that pays nobody on a pass says so: an ERROR log, a NOT PROGRESSING field on the payout
   panel, and a throttled post to the proof channel naming the blocking row and core's own
   error on it.
5. **Void** → release open holds, and for a market already captured, refund each stake
   at 100% with key `…:refund:user:<uid>`. Rake is not taken on a void.

Displayed odds are indicative and must say so — pari-mutuel odds float until close, and
a punter who thinks they locked in a price at bet time will (correctly) call it a bug.

**Resolution must be reversible.** Observed live at a competitor (Stoshi, Stoneworks):
a market was resolved to the wrong outcome and the owners had to announce a fix in
`#announcements` after payouts had already started. Build for it:

- Resolution is a two-step `resolve` (proposed) → `confirm` (pays out), with a
  configurable hold-down between them. A single misclick must not pay 60k.
- `unresolve` before confirm is free. After confirm, reversal is a *compensating
  ledger run*, never a deletion: claw back with keys
  `estates:market:<id>:reverse:user:<uid>`, which fails loudly if a winner already
  spent the coins rather than silently pushing them negative.
- **A reversal only covers what has already been PAID** (R5-A). The claw-back run is
  built by reading back the payout rows that are `paid` at build time, so a run that
  is still mid-flight has rows in neither run — and the reversal returns the market
  to `closed`, which is precisely what lets the corrected attempt pay those users a
  second time, on a fresh key namespace, out of a pool that only ever held the money
  once. Two rules, both required, and neither is optional because a stall is exactly
  when staff reach for this button: the run loop refuses a payout/refund run whose
  resolution is `reversing`/`reversed`, and the reversal itself refuses while any row
  is still `pending` or `claimed`. Rows that will not be paid are moved to a terminal
  `withdrawn` status by an explicit staff action, counted on the payout panel and in
  the proof post, and owed by the corrected attempt instead. "Still pending" must
  never be the record of a decision nobody made.
- Every payout run and every reversal posts to a public proof channel automatically.
  Their `#payout-proof` is manual screenshots; yours should be bot-written and
  therefore unforgeable-by-omission.

**One rake number, defined in one place.** Their FAQ says 7.5% and their live market
embed says 10%. That is not a rounding difference, it is two hard-coded constants —
and every punter who does the arithmetic will read it as theft. The rake lives in
config, the embed renders it from config, and the FAQ text renders it from config too.

### Lands
Parcels are inventory, not money: ownership rows in `estates.db`, sale/lease flows reuse
the auction path above. Rent is a scheduled `transfer` from tenant to owner treasury with
key `estates:parcel:<id>:rent:<period>` — the period in the key is what stops a retry from
charging two months.
