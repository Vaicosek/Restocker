# Handoff — Real Estate / Land Auctions (Restocker · V Tech)

**Purpose of this doc:** a self-contained brief so a fresh chat can pick up the *land / real-estate / auctions* workstream without the prior context. Everything about the Minecraft economy bot ("Restocker" / "V Tech") and its website ("Abexilas Economy Hub", dashboard.vaicosmarket.com) that matters for this feature is captured below.

---

## 1. The goal (owner's words)

> "Now I hit something important called real estate business. Here are two Discord companies that sell land and do auctions, I want to move to their space providing a better product."

Two competitor Discord companies (IDs the owner provided — **not yet resolved**, no Discord API access in-session):
- `1387557122191134870`
- `1434741352519696427`

**Owner's edge to exploit:** the bot already has an **AI land/company valuation engine**. Competitors sell dirt at vibes prices; V Tech can attach a **defensible, data-backed valuation** to every plot and plug land straight into its **stock market** (land can back a listed company). That integration is the moat.

**OPEN INPUT NEEDED FROM OWNER (blocking a truly "better" build):** how do the two competitors actually operate — live auctions vs fixed-price sales vs broker/flip? What do they charge? What's the most annoying thing about using them? Ask for a screenshot. Do not assume.

---

## 2. Proposed product — "Restocker Land Exchange"

A real-estate arm of the bot, five pillars:

1. **List a plot.** Owner lists land with chunk count, coords, build description. Bot auto-attaches an **AI-valued reserve price** using the existing valuation engine (chunks × per-chunk rate + build/farm markup; comps like AllMart sold at 5M).
2. **Two sale modes.** Fixed-price *buy-now*, or a **timed auction** with public bid history and an **anti-snipe** timer (last-second bid extends the clock).
3. **Escrow + house cut.** Buyer's coins held by the bot, released to seller on close; the house takes a **listing fee + commission**. The commission *is* the business/revenue.
4. **Ownership transfer.** On sale the bot rebinds the land in `land_map` (existing `lands.py` plumbing) so the plot's treasury/teleport-fees follow the new owner automatically.
5. **Company tie-in.** A won plot can immediately back a listed company at the **65% rule** — land flows into the stock market.

**Decision still open:** build the auction cog directly (owner leaned toward "design the auction product" first), or write a go-to-market strategy doc first. Owner had NOT finalized when this handoff was written. Confirm before building.

---

## 3. Existing land infrastructure (what's already built)

**`cogs/lands.py`** — consumes the CSN mod's LANDS FEED webhook posts. It does NOT sell/auction land; it tracks land as *treasury/backing*:
- Ingests two line types from the mod: `LANDS-BAL|<land>|<balance>|<ts>` and `LANDS-ENTRY|<land>|#<n>|<date>|<text ... New balance: $X>`.
- **Treasury sync:** a bound land's latest balance auto-updates its market's treasury (via `upsert_market_shares(mid, treasury_coins=...)` + `_recompute_share_price`).
- **Teleport fees by math:** fees are inferred as the unexplained positive gap between consecutive balances, bucketed per month.
- **Security:** feed posts only accepted from a **webhook** and (if `lands_feed_channel` set) only in that channel — spoof protection, because a forged balance would set treasury/dividends.
- Config binding key: `land_map:<land_lowercased>` → market_id. Set/cleared via `/land bind`.
- Commands: `/land bind`, `/land feed_channel`, `/land status`.

**DB tables relevant to land:** land entries, land balances, land fees (see `get_land_entries`, `get_land_balance`, `set_land_balance`, `replace_land_fees`, `get_all_land_fees` in `Restocker_db.py`). No table yet for *listings/auctions/bids* — that's net-new.

---

## 4. The valuation engine (the differentiator) — `cogs/valuation.py`

- `gather_and_value(market_id)` computes a full valuation from hard data (CSN trailing earnings, hive income, TP fees, inventory, backing) and grades it by **collateral backing %**.
- **Land claim is already a backing pillar** (added this session): config key `valuate:land_claim:<market_id>`, applied at the **land haircut 0.65** (`DEF["land_haircut"]`). Amazonia example: 200 chunks × 10k = 2M raw → assessed **3.5M** with build/farms → counted at 65% = **2.275M** backing.
- Grade gates (backing % of cap): AAA ≥80, AA ≥60, A ≥50, BBB ≥30, BB ≥15, else C.
- For a land-sale product, **reuse `gather_and_value`'s land math** (chunks × rate + markup, comps) to auto-price a plot's reserve. A standalone `value_plot(chunks, build_quality, comps)` helper could be factored out of it.

**Per-chunk rate anchor (owner's number):** 10,000 coins/chunk raw land. Build/farms/market quality can multiply up (Amazonia: 2M raw → 3.5M assessed). AllMart comp: sold at 5M.

---

## 5. How to build / deploy (mechanics)

- Repo lives at `C:\Users\Vaicos\Desktop\AI\RestockerLocal`. Cogs in `cogs/`. Main file `Restocker_main.py` (~508KB), web server `Restocker_web.py` (~294KB), DB helpers `Restocker_db.py`, SQLite `restocker.db` (WAL).
- **Cogs and the web file persist to disk cleanly.** `Restocker_main.py` has historically been reverted by PyCharm overwriting external edits — put new logic in a **cog**, not main. If main must change, give the owner exact PyCharm-side edit instructions.
- **New cog registration:** cogs are loaded in `Restocker_main.py` (look for the `cogs.valuation` / `await bot.load_extension(...)` registration block). A new `cogs.land_exchange` must be added there — coordinate with owner since main.py edits can revert.
- **Deploy flow:** `deploy.bat` = `git add -A && git commit && git push`; the Wispbyte host does `git pull` on boot/restart. So: edit → deploy.bat → restart bot.
- **AI narration:** `core._get_anthropic_client()`; model env `VALUATE_AI_MODEL`.
- **Website routing:** `Restocker_web.py` registers per-section routes (`/inventory`, `/ledger`, `/exchange`, `/orders`, `/teams`, `/mymarket`, `/exchange`, `/health`). A land-exchange page would follow the same pattern (a `_LANDS_HTML` template + `_handle_lands_page` + route + a `_load_lands_data` cache loader), using the shared `_TERMINAL_CSS` / `_TERMINAL_NAV`.

---

## 6. Suggested first tasks for the next chat

1. **Get the competitor model from the owner** (screenshots) — this shapes everything. Don't build blind.
2. Confirm: **auction-first cog** vs **strategy doc first**.
3. If building: scaffold `cogs/land_exchange.py` with DB tables `land_listings` (id, seller, market_id, chunks, coords, desc, mode[fixed/auction], reserve, buy_now, status) and `land_bids` (listing_id, bidder, amount, ts). Commands: `/land list`, `/land bid`, `/land buy`, `/land close`, `/land listings`. Escrow via treasury holds. Anti-snipe: extend end time if a bid lands in the final N minutes.
4. Factor a `value_plot()` reserve-price helper out of `valuation.gather_and_value`.
5. Add a `/lands` website page (terminal aesthetic) showing open listings + live auctions.

**Privacy rule (server-wide):** never expose raw Discord IDs in UI; anonymize to `…XXXX` unless the viewer is the owner/self or the holder opted in.

---

*Written mid-session as a clean cutover point. The inventory-categories work and the Amazonia land-claim valuation change were completed separately in the originating chat.*
