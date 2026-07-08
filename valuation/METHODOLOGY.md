# Valuation Methodology

Source of truth: the **Crimson Investment Bank IPO Recommendation** for GEX (Global Exports), plus the
GEX earnings history. The `value_company.py` engine reproduces the bank's original figure exactly
(EV **$28.99M**, equity **$33.30M**, terminal value **$25.92M**) — that self-test is the proof the
model is implemented correctly.

## 1. DCF (discounted cash flow) — the owner's ceiling

Five-month forward projection off the latest run-rate, discounted monthly, plus a terminal value.

**Assumptions (bank defaults — override per company):**

| Parameter | Value | Notes |
|---|---|---|
| Revenue growth | **5.00% / month** | applied from month 2 (month 1 = base run-rate) |
| Working capital (NWC) | **30% of sales** | |
| Capex | **7% of sales** | |
| → Free-cash-flow margin | **63%** of revenue | `1 − NWC − capex` (forward months) |
| WACC (discount rate) | **4.41% / month** | |
| Terminal growth | **−1.00%** | perpetuity; negative = conservative, fits server risk |

**Formulas:**

```
FCF_month0 (realized)  = last_actual_revenue − capex            (no NWC; undiscounted)
FCF_month_t (t=1..5)   = revenue_t × 0.63,  revenue_t = fwd_rev × (1.05)^(t-1)
PV(FCF_t)              = FCF_t / (1 + 0.0441)^t
Terminal value @ m5    = FCF_5 × (1 + g) / (WACC − g),   g = −0.01
PV(terminal)           = TV / (1 + 0.0441)^5
Enterprise value (EV)  = FCF_0 + Σ PV(FCF_1..5) + PV(terminal)
Equity value           = EV + cash − debt
Value per share        = Equity value / total shares (all classes)
```

## 2. DDM (dividend discount model) — the minority floor

What an outside shareholder's stream is actually worth if they only ever receive the dividend:

```
DDM value      = dividend_per_month × (1 + g) / (WACC − g),   g = −0.01
DDM per share  = DDM value / total shares
Payout ratio   = dividend / profit        (track the TREND — a falling payout is a red flag)
```

For GEX this is tiny vs the DCF because the payout ratio fell to ~8% — most cash is retained/
withdrawn by the owner, not distributed. **A minority share is worth between the DDM floor and the
DCF ceiling.**

## 3. Market check

`traded_price × shares` = implied market cap. Compare to the DCF/DDM. If the market price is above
the DCF ceiling, buyers are paying for a growth story beyond the model; if it's near the DDM floor,
the market is treating it as a pure income stock.

## Data inputs (what to collect per company)

| Input | Where it comes from |
|---|---|
| Monthly revenue history → **forward run-rate** | earnings sheet (e.g. `earnings_extended.xlsx`, "Monthly Summary"); use the latest full month or a trailing-3/6-month average |
| Monthly **profit** | same sheet (for payout ratio) |
| Monthly **dividend** run-rate | dividend schedule / bank transfers (recent months) |
| **Cash & equivalents**, **debt** | latest balance sheet / financial statements |
| **Shares outstanding** (all classes) | cap table — `market_shares.shares_outstanding`; on the site `/shares/<market>` |
| **Traded price** | current lowest ask — cap table / `/shares/<market>` |

## Scenarios

Run at least three forward-revenue cases (conservative / base / optimistic) — e.g. trailing-avg,
latest month, trend-extrapolated — because the valuation scales with the revenue baseline. Report
the range, not a single number.

## Reproducibility

`python value_company.py` prints `[self-test OK] reproduces memo: EV $28.99M, equity $33.30M` on
startup. If that line is missing or the numbers drift, the model or its constants changed — fix
before trusting any output.
