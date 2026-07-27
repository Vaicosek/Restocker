# CSN — the export mod + report import

## What it is
A Fabric client mod (`csn`, source in `Sales/csn-mod-src-1.21.11/`) that scrapes ChestShop
Notifier's `/csn history` chat output and exports it to CSVs, then posts them to a Discord
webhook. The bot ingests those CSVs into market earnings, catalog, and hive wages.

## Verb semantics (CRITICAL — do not get wrong)
ChestShop phrases everything from the **customer's** view:
- `bought` = a customer bought FROM you → **your income / sale** (`total_sold_qty`).
- `sold` = you bought FROM someone → **your expense / purchase** (`total_bought_qty`).

## Mod behaviour
- Press **K** → sends `/csn history 1..Y`, scrapes every line, writes CSVs to
  `.minecraft/sales/`, then (on a fully-read run) runs `/csn clear`.
- Files: `csn_export_<period>.csv` (per-transaction, has the **`actor`** = seller IGN),
  `csn_monthly_<YYYY-MM>.csv` (aggregated by item — no seller), `*.seen` (dedup hashes),
  `csn_stock_*.csv` (barrel/stock scan).
- Dedup: the `.seen` set counts each transaction once, even if `/csn clear` fails.
- **Hive lines:** the mod also posts each honey/comb `sold` transaction as
  `IGN sold you Nx Item @<ts>` to the webhook, which drives hive wages (see [hive.md](hive.md)).

## Config (`.minecraft/sales/csn_config.json`)
`discord_webhook`, `market_id`, `market_code`, `brew_aliases`. Per site: same
`market_id`/`market_code`, a **different webhook** per channel.

## How a report reaches the right market
- The CSV header carries `# MARKET,<id>,<code>`. On a channel with no binding, a **valid
  code auto-binds** the channel to that market (code = `/market_code`). Or a manager runs
  `/market set_channel market_id:<id>` (no code needed after). Channel binding always wins.
- Reports posted to a bound channel are auto-imported (no command needed).

## Bot-side dedup / safety
- `_parse_monthly_csv` de-dups duplicate `# RUN` timestamps and auto-detects
  cumulative-vs-delta files. `/csn_audit` verifies a month; `_csn_anomaly_check` flags a
  net >3× the recent average (possible un-cleared/duplicate report).

## Gotchas the AI must know
- The **monthly** CSV cannot name sellers — only the **export** CSV (or the mod's live
  hive lines) can. Per-harvester pay needs the export/hive lines, not the monthly file.
- Re-posting the same file is safe; the bot dedups.
