"""
SKETCH — company_financials table + /set_financials command.

This closes the last gap in /valuate: the DB has shares/price/holders but NOT total
revenue/profit/dividend/cash (CSN only tracks shop sales). Once the owner records those
monthly, `value_company.value_from_db` reads them automatically (it already prefers the
`company_financials` table) and /valuate becomes fully self-serve — no manual args.

Rough. To wire up: (1) add SCHEMA_SQL to Restocker_db.SCHEMA, (2) add the DB helpers to
Restocker_db.py, (3) add the command to a cog, (4) test on the live server.
"""

# ─────────────────────────────────────────────────────────────────────────────
# 1) SCHEMA — append to Restocker_db.SCHEMA
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS company_financials (
    market_id   TEXT NOT NULL,
    month       TEXT NOT NULL,               -- 'YYYY-MM'
    revenue     REAL NOT NULL DEFAULT 0,     -- TOTAL monthly revenue (all sources, not just CSN)
    profit      REAL NOT NULL DEFAULT 0,     -- monthly profit
    dividend    REAL NOT NULL DEFAULT 0,     -- dividend paid that month
    cash        REAL,                        -- cash & equivalents snapshot (optional; latest non-null wins)
    note        TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (market_id, month)
);
CREATE INDEX IF NOT EXISTS idx_company_financials_market ON company_financials(market_id);
"""

# ─────────────────────────────────────────────────────────────────────────────
# 2) DB HELPERS — add to Restocker_db.py
# ─────────────────────────────────────────────────────────────────────────────
def set_company_financials(market_id, month, revenue, profit=0.0, dividend=0.0,
                           cash=None, note=None):
    """Upsert one month of a company's total financials (used by /valuate)."""
    with db() as conn:  # noqa: F821  (db() is Restocker_db's context manager)
        conn.execute(
            "INSERT INTO company_financials (market_id, month, revenue, profit, dividend, cash, note, updated_at) "
            "VALUES (?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(market_id, month) DO UPDATE SET "
            "  revenue=excluded.revenue, profit=excluded.profit, dividend=excluded.dividend, "
            "  cash=COALESCE(excluded.cash, company_financials.cash), "
            "  note=excluded.note, updated_at=excluded.updated_at",
            (market_id, month, float(revenue), float(profit or 0), float(dividend or 0),
             (float(cash) if cash is not None else None), note))


def get_company_financials(market_id, months=6):
    """Most-recent months of financials for a market (newest first)."""
    with db() as conn:  # noqa: F821
        rows = conn.execute(
            "SELECT * FROM company_financials WHERE market_id=? ORDER BY month DESC LIMIT ?",
            (market_id, int(months))).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# 3) COMMAND — add to a cog (e.g. cogs/stock.py StockCog)
# ─────────────────────────────────────────────────────────────────────────────
_COMMAND_SKETCH = r'''
    @app_commands.command(name="set_financials",
        description="(Owner/Manager) Record a month's TOTAL revenue/profit/dividend/cash for /valuate")
    @app_commands.describe(
        market_id="Market this is for",
        revenue="TOTAL monthly revenue (all sources — more than CSN shop sales)",
        profit="Monthly profit",
        dividend="Dividend paid this month (0 if none)",
        cash="Cash & equivalents snapshot (optional)",
        month="YYYY-MM (default: current month)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def set_financials(self, interaction, market_id: str, revenue: float,
                             profit: float = 0.0, dividend: float = 0.0,
                             cash: Optional[float] = None, month: Optional[str] = None):
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)):
            return await interaction.response.send_message("Managers / market owner only.", ephemeral=True)
        import re as _re
        month = (month or "").strip() or __import__("datetime").datetime.utcnow().strftime("%Y-%m")
        if not _re.fullmatch(r"\d{4}-\d{2}", month):
            return await interaction.response.send_message("month must be YYYY-MM.", ephemeral=True)
        import Restocker_db as _db
        _db.set_company_financials(market_id, month, revenue, profit, dividend, cash)
        await interaction.response.send_message(
            f"✅ Recorded {market_id} {month}: revenue {revenue:,.0f}, profit {profit:,.0f}, "
            f"dividend {dividend:,.0f}" + (f", cash {cash:,.0f}" if cash is not None else "")
            + f".\nRun `/valuate market_id:{market_id}` — it now uses this automatically.",
            ephemeral=True)
'''

# ─────────────────────────────────────────────────────────────────────────────
# 4) WIRING — already done in value_company.py:
#    value_from_db() calls load_financials_from_db(), which reads this table and
#    supplies revenue/profit/dividend/cash. So after a few /set_financials entries,
#    `/valuate market_id:greyhames` needs NO manual args. Explicit args still override.
# ─────────────────────────────────────────────────────────────────────────────
