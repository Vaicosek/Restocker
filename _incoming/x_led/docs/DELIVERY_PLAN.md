# Delivery plan — what lands where, and what blocks it

Nothing has been written to your disk or any repo yet. Everything below is in the cloud
workspace. This is the preview; you approve, then it moves.

All four repos get a branch + PR. Nothing touches `main`, nothing deploys.

---

## Repo: `Vaicosek/Restocker` — branch `feat/ledger-v2`

The core. Owns the only coin wallet, and now the escrow primitive.

| File | New/edit | Blocked on |
|---|---|---|
| `ledger_v2.py` | new — the aiohttp ledger blueprint (holds, scopes, idempotency, freeze) | round 3 money review |
| `ledger_migrate.py` | new — schema + the escrow guard trigger | round 3 money review |
| `land_money_migrate.py` | new — float → integer coins, dry-run by default | float exposure report |
| `docs/LEDGER_API_v2.md` | new — the cross-bot money contract | — |
| `docs/CORE_MONEY_PRIMITIVES.md` | new — map of the existing wallet functions | — |
| `docs/LAND_EXCHANGE_AUDIT.md` | new — what the existing escrow actually guarantees | land audit |
| `docs/LAND_ESCROW_PLAN.md` | new — bringing `land_listings` onto holds | land audit |
| `docs/LAND_MIGRATION_RUNBOOK.md` | new — how to run it with no shell on Wisp | float exposure |
| `docs/design/*.html` | new — the seven mockups | UI cleanup |
| `Restocker_web.py` | **edit** — register the ledger routes (2 lines, same pattern as `bank_api`) | round 3 |
| `cogs/land_exchange.py` | **edit** — escrow retrofit | LAND_ESCROW_PLAN, then a build round |

The two `Restocker_web.py` lines are the only edit to a file you already run. Everything
else in this repo is additive, so the PR can be read as "new files + 2 lines".

## Repo: `Vaicosek/Osentar-Bank` — branch `feat/ledger-v2-client`

| File | New/edit | Blocked on |
|---|---|---|
| `OSENTAR_MIGRATION.md` | new — the 11-item ordered change list | — |
| `restocker_client.py` | **edit** — the version-check that hard-fails against a `2.0` server | nothing; this is the urgent one |

That version check is the single item that takes the bank **offline** the moment core
reports 2.0. It ships first, on its own, before anything else in this plan.

## Repo: `Vaicosek/RestockerLightWeight` — branch `feat/escrow-aware-board`

| File | New/edit | Blocked on |
|---|---|---|
| `app.py` | **edit** — surface hold state on the board; no money decisions move here | LAND_ESCROW_PLAN |

The constraint that governs this repo: it runs in partner servers you do not control and
holds no DB. It stays a relay. If a change would make it a place where a money decision
happens, the change is wrong.

## New repo: `Vaicosek/Estates` — branch `main` (initial commit)

| File | Blocked on |
|---|---|
| `estates_db.py`, `estates_main.py` | round 3 + **auction removal** (below) |
| `ledger_client.py` | round 3 |
| `ESTATES_DB_INTERFACE.md`, `ESTATES_DB_USAGE.md` | regenerate after auction removal |
| `FINDINGS.md`, `FINDINGS_R2.md` | — |

---

## The one piece of real work still unscheduled

**Auctions have to come out of `estates_db.py` and `estates_main.py`.**

You chose to upgrade the existing exchange, so the parallel implementation is dead code —
but it is not a clean deletion. Auctions and prediction markets share the hold/capture
state machine, the payout-run engine, and the reconciliation sweeper in `estates_db`. The
auction-specific parts (`auctions`, `bids`, `claim_high_bid`, `build_auction_settle_run`,
`close_auction`, the lot views and embeds) come out; the shared machinery stays and gets
re-frozen into a new interface file.

That is a surgery round, not a delete. It should happen **after** round 3 clears, so the
reviewers grade the code that actually ships rather than code half of which is leaving.

Doing it in the other order would mean grading auction code that is about to be deleted,
and then re-grading everything anyway.

---

## Order of operations

1. **Osentar version-check fix** — ships alone, immediately, unblocks nothing but prevents
   an outage.
2. **Round 3 clears** — money + Discord reviewers come back with no new criticals.
3. **Auction surgery** on the Estates files, then re-freeze the interface, then re-verify.
4. **Land audit + float exposure land** → you pick the rounding rule with real numbers.
5. **Branches pushed, four PRs opened.** You read the diffs.
6. Only after that: the `cogs/land_exchange.py` escrow retrofit, as its own PR against a
   codebase you have already reviewed the foundation of.

Steps 1 and 2 are independent and running now. Step 4 is running now.

## What I will not do without you saying so

- Push to `main` on any repo.
- Run `land_money_migrate.py` with `--apply` against anything.
- Touch `restocker.db`.
- Create the `Estates` repo (that is a new public artifact under your account).
