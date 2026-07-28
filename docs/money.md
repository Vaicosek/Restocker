# Money — balances, withdrawals, investors, fees

## Balances
- `/balance [user]` — coin balance (others require Manager).
- Balance history, manual credits and withdrawal requests are handled by the AI (ask it) or
  through the ticket flow — the old `/balance_history`, `/deposit` and `/withdraw_request`
  commands were retired.
- Balances carry `coins`, `principal` (for interest) and `lp`. Every movement writes a
  `coin_ledger` row with a reason — an unreasoned entry means it was paid by hand outside
  the normal flow.

## Investors (GEX.PR preferred shareholders)
- `/investor status` — register, pool %, recent distributions.
- Pool %, manual payouts, role grants, liquidation and cap-table sync are AI-side now — ask
  the bot; distributions still run automatically when a V Tech market's monthly CSN net
  records (positive months only, once per market-month).

## Platform fees
Fees are configured in the DB and charged automatically. The `/fees` command group was
retired — ask the AI for fee status or the platform balance.

## Consignment futures (bulk deals)
Bulk consignment tracking is AI-side. For custom build requests see [futures.md](futures.md)
(`/futures_order`, `/futures_bulk`).

## Gotchas the AI must know
- Coin movements should always carry a reason; hand-paid credits are the ones that break
  audits later.
- Withdrawals go through the ticket flow so a manager confirms delivery in-game.
