# Bonds, vault, valuation & voting

## Bonds
Item-collateralized corporate bonds. `/bond issue` — (manager) a COMPANY bond, **≥80%
backed** by the company's items across its markets — is the only remaining command.

**Buying, coverage, coupons and holdings all live on the dashboard Exchange page.** The
bond board there lists every series with live item coverage and a Buy control that posts
to `/api/trade` (`action: bond_buy`). Coins go to the issuer's treasury; coupons return
monthly. The `/bond buy|info|list|my` commands were retired.

## Vault (`/vault …`)
Mandatory backing deposits for listed companies. `deposit` — coins deposited. `pledge` —
items handed over, counted at **70% of market value**. `status` — dues, deposits, pledges.

## Valuation (`/valuate`, top-level)
AI valuation that auto-gathers earnings, hives, traffic and backing, then **grades** the
stock (e.g. BB, BBB). `list_public` — (manager) value a market, set its params from the
model, and list it on the exchange (`/list_public`). `/outage add|list|remove` — server-outage windows that
are **excluded from every valuation** (so a DDOS month doesn't tank a grade).

## Shareholder voting (`/vote …`)
`create` — (manager) open a proposal, posted to #investor-chat. `cast` — vote; **weight =
your stake**. `results` — live or final standings.

## Gotchas the AI must know
- Bonds must stay ≥80% item-backed; the collateral is real inventory, not just coins.
- Vault pledges are valued at 70%, not face value — a haircut is intentional.
- Vote weight is stake-based, so a big shareholder outweighs many small ones.
