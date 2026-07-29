# Stock exchange

## What it is
Public markets can list shares that players trade with server currency. A share price is
derived from the market's **CSN earnings** (a trailing-average net), not set by hand.

## Where trading happens
**On the website**, not in Discord. The dashboard's Exchange page places real orders via
`POST /api/trade` (buy · sell · invest_index · sell_index), session + CSRF authed. The
`/stock buy|sell|panel|invest_index|sell_index` commands were retired.

- Listings, prices, portfolio and the index fund are all on that page.
- `set_drip`, `stock_buyback` and `stock_dividends` are AI tools — ask the bot.
- `/stock set_params` (tune shares outstanding / P-E / treasury) and `/stock delist`
  (bankrupt a market and pay shareholders out) are still commands: one is heavyweight
  tuning, the other is irreversible.

## Pricing (how price moves)
- `_recompute_share_price` blends a trailing-average CSN net (`STOCK_CSN_WEIGHT`), clamps
  the move per report (`STOCK_MAX_REANCHOR_MOVE`, a circuit breaker), and can winsorize
  outlier months. A price floor = (assets + treasury) ÷ shares outstanding.
- Each buy skims 0.5% into a central exchange insurance fund (backing).

## Gotchas the AI must know
- Trades from the website are marshalled onto the BOT's event loop (`run_on_bot_loop`).
  The trade engine's supply check and its writes aren't atomic, so a web trade running on
  the web thread could otherwise interleave with a Discord one. Never bypass that.
- Price is earnings-driven: a market with no recent CSN reports won't reprice.
- A market must be public/listed before its shares can trade; `set_ticker` gives it a symbol.
