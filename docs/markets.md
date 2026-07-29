# Markets

## What it is
A "market" is a shop/business with its own sales history (from CSN), a website dashboard
tab, an owner + managers, and optionally a public stock listing (see [stock.md](stock.md)).
Every CSN report and order belongs to a market.

## MarketSettings — one panel (`/market settings`)
Everything about a market lives on one ephemeral panel. Seventeen subcommands used to do
this; the panel replaced them.

- **Market selector**, then: **Edit** (name · fee · active · bind a land), **Rewards**
  (restock loyalty), **Ticker**, **CSN code**, **Leader role**.
- **Set owner** · **Add manager** · **Remove manager** · **Remove item** · **V Tech group**.
- **Go public / Delist** · **Withdraw treasury** · **Delete market**.
- The card shows owner, status, fee, code, bound channel, site managers, listing state
  (price, shares, treasury, **withdrawable excess**) and the CSN webhook (spoilered).

Registering a new market and inspecting one are BOTH on the panel now (Register new market
button, server managers only). The only other market command is `/market_code`, which is
gated by Discord ROLE rather than manager_ids so a market leader who isn't a registered
manager can still fetch their CSN code.

### Guards the panel keeps
- **Launch price** may not exceed **2x the computed fundamental** unless a server manager
  sets it — otherwise a site manager could list at any price and sell into the treasury.
- **Withdrawable treasury** is `treasury - (shares held x price)`. That subtraction is the
  buyback cover; without it you drain the money backing shareholders.
- **Reward caps for owners**: coin bonus <= 5,000, percent <= 50%, multiplier <= 3x.
- **Delete** and **delist-with-holders** need the market id typed back.


## Data / binding
- Markets store `owner_id`, `manager_ids`, `leader_code` (CSN code), `report_channel_id`
  (bound channel), `active`, fee. Channel binding is `report_channel_id` / lookup by
  channel; it **wins over** whatever market the CSV declares.

## Gotchas the AI must know
- Owners/managers can act on their **own** market without the global Manager role.
- To attribute a channel's CSN reports: either the config carries `market_id`+`market_code`
  (auto-binds on first valid report) OR a manager runs `/bind_market`.
- Deleting a market also removes its dashboard tab, stock listing, and stock rows.
