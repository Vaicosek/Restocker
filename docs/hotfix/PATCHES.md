<!-- SECTION OWNER: cogs/land_exchange.py (H1-H4). Written by the land_exchange hotfix task.
     A verbatim copy of this section lives at PATCHES_land_exchange_H1-H4.md in case this
     shared file is overwritten by a sibling task working on another file. If you are
     another task: APPEND your section below, do not overwrite this one. -->

# Land Exchange hotfix — `cogs/land_exchange.py`

**Branch:** hotfix against live production code. **Scope:** one file, four findings.
**Source of truth:** `LAND_EXCHANGE_AUDIT.md` §4 (H1), §5 (H2), §8 Sequence 3 (H3), §6 (H4).

| | |
|---|---|
| File changed | `/home/claude/build/hotfix/cogs/land_exchange.py` (copy of the staged original, edited) |
| Original, untouched | `/mnt/user-data/uploads/RestockerLocal/cogs/land_exchange.py` |
| Diff size | 12 hunks, +278 / −57 lines, **no file other than this one is modified** |
| New imports | `import math` (one line) |
| New config knob | `realestate:max_auction_days`, default `14.0` — H4 only |
| New listing status | `settling` — a transient claim state, H1 only |
| Money semantics changed | **none.** No rounding touched, no float→int conversion, `net + commission == int(round(price))` unchanged and re-verified by execution |

**Verification:** `python3 /home/claude/build/hotfix/_harness/test_hotfix.py`
→ **47 passed, 0 failed.** The same harness against the untouched staged original
(`--original`) → **28 passed, 17 failed**, and the 17 failures are exactly the four
findings. Details in "What I proved by execution" at the bottom.

---

## Table of contents

1. [H1 — the double-settle mint](#h1)
2. [H2 — instant-buy idempotency](#h2)
3. [H3 — NaN passes both money guards](#h3)
4. [H4 — uncapped anti-snipe](#h4)
5. [Guards inventoried and preserved](#guards)
6. [What I proved by execution vs. what rests on reading](#proof)
7. [Open questions for the owner](#owner)
8. [Deploying this from the Wisp panel](#deploy)

---

<a name="h1"></a>
## H1 — CRITICAL: the double-settle mint

### The finding

`_finalize_sale_core` paid the seller (`:375`) and the house (`:377`) **before** marking
the listing sold (`:379`), each in its own transaction. `auction_sweep_loop` (`:926`)
re-runs every 60 seconds over `status='active' AND ends_at <= now`. If that final UPDATE
throws — and `busy_timeout=5000` against a web server on its own OS thread means
`database is locked`, not a crash — the sweep settles the listing again a minute later,
and again. On an 8.5M listing that is **8.5M minted per minute**.

Measured on the staged original with the harness below (3 failing sweeps then one that
succeeds, one 8,500,000 listing):

```
seller credited  32,300,000   (4 x 8,075,000)
house credited    1,700,000   (4 x   425,000)
coins collected   8,500,000
COINS MINTED     25,500,000
realestate:sale:<id> ledger rows: 4
```

### The decision: claim-first **and** a ledger-key guard, not one or the other

The owner's rule is claim-first, and claim-first is what ships. But claim-first **alone is
not sufficient here**, and it is worth being precise about why, because the naive version
looks like it works:

- Claim-first gives **mutual exclusion**. It guarantees that only one caller is ever
  settling a given listing. That kills the *re-entrancy* half of the bug — the sweep can no
  longer walk in on a settle that is already underway.
- It does **not** give **idempotency**. The "act" here is not one write; it is a refund, a
  seller payment, a house credit, a status write, a config write and two loyalty writes,
  across seven independent transactions. A crash in the middle leaves some of them done.
  Mutual exclusion says nothing about what happens on the *next* attempt.

So the single-phase version of claim-first — flip `active` → `sold` in one UPDATE, then pay
— is genuinely unrecoverable. If the payment fails after that flip, the listing is sold, the
buyer's coins are gone, the seller has nothing, and no sweep will ever look at the row again
because it is no longer `active`. That is the failure the brief asked me to name, and it is
the reason I did not ship it. It converts a minting bug into a silent-theft bug. Quieter,
still wrong.

What ships is two-phase:

```
active  --[atomic UPDATE ... WHERE status='active']-->  settling  ----> sold
                                                            |
                                                            +--(any raise)--> active   (retry)
```

- **`settling` is not `active`**, so `get_expired_active_listings()` skips it and the sweep
  cannot re-enter. That is the claim.
- **Every payout inside the claim carries its own `coin_ledger` key**, so re-entry after a
  partial failure is *safe* rather than merely *prevented*. That is what makes releasing the
  claim on failure a legitimate move instead of an invitation to the original bug.
- **On any raise the claim is released back to `active`**, and the sweep retries it within
  60 seconds. The retry re-checks each key and pays only what did not already pay.

`coin_ledger_has` already existed in `Restocker_db.py:1042` and was never called from this
file. `record_coin_ledger` has always written `realestate:sale:<listing_id>` on every
seller payout. The key was already there; nothing read it. Now something does.

### The case where the status flips and the payment then fails — stated plainly

There are three sub-cases, and I want the owner to be able to check my work on each:

**(a) Claim succeeds, a payment raises, release succeeds.** The row goes back to `active`.
The sweep picks it up within 60s, re-claims, sees `realestate:sale:<id>` already on record
for whatever already paid, skips it, and finishes the rest. Net effect: settled once.
*Proven by execution* — H1a–H1h, and H1c shows the seller ends on exactly `net`, once.

**(b) Claim succeeds, a payment raises, and the release ALSO fails** (the DB is still
locked). The row stays `settling`. Nobody is double-paid — that was the whole point — but
nobody is paid either. This is the case the brief asked me to make recoverable, and it is
handled two ways:

  - `_rearm_stale_claims()` runs at the top of every sweep pass and flips any row that has
    been `settling` for more than `_STALE_CLAIM_MINUTES` (10) back to `active`.
    `_finalize_sale_core` is fully synchronous and sub-second, so a claim older than ten
    minutes is dead by definition. Re-entry is safe for the same reason as (a).
    *Proven by execution* — H1i/H1j.
  - If even that cannot run, `_release_listing_claim` logs at **ERROR** with the listing id
    and the literal recovery SQL:
    `UPDATE land_listings SET status='active' WHERE id=<n> AND status='settling';`

**(c) The seller payment lands but `record_coin_ledger` fails to write the key.**
`record_coin_ledger` is best-effort and swallows its own exceptions
(`Restocker_db.py:1031-1040`), so this is possible, and on a retry the seller would be paid
twice. It is a much narrower window than the original bug — it needs the *ledger insert
specifically* to fail while the *balance update* succeeded, in separate transactions, and
it only matters if a later step then also raises. I did not close it because closing it
means writing to `coin_ledger` inside the same transaction as the balance update, which is
a change to `Restocker_db.adjust_balance` and therefore outside this hotfix. **Flagged, not
fixed** — it belongs with the ledger-v2 migration, where the mint key is written to durable
storage *before* the money call (`ESTATES_DB` does exactly this).

### The commission tradeoff, stated because it is a real one

The house commission is credited inside the same `if not _already_paid:` branch as the
seller payment. `_credit_platform_balance` has no `coin_ledger` row of its own to key on
(it writes `platform_balance_log`, and reading that back would need a new `Restocker_db`
helper — out of scope for a one-file hotfix).

Consequence: if the process dies in the microseconds *between* `add_coins(seller)` and
`_credit_platform_balance`, the retry sees the sale key, skips both, and **the house never
collects that one commission**. It is logged at WARNING with the listing id.

That is a bounded, one-off, house-side **loss**, recoverable by hand from the log. The
alternative — ungating the commission so it retries independently — is an unbounded
house-side **mint** on every retry. Between a loss you can see and a mint you cannot, take
the loss. I have not hidden it: the log line names the listing.

### Before / after

**Before** (`cogs/land_exchange.py:353-397`, execution order = money first, marker last):

```python
    import Restocker_db as _db
    listing = _db.get_land_listing(listing_id)
    if not listing:
        return {"ok": False, "error": "Listing not found."}
    if listing["status"] != "active":
        return {"ok": False, "error": "That listing is no longer active."}
    if listing.get("current_bidder") and str(listing["current_bidder"]) != str(buyer_id):
        add_coins(int(listing["current_bidder"]), int(round(listing.get("current_bid") or 0)),
                  reason=f"realestate:preempted_refund:{listing_id}")
    ...
    add_coins(int(seller_id), net, reason=f"realestate:sale:{listing_id}")
    if commission > 0:
        core._credit_platform_balance(commission, ...)
    _db.update_land_listing(listing_id, status="sold", ...)      # <-- marker written LAST
    ...
    pts = _loyalty_award_points(_db, price)
    try:
        _db.add_loyalty_points(str(seller_id), pts)
        if str(buyer_id) != str(seller_id):
            _db.add_loyalty_points(str(buyer_id), pts)
    except Exception as e:
        log.warning("[realestate] loyalty award failed for #%s: %s", listing_id, e)
```

**After** (hotfix `:471-540`). Same guards, same arithmetic, same order of *effects* — the
claim and the keys are wrapped around them:

```python
    # H1 — CLAIM the row before a single coin moves. If we lose this race (a concurrent
    # settle, or the sweep already holding it) we are not the settler and must not pay.
    if not _claim_listing_for_settlement(_db, listing_id):
        return {"ok": False, "error": "That listing is no longer active."}
    try:
        if listing.get("current_bidder") and str(listing["current_bidder"]) != str(buyer_id):
            _preempt_key = f"realestate:preempted_refund:{listing_id}"
            if not _db.coin_ledger_has(str(listing["current_bidder"]), _preempt_key):
                add_coins(int(listing["current_bidder"]), int(round(listing.get("current_bid") or 0)),
                          reason=_preempt_key)
        ...
        commission = int(round(float(price) * eff_comm_pct / 100.0))   # UNCHANGED
        net = int(round(price)) - commission                            # UNCHANGED
        _sale_key = f"realestate:sale:{listing_id}"
        _already_paid = _db.coin_ledger_has(str(seller_id), _sale_key)
        if not _already_paid:
            add_coins(int(seller_id), net, reason=_sale_key)
            if commission > 0:
                core._credit_platform_balance(commission, ...)
        else:
            log.warning("[realestate] #%s already paid (%s on record) — marking sold without "
                        "re-paying seller %s or re-crediting commission", listing_id, _sale_key, seller_id)
        _db.update_land_listing(listing_id, status="sold", ...)
        ...
        pts = _loyalty_award_points(_db, price) if not _already_paid else 0.0
        if not _already_paid:
            try:
                _db.add_loyalty_points(str(seller_id), pts)
                ...
    except Exception:
        # H1 — hand the row back so the sweep retries it. Safe because of the keys above.
        _release_listing_claim(_db, listing_id)
        raise
```

New helpers, all at `:373-459`: `_claim_listing_for_settlement`, `_release_listing_claim`,
`_rearm_stale_claims`, `_ledger_has_charge` (the last one is H2's).

`_claim_listing_for_settlement` writes raw SQL rather than calling `update_land_listing`
because `update_land_listing` has no `WHERE` beyond the id — a conditional claim is not
expressible through it. It uses `_db.db()`, the same connection helper, so it inherits the
same pragmas and the same thread-local connection. No new plumbing.

**The loyalty re-award was also a mint vector and is now closed.** The audit noted that
loyalty tiers *cut the commission*, so a re-settle loop lowered the commission on each pass
and minted slightly more each time. Points are now gated on the same key as the payout.

### Sweep wiring (`:1150-1160`)

```python
             import Restocker_db as _db
+            # H1 — recovery half: hand back any settlement claim whose owner died before it
+            # could release. A claimed row is not 'active', so without this it would never
+            # be swept again and the seller would be silently unpaid.
+            _rearm_stale_claims(_db)
             for listing in _db.get_expired_active_listings():
```

### Is `settling` safe as a new status value?

Every consumer of `status` was checked. All of them fail safe:

| Consumer | Behaviour on `settling` |
|---|---|
| `get_expired_active_listings()` (`Restocker_db.py:3838`) | `status='active'` → skipped. **This is the fix.** |
| `get_active_land_listings()` (`:3813`) | `status='active'` → hidden from the board for the sub-second claim window |
| `network_land_listings()` → satellite board | same, via `get_active_land_listings` |
| `_place_bid_core:406` | `!= "active"` → "That listing isn't active." |
| `_instant_buy_core` | same refusal |
| `cancel_listing_core:551`, `close_listing_core:567` | same refusal |
| `/realestate cancel`, `/realestate close` | same refusal |
| `_listing_embed:179` colour map | `.get(status, 0x3498DB)` → falls back to the default blue |
| `_listing_view:695` | `!= "active"` → no Bid/Buy buttons |

No consumer treats an unknown status as active, and none raises on one.

### How a human verifies this worked in production

1. **Before deploying, find out whether §4 has already fired** (the audit says run this
   ahead of everything; it is a read-only query on a copy):
   ```sql
   SELECT reason, COUNT(*) n, SUM(delta) total
   FROM coin_ledger
   WHERE reason LIKE 'realestate:sale:%'
   GROUP BY reason HAVING n > 1
   ORDER BY total DESC;
   ```
   **Any row here is a listing that was already settled more than once, and `total` is the
   coins minted.** If this returns rows, the money is already in the economy and needs a
   separate, dry-run-first repair — this hotfix stops the bleeding, it does not undo it.

2. **After deploying**, the same query must never grow a new row. Re-run it weekly.

3. **A stuck claim is visible and self-healing.** These two should normally return nothing;
   if the first returns a row that persists for more than ~2 minutes, something is wrong:
   ```sql
   SELECT id, seller_id, current_bid, updated_at FROM land_listings WHERE status='settling';
   ```
   Log lines to grep for: `re-armed N stale settlement claim(s)` (WARNING, benign recovery)
   and `STUCK SETTLEMENT CLAIM` (ERROR, needs a look — it carries the exact SQL to fix it).

4. **The skip path is auditable.** `already paid (realestate:sale:N on record)` at WARNING
   means the guard fired and prevented a double-pay. Each such line is also the flag that
   the house may have missed that listing's commission — reconcile against
   `platform_balance_log` for the matching `realestate:commission:<id>` note.

---

<a name="h2"></a>
## H2 — instant-buy has no idempotency

### The finding

The compensating refund at `:468` only fired when `_finalize_sale_core` returned
`{"ok": False}`, which it only did **before any money moved** (its first two guards). Any
raise after that skipped it entirely: buyer debited, seller paid,
`_record_network_land_buy` catches it (`Restocker_main.py:17951`) and tells the buyer
*"Couldn't complete that purchase — try again shortly."* — and nothing stopped the retry
from buying the same plot again.

Measured on the staged original (2,000,000 buy-now, one transient lock on the sold-marker,
one retry — exactly the sequence the audit describes in §5):

```
realestate:buy:<id>  ledger rows: 2      <- buyer charged TWICE
realestate:sale:<id> ledger rows: 2      <- seller paid TWICE
buyer wallet: 5,000,000 -> 1,000,000     <- 4,000,000 gone for one 2,000,000 plot
```

### The fix

One idempotency key per `(buyer, listing)`, minted from the domain event and re-using the
reason string `deduct_coins` has always written:

**Before** (`:462-469`):
```python
    deduct_coins(int(buyer_id), int(round(price)), reason=f"realestate:buy:{listing_id}")
    res = _finalize_sale_core(listing_id, buyer_id, price)
    if res.get("ok"):
        res["message"] = f"Bought listing #{listing_id} for {_fmt(price)} coins."
    else:
        # settlement failed after collecting — refund so coins can't be swallowed
        add_coins(int(buyer_id), int(round(price)), reason=f"realestate:buy_refund:{listing_id}")
    return res
```

**After** (hotfix `:646-690`):
```python
    _buy_key = f"realestate:buy:{listing_id}"
    _refund_key = f"realestate:buy_refund:{listing_id}"
    if not _ledger_has_charge(_db, str(buyer_id), _buy_key):
        deduct_coins(int(buyer_id), int(round(price)), reason=_buy_key)
    else:
        log.warning("[realestate] #%s: buyer %s already charged (%s on record) — settling the "
                    "existing purchase instead of charging again", listing_id, buyer_id, _buy_key)
    try:
        res = _finalize_sale_core(listing_id, buyer_id, price)
    except Exception as e:
        # The money is collected and the settlement is mid-flight or released for retry.
        # Do NOT blind-refund here: the seller may already hold the coins, and the refund
        # would mint. Tell the truth, and let the retry be safe rather than cheap.
        log.warning("[realestate] instant-buy settle raised for #%s (buyer %s): %s", listing_id, buyer_id, e)
        return {"ok": False, "listing_id": listing_id, "price": float(price),
                "error": ("Your payment went through and this purchase is still completing. "
                          "You will NOT be charged twice — check back in a minute, and if it "
                          f"hasn't completed, tell a manager listing #{listing_id}.")}
    if res.get("ok"):
        res["message"] = f"Bought listing #{listing_id} for {_fmt(price)} coins."
        return res
    # Settlement refused. If this buyer already owns it, an earlier attempt of THIS purchase
    # succeeded and the refund below would hand back the coins for a plot they already have.
    fresh = _db.get_land_listing(listing_id) or {}
    if fresh.get("status") == "sold" and str(fresh.get("sold_to") or "") == str(buyer_id):
        return {"ok": True, "listing_id": listing_id, "price": float(price), "duplicate": True,
                "seller_id": fresh.get("seller_id"), "market_id": fresh.get("market_id"),
                "message": f"Listing #{listing_id} is already yours — that purchase went through."}
    # settlement failed after collecting — refund so coins can't be swallowed (once).
    if not _db.coin_ledger_has(str(buyer_id), _refund_key):
        add_coins(int(buyer_id), int(round(price)), reason=_refund_key)
    return res
```

Four distinct behaviours, each traceable:

1. **Retry after a mid-settle raise** → the charge key is on record, the buyer is not
   charged again, and the settle is re-attempted (idempotently, per H1). *H2b–H2f.*
2. **A raise no longer skips the compensation silently.** It is caught, logged, and turned
   into a truthful message. The old copy invited a retry that cost 8.5M; the new copy says
   the retry is free, which is now true.
3. **A repeat click after a *successful* purchase does not refund.** This one is subtle and
   would have been a fresh mint if I had only added the charge key: on a retry,
   `_finalize_sale_core` correctly refuses ("no longer active"), which would have fallen
   into the refund branch and handed the buyer their coins back **for a plot they already
   own**. The `sold_to == buyer_id` check catches it and reports success instead. *H2g.*
4. **A genuine refusal after collection still refunds — exactly once**, keyed. *H2h–H2j.*

### Why the charge probe fails OPEN and the payout probes fail CLOSED

This is the one asymmetry in the patch and it is deliberate. `Restocker_db.coin_ledger_has`
fails **closed** — it returns `True` when it cannot read (`Restocker_db.py:1042-1055`,
*"Fails CLOSED (returns True) on error — if we can't verify, we must not pay again"*). That
is the correct bias for a **payout**.

It is exactly the wrong bias for a **debit**: a false "already charged" would skip the
charge and hand out a plot for free, which mints. So the charge side gets its own probe,
`_ledger_has_charge` (`:440-459`), which fails **open** — if we cannot prove the buyer was
already debited, we debit.

The bad case then becomes a double *charge* (visible in `coin_ledger` as two rows under the
same reason, refundable by hand) rather than a free sale (invisible, and it creates coins).
In a closed economy that is the right way round. It is commented as such at the definition.

### How a human verifies this worked in production

1. **A buyer must never hold two charges for one listing:**
   ```sql
   SELECT user_id, reason, COUNT(*) n FROM coin_ledger
   WHERE reason LIKE 'realestate:buy:%' GROUP BY user_id, reason HAVING n > 1;
   ```
   Empty before *and* after. If it grows, `_ledger_has_charge` failed open under sustained
   lock contention — the affected buyer is named in the row and is owed a manual refund.

2. **A refund must never coexist with a completed sale to the same buyer:**
   ```sql
   SELECT l.id, l.sold_to, l.sold_price FROM land_listings l
   JOIN coin_ledger c ON c.user_id = l.sold_to
                     AND c.reason = 'realestate:buy_refund:' || l.id
   WHERE l.status = 'sold';
   ```
   Any row here is a buyer who got the plot *and* their money back. Must be empty.

3. **Live smoke test:** on a throwaway listing, click Buy twice fast from a partner server.
   Expected: one charge, one sale, the second click reports *"already yours"*. Check the
   buyer's `/balance` moved by exactly the price, once.

4. Grep for `already charged (realestate:buy:` — each line is the guard doing its job.

---

<a name="h3"></a>
## H3 — NaN passes both money guards

### The finding

`amt < min_bid` and `bal < amt` are each `False` for NaN — every comparison against NaN is
False, so **both money guards fail open**. `json.loads` accepts a bare `NaN` token
(verified in the harness, H3a), so it survives the satellite hop from a partner server.
Only `int(float('nan'))` raising at `:425` stopped it — one line past two guards that had
already let it through. Measured on the staged original:

```
_place_bid_core(lid, bidder, float('nan'))
  -> ValueError: cannot convert float NaN to integer     (an exception, not a refusal)
_finalize_sale_core(lid, buyer, float('nan'))
  -> ValueError: cannot convert float NaN to integer
create_listing_core(seller, "Plot", float('nan'))
  -> IntegrityError: NOT NULL constraint failed: land_listings.reserve
```

### The fix

One validator, `_coin_amount` (`:133-150`), applied at the entry of every money path:

```python
def _coin_amount(v):
    """H3 — a FINITE, POSITIVE coin amount, or None when the value cannot be money."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f
```

Applied at:

| Money path | Line (hotfix) | Refusal returned |
|---|---|---|
| `_place_bid_core` — the bid floor | `:571-579` | *"This listing's price data is invalid — ask a manager to check it."* |
| `_place_bid_core` — the user amount | `:580-587` | *"Bid must be a positive number of coins."* |
| `_instant_buy_core` — `buy_now` | `:636` | *"No instant-buy price on this listing — place a bid instead."* (existing message reused) |
| `_finalize_sale_core` — settlement price | `:471-474` | *"That sale price isn't a valid coin amount."* |
| `create_listing_core` — `starting_price` | `:706-708` | *"Starting price must be > 0."* (existing message preserved) |
| `create_listing_core` — `buy_now` | `:716-717` | *"Buy-Now price must be > 0."* (new) |

All six are **normal refusal dicts**, not raises — which is what the brief asked for, and
which matters because a raise out of `_place_bid_core` becomes
`"Couldn't place that bid — try again shortly."` at `Restocker_main.py:17941`, i.e. the
user is told to retry an input that can never work.

**Bid amount, before / after** (`:416-419` → `:571-590`). Note the falsy-`amount`
semantics are preserved *exactly*: `amount=0`/`None`/`""` still means "bid the minimum",
because that is the existing behaviour of `float(amount) if amount else min_bid` and
changing it is not a finding:

```python
-    min_bid = _min_next_bid(listing)
-    amt = float(amount) if amount else min_bid
+    try:
+        min_bid = _min_next_bid(listing)
+    except Exception:
+        min_bid = None
+    if min_bid is None or _coin_amount(min_bid) is None:
+        return {"ok": False, "error": "This listing's price data is invalid — ask a manager to check it."}
+    if amount:
+        amt = _coin_amount(amount)
+        if amt is None:
+            return {"ok": False, "error": "Bid must be a positive number of coins."}
+    else:
+        amt = min_bid
     if amt < min_bid:
         return {"ok": False, "error": f"Minimum bid is {_fmt(min_bid)} coins."}
```

### Two things I measured that change the shape of this finding

Both were discovered by the harness and neither is in the audit:

**1. SQLite silently coerces a NaN REAL to NULL on write. It stores `inf` faithfully.**

```
INSERT INTO t(x REAL) VALUES (nan) -> stored as NULL, typeof='null'
INSERT INTO t(x REAL) VALUES (inf) -> stored as inf,  typeof='real'
```

This is *good* news for the audit's worst case. §8 Sequence 3 warns that a refactor could
turn this into "a bid of NaN written into `current_bid`, after which `_min_next_bid` returns
NaN and the listing accepts any amount from anyone." **That specific escalation is not
reachable through SQLite** — a NaN `current_bid` becomes NULL, which reads as "no bids yet".
The persistent-poisoning risk is `inf`, not NaN.

**2. `_min_next_bid` *raises* on an `inf` current_bid, before any guard can see it.**
`round(inf + inf)` → `OverflowError: cannot convert float infinity to integer`. So a guard
placed after the call is dead code for the case it was written for. That is why the call is
wrapped (`:571-574`) rather than just its result checked. I would not have found this by
reading; the harness found it (H3e failed on my first patch).

`inf` is not reachable through the code today — `_place_bid_core` now rejects it at entry,
and before the fix `int(round(inf))` raised before any write — so this is defence for a
hand-edited or externally-written row. It costs four lines and it turns a crash into a
refusal. *Proven by execution: H3e0/H3e/H3f.*

### What I deliberately did not touch

`BidModal.on_submit:629` and the `/realestate bid` slash parameter also accept a raw float.
I left both alone: the guard belongs at the **money path**, and all three surfaces (modal,
slash, network) funnel into `_place_bid_core`, which now refuses cleanly and whose error
string is what all three display. Adding the same check at each caller would be three more
places to keep in sync for no additional coverage. *Verified by execution* — the harness
drives the core directly with a `json.loads`-produced NaN, which is exactly what the
satellite delivers.

### How a human verifies this worked in production

1. **Live smoke test, no DB access needed.** On the satellite bot in a partner server, open
   the Bid modal on any test listing and type `nan` (also try `inf`, `-5`, `0.0`). Expected:
   *"❌ Bid must be a positive number of coins."* — an ordinary red refusal.
   Before the fix, `nan` produced *"Couldn't place that bid — try again shortly."*, which is
   the tell: that string means an exception was swallowed upstream.
2. **The logs go quiet.** `grep 'land bid failed' + 'cannot convert float NaN'` should
   return nothing after deploy.
3. **No non-finite values are reachable in storage:**
   ```sql
   SELECT id, reserve, buy_now, current_bid, sold_price FROM land_listings
   WHERE reserve != reserve OR buy_now != buy_now OR current_bid != current_bid
      OR reserve  IN (9e999, -9e999) OR buy_now IN (9e999, -9e999)
      OR current_bid IN (9e999, -9e999);
   ```
   (`x != x` is the SQL NaN test; `9e999` is how SQLite spells `inf` in a literal.)
   Should be empty. If it is not, those rows predate the fix and need a manager.

---

<a name="h4"></a>
## H4 — anti-snipe is uncapped

### The finding

`ends_at` reset to `now + N` with **no cap and no maximum auction length**, so two colluding
accounts extend forever at zero net cost: A bids, B outbids (A refunded in full at `:427`),
A outbids (B refunded), forever. Only the current top bidder's coins are held, so the pair's
combined outlay is one bid, not N. A genuine bidder can never win, because they can never be
last.

Measured on the staged original: an auction whose `starts_at` is **20 days** in the past
still accepts an extension and pushes `ends_at` another 240 seconds out. There is nothing
that ever stops it.

### The fix — a hard deadline at `starts_at + max_auction_days`

**Before** (`:430-436`):
```python
    if listing.get("ends_at"):
        remaining_min = (_epoch(listing["ends_at"]) - datetime.now(timezone.utc).timestamp()) / 60.0
        anti_snipe = float(listing.get("anti_snipe_minutes") or DEF["anti_snipe_minutes"])
        if remaining_min < anti_snipe:
            updates["ends_at"] = _sql_now_plus(minutes=anti_snipe)
            anti_snipe_extended = True
```

**After** (hotfix `:601-624`):
```python
    if listing.get("ends_at"):
        now_ts = int(datetime.now(timezone.utc).timestamp())
        end_ts = _epoch(listing["ends_at"])
        anti_snipe = float(listing.get("anti_snipe_minutes") or DEF["anti_snipe_minutes"])
        if (end_ts - now_ts) < anti_snipe * 60.0:
            want_ts = now_ts + int(round(anti_snipe * 60.0))
            if listing.get("starts_at"):
                max_days = _gd(_db, "max_auction_days", DEF["max_auction_days"])
                max_days = max_days if (math.isfinite(max_days) and max_days > 0) else DEF["max_auction_days"]
                hard_ts = _epoch(listing["starts_at"]) + int(max_days * 86400)
                want_ts = min(want_ts, hard_ts)
            # Never SHORTEN an auction — once the hard deadline is reached the extension
            # simply stops happening and the listing runs out on time.
            if want_ts > end_ts:
                updates["ends_at"] = _sql_ts(datetime.fromtimestamp(want_ts, timezone.utc))
                anti_snipe_extended = True
```

Properties, in the order they matter:

- **Bounded.** `ends_at` can never exceed `starts_at + max_auction_days`. The ping-pong
  attack now terminates. *Proven: H4b over 30 collusive extensions, H4e decisively.*
- **The bid is still accepted.** Hitting the cap does not reject the bid — it just does not
  buy the bidder more time. Nobody is locked out of an auction they are legitimately
  winning. *H4d.*
- **Never shortens.** The `want_ts > end_ts` guard matters: past the cap, `min()` produces a
  timestamp in the past, and without the guard that would *pull the auction's end
  forward* — a fresh way to snipe. Reading alone would have shipped that; the guard is why
  H4e asserts `after_end == before_end`, not merely `<= cap`.
- **Integer seconds throughout** (rule 5). The only float is `anti_snipe_minutes`, which is
  pre-existing config, and it is rounded to an integer number of seconds before use.
- **Honest anti-snipe still works.** A last-minute bid on a normal auction still extends it
  by the full window. *H4c: +240s.* The competitive feature is untouched.
- **Missing `starts_at` degrades to the old behaviour** rather than refusing. `starts_at` is
  `NOT NULL DEFAULT (datetime('now'))` so it is always present; the check is belt-and-braces.

The knob is `realestate:max_auction_days`, default **14.0** (2× the 7-day default auction).
Because `DEF` is iterated by `get_exchange_config()` and `set_exchange_config()`, it is
readable and settable through the satellite's `/config` with no further change. It is also
added to the `/realestate config` slash command (`:1535`, `:1541`, `:1548`) so the home-bot
manager surface can set it — **validated against the real discord.py**, which checks that
`@app_commands.describe` names a parameter that actually exists.

### The shill exposure — NOT fixed, and I am not pretending otherwise

`:412` (unchanged, hotfix `:564`) blocks only the seller's **own** id:

```python
    if str(bidder_id) == str(listing["seller_id"]):
        return {"ok": False, "error": "You can't bid on your own listing."}
```

A seller with a second Discord account can still ratchet their own auction to any price. If
the shill wins, `_finalize_sale_core` pays the seller `price − commission` **out of the
shill's own wallet** — a round trip costing the seller exactly `commission_pct` of a number
they chose.

**This is not solvable in this file.** Alt detection is an identity problem, not an escrow
problem: it needs account age, alt-linking, IP or Mojang-UUID correlation, or a manual
review queue — none of which exist here and none of which belong in a hotfix. Capping the
extension does not touch it: a shill does not need to extend an auction, only to bid on it.

**It is listed under [Open questions](#owner) as a policy decision for the owner, because
that is what it is.** The second-order payoff the audit identified — the sale price is
written to `valuate:land_claim:<market_id>` (`:384`), which the module docstring claims the
65% land-backing rule reads — makes this a *valuation* exposure as well as a market-fairness
one, and that raises the stakes on whether anything actually reads that key.

### How a human verifies this worked in production

1. **The cap is visible where the knobs are:** run `/config` on the satellite (or
   `/realestate config` on the home bot). `max_auction_days` should read `14.0`.
2. **No live auction can exceed it:**
   ```sql
   SELECT id, seller_id, starts_at, ends_at,
          ROUND((julianday(ends_at) - julianday(starts_at)), 2) AS days_long
   FROM land_listings
   WHERE status = 'active' AND ends_at IS NOT NULL
     AND julianday(ends_at) - julianday(starts_at) >
         (SELECT COALESCE(CAST(value AS REAL), 14.0) FROM bot_config
          WHERE key = 'realestate:max_auction_days');
   ```
   Rows that predate the deploy may appear once and will not grow; after the deploy this
   must never gain a new row.
3. **Anti-snipe still fires.** Bid in the last minute of a test auction — the channel note
   must still say `⏱️ anti-snipe extended the end time`, and `ends_at` must move.
4. **Spot the attack if it is happening now:** an auction with an unusually long bid history
   between the same two accounts —
   ```sql
   SELECT listing_id, COUNT(*) bids, COUNT(DISTINCT bidder_id) bidders
   FROM land_bids GROUP BY listing_id
   HAVING bidders <= 2 AND bids >= 10;
   ```

---

<a name="guards"></a>
## Guards inventoried and preserved (rule 4)

Every permission check, cap and confirmation gate in the four functions I touched, and its
status. **Zero removed, zero weakened.** Verified by string-count diff, not by eye:

| Guard | Where | Status |
|---|---|---|
| `is_manager(interaction)` × 5 (`close`, `notify_role`, `notifypanel`, `config`, `cancel`) | slash layer | **verbatim** — count identical, 5 → 5 |
| Cancel: seller-or-manager only | `cancel_listing_core:553`, `/cancel:1224` | verbatim (both copies) |
| Cancel refused when a bid is held | `:555`, `:1227` | verbatim |
| Listing must be `active` (6 sites) | bid, buy, finalize, cancel, close ×2 | verbatim, count 6 → 6 |
| Auction must not have ended | `_place_bid_core:410` | verbatim |
| Auction mode required for bidding | `:408` | verbatim |
| No bidding on your own listing | `:412` | verbatim (see H4 shill note) |
| No outbidding yourself | `:414` | verbatim |
| Minimum-bid floor `_min_next_bid` | `:416`, `:418` | verbatim — H3 adds a *pre*-check, never bypasses it |
| Balance check before debit (bid) | `:420` | verbatim |
| Balance check before debit (buy) | `:459` | verbatim |
| Buy-Now blocked once bidding passes it | `:455` | verbatim |
| No buying your own listing | `:457` | verbatim |
| Buy-Now must exceed starting price | `create_listing_core:494` | verbatim |
| Starting price must be > 0 | `:486` | preserved; NaN now also caught, same message |
| Company must exist (`_get_market`) | `:497` | verbatim |
| Loyalty min-commission floor | `_finalize_sale_core:371` | verbatim |
| Commission snapshotted at listing time | `:364` | verbatim (the audit's one genuine mitigation against a partner admin re-pricing commission) |
| Photo URL `http(s)` allow-list | `_photos_of:169`, `set_listing_photos:541` | untouched |
| Restart-safe DynamicItem templates | `BidButton`/`BuyButton`/`NotifyButton` | untouched, compiled and match-tested against real discord.py |

Automated check (in the transcript, reproducible): every pre-existing refusal string still
present at the same count or higher; one string added (`"Buy-Now price must be > 0."`).

### Findings from the audit I did **not** touch, on purpose

Named here so the owner can see the boundary of this hotfix rather than infer it:

- §2a **the `adjust_balance` clamp** — `deduct_coins` discards `applied`, so a deduction
  that removed less than `amt` still reports success. Needs a `Restocker_main.py` change.
- §3 **outbid refunds are not keyed per event.** `realestate:outbid_refund:<listing_id>`
  omits the bidder and the bid sequence, so it cannot distinguish P outbid at 10k from P
  outbid again at 20k. Making it a real key needs the `land_bids.id` that `add_land_bid`
  returns and `:428` discards — that is the escrow migration, not a hotfix.
- §5 **`is_manager` supplied by the client** on `/api/network/land/cancel`, and
  `/close` and `/config` carrying no requester identity at all. **This is arguably more
  urgent than H4** — it lets a partner-server admin force-settle a live auction — but it
  lives in `Restocker_web.py`, not this file.
- §5 **the listing fee is burned**, debited from the seller and credited to nobody. Dormant
  at the `0.0` default.
- §6 `anti_snipe_minutes` is an `INTEGER` column, so a manager setting `0.5` stores `0` and
  silently disables anti-snipe while `get_exchange_config()` still reports `0.5`.
- §6 `_min_next_bid:154` reads `DEF["min_increment_floor"]` hard-coded, never `_gd()`, so
  the configured value does nothing.
- §7 `close_listing_core` / `/realestate close` and `cancel_listing_core` / `/realestate
  cancel` are forked copies that already differ (`:1249` lacks the `or 0` and will
  `TypeError` on a NULL bid).
- §8 **float money everywhere.** Untouched by explicit instruction — `net + commission ==
  int(round(price))` is correct today and is re-verified by execution here (H1f, Rk).
- `close_listing_core`'s `refund_bidder` path is act-then-mark like the old settle path.
  Lower stakes (a manager-initiated refund, not a 60-second loop) and not a named finding,
  so it is left alone — but it is the same shape and belongs in the next round.

---

<a name="proof"></a>
## What I proved by execution vs. what rests on reading

### Harness

| Path | What it is |
|---|---|
| `/home/claude/build/hotfix/_harness/stubs.py` | Fake `Restocker_db` / `Restocker_main` / `discord`. The DDL for `land_listings`, `land_bids`, `balances` and `coin_ledger` is copied from `Restocker_db.py`; `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON` match production (rule 7). `adjust_balance` reproduces the clamp-at-zero and the `applied` return; `coin_ledger_has` reproduces the fail-closed behaviour verbatim. Includes a fault injector so a chosen write can raise `sqlite3.OperationalError: database is locked`. |
| `/home/claude/build/hotfix/_harness/test_hotfix.py` | 47 assertions. Imports the **real patched module** — not a transcription — and drives the **real `auction_sweep_loop` body**. `--original` points the same suite at the untouched staged file. |
| `/home/claude/build/hotfix/_harness/test_real_discord.py` | Loads the cog against the **real discord.py 2.7.1** with nothing about Discord stubbed. |
| `/tmp/land_hotfix_test/restocker.db` | The temp DB. Synthetic. Not production data. |

```
$ python3 _harness/test_hotfix.py                47 passed,  0 failed
$ python3 _harness/test_hotfix.py --original     28 passed, 17 failed
$ python3 _harness/test_real_discord.py /home/claude/build/hotfix        all PASS
```

The 17 control failures are exactly the four findings — H1 (5), H2 (5), H3 (6), H4 (1) —
which is the point of running it: **the tests fail on the original and pass on the fix.** A
test suite that passes on both proves nothing.

### PROVEN BY EXECUTION

- **H1, the headline.** With the sold-marker UPDATE raising `database is locked` on three
  consecutive sweep passes and succeeding on the fourth: original → seller **32,300,000**,
  house **1,700,000**, **4** `realestate:sale` ledger rows, **25,500,000 coins minted**
  against an 8,500,000 collection. Hotfix → seller **8,075,000**, house **425,000**, **1**
  ledger row, money supply up by **exactly 8,500,000**, listing `sold`.
- **H1 is inert once settled.** 20 further sweep passes after the sale move nothing.
- **H1 recovery.** A listing stranded in `settling` with a 30-minute-old `updated_at` is
  re-armed by the next sweep and settles, paying the seller exactly once.
- **H2.** Original: 2 charge rows, 2 sale rows, buyer down 4,000,000 for one 2,000,000 plot.
  Hotfix: 1 charge row, 1 sale row, buyer down exactly 2,000,000, no phantom refund, and a
  third click does not hand the coins back.
- **H2 refund still works and is keyed.** A genuine post-collection refusal refunds in full,
  once, across repeated retries.
- **H3.** `json.loads('{"amount": NaN}')` really does yield NaN. NaN, `+inf`, `-inf`,
  negative and a `json`-sourced NaN all produce clean refusal dicts with no coins moved and
  no exception, at `_place_bid_core`, `_instant_buy_core`, `_finalize_sale_core` and
  `create_listing_core`. Original raises `ValueError`/`OverflowError`/`IntegrityError` on
  four of those.
- **H3 storage facts.** SQLite coerces a NaN REAL to NULL and stores `inf` faithfully —
  measured directly, and it changes the shape of the audit's §8 Sequence 3 escalation.
- **H4.** An auction 20 days past a 14-day cap: original extends it another 240s; hotfix
  moves `ends_at` by **0s** while still accepting the bid. 30 collusive ping-pong bids never
  push past the cap. An honest last-minute bid on a normal auction still extends by 240s.
- **Coin conservation on the ordinary path.** Bid → outbid (refunded in full) → sweep settle:
  money supply delta **0**, seller net + house commission **== price**. The audit's
  "settle arithmetic conserves coins" property still holds after the patch (rule 2).
- **The cog loads under real discord.py 2.7.1**, instantiates, and builds the identical set
  of 10 `/realestate` subcommands. `max_auction_days` is exposed with a real description —
  discord.py itself validates that `@app_commands.describe` names an existing parameter.
  The three restart-safe `DynamicItem` templates still compile and match
  (`rex:bid:7`, `rex:buy:7`, `rex:notify:land`). `auction_sweep_loop` is still a real
  1-minute `tasks.loop`.

### RESTS ON READING — not exercised

Stated so nobody mistakes a green suite for full coverage:

1. **The production trigger for H1.** I *injected* `sqlite3.OperationalError: database is
   locked`. I did not reproduce real lock contention between the bot loop and the web
   thread. That the exception occurs in normal operation is the audit's finding, read from
   `Restocker_db.py:25` (`busy_timeout=5000`) plus `Restocker_web.py:4580` (its own OS
   thread on the same SQLite file). **What is proven is the handling, not the trigger.**
2. **`add_coins` / `deduct_coins`'s whole-table YAML fallback** (`Restocker_main.py:2326`,
   `:2333`). My stubs implement only the SQLite path. If the DB path fails, the real code
   writes `balances.yaml` directly and `record_coin_ledger` is best-effort there too — so a
   payout could land without its idempotency key. Read-only claim; see LEDGER_API_v2 §9.1,
   which calls this the most dangerous interaction in the system.
3. **`_credit_platform_balance`'s real behaviour** (DB store + YAML mirror,
   `Restocker_main.py:9637`) is stubbed as a counter. That it swallows its own exceptions —
   and therefore cannot be the step that raises — is read from source, not executed.
4. **Every Discord surface**: embeds, buttons, modals, winner DMs, deal rooms, notify roles,
   `_post_sale`/`_refresh_message`. Stubbed to no-ops. The patch does not touch them, but I
   have not run them.
5. **The satellite** (`RestockerLightWeight/app.py`) and the `Restocker_web.py` routes are
   not exercised end-to-end. The NaN-over-JSON hop is proven only at the `json.loads` step;
   the HTTP relay itself is read.
6. **`run_on_bot_loop`** marshalling and its 20-second timeout, including the audit's §5
   observation that the sync core cannot be cancelled once started and runs to completion
   while the web handler has already returned 500. Not exercised.
7. **Concurrency.** The harness is single-threaded. The claim is a single atomic
   `UPDATE ... WHERE status='active'`, which SQLite serialises, and `rowcount == 1` is the
   standard proof of winning it — but I have not run two threads at it. Given that
   `_finalize_sale_core` is fully synchronous on the bot loop, the realistic contention is
   sweep-vs-web-thread, which the H1 test models sequentially rather than in parallel.
8. **Real production data.** No `restocker.db` is staged. Every SQL query in the
   verification sections above is written against the real schema and validated against the
   synthetic DB, but has never been run on production. Run them on a **copy**.

---

<a name="owner"></a>
## Open questions for the owner

Policy decisions, not code. I have not guessed at any of them.

1. **The shill / alt problem (H4).** A seller can still ratchet their own auction through a
   second account for the cost of the commission. Not solvable in this file — it needs an
   identity signal. Options, cheapest first: (a) accept it and monitor via the two-bidder
   query above; (b) require a minimum account age or a linked Minecraft UUID to bid;
   (c) cap the number of bids per account per listing; (d) manual review over some coin
   threshold. **This needs your call before anyone writes code for it.**
2. **The valuation payoff of (1).** `_finalize_sale_core:384` writes the sale price to
   `valuate:land_claim:<market_id>`, which the module docstring says the 65% land-backing
   rule reads. If that is true, a seller can set a company's land-backed book value to an
   arbitrary number for 5% of that number, and (1) becomes a stock-exchange exposure rather
   than a land-market annoyance. **But a grep across the whole staged tree finds that key
   written and read nowhere**, and `gather_and_value` is not in the staged files. Either the
   reader is in the unstaged `cogs/valuation.py`, or the docstring's headline claim is false
   and the write is dead. **One grep in the real tree answers it. It changes the priority of
   (1) by a lot.**
3. **`max_auction_days = 14.0` — is that the number you want?** It is 2× the 7-day default.
   Too low and a legitimately hot auction gets cut off mid-bidding-war; too high and the
   collusion window stays open. It is a live config key, so it can be changed without a
   deploy.
4. **`_STALE_CLAIM_MINUTES = 10`** is the delay before a stranded settlement is retried. It
   only fires if a release failed, which should be never. Lower is a faster recovery; higher
   is more margin against a pathologically slow settle. 10 is conservative given the core is
   sub-second.
5. **The double-charge-vs-free-plot tradeoff in H2.** I chose: if we cannot verify whether a
   buyer was charged, charge them (and refund by hand if wrong) rather than risk giving the
   plot away. That is a business call as much as a technical one. Say if you want it the
   other way.
6. **Has H1 already fired?** Run the `HAVING n > 1` query in H1's verification section
   **today**, before deploying anything. It is read-only, it takes a second, and it tells
   you whether there are already minted coins in the economy that this patch will not undo.
   The audit puts the same query ahead of all other work and I agree with it.

---

<a name="deploy"></a>
## Deploying this from the Wisp panel (no shell)

1. **Back up** the current `cogs/land_exchange.py` — download it before overwriting. There
   is no `git checkout` on a Wisp panel.
2. **Run the H1 "has it already fired" query on a copy of `restocker.db`** (item 6 above)
   before you change anything, so you can tell pre-existing damage from anything new.
3. Upload `/home/claude/build/hotfix/cogs/land_exchange.py` over
   `cogs/land_exchange.py`. **One file. Nothing else in this hotfix changes any other
   file** — `Restocker_db.py`, `Restocker_main.py`, `Restocker_web.py` and the satellite are
   untouched, so there is no ordering requirement and no partial-deploy state.
4. Restart the bot. Expected on boot: no new log lines from this cog (`_rearm_stale_claims`
   is silent when there is nothing to re-arm).
5. **Smoke test, in this order** — each one exercises a different fix:
   - `/realestate config` (or the satellite `/config`) → `max_auction_days` reads `14.0`. **H4**
   - Bid `nan` in the Bid modal → *"Bid must be a positive number of coins."* **H3**
   - Place a normal bid, then outbid it from another account → outbid refunded in full, and
     the channel note still shows the refund. **regression**
   - Instant-buy a cheap test listing, then click Buy again → second click says *"already
     yours"*, and the buyer's balance moved once. **H2**
   - Let a short test auction expire → it settles once, seller credited `net`, and
     `SELECT reason, COUNT(*) ... HAVING n > 1` still returns nothing. **H1**
6. **Rollback** is re-uploading the file you saved in step 1 and restarting. The patch adds
   no schema, no migration and no new table. The one durable artefact is the string
   `'settling'` in `land_listings.status`, which can only exist while a settlement is
   mid-flight; if you roll back while one is stuck, run:
   ```sql
   UPDATE land_listings SET status='active' WHERE status='settling';
   ```
   The old code will then settle it — and, because the old code has no ledger guard, it
   will pay the seller again if the seller was already paid. **Check
   `realestate:sale:<id>` for that listing before re-arming it under the old code.**
