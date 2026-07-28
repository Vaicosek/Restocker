"""Live inventory: barrel fullness, capacity, deficit restock, and owner stock alarms.
Split out of /market because Discord caps a command group at 25 subcommands."""
import sys

import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
_is_market_owner = core._is_market_owner
_get_market = core._get_market
_market_autocomplete = core._market_autocomplete
_fullness_bar = core._fullness_bar
_create_restock_orders = core._create_restock_orders
_load_items = core._load_items
STOCK_LOW_PCT = core.STOCK_LOW_PCT


class InventoryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    inventory = app_commands.Group(name="inventory",
                                   description="Live barrel stock: fullness, capacity, deficit restock, low-stock alarms")

    @inventory.command(name="restock_deficit",
                    description="(Manager) Create restock orders from the real shortfall (capacity - current stock)")
    @app_commands.describe(market_id="Market", min_deficit="Only items short by at least this many pieces")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def restock_deficit(self, interaction: discord.Interaction, market_id: str, min_deficit: int = 1):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        import Restocker_db as _db
        st = _db.get_market_stock(market_id)
        if not st:
            return await interaction.response.send_message(f"No live stock for `{market_id}`.", ephemeral=True)
        known = (_load_items().get("items") or {})
        to_order = []
        skipped = 0
        for it, x in st.items():
            deficit = int(x.get("capacity") or 0) - int(x.get("stock") or 0)
            if deficit < max(1, int(min_deficit)):
                continue
            if it not in known:
                skipped += 1
                continue
            to_order.append((it, deficit, known[it]))
        if not to_order:
            return await interaction.response.send_message(
                f"Nothing short by >= {min_deficit} for `{market_id}`"
                + (f" ({skipped} not in catalog)." if skipped else "."), ephemeral=True)
        created = _create_restock_orders(to_order)
        top = ", ".join(f"{it} ({d:,})" for it, d, _ in sorted(to_order, key=lambda r: -r[1])[:8])
        await interaction.response.send_message(
            f"Created **{created}** restock order(s) from real deficit for `{market_id}`."
            + (f" {skipped} item(s) skipped (not in catalog)." if skipped else "")
            + f"\nTop shortfalls: {top}", ephemeral=True)



async def setup(bot):
    await bot.add_cog(InventoryCog(bot))
