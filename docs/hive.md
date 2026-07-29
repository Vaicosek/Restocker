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

## HiveSettings — one panel (`/hive settings`)
Everything hive-related is on a single ephemeral panel. Seven subcommands used to do
this; they were replaced by it.

- **Site selector** — switch between bound hive feeds.
- **Autopay** — toggle instant payment on ingest. Turning it ON warns if lines are
  already unpaid: autopay only touches NEW lines.
- **Pay now** — settle the whole backlog (this is the old `payout`).
- **Bind this channel / Unbind** — make the current channel a feed for the selected site.
- **Item value / Wage % / Owner split** — modals for the per-piece value, the harvester
  percentage, and a partner owner's cut.
- The panel also shows unpaid value, who's held for lacking `/register_ign`, and which
  items are skipped for having no value set.

Also available as AI tools: `set_hive_autopay`, `run_hive_payout`,
`get_hive_harvester_detail`, `get_hive_status`.

## Item values — PER PIECE, and the shop quotes PER STACK
This is the single most misread thing in the system, so state it explicitly whenever
value comes up:

| Item | Shop price | Stack | **Value per piece** |
|---|---|---|---|
| Honeycomb Block | 300 per stack | 64 | **4.6875** |
| Honey Block | 350 per stack | 64 | **5.46875** |

- `hive_value:<item>` and `hive_harvests.unit_value` are **per piece**, always.
- The built-in defaults are already `300/64` and `350/64` — they are correct, do NOT
  "fix" them to 300 and 350.
- A harvest of 3,856 comb blocks is therefore `3,856 x 4.6875 = 18,075` value, and at a
  15% wage the harvester earns `2,711` coins. That arithmetic is right — if someone
  thinks the payout looks small, the reason is the **wage %**, not the item value.
- **"Value" is the goods' market worth, not the wage.** The harvester receives the wage
  percentage of it; the remainder is the company's. Never present value as money owed.
- If a value is ever set to a *stack* price by mistake, wages come out **64x too high**.

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
