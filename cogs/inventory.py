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

    # /inventory retired — restock_deficit is the AI tool create_restock_orders,
    # and live barrel stock is on the dashboard Inventory page.




async def setup(bot):
    await bot.add_cog(InventoryCog(bot))
