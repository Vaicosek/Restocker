# Worked Example — GEX (Global Exports)

End-to-end valuation of GEX (market: **greyhames**), the first public company, as of mid-2026.

## Inputs used

| Input | Value | Source |
|---|---|---|
| Forward monthly revenue | **$4.0M** (base) | latest actual (May 2026); 2026 trend 3.2M→4.0M |
| Cash & equivalents | $4,311,630 | last documented balance sheet |
| Debt | 0 | memo |
| Shares outstanding | **40,000** | 36,000 (you) + 3,500 common + 500 preferred |
| Recent dividend | ~$171,000 / mo | 2026 avg (Feb–Jun) |
| Recent monthly profit | ~$2.5M | earnings sheet (70% margin) |
| Traded lowest-ask | $1,899 | cap table |

## Results (from `value_company.py`)

| Lens | Value | Per share |
|---|---|---|
| **DCF** (all FCF, owner ceiling) | **$65.4M** | **$1,635** |
| **DDM** (dividend only, minority floor) | $3.1M | $78 |
| **Market** (traded ask) | $75.96M | $1,899 |

**Scenarios (DCF equity):** Conservative $3.6M/mo → **$59.3M** ($1,483/sh) · Base $4.0M → **$65.4M**
($1,635/sh) · Optimistic $4.2M → **$68.5M** ($1,712/sh).

**vs. IPO (Apr 2025):** $33.30M equity / $750 per share. The company is worth **~2× the IPO
valuation** now — revenue roughly doubled (from the ~$1.85M forward-base at IPO to ~$4.0M/mo).

## The dividend insight (why the DDM matters here)

Dividends were **cut ~58%** — from ~$404k/mo in late 2025 (16% payout) to ~$171k/mo in 2026
(**8% payout**) — *while revenue rose*. That barely moves the DCF (retained cash still compounds
your ownership value) but collapses the dividend value: a pure-dividend investor's shares are worth
only ~$78 each vs the $1,635 DCF ceiling.

**Takeaway:** the $1,899 market price is pricing the DCF/growth story, not the dividend. A minority
share's *fair* value sits between the DDM floor (~$78) and DCF ceiling ($1,635); the low, falling
payout drags it toward the floor. If you want the public stock to justify its price, the lever is
the **payout ratio** — raising the dividend lifts the DDM floor.

## How this was validated

The engine reproduces the bank's original memo exactly (EV $28.99M / equity $33.30M / TV $25.92M),
so the same code applied to current earnings is trustworthy.
