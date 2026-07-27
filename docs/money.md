# Money — balances, withdrawals, investors, fees

## Balances
- `/balance [user]` — coin balance (others require Manager).
- `/balance_history` — recent coin movements.
- `/deposit` — (manager) credit a user. `/withdraw_request` — a user requests a payout;
  opens a **manager ticket** with Approve & mark paid / Reject buttons.
- Balances carry `coins`, `principal` (for interest) and `lp`. Every movement writes a
  `coin_ledger` row with a reason — an unreasoned entry means it was paid by hand outside
  the normal flow.

## Investors (GEX.PR preferred shareholders)
- `/investor sync` — rebuild the register from a pasted Crimson Banking cap-table export.
- `/investor status` — register, pool %, recent distributions.
- `/investor set_pool` — % of V Tech monthly net that goes to investors.
- `/investor payout` — manual extra payout. `/investor apply_roles` — grant the Investor role.
- `/investor liquidate` — a gone-for-good holder's equity returns to the company.

## Platform fees
- `/fees status` — are fees on, balance, recent charges. `/fees toggle` — on/off at runtime.
- `/fees charge` — manually charge a user (e.g. tool/factory rental) into the platform balance.

## Consignment futures (bulk deals)
`/futures deals` · `view` · `price` · `sold` · `bill` · `pay` — track a bulk consignment:
price each line against a catalog item, record customer resales, post an invoice, and log
payments against the running balance. (Custom build requests are a different thing — see
[futures.md](futures.md).)

## Gotchas the AI must know
- Coin movements should always carry a reason; hand-paid credits are the ones that break
  audits later.
- Withdrawals go through the ticket flow so a manager confirms delivery in-game.
