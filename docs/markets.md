# Markets

## What it is
A "market" is a shop/business with its own sales history (from CSN), a website dashboard
tab, an owner + managers, and optionally a public stock listing (see [stock.md](stock.md)).
Every CSN report and order belongs to a market.

## Commands (`/market …`)
- `add` / `delete` / `list` / `info` — register, remove, list, inspect a market.
- `earnings` — monthly income / spending / net. `report` — owner's private best-sellers +
  missing-stock + earnings view.
- `/bind_market market_id [channel]` (top-level) — **bind a Discord channel to a market** so CSN reports
  there record to it, no in-game code needed. (Alternative to the CSV market code.)
- `set_owner` / `add_manager` / `remove_manager` / `set_leader_role` — ownership + roles.
- `edit` — name / fee / active. `set_ticker` — stock symbol (e.g. GEX).
- `treasury` / `treasury_withdraw` — view/withdraw a public market's excess treasury.
- `remove_item` / `log_restock` / `suggest_price` — catalog upkeep; `log_restock` keeps net
  profit honest when stock is added by hand.
- `platform_balance` — total platform fee collected (manager).
- Related, separate groups: `/market_code` (get a market's CSN verification code),
  `/inventory …` (live barrel fullness, capacities, restock deficit), `/shop …` &
  `/item_edit` (catalog item price + stackability).

## Data / binding
- Markets store `owner_id`, `manager_ids`, `leader_code` (CSN code), `report_channel_id`
  (bound channel), `active`, fee. Channel binding is `report_channel_id` / lookup by
  channel; it **wins over** whatever market the CSV declares.

## Gotchas the AI must know
- Owners/managers can act on their **own** market without the global Manager role.
- To attribute a channel's CSN reports: either the config carries `market_id`+`market_code`
  (auto-binds on first valid report) OR a manager runs `/bind_market`.
- Deleting a market also removes its dashboard tab, stock listing, and stock rows.
