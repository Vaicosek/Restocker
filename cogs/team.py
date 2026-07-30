"""Manager teams (/team). Workers join a manager and register their EXACT in-game
name (IGN) - that IGN is what links them to CSN / chest-shop sales tracking, so the
manager's override and (later) sales sync can attribute activity to the right person.
The manager earns an override commission on their workers' order payouts."""
import re
import sys

import discord
from discord import app_commands
from discord.ext import commands

import Restocker_db as db

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
MANAGER_OVERRIDE_ORDER_PCT = core.MANAGER_OVERRIDE_ORDER_PCT
_owner_markets_for_user = core._owner_markets_for_user
_team_perf_embed = core._team_perf_embed
_all_teams_leaderboard = core._all_teams_leaderboard

_IGN_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def _team_name(manager_id) -> str:
    """A manager's chosen display name for their team, or '' if unset."""
    try:
        return (db.get_config(f"team_name:{manager_id}") or "").strip()
    except Exception:
        return ""


class TeamCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    team = app_commands.Group(name="team", description="Worker teams + manager overrides (synced to your in-game name)")


    @team.command(name="settings",
                  description="(Manager) TeamSettings — roster, add/remove, rename, leaderboard")
    async def team_settings(self, interaction: discord.Interaction):
        """One panel replacing add / remove / name / list / mine / leaderboard.
        `/me → Join a team` stays separate — that's the one workers run."""
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        from views.team_settings import TeamSettingsView, build_embed
        view = TeamSettingsView(interaction.user.id)
        await interaction.response.send_message(
            embed=build_embed(interaction.user.id), view=view, ephemeral=True)














async def setup(bot):
    await bot.add_cog(TeamCog(bot))
