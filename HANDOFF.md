# V Helper — session handoff

Read this first in a new chat. It captures decisions and state that aren't obvious from
the code alone. Last updated: 2026-07-15 (Stages 2-4 implemented).

---

## ⚠️ Deploy state — READ THIS

**Everything below is written to disk but NOT deployed.** Nothing is live until:

```bash
python healthcheck.py          # preflight: compiles all .py, checks DB integrity
git add -A && git commit && git push
# then on Wispbyte: pull + RESTART (git pull alone does NOT restart the process)
```

`python healthcheck.py` was run against this session's copy of the code (not the live
restocker.db) and passed: 29 files compile, Restocker_db imports cleanly. A separate smoke
test exercised the new DB functions (market_item_targets, market_loyalty_ledger) against a
scratch SQLite file and the existing `tests/test_restocker_db.py` suite (6 tests) still
passes unmodified. None of this touched the live restocker.db or restarted anything.

**Before deploying, actually click through the new UI/commands on a test market** —
static checks catch syntax/import errors, not "does this feel right" or Discord-permission
edge cases (e.g. the ticket-owner overwrite in Stage 3 silently no-ops if the owner isn't a
member of the guild the ticket channel lives in).

## ⚠️ Sandbox gotcha — false syntax errors

The dev sandbox mount **serves truncated copies of large files**, so `ast.parse` reports
fake `SyntaxError` / `UnicodeDecodeError` / `IndentationError` at whatever byte it cut at
(usually mid-token, e.g. `o.ge` instead of `o.get`).

**How to tell a real error from a mount artifact:** check whether the reported line is the
*last* line the mount has (`wc -l`). If the parser reached line N of N, everything before N
is valid — Python reports the FIRST error, so an error past your edit means your edit parsed.
Verify the real file with the Read tool, and isolate-parse new code as a standalone snippet.

---

## Current work: market order builder (5 features) — Stages 2-4 done, Stage 1 recap below

Agreed with Vaicos. Build order: **categories + order builder first**, loyalty last. All
four stages are now written; nothing is deployed yet (see Deploy state above).

### ✅ Stage 1 — data foundation (DONE, not deployed)

* `items.category` column (+ `_migrate` ALTER, safe/idempotent).
* `market_item_targets` table — per-market, per-item `target_pct` + `tracked`.
* DB: `set_item_category`, `get_market_item_targets`, `set_market_item_target`
  (partial-update safe), `clear_market_item_target`.
* `Restocker_main.py`: `ITEM_CATEGORIES`, `_CATEGORY_RULES`, `_classify_item`,
  `_is_known_brew`, `_item_category`, `_backfill_item_categories`.
* Classifier tuned against the real 396-item catalog (Misc 42% -> 11.6%). Don't regress it.

### ✅ Stage 2 — API + UI (DONE, not deployed)

* `Restocker_main.py`: `_stock_refill_plan(market_id, target_pct=80.0, item_targets=None)` —
  now takes an optional per-item targets map. When given, ONLY items present and
  `tracked=True` are refilled, each to its OWN `target_pct`. `item_targets=None` keeps the
  old blanket behaviour (every under-target item, one scalar `target_pct`) so
  `/order_from_stock` and the legacy `generate_orders` web endpoint are untouched.
* `Restocker_main.py`: new `_market_catalog_by_category(market_id)` — items grouped by
  category with stock/capacity/target_pct/tracked, for the order-builder UI.
* `Restocker_web.py`: new routes `GET /api/owner/catalog`, `POST /api/owner/set_target`,
  `POST /api/owner/build_order` (owner-authed + CSRF, same pattern as the existing
  `/api/owner/*` handlers). `build_order` calls `_stock_refill_plan` with the market's
  `get_market_item_targets()` map.
* My Market tab: new "Order builder" card — categories, per-item checkbox (tracked) + %
  input (target_pct, autosaves via `set_target` debounced 500ms), "Build order" button
  (preview -> confirm -> create, mirrors the existing Inventory-tab generate-orders flow).

### ✅ Stage 3 — requester ping + ticket access (DONE, not deployed)

* `Restocker_main.py`: new `_market_owner_id(market_id)` — resolves the requesting
  market's owner (owner_id, falling back to leader_discord_id).
* `views/orders.py` ticket creation (`open_ticket`-style handler): the requesting market's
  owner is now added to the verification channel's permission overwrites (view + read
  history, no send) at CREATION time, same as a manager would get — so they can watch
  proof come in. Skipped if they're already the worker or unresolvable (legacy order).
* `views/orders.py` `ManagerReviewView.approve`: on fulfillment, DMs the requesting
  market's owner ("Your market's Order #N was fulfilled...") right before the ticket
  channel gets deleted. This is the "ping" half of the ask — the ticket access itself has
  to happen at creation time since the channel is deleted on approval.

### ✅ Stage 4 — V Tech group + per-market loyalty (DONE, not deployed — BIGGEST, review first)

This is the real data-model change flagged as risky. Implemented as an ADDITIVE layer, not
a replacement, specifically to minimize regression risk on live money:

* NEW table `market_loyalty_ledger` (user_id, market_id) -> points/total_earned — each
  market owner's own reward currency, independent per market. The EXISTING `loyalty` table
  is untouched and still drives tiers/interest/payout bonus (referred to as "the V Tech
  pool" going forward) — nothing about today's tier/interest/redemption mechanics changed
  in how they're computed, only in how much flows into that pool per order (see below).
* `Restocker_db.py`: `get_market_loyalty`, `add_market_loyalty_points`,
  `set_market_loyalty_points`, `get_market_loyalty_leaderboard`,
  `get_all_market_loyalty_for_user`.
* `Restocker_main.py`: `_vtech_group_markets()` / `_set_vtech_group_markets()` (config
  key `vtech_group_markets`, JSON list, via existing `bot_config`), `_is_vtech_market()`,
  `_award_market_loyalty_points()`. New constant `VTECH_SLICE_PCT` (env-configurable,
  default 25) — **this is a business-model knob, not a technical default. Vaicos should
  set the real number before deploy.**
* `views/orders.py` `approve()`: on fulfillment, a worker now earns points in TWO places —
  (1) the order's own market's ledger, in full, and (2) the shared V Tech pool: FULL
  amount if the order's market is in the V Tech group, else `VTECH_SLICE_PCT`% of it.
  **This changes the actual point totals workers accrue in the existing global `loyalty`
  table for any order NOT tagged to a V Tech market** — test this against real numbers
  before deploying, and set the V Tech group (see below) first so the slice logic doesn't
  apply to V Tech's own markets by mistake.
* `/market vtech_group action:view|add|remove market_id:...` (manager-only, in
  `cogs/market.py`) — configure which markets are "V Tech-owned" (Greyhames, Bank,
  Dragonmart per the business context). Empty by default — **nothing is in the V Tech
  group until this is run**, so right now EVERY market's orders only credit the pool at
  the slice rate. Run this before deploy or every market effectively gets the slice rate,
  including V Tech's own.
* `/loyalty stats`: now also shows a "Market Points" field (top 8 markets by balance).
* `/loyalty redeem`: new optional `market` param — redeem from that market's own ledger
  instead of the V Tech pool. Notifies the market owner (not just global managers) when
  market-scoped.
* `/loyalty redemptions` / `approve` / `deny`: a market's own owner can now action
  redemptions scoped to their market (global managers can still action anything).

**Not done / left for a follow-up session:** payout-bonus-pct and interest-weekly-pct
(the two economic levers on `LOYALTY_TIERS`) still key off the single V Tech pool total,
not any blend with market points — that matches "V Tech supplies mats/tools/factories/
brews... takes a cut" as the umbrella progression currency, but confirm that reading with
Vaicos before relying on it. No UI was added for a market owner to browse/adjust members
of the V Tech group (Discord-only, `/market vtech_group`) — could add a website panel
later if wanted.

---

## UI cleanup — manager panel + /orders (2026-07-15, DONE, not deployed)

Agreed with Vaicos: the manager panel was half-broken and looked nothing like `/orders`.

* **Manager Panel (`ManagerPanelView` in `views/orders.py`) slimmed to 3 buttons**: View
  Orders, Escalate order, Prune Fulfilled/Cancelled. Removed the five dead/duplicate
  buttons: Hive pickup status, Clear hive pickups, Set coin price, Funds report now, Apply
  interest now. Panel embed text in `cogs/orders.py::manager_panel` updated to match.
* **Only the BUTTONS were removed — not the systems.** Weekly interest and funds report
  still run automatically on their loops (`cogs/loops.py::weekly_interest_loop` /
  `weekly_funds_report_loop`), and interest still feeds the loyalty tiers. Pricing is on the
  website + `/set_price`. If you want manual triggers back, they're one button each.
* **One order UI everywhere.** "View Orders" now calls `orders_cmd()` (the exact `/orders`
  renderer — the "All Orders (N)" list + `OrdersBrowser` dropdown) instead of building its
  own embed. Retired `build_orders_pages()` and `OrdersPaginator` (the old paginated
  "Page X/Y" view) — they had exactly one caller, now gone. `RemindByIdModal` is left
  defined (harmless, still imported) in case the reminder-by-id flow is wanted elsewhere.
* **"Are all orders there?" — yes.** The manager `/orders` view iterates EVERY order in
  orders.yml, newest-first, showing all of them; ID gaps (e.g. #10, #1–8) are orders that
  were **pruned** via Prune Fulfilled/Cancelled, not hidden. One caveat: the manager embed
  truncates at ~3900 chars (Discord's limit) with a "showing N of M" footer rather than
  paginating — with the active list kept small by pruning this never bites, but if you ever
  expect 50+ live orders at once, add paging to `OrdersBrowser`. (This is the only thing the
  retired paginator did that the new view doesn't.)

## Recently fixed — the payout bug chain (deployed? NO — unchanged this session)

Three chained faults meant worker "Dr" did ~6 orders and was paid for 3:

1. `_get_coin_price` matched item names **exact-key only** → any drift (trailing space, case,
   `#variant` hash) returned **0**. It also `int()`-truncated (fractional prices lost; <1¢ → 0).
   → now tolerant (exact → case/space-insensitive → hash/colour-stripped), returns float, logs a warning.
2. The payout did `if payout <= 0: continue` — **silently skipping the worker**, while the order
   still closed as fulfilled. → now surfaces a loud 🚨 "NOT PAID" to the manager.
3. `add_coins` was called **without a `reason`**, so `coin_ledger` rows were anonymous — which is
   why "was order #22 paid?" was unanswerable. → now tagged `order#N`.

**Repair tooling** (all dry-run by default):
* `/admin repair_all` — payouts + team ledger + brew names in one.
* `/admin repair_payouts` — repays only orders the OLD lookup zeroed (filter: old==0 AND new>0).
* `/admin repair_order <id> <worker> <qty>` — for **orphaned** orders (fulfilled, NO claim and
  no `claimed_by` — nothing records who did it, so nothing can auto-attribute). **Order #22 is
  one of these** → Dr is owed ~47,250 + loyalty (mirrors #21, same shovel).
* `/admin backfill_team_perf` — team ledger only. NOTE: it had `if coins <= 0: continue`, so the
  very orders broken by the price bug were **skipped by the repair tool**. Fixed by #1.

**Idempotency:** repairs tag the ledger `repair:order#N` and check `coin_ledger_has()` before
paying. That helper **fails CLOSED** (returns True on error) — if we can't verify, we must not
pay twice. My first version WOULD have double-paid on a second run; the filter stays true after
repairing, so idempotency must come from the ledger, not the filter.

---

## Other things built this session (none deployed) — unchanged

* **Brew name cleanup** — strips lore junk (ads `@ /la spawn X`, state tags `Barrel aged`/
  `Distilled`, quality bar `[·····]`, durations, emoji, flavour prose), keeps only real effects.
  `_pretty_item_name` is the single canonical cleaner used by reports AND the website.
  Curated map: `data/state/brew_effects_manual.yml` — **manual entries always win and are never
  overwritten by learn/purge.**
* **Website ordering** — `POST /api/order` (logged-in, CSRF, catalog-only, multi-item cart) +
  Orders-tab cart UI. Approving a web order now **creates real claimable restock orders**,
  DMs the customer, and blocks self-approval (futures had this guard; web didn't).
* **Trade network** — auto-posts open orders as ONE consolidated thread to a SWTN-connected
  forum, throttled (network caps 3 posts/hour/guild). `/network invite|autopost|post`.
* **Satellite bot** (`../RestockerLightWeight/app.py`) — separate lightweight bot trusted into
  partner servers. Pulls orders from `/api/network/orders`, posts a board with a working Claim
  dropdown, reports claims to `/api/network/claim`. `/setup` registers a channel.
  Syncs commands **per-guild** (global sync takes up to an hour to appear).
* **Dev access** — `MANAGER_DM_IDS` now get EVERY market in `_owner_markets_for_user` AND
  `_markets_owned_by` (both, deliberately — the web panel and Discord gate must agree).
* `/market delete`, case-insensitive market lookup (TEST vs test made two markets).
* `/loyalty remind_unlinked` — DMs unlinked employees. **`set_deadline` defaults OFF**: the
  existing prompt flow writes `ign_pending`, and `loops.py` **removes the role** on timeout.
  62 people unlinked → turning it on would mass-strip 62 staff.

## Recurring bug pattern — worth grepping for

Twice now: **the code did its internal bookkeeping and skipped telling the human.**
Also watch for: missing module-level bindings in cogs (`log`, `asyncio`, `datetime` are NOT
auto-available — they're bound from `core` per-file; `ast.parse` passes, runtime NameErrors).
