# Company Valuation Workflow

A repeatable way to value any market/company on the server (GEX and future listings),
using the same DCF the bank used for the GEX IPO — **plus** a dividend cross-check so you
never over-value a stock that's paying out little.

## The 3-lens rule (the important part)

The *same* shares are worth very different amounts depending on what a holder actually gets:

| Lens | What it measures | Whose value |
|---|---|---|
| **DCF** | present value of ALL free cash flow | the **owner's ceiling** |
| **DDM** | present value of the **dividend stream only** | a **minority investor's floor** |
| **Market** | the current traded price | what buyers will pay today |

A **minority share** is worth somewhere **between the DDM floor and the DCF ceiling**.
Cutting the dividend barely moves the DCF (retained cash still builds the owner's value) but
**guts the DDM** — so a dividend cut drags a minority share toward the floor. Always report all
three, plus the **payout ratio** (dividend ÷ profit) and its trend.

## How to value a company (steps)

1. **Gather inputs** (see `METHODOLOGY.md → Data inputs`): forward monthly revenue run-rate,
   cash, debt, shares outstanding (all classes), recent monthly dividend, recent monthly profit,
   and the current traded price.
2. **Edit the INPUTS block** at the bottom of `value_company.py` and run it:
   ```
   python value_company.py
   ```
   It prints the DCF schedule, the three lenses, the payout ratio, and the minority-share range.
   (It self-tests on startup by reproducing the bank's original $33.30M GEX figure — if that line
   doesn't print, don't trust the output.)
3. **Read the output**: DCF ceiling, DDM floor, market price, payout %.
4. **Judge fair value**: for a controlling stake use the DCF; for minority/public float weight the
   DDM and payout trend heavily; sanity-check against the traded price.

## Files

| File | What it is |
|---|---|
| `value_company.py` | The valuation engine (DCF + DDM + scenarios), self-validating. |
| `METHODOLOGY.md` | The model: assumptions, formulas, validation, and required data inputs. |
| `examples/GEX.md` | Worked example — GEX valued end-to-end with the dividend-cut insight. |

## Caveat baked in

This is a **server economy** — the whole thing assumes the world keeps running. The DCF uses a
**−1% terminal growth** (a slow perpetual decline) as the bank's nod to that risk; if you think
the server is nearer its end, lower `TERMINAL_G` further or discount the DCF ceiling toward the
DDM floor.
