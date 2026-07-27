# Stock exchange

## What it is
Public markets can list shares that players trade with server currency. A share price is
derived from the market's **CSN earnings** (a trailing-average net), not set by hand.

## Commands (`/stock …`)
- `list` — every listed market. `price market` — current share price + recent history.
- `buy` / `sell` — trade shares for server currency. `panel` — interactive live trade panel.
- `portfolio` — your holdings + unrealized P/L. `dividends` — show/set shareholder payout.
- `dashboard` — (manager) post a live auto-updating market dashboard in a channel.
- `set_params` — (manager) tune shares outstanding / P-E / treasury.
- `backing market` — backing score: treasury cash + inventory assets + insurance-fund share
  vs market cap (targets ~10/10/5%). `delist market confirm:` — bankrupt + pay shareholders
  pro-rata from real backing, then remove.

## Pricing (how price moves)
- `_recompute_share_price` blends a trailing-average CSN net (`STOCK_CSN_WEIGHT`), clamps
  the move per report (`STOCK_MAX_REANCHOR_MOVE`, a circuit breaker), and can winsorize
  outlier months. A price floor = (assets + treasury) ÷ shares outstanding.
- Each buy skims 0.5% into a central exchange insurance fund (backing).

## Gotchas the AI must know
- The website can't trade (no per-user trade auth) — the site's ticket is an **estimator**
  that produces the exact `/stock buy|sell` command to run in Discord.
- Price is earnings-driven: a market with no recent CSN reports won't reprice.
- A market must be public/listed before its shares can trade; `set_ticker` gives it a symbol.
