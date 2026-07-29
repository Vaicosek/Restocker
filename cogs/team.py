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

    @team.command(name="join", description="Join a manager's team and register your EXACT in-game name")
    @app_commands.describe(manager="The manager whose team you're joining",
                           ign="Your EXACT Minecraft username (case-sensitive) - used to track your chest-shop sales")
    async def join(self, interaction: discord.Interaction, manager: discord.Member, ign: str):
        ign = ign.strip()
        if not _IGN_RE.match(ign):
            return await interaction.response.send_message(
                "Invalid IGN - must be 3-16 characters: letters, numbers, underscores.", ephemeral=True)
        if manager.bot or manager.id == interaction.user.id:
            return await interaction.response.send_message(
                "Pick a real manager (not yourself or a bot).", ephemeral=True)
        owner = db.get_user_id_by_ign(ign)
        if owner and str(owner) != str(interaction.user.id):
            return await interaction.response.send_message(
                f"IGN `{ign}` is already registered to someone else. Use your own exact name.", ephemeral=True)
        existing = db.get_manager_of(str(interaction.user.id))
        if existing and str(existing) != str(manager.id):
            return await interaction.response.send_message(
                f"You're already on <@{existing}>'s team - ask them to remove you in `/team settings` first.", ephemeral=True)
        # AUDIT FIX (high): money-bearing IGNs can't be self-claimed (anti-squatting).
        try:
            _pend_val = db.ign_unpaid_value(ign)
        except Exception:
            _pend_val = 0
        if _pend_val > 0:
            return await interaction.response.send_message(
                f"⚠️ `{ign}` has **{int(_pend_val):,}** coins of unpaid harvests waiting, so it "
                f"can't be self-claimed. Ask a manager to link it (they'll verify it's yours).",
                ephemeral=True)
        # AUDIT FIX (high): /team join bypassed the per-user IGN cap — re-running it
        # with different names let one account squat hundreds of IGNs preemptively.
        try:
            _max = int(getattr(core, "MAX_IGNS_PER_USER", 12) or 12)
            if db.count_igns(str(interaction.user.id)) >= _max and ign not in (db.get_igns(str(interaction.user.id)) or []):
                return await interaction.response.send_message(
                    f"❌ You've hit the max of **{_max}** in-game names. Ask a manager to "
                    f"unlink one you no longer use first.", ephemeral=True)
        except Exception:
            pass
        db.set_ign(str(interaction.user.id), ign)
        db.delete_ign_pending(str(interaction.user.id))   # registered now → cancel any pending
        # role-strip deadline (every registration path must clear this, or the deadline loop
        # would strip the role of someone who DID register)
        db.set_team_member(str(interaction.user.id), str(manager.id))
        await interaction.response.send_message(
            f"Joined {manager.mention}'s team as **{ign}**. Your orders (and tracked sales) now credit them.",
            ephemeral=True)
        try:
            await manager.send(f"{interaction.user.mention} (IGN `{ign}`) joined your team.")
        except Exception:
            pass

    @team.command(name="settings",
                  description="(Manager) TeamSettings — roster, add/remove, rename, leaderboard")
    async def team_settings(self, interaction: discord.Interaction):
        """One panel replacing add / remove / name / list / mine / leaderboard.
        `/team join` stays separate — that's the one workers run."""
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        from views.team_settings import TeamSettingsView, build_embed
        view = TeamSettingsView(interaction.user.id)
        await interaction.response.send_message(
            embed=build_embed(interaction.user.id), view=view, ephemeral=True)














async def setup(bot):
    await bot.add_cog(TeamCog(bot))
