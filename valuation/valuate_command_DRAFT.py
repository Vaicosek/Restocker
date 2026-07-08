"""
DRAFT — /valuate Discord command (ROUGH — needs integration + in-game testing).

This is a starting point, not finished. To wire it up later:
  1. Either load this file as its own cog, or move the `valuate` method into
     cogs/stock.py's StockCog.
  2. `valuation/` is already a package (__init__.py), so `from valuation.value_company
     import ...` works when the bot's cwd is the project root (/home/container).
  3. Test on the live server — the local restocker.db snapshot has no listed markets,
     so this can only be verified against real data.

It pulls shares / traded price / holders from restocker.db automatically and runs the
validated valuation engine (DCF + dividend DDM + market), returning the 3-lens result.

See TODO.md for what still needs doing before this is production-ready.
"""
import sys
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
_market_autocomplete = core._market_autocomplete


class ValuationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="valuate",
        description="(rough) Estimate a listed company's value — DCF + dividend + market")
    @app_commands.describe(
        market_id="Publicly-listed market to value",
        revenue="Forward monthly revenue from your earnings sheet (blank = CSN shop-sales proxy — undercounts)",
        cash="Cash & equivalents (default: last-known 4,311,630)",
        dividend="Recent monthly dividend run-rate (drives the dividend-floor lens)",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def valuate(self, interaction: discord.Interaction, market_id: str,
                      revenue: Optional[float] = None,
                      cash: Optional[float] = 4_311_630,
                      dividend: Optional[float] = None):
        # TODO: decide who can run this (managers/market owners only?). Open for now.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            from valuation.value_company import value_from_db, _fmt
        except Exception as e:
            return await interaction.followup.send(
                f"⚠️ valuation engine not importable ({e}). Make sure `valuation/` is at the "
                f"project root with __init__.py.", ephemeral=True)
        try:
            import Restocker_db as _db
            res, dbd = value_from_db(str(_db.DB_PATH), market_id, cash=cash,
                                     dividend_mo=dividend, fwd_rev=revenue)
        except SystemExit as e:      # engine's controlled "not listed / no revenue" errors
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ valuation failed: {e}", ephemeral=True)

        e = discord.Embed(title=f"📊 Valuation — {market_id}", color=0x22FF7A,
                          description="Rough estimate — three lenses on the same shares.")
        e.add_field(name="DCF · owner ceiling",
                    value=f"`{_fmt(res.dcf_equity)}` ¢\n`{_fmt(res.dcf_per_share)}`/share", inline=True)
        e.add_field(name="Dividend floor · DDM",
                    value=f"`{_fmt(res.ddm_value)}` ¢\n`{_fmt(res.ddm_per_share)}`/share", inline=True)
        if res.market_cap:
            e.add_field(name="Market · traded",
                        value=f"`{_fmt(res.market_cap)}` ¢\n`{_fmt(res.market_per_share)}`/share", inline=True)
        e.add_field(name="Fair value of a minority share",
                    value=f"`{_fmt(res.fair_minority_low)}` – `{_fmt(res.fair_minority_high)}` /share "
                          f"(dividend floor → DCF ceiling)", inline=False)
        if res.payout_pct is not None:
            e.add_field(name="Payout", value=f"{res.payout_pct:.0f}% of profit", inline=True)
        e.add_field(name="From DB",
                    value=f"{dbd['shares']:.0f} shares · {dbd['holders']} holders · traded {dbd['traded_price']}",
                    inline=False)
        if revenue is None:
            e.set_footer(text="⚠ used CSN shop-sales as a revenue proxy (undercounts) — pass revenue: "
                              "from your earnings sheet for a real figure.")
        await interaction.followup.send(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ValuationCog(bot))
