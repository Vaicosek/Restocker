# Valuation — status & TODO

Rough but working. This is a starting point; more work needed before it's production-grade.

## Done ✅
- `value_company.py` — validated DCF + dividend DDM + scenarios; self-tests by reproducing the
  bank's $33.30M GEX memo figure.
- DB wiring — `load_from_db` / `value_from_db` pull shares, traded price, holders straight from
  `restocker.db`. CLI: `python value_company.py --db ../restocker.db --market greyhames --revenue …`.
- `valuate_command_DRAFT.py` — rough `/valuate` Discord command (3-lens embed).
- Docs — `README.md`, `METHODOLOGY.md`, `examples/GEX.md`.

## Sketched, not yet wired 📝
1. **Total revenue + cash + dividends → `company_financials` table.** Sketched in
   `financials_DRAFT.py`: the schema, `set_company_financials` / `get_company_financials` DB
   helpers, and a `/set_financials` command. The engine already reads it
   (`value_company.load_financials_from_db`), so once the table exists and the owner records a few
   months, `/valuate market_id:greyhames` needs **no manual args**. To finish: add the schema to
   `Restocker_db.SCHEMA`, the two helpers to `Restocker_db.py`, the command to a cog, then test live.

## Still needs work 🔧
3. **Permissions.** Decide who can run `/valuate` (anyone? managers? market owners only?).
4. **Integrate the command.** Move into `cogs/stock.py` or load `valuate_command_DRAFT.py` as a cog,
   register it, and **test on the live server** (the local DB snapshot has no listed markets).
5. **Assumptions per company.** Defaults (5% growth, 63% FCF margin, 4.41% WACC, −1% terminal) are
   GEX's. Let them be overridden per market (store on the market row, or command args).
6. **Sanity guardrails.** Flag when the market price is way above the DCF ceiling, when payout is
   collapsing, or when revenue data looks stale.
7. **(Optional) AI tool.** Once `/valuate` is stable, expose it as a tool the @mention AI can call,
   so conversational "what's GEX worth?" runs the real calc instead of guessing.

## Reminder
Do NOT feed the AI screenshots/pictures of prices+earnings to "compute" valuations — LLMs drift on
multi-step math. Keep the math in `value_company.py`; let the AI call it and explain the output.
