# Bonds, vault, valuation & voting

## Bonds (`/bond …`)
Item-collateralized corporate bonds. `issue` — (manager) a COMPANY bond, **≥80% backed** by
the company's items across its markets. `buy` — coins go to the issuer's treasury, coupons
return monthly. `info` — coverage, coupon, holders, status. `list` / `my`.

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
