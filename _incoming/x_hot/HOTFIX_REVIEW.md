## Setup

Diffed both files myself (`land_exchange.py`: 12 hunks, +283/−53; `Restocker_web.py`: 9 hunks, +105/−6, all confined to lines 4409–4680). Originals are mode `-r--r--r--` and unmodified (md5 `3280e141…`, `373cf811…`). Built an independent harness at `/tmp/rev/` — real `Restocker_db` against a temp DB with production DDL, real patched module, `add_coins`/`deduct_coins` reimplemented exactly as `Restocker_main.py:2265/2313` (`adjust_balance` → `record_coin_ledger`), faults injectable at `update_land_listing(status='sold')`, `record_coin_ledger`, and `_credit_platform_balance`. Everything below marked CONFIRMED was executed against both trees.

---

# (a) Findings H1–H6

## H1 — double-settle mint: **VERIFIED FIXED**

`hotfix/cogs/land_exchange.py:380-437` (claim/release/re-arm), `:486` (claim), `:513` (ledger-key guard), `:548` (release), `:1156` (sweep re-arm).

**Crash sequence replayed against the patched code.** Listing #412, hammer 8,500,000, commission 5%. `update_land_listing(status='sold')` raises `sqlite3.OperationalError: database is locked`; the sweep re-enters every 60s:

```
pass1: RAISED OperationalError: database is locked
       status='active'  seller=8,075,000  house=425,000        <- claim released, money already moved
LOG WARNING [realestate] #1 already paid (realestate:sale:1 on record) —
            marking sold without re-paying seller 1001 or re-crediting commission
pass2: ok=True  net=8075000 comm=425000
       status='sold'    seller=8,075,000  house=425,000        <- key held; no second payout
pass3: ok=False "That listing is no longer active."
sale ledger rows: 1     MINTED: 8,500,000  (== the one real sale)
```

Original, same script: `sale ledger rows: 2`, `MINTED: 17,000,000`. Five consecutive locked sweeps: original `total=42,500,000`, patched `total=8,500,000` — flat. Re-arm proven live: a claim aged 30 min is invisible to `get_expired_active_listings()` (`visible=False`), `_rearm_stale_claims` returns 1, `visible=True`.

Loyalty gating is real and was a mint vector: in the original run the double-settles farmed loyalty and the *next* sale's commission dropped from 425,000 to 382,500 (4.5%). Patched holds 5%.

## H2 — instant-buy idempotency: **PARTIALLY FIXED**

`:655-657` (charge key), `:663-679` (raise → truthful message), `:681-687` (duplicate/refund).

The named failure is closed. Locked `mark_sold`, buyer clicks twice:

| | click 1 | click 2 | buyer paid |
|---|---|---|---|
| original | RAISED `database is locked` | ok | **17,000,000** |
| patched | "Your payment went through and this purchase is still completing…" | ok | **8,500,000** |

Three residuals, below (N2, N3, N4). Grading it PARTIALLY because N3 is a free-plot mint reachable through the same code, and N4 re-opens the double-charge under the exact DB stress H2 exists for.

## H3 — NaN/inf: **VERIFIED FIXED**

`:133-147` (`_coin_amount`), `:570-583` (bid entry + poisoned-floor), `:637`, `:705`, `:715`, `:481`; `Restocker_web.py:4503`.

| input | original | patched |
|---|---|---|
| `bid(nan)` | RAISED `ValueError: cannot convert float NaN to integer` | refused, balance untouched |
| `bid(inf)` | refused *by accident* ("Bid is inf coins but you have…") | refused |
| `bid(-inf)` / `bid(-5)` | refused | refused |
| listing with `current_bid=inf` persisted, then `bid(5)` | RAISED `OverflowError` | "This listing's price data is invalid" |
| `create_listing_core(nan)` | RAISED `sqlite3.IntegrityError: NOT NULL constraint failed: land_listings.reserve` | "Starting price must be > 0." |
| web `{"amount":NaN}` / `Infinity` (bare JSON tokens) | 200, reached the core | 200 `ok:false`, core never called |

The audit's §8-Sequence-3 claim that `int()` "arrests" NaN is confirmed, and the patch's two extra observations are confirmed too: SQLite stores `inf` faithfully (`stored current_bid = inf`), and `_min_next_bid` raises `OverflowError` before any guard.

## H4 — anti-snipe cap: **VERIFIED FIXED**, with one behaviour change worth knowing (N6)

`:63` (`max_auction_days=14.0`), `:602-621`, `:1535`/`:1545` (config knob).

Bid placed 2 min before the end, anti-snipe 5 min:
- day-1 auction: `ends_at` moves **+180s**, `anti_snipe_extended=True` — unchanged behaviour.
- day-20 auction (past the 14d wall): `ends_at` moves **−0s**, `anti_snipe_extended=False` — not extended, and critically **not shortened**. The `want_ts > end_ts` guard at `:620` is load-bearing; without it `min()` would pull the deadline forward.

## H5 — network identity: **VERIFIED FIXED**

`Restocker_web.py:4421` (`_land_manager_ok`), `:4463` (`_land_require_manager`), `:4598` (cancel), `:4636` (close), `:4669` (config write).

Executed against real handlers with a fake home guild (`954487497411403806`) and a fake partner guild:

| request | original | patched |
|---|---|---|
| `/close {listing_id, refund_bidder}` (**the deployed satellite's exact payload**) | 200, `_record_network_land_close` **called** | **400**, core **not called** |
| `/close` by partner-guild administrator | 200, called | **403**, not called |
| `/close` by home-guild administrator | 200, called | **200**, called |
| `/close` by home-guild non-manager | 200, called | **403**, not called |
| `/close` bad secret | 401 | **401** (secret gate still first) |
| `/cancel` partner admin sending `is_manager:true` | `is_mgr=True` into the core | **`is_mgr=False`** |
| `/cancel` home admin sending `is_manager:false` | `is_mgr=False` | **`is_mgr=True`** (recomputed, not trusted) |
| `/config` write, no identity | 200, config written | **400**, not written |
| `/config` write, partner admin | 200, written | **403**, not written |
| `/config` write, home "Manager" role | 200, written | **200**, written |
| `/config` **read** (`{}`, the deployed satellite's view) | 200 | **200** — still works |

`_land_manager_ok` is a faithful transposition of `Restocker_main.is_manager()` (`:1709-1725`) onto the home guild, plus the `MANAGER_DM_IDS` DM branch and the `HOME_GUILD_ID` pin from `_config_home_ok()` (`:16828-16833`). `intents.members = True` (`Restocker_main.py:388`), so `guild.get_member()` will resolve.

## H6 — display vs money: **VERIFIED FIXED**

`Restocker_web.py:4522` and `:4555`. `int(...)` → `int(round(float(...)))`. A 1000.6 bid deducts `int(round(1000.6)) = 1001`; the original note printed `int(1000.6) = 1000`. Both surfaces now print 1001. The instant-buy note at `:4555` had the identical defect and is fixed too. No amount changed.

---

# (b) Did the patch break anything that was working?

**Permission checks, caps, confirmation gates — all survive. Nothing dropped or weakened.**

- `is_manager` gates: 5 in the original (`:1224` seller-or-manager on cancel, `:1240` close, `:1274`, `:1286`, `:1313`), 5 in the patch (`:1451`, `:1467`, `:1501`, `:1513`, `:1542`). Same predicates, same order, same `"⛔ Managers only."` string ×4.
- All **23** distinct `{"error": ...}` refusal strings in the original are present in the patch. Zero missing. Business guards intact by count and by text: `"You can't bid on your own listing."`, `"A bid is already held on it — a manager must /close to unwind."`, `"Only the seller (or a manager) can cancel this."` ×2, the instant-buy-below-standing-bid floor `float(listing["current_bid"]) >= float(price)`, `bn <= starting_price`, `bal < amt`, `bal < price`, the post-expiry bid guard `:562`.
- Caps: `[:4]` photos ×2, `[:120]` title ×5, `[:1500]` description, `limit` ×5 — identical counts.
- Function inventory: **nothing removed**. Five pure additions (`_coin_amount`, `_claim_listing_for_settlement`, `_release_listing_claim`, `_rearm_stale_claims`, `_ledger_has_charge`).

**Did the order change leave a listing sold-but-unpaid, or a seller paid twice on a legitimate retry?**

Sold-but-unpaid: **no**. The order of money movements inside the settle is byte-identical to the original (preempt refund → seller → house → mark sold). The only insertion is the claim *before* all of it and the release *after* a raise. `update_land_listing(status='sold')` is still last among the money steps, so there is no window where the row reads `sold` with the seller unpaid.

Seller paid twice on a legitimate retry: **no in the ordinary case, yes in one narrow one** — see **N2**, which is not a claim-first problem but a durability problem in the key it relies on.

**Does the identity requirement break the deployed satellite?** Partially, and correctly — the board stays up.

| satellite command | payload it sends today | outcome against the patched V Helper |
|---|---|---|
| board / `/lands` | — | **works** |
| `/bid`, `/buy`, `/sell` | `bidder_id`/`buyer_id`/`seller_id` | **works** |
| `/cancel` | already sends `requester_id` (`app.py:668-670`) | **works** — seller-cancels unaffected; partner-admin cancels correctly refused |
| `/auction_config` (view) | `{}` | **works** |
| `/auction_config` (set) | `{"updates": {...}}` | **refused, 400** |
| `/auction_close` | `{"listing_id", "refund_bidder"}` (`app.py:687`) | **refused, 400** |

`_api_land_post` (`app.py:278-289`) returns `await r.json()` regardless of HTTP status, so the two broken commands surface the actual message — `❌ requester_id is required — update the V Tech Lands & Auctions satellite to the matching build.` — not a crash and not a timeout. Two manager-only commands degrade with an actionable error; the member-facing board and all trading keep working. That is a legitimate hotfix outcome, not an outage.

**Float arithmetic the audit measured as coin-conserving: untouched.** Re-verified by execution on the patched code:

| price | net | commission | sum | `int(round(price))` | conserved |
|---|---|---|---|---|---|
| 8500000.0 | 8075000 | 425000 | 8500000 | 8500000 | ✅ |
| 2500.5 | 2387 | 113 | 2500 | 2500 | ✅ |
| 1000.5 | 955 | 45 | 1000 | 1000 | ✅ |
| 1.0 | 1 | 0 | 1 | 1 | ✅ |
| 999999.99 | 955000 | 45000 | 1000000 | 1000000 | ✅ |

No `round()` changed, no float→int conversion, `sold_price` still stores the same REAL, `res["price"]` still `float(price)`. `_coin_amount` returns `float(v)` unchanged when valid — it is a validator, not a converter.

---

# (c) New defects

### N1 — CONFIRMED, most serious. The settle key is scoped to the listing, not to the sale; releasing to `'active'` re-opens trading on a half-settled row

`cogs/land_exchange.py:513` (`_sale_key = f"realestate:sale:{listing_id}"`) with `:393-414` (`_release_listing_claim` → `'active'`).

`'active'` means "bids are live and escrow is refundable". By the time the release runs, the seller may already hold that escrow. For an expired auction this is harmless — the sweep retries with the same bidder and the same price, and the post-expiry bid guard at `:562` blocks new bids. For a **manager-closed live auction** and for **fixed-price listings** it is not.

Reachable with **no concurrency at all**, executed:

```
B bids 5,000,000 on a live (non-expired) auction.
Manager /close  ->  seller paid 4,750,000, house 250,000, then mark_sold hits a lock.
                    Manager sees "Couldn't close — try again shortly."
                    Claim released -> status='active'.
C outbids at 8,000,000  ->  B refunded 5,000,000 IN FULL (already spent on the seller).
Manager /close again    ->  ok=True, outcome="sold", net_CLAIMED=7,600,000
                            seller ACTUALLY holds 4,750,000.
```

Two wrongs: 5,000,000 minted (B's escrow paid out *and* refunded), 8,000,000 destroyed (C's money never reaches the seller, because `realestate:sale:<id>` already exists from B's aborted attempt). Same script on the original: **+10,000,000**. So the patch cuts the damage from +10M to +2M but leaves the row wrong in both directions — and it now says `ok=True` with a `net` the seller never received.

Second reachable variant, also executed: fixed-price listing, buyer B's settle fails and releases, buyer C then instant-buys — seller receives **nothing** for C's purchase, `DELTA total = −8,500,000`, and the `_already_paid` warning is the only trace.

`_instant_buy_core` has no `ends_at` guard (`:632-645`), so an expired-but-released auction carrying a `buy_now` above the standing bid is exposed to this too.

### N2 — CONFIRMED. The idempotency key is not durable, and it fails under the exact condition H1 targets

`Restocker_db.py:1031-1040`: `record_coin_ledger` is **best-effort, swallows every exception, and runs in a transaction separate from `adjust_balance`**. `add_coins` (`Restocker_main.py:2288-2289`) calls them in sequence. So a lock storm can commit the balance and silently lose the key, then fail `mark_sold` — and `coin_ledger_has` returns a clean `False` on the retry.

Executed (`ledger_write` fault ×1, `mark_sold` fault ×1): patched total **17,000,000** on an 8,500,000 sale — identical to the original. The window is much narrower than the original's (one extra payment, not a 60-second loop), but it is not zero and the triggering condition is the same contention.

### N3 — CONFIRMED (needs a concurrency window). A burned charge key hands out a free plot

`:657` skips the debit if `realestate:buy:<id>` exists for this buyer; `:686` writes `realestate:buy_refund:<id>` on the refund path. The two are never paired. After charge-then-refund, the charge key survives:

```
attempt1: refused after the charge -> charge rows: 1, refund rows: 1, buyer whole
(row re-armed 10 min later by _rearm_stale_claims)
attempt2: ok=True   buyer NET paid = 0   seller 8,075,000   house 425,000
DELTA total = +8,500,000  (MINT)
```

Today `_finalize_sale_core` can only refuse-after-charge if the status changes between two reads, which on the single bot loop needs the `run_on_bot_loop` direct-call fallback (`Restocker_main.py:18023-18026`, `_BOT_LOOP is None` during the boot window) or a stranded `'settling'` row. Not trivially reachable — but it is a latent mint sitting one refactor away, and the fix is one condition: skip the charge only if charged **and** not refunded.

### N4 — read-verified. The charge probe fails open, re-opening the double charge under lock

`:451-453`: `_ledger_has_charge` catches `Exception`, logs, `return False` → `deduct_coins` runs again. The docstring argues this deliberately (double charge is recoverable, free plot mints — and N3 shows the free-plot side is live, so the bias is defensible). The consequence is that H2's protection evaporates exactly when the DB is contended, which is exactly when the retry happens.

### N5 — the new refund logic is unreachable in the case that strands a buyer

`:681-687` refunds only when `_finalize_sale_core` *returns* `ok:False`. But the entry guard at `:634` (`listing["status"] != "active"`) returns first. Executed: B charged 8,500,000, settle fails, C then buys the plot, B clicks again → "That listing isn't active", **B never refunded**, 8,500,000 stranded. Pre-existing harm (the original strands B identically, and only *looks* conserved because it mints a matching amount), but the patch adds a refund path that cannot fire in the case that needs it.

### N6 — the anti-snipe cap silently disables anti-snipe on legitimately long auctions

`:615-621` vs `create_listing_core:744` (`dur = float(duration_days)`, **uncapped** — the satellite's `/sell duration_days` is a free integer). A 30-day auction now has full anti-snipe for 14 days and **none** for the remaining 16. An anti-snipe fix that opens a snipe window on any auction longer than `max_auction_days`. Either cap `duration_days` at creation, or clamp the hard deadline to `max(starts_at + max_days, original ends_at)`.

### N7 — the commission can be silently destroyed, in full

`:517-521`: the house credit sits inside `if not _already_paid`. Executed with `platform_credit` faulting once: patched ends `seller=8,075,000 house=0 status=sold` — the entire 425,000 commission is not collected and not held by anyone. Documented as a bounded tradeoff and it is far better than minting, but the magnitude is the *whole* commission for that sale, not "one commission" in any smaller sense, and the only signal is a `log.warning`.

### N8 — the settle response disagrees with the money

`:530-533` returns `net`/`commission` computed fresh even when `_already_paid` short-circuited the payout. Executed: `/close` returns `net=7,600,000` while the seller received 0 for that sale, and the manager sees `✅ Settled #N as sold.` Same dict feeds `_notify_network_land`. Under the ledger review's own standard this is "a display that disagrees with the money" — and it is the display a human would use to decide nothing is wrong.

### N9 — pre-existing, untouched, and now the largest remaining mint in the file

`close_listing_core:793-795` and the slash copy at `:1477-1479`: `add_coins(current_bidder, ..., reason=f"realestate:manager_refund:{listing_id}")` with no claim, no key, and the status update after it. Executed on both trees, identically: a locked `status='cancelled'` UPDATE then a manager retry → `manager_refund ledger rows: 2`, **bidder=10,000,000 for a 5,000,000 refund**. Not a regression — but it is the sibling of the path H1 hardened, it is reachable from the `/close` endpoint H5 just secured, and the same three-line key treatment would close it.

### N10 — `fresh = _db.get_land_listing(listing_id)` at `:680` is outside any `try`

Reached only after the buyer has been charged. A lock there propagates to `_record_network_land_buy`'s blanket handler → "Couldn't complete that purchase — try again shortly." with no refund. Narrow, but it is the audit's §5 shape reappearing in the new code.

### N11 — duplicate deal room and winner DM

The new `duplicate` branch at `:681-684` returns `ok:True`, so `_record_network_land_buy` (`Restocker_main.py:17949`) sets `sold_to_buyer` and `_notify_network_land` runs `_post_sale` a second time — second winner DM, second private transfer room. Cosmetic, but it will look to the seller like two sales.

### N12 — deliverable gap: the H5/H6 documentation is gone

`/home/claude/build/hotfix/PATCHES.md` contains **zero** occurrences of `H5`, `H6`, or `_land_manager_ok`. Its table of contents lists H1–H4 only, and it is byte-for-byte the land agent's section plus a 5-line ownership banner. The web agent's write was overwritten. The one patch that requires a coordinated two-server deploy is the one with no written record.

---

# (d) Deploy verdict

**Ship it — the patch is a large net improvement and I would not leave the original running another week.** It kills a mint that runs at the full sale price every 60 seconds on ordinary lock contention, kills the network double-charge, kills the NaN path, caps the auction, and closes a hole that let any partner-server administrator force-settle V Tech auctions and set the global commission. Every one of those is verified by execution, and nothing that worked was removed: 5/5 manager gates, 23/23 refusal strings, all caps, no function deleted, settle arithmetic bit-identical.

**Worst plausible outcome.** Not a mint — the unbounded loop is genuinely gone. It is **N1**: during a lock storm, a manager force-closes a live auction, the settle half-completes and the row is handed back to `'active'`; a higher bid lands; the manager closes again and gets `✅ Settled — sold`. The seller is paid for the *first* price, the outbid refund pays the first bidder out of coins already spent, and the winner's money is destroyed. On the numbers I ran: 5,000,000 minted and 8,000,000 destroyed on one 8,000,000 auction. The original does the same thing worse (+10,000,000), so this is not a regression — but it is the failure that will actually reach him, and the response text says everything is fine.

**How he would notice.** Three signals, in order of usefulness:

```sql
-- 1. Did §4 fire, before or after the patch? Run this first, today.
SELECT reason, COUNT(*) c FROM coin_ledger
 WHERE reason LIKE 'realestate:sale:%' GROUP BY reason HAVING c > 1;
-- 2. N1/N7: settled listings whose seller has no sale row (patched code only).
SELECT l.id, l.sold_price, l.seller_id FROM land_listings l
 WHERE l.status='sold' AND NOT EXISTS (
   SELECT 1 FROM coin_ledger cl
    WHERE cl.user_id = l.seller_id AND cl.reason = 'realestate:sale:'||l.id);
-- 3. Rows the recovery never reclaimed.
SELECT id, updated_at FROM land_listings WHERE status='settling';
```

And in the log: `[realestate] #N already paid (…) — marking sold without re-paying seller` is the N1/N7 tell, and `STUCK SETTLEMENT CLAIM on #N` is the release-failed tell. Both are `WARNING`/`ERROR` and both carry the listing id. Tell him to grep for the first string specifically — if it appears without a matching stale-claim message, a seller was shorted.

**Ordering: restart V Helper (Restocker) first, the satellite second.** Both orders are safe, but that one is strictly better. Restocker-first means `/auction_close` and `/auction_config <with args>` return `❌ requester_id is required — update the … satellite` for the length of the gap while the board, `/bid`, `/buy`, `/sell`, `/cancel` and `/auction_config` (view) all keep working. Satellite-first would work too — the old Restocker ignores the extra `requester_id` key — but it leaves the anonymous force-settle hole open for the length of the gap. The satellite's two-line change is `{"listing_id": …, "refund_bidder": …, "requester_id": str(interaction.user.id)}` at `app.py:687` and the same key in `auction_config_cmd`'s body at `app.py:717`.

**Three things to do before pressing restart:**

1. **Check `HOME_GUILD_ID` on the Wisp panel.** If it is set to anything other than `954487497411403806`, `_land_manager_ok` will refuse every manager except the two `MANAGER_DM_IDS` accounts, and `/auction_close` from the satellite goes dead for everyone. Unset is fine (the default matches `_config_home_ok`).
2. **Know the rollback SQL.** If he reverts to the original code, any row left in `status='settling'` becomes permanently invisible — the original has no re-arm and no query looks at that value. Recovery is `UPDATE land_listings SET status='active' WHERE status='settling';` and it must be run as part of any rollback, not after someone notices a seller was never paid.
3. **Run query 1 above.** It answers "has §4 already fired in production" and it is the only thing here that gets *harder* to answer after the patch ships.

**Two one-line-scale follow-ups I would push with it, not after** — both are inside the code the patch already touches:

- `:657` — skip the charge only if charged **and** not refunded (closes N3).
- `:548` — on the exception path, release only when the seller was **not** paid this listing; otherwise leave the row `'settling'`, log at ERROR, and have `_rearm_stale_claims` (`:417`) skip rows where `coin_ledger_has(seller, sale_key)`. That keeps a half-settled row out of circulation instead of handing it back to bidders, and it is what closes N1 — the worst plausible outcome above.

And restore the H5/H6 section to `PATCHES.md` (N12). He is deploying two servers in a required order on the strength of a document that no longer describes half of the change.