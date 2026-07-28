# Restocker bot — subsystem reference (for the AI)

These files are the bot's own knowledge base. The AI loads them as context so it can
answer questions about how the bot works and point users to the right command — instead
of guessing. Keep each file concise and **accurate to the code**; if behavior changes,
update the doc in the same commit.

## Index

| Doc | Subsystem | One-liner |
|-----|-----------|-----------|
| [hive.md](hive.md) | Hive harvest wages | Pays harvesters for honey/comb sold to company chest shops |
| [csn.md](csn.md) | CSN mod + import | Scrapes ChestShop sales → CSVs → webhook → bot records money + hive wages |
| [markets.md](markets.md) | Markets | A shop with its own sales history, dashboard tab, optional stock listing |
| [stock.md](stock.md) | Stock exchange | Public markets trade as shares, priced off CSN earnings |
| [orders.md](orders.md) | Orders | Production requests workers claim and fulfill |
| [futures.md](futures.md) | Futures + cost sheet | Custom build requests — **priced at cash cost from a fixed tier sheet** |
| [pricing.md](pricing.md) | Buyer groups & prices | **Inner group vs external markets** — who pays cash cost / group / sell |
| [inventory.md](inventory.md) | Inventory / restock | Barrel fullness, capacities, shortfall restocking, item edits |
| [loyalty.md](loyalty.md) | Loyalty + IGNs | Points, tiers, rewards — and the IGN registry that routes pay |
| [money.md](money.md) | Money | Balances, withdrawals, investors, platform fees, consignment deals |
| [finance.md](finance.md) | Bonds/vault/voting | Bonds, escrow, vault, valuation grades, shareholder votes |
| [teams.md](teams.md) | Teams + projects | Worker teams, performance feeds, project budgets, name aliases |
| [ai-and-admin.md](ai-and-admin.md) | AI, admin, lands | Your own permissions, repair tools, lands, auctions, config |

## How the AI should use these
- Treat these as ground truth about the bot's capabilities. Never claim a feature doesn't
  exist if it's documented here.
- When asked "how do I…" or "who can…", cite the exact command (e.g. `/hive payout`).
- **Never invent numbers.** Prices, costs and rates come from the sheets/commands
  documented here (e.g. futures = cash cost via `/futures_quote`). If a figure isn't
  documented, say you'd need to look it up — don't estimate or add made-up surcharges.
- These cover every major subsystem. If something genuinely isn't here, say so and offer to
  check, rather than guessing.

## Retired commands (2026-07-28) — never tell anyone to run these
The command surface was cut from ~205 to ~135. These **no longer exist**; use the
replacement, or just answer from your own tools:

| Gone | Use instead |
|---|---|
| `/hive status` | your `get_hive_status` tool |
| `/hive ingest`, `/hive settle` | the mod posts harvest lines itself; `/hive payout` settles |
| `/inventory stock`, `/inventory clear_stock` | your `get_stock_fullness` tool, or the website |
| `/market list`, `/market report`, `/market earnings` | `/market info` + your `get_market_earnings` tool |
| `/market suggest_price`, `platform_balance`, `hide_earnings` | answer from tools / the website |
| `/csn_audit`, `/import_earnings` | `get_market_earnings`; imports come from the mod |
| `/stock list/price/portfolio/index_fund/dashboard` | the website Exchange page |
| `/brew set|remove|list`, `/tool set|remove|list` | the mod auto-names items from lore; you still have `set_alias` / `remove_alias` / `list_aliases` |
| `/enchant_area …`, `/escrow …`, `/suggest …`, `/network …` | retired entirely (unused) |
| `/fees …`, `/investor sync|payout|set_pool|apply_roles|liquidate` | rare admin — say a manager must do it manually |
| `/admin` repair tools (repair_all/payouts/order, backfill_team_perf, dedupe_perflog, migrate_stock, hive_audit, purge_brews, value_free_stock, csn_provenance, csn_delete_month) | one-off fixes, already done |
| `/loyalty register_ign`, `/team perf` | `/register_ign`, `/team leaderboard` |

`/admin` now holds only: `wipe`, `ai_audit`, `dm_setup`, `rebuild_market_channel`,
`fix_month_close`, `csn_cleanup`.

## Conventions
- **Verb semantics (critical):** in ChestShop/CSN, `bought` = a customer bought FROM you =
  **your income/sale**; `sold` = you bought FROM someone = **your expense**. This is the
  opposite of the intuitive reading. All money logic follows it.
- Access to the AI itself is an allow-list managed with `/ai_allow add|remove|list`,
  separate from Discord roles.
