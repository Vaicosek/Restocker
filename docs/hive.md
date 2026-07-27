# Hive — harvest wages

## What it is
The company's perpetual "hive harvesting" project. Harvesters sell honey/comb to the
company's chest shops (which buy at **0 coins**), so the company owes them a wage. The
hive system records those sales and pays each harvester a % of the harvested value.

## How a payout happens (end to end)
1. A harvester sells honey/comb to a chest shop in-game.
2. The CSN mod (see [csn.md](csn.md)) scrapes it and posts a line to the **hive-bound
   channel**: `IGN sold you <qty>x <item> @<timestamp> (-0 Coins)`.
3. The bot parses it (`_parse_hive_feed`), records a `hive_harvests` row valued at the
   item's hive value, resolves the IGN to a Discord user via the IGN registry.
4. With **autopay on**, the harvester is paid their % immediately; otherwise the row waits
   for a manual `/hive payout`. The remainder books to `hive_ledger` (feeds stock price).

## Commands (`/hive …`)
- `bind` — mark THIS channel as a market's hive feed. `unbind` — reverse it.
- `autopay enabled:True|False` — pay instantly on ingest vs record-only.
- `set_value item value` — coins per piece for a hive item (autocompletes hive items).
- `set_wage pct` — harvester's % of value (default 17).
- `set_split market_id owner_pct` — a partner site owner's cut (V Tech's own hives = 0).
- `status` — unpaid harvests for a market (who's owed what).
- `payout market_id [apply]` — manual sweep. `apply:false` previews, `apply:true` pays.
- `ingest` — manually paste feed lines (backfill / when the mod isn't posting).

## Config / data
- Value per item: `hive_value:<item lowercased>` (e.g. `honey block`, `honeycomb block`).
- Wage %: `hive_harvester_pct`. Channel→market feed: `hive_feed:<channel_id>`. Autopay:
  `hive_autopay:<market_id>`. Tables: `hive_harvests`, `hive_ledger`.

## Gotchas the AI must know
- **Pay is per person (by IGN), not per site.** Multiple channels can bind to the SAME
  market (e.g. all `*-hive-site` channels → `vtech`); the channel is just the inbox, the
  seller's IGN determines who's paid. There is no separate "hive site" market object.
- **No double-pay.** Each sale is unique by `(market, ign, item, qty, sale_ts)` via the
  `uq_hive_sale` index, so re-posting, re-scanning, or two instances reporting the same
  shop still pays once. Safe to re-run backfills.
- **Unregistered harvesters are held, not lost.** If an IGN hasn't run `/register_ign`,
  its wage waits and pays automatically once they register.
- Setting the wage/value is **never retroactive** — it applies to future payouts only.
