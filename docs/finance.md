# Bonds, vault, valuation & voting

## Bonds
Item-collateralized corporate bonds. **Issuance is on the dashboard**, not Discord:
the exchange page shows an *Issue a bond* panel to owners of listed companies, with a
live collateral-headroom readout. `/bond issue` was retired — a 6-argument slash
command could not show you your headroom before you committed. A COMPANY bond, **≥80%
backed** by the company's items across its markets — is the only remaining command.

**Buying, coverage, coupons and holdings all live on the dashboard Exchange page.** The
bond board there lists every series with live item coverage and a Buy control that posts
to `/api/trade` (`action: bond_buy`). Coins go to the issuer's treasury; coupons return
monthly. The `/bond buy|info|list|my` commands were retired.

## Vault (on the `/market settings` panel)
The `/vault` commands were retired. Deposits and item pledges are the **Vault** button;
status is a line on the panel embed. Pledges are stored at FULL market value — the
haircut is applied when backing is read, never on write.

Mandatory backing deposits for listed companies. `deposit` — coins deposited. `pledge` —
items handed over, counted at **70% of market value**. `status` — dues, deposits, pledges.

## Valuation (`/valuate`, top-level)
AI valuation that auto-gathers earnings, hives, traffic and backing, then **grades** the
stock (e.g. BB, BBB). `list_public` — (manager) value a market, set its params from the
model, and list it on the exchange (`/list_public`). Outage windows are now an AI tool (`manage_outages`) — server-outage windows that
are **excluded from every valuation** (so a DDOS month doesn't tank a grade).

## Shareholder voting (`/investor` (Discord) + the dashboard **Investor** page)
`create` — (manager) open a proposal, posted to #investor-chat. `cast` — vote; **weight =
your stake**. `results` — live or final standings.

## Gotchas the AI must know
- Bonds must stay ≥80% item-backed; the collateral is real inventory, not just coins.
- Vault pledges are valued at 70%, not face value — a haircut is intentional.
- Vote weight is stake-based, so a big shareholder outweighs many small ones.
