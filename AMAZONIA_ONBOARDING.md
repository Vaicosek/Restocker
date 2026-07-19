# Amazonia Onboarding — Runbook (2026-07-19)

Prepared from `RestockerLocal` code + local `restocker.db` + `data/csn_history/csn_history_amazonia.yml`.
**Caveat:** the local `restocker.db` may not be the live production copy (bot runs on Wispbyte). Treat the
CSN-history numbers below as reliable (they're file-based and versioned), but re-check treasury/coin
balances against the live dashboard before running anything that moves coins.

## 1. Lie-detector check: Jacob's claim vs mod-recorded CSN

`amazonia_earnings_import.csv` (Jacob's rollup, mentioned in the last handoff) **no longer exists** in the
repo — it may have been deleted or was never committed. I used the mod-recorded
`csn_history_amazonia.yml` directly instead, which is the harder-data source anyway.

| Month | Jacob claimed | CSN-recorded net | Delta |
|---|---|---|---|
| May 2026 | ~807,000 | **1,356,150.52** | mod net is **+68%** vs. his claim |
| June 2026 | ~1,040,000 | **1,892,960.59** | mod net is **+82%** vs. his claim |
| July 2026 (partial, thru 07-04) | — | -101,178.57 (income 155,776 / spent 256,954) | one export only, incomplete month |

**Read on this:** the mod-recorded net is *higher* than what Jacob told you both months, not lower — so
this isn't him inflating results to look good. Two honest explanations: (a) "net" in the CSN export is
gross auction-house net and doesn't subtract restocking costs bought off-market, hive worker wages, or
other overhead he's netting against; or (b) he's rounding down casually when talking about it. Flagging
it rather than concluding either way — worth a quick "hey, CSN shows higher, what's the gap from?" before
you set `assets`/`sellable`, since if it's real uncounted overhead it should lower his book value, and if
it's just casual rounding it doesn't matter.

## 2. What's already true in the DB (checked against local `restocker.db`)

- `markets` row for `amazonia` already exists (`active=1`, `csn_history_file=csn_history_amazonia.yml`,
  `owner_id=NULL` — **not yet assigned to Jacob**).
- No stock listing yet (`market_stock` table holds his **shop catalog**, not asset backing — 233 items,
  ~99M in stock×sell_price, but that's shelf inventory for order fulfillment, not his hive/farm book
  value; don't use it for `assets`).
- `hive_claims` / `hive_active_batch` have nothing amazonia-specific — the "hive book value" figure the
  handoff refers to is not stored anywhere in the DB. That number is your own assessment of his automated
  farms (walk the land / know the farm count), same as the "sellable — his hives sell at 60-70% median,
  you set 50%" note from last session. I can't compute it from data on hand.

## 3. Commands to run, in order

**Step 0 — assign ownership** (missing currently):
```
/market set_owner market_id:amazonia owner:@Jacob1304
```

**Step 1 — go public** (let it price off the real CSN net you just verified above — don't pass
`initial_price`, house rule is "price is earned from CSN history, not typed in"):
```
/market go_public market_id:amazonia
```
Defaults: 1000 shares outstanding, 12x P/E on monthly net. Launch price will be computed off June's
1,892,960.59 net (or a blended figure, depending on how `_recompute_share_price` weights history —
worth eyeballing the resulting price against ~1.89M×12/1000 ≈ 22,700/share as a sanity check).

**Step 2 — set asset params** (fill in the two numbers only you can supply):
```
/stock set_params market_id:amazonia assets:<hive fleet book value> sellable:<50% of that figure>
```
Price floor becomes `(assets + treasury) ÷ shares_outstanding` per house rule.

## 4. Land binding — traffic pillar

This is what makes `/land bind` and the mod's auto-sweep work; per last session's parked notes,
amazonia isn't bound yet and its traffic pillar reads 0 until it is.

1. **Get the exact in-game claim name** for Jacob's land (not necessarily "amazonia" — `/land bind` needs
   it exactly as the game shows it, e.g. `MardURAK`-style).
2. On the Minecraft client that has `LandTracker` built in (only your MAIN/Boris instance per the parked
   note), add that land name to `sales/lands_config.json` in that client's config directory — this is a
   *runtime* file on the game client, not something in either connected repo folder, so I can't edit it
   directly. If you want me to, connect that folder (wherever `CsnExportClient.configDir` points on that
   machine — typically inside `.minecraft/config/`) and I'll edit the JSON for you.
3. Rebuild + install the mod jar if it isn't already current on that instance: `Sales\build_and_install.bat`.
4. Bind it in Discord:
   ```
   /land bind land_name:<exact in-game name> market_id:amazonia
   ```

**⚠ Known bug, still unfixed (money-core pass, priority #2 in the backlog):** `/land bind` immediately
overwrites `amazonia`'s treasury with the land's current balance snapshot (`lands.py` line ~219). If the
bot has already made any bot-side deductions against amazonia's treasury (overrides, payouts) that
haven't been mirrored by an in-game withdrawal, binding will silently wipe them. Check the in-game land
balance matches what the bot thinks the treasury is before you run this, or expect to reconcile after.

## 5. Open items / not done in this pass

- Hive fleet book value + sellable % — needs your on-site assessment, not computable from files.
- Exact in-game land claim name for Jacob's plot — needs you or him to confirm.
- `lands_config.json` edit + mod rebuild on the LandTracker-equipped client — needs that folder connected,
  or you can do it by hand (see §4.2).
- Everything from last session's post-deploy checklist is still outstanding since deploy hasn't happened
  (`deploy.bat` → Wispbyte restart) — amazonia's go_public/set_params commands only work once that build
  is live.
