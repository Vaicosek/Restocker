"""Team projects — fixed-budget tasking. A funder hands a manager a budget to "make
something happen"; the manager pays out their team from it and keeps whatever's left.
No escrow / approval / shares — the manager has full discretion (that's the point).
Paying team members (loyalty points + leaderboard credit) is done from the Manager Panel;
every coin move is recorded to the coin ledger."""
import sys

import discord
from discord import app_commands
from discord.ext import commands

import Restocker_db as db

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
add_coins = core.add_coins
deduct_coins = core.deduct_coins
_award_loyalty_points = core._award_loyalty_points
bot = core.bot


class ProjectsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot





async def setup(bot):
    await bot.add_cog(ProjectsCog(bot))
