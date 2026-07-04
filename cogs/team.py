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
                f"You're already on <@{existing}>'s team - ask them to `/team remove` you first.", ephemeral=True)
        db.set_ign(str(interaction.user.id), ign)
        db.set_team_member(str(interaction.user.id), str(manager.id))
        await interaction.response.send_message(
            f"Joined {manager.mention}'s team as **{ign}**. Your orders (and tracked sales) now credit them.",
            ephemeral=True)
        try:
            await manager.send(f"{interaction.user.mention} (IGN `{ign}`) joined your team.")
        except Exception:
            pass

    @team.command(name="add", description="(Manager) Add a worker to your team and link their in-game name")
    @app_commands.describe(
        worker="The worker to put under you",
        ign="Their EXACT Minecraft username — links their CSN sales/harvests to this Discord account (optional)")
    async def add(self, interaction: discord.Interaction, worker: discord.Member, ign: str = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        if worker.bot or worker.id == interaction.user.id:
            return await interaction.response.send_message("Pick a real worker (not yourself or a bot).", ephemeral=True)
        existing = db.get_manager_of(str(worker.id))
        if existing and str(existing) != str(interaction.user.id):
            return await interaction.response.send_message(
                f"{worker.mention} is already on <@{existing}>'s team.", ephemeral=True)

        # Optionally register the worker's IGN now — that's what links incoming CSN
        # "who sold what" rows (keyed by in-game name) back to this Discord account.
        ign_note = ""
        if ign is not None:
            ign = ign.strip()
            if not _IGN_RE.match(ign):
                return await interaction.response.send_message(
                    "Invalid IGN - must be 3-16 characters: letters, numbers, underscores.", ephemeral=True)
            owner = db.get_user_id_by_ign(ign)
            if owner and str(owner) != str(worker.id):
                return await interaction.response.send_message(
                    f"IGN `{ign}` is already linked to <@{owner}>. Use that worker's own exact name.",
                    ephemeral=True)
            db.set_ign(str(worker.id), ign)
            db.delete_ign_pending(str(worker.id))   # registered now → cancel any pending deadline
            ign_note = f"\nLinked in-game name **{ign}** → their CSN sales/harvests now credit {worker.mention}."

        db.set_team_member(str(worker.id), str(interaction.user.id))

        if not ign_note and not db.get_ign(str(worker.id)):
            ign_note = (f"\n⚠️ No in-game name linked yet — re-run `/team add` with the **ign** field "
                        f"(or have {worker.mention} run `/team join`), or their CSN sales can't be attributed.")

        await interaction.response.send_message(
            f"{worker.mention} is now on your team - you earn **{MANAGER_OVERRIDE_ORDER_PCT:g}%** "
            f"on their order payouts." + ign_note, ephemeral=True)

    @team.command(name="remove", description="(Manager) Remove a worker from your team")
    @app_commands.describe(worker="The worker to remove")
    async def remove(self, interaction: discord.Interaction, worker: discord.Member):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        mgr = db.get_manager_of(str(worker.id))
        if str(mgr) != str(interaction.user.id):
            return await interaction.response.send_message(f"{worker.mention} isn't on your team.", ephemeral=True)
        db.remove_team_member(str(worker.id))
        await interaction.response.send_message(f"Removed {worker.mention} from your team.", ephemeral=True)

    @team.command(name="list", description="(Manager) Show your team and their in-game names")
    async def list(self, interaction: discord.Interaction):
        members = db.get_team(str(interaction.user.id))
        if not members:
            return await interaction.response.send_message(
                "Your team is empty. Have workers run `/team join manager:@you ign:<name>`.", ephemeral=True)
        lines = []
        for w in members:
            ign = db.get_ign(w)
            lines.append(f"- <@{w}> - " + (f"`{ign}`" if ign else "no IGN set"))
        embed = discord.Embed(title=f"Your team ({len(members)})",
                              description="\n".join(lines), color=0x22FF7A)
        embed.set_footer(text=f"You earn {MANAGER_OVERRIDE_ORDER_PCT:g}% on their order payouts; IGNs link to CSN sales")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @team.command(name="mine", description="See who your manager is and your registered IGN")
    async def mine(self, interaction: discord.Interaction):
        mgr = db.get_manager_of(str(interaction.user.id))
        ign = db.get_ign(str(interaction.user.id))
        if not mgr:
            return await interaction.response.send_message(
                "You're not on anyone's team. Join with `/team join manager:@them ign:<name>`.", ephemeral=True)
        await interaction.response.send_message(
            f"Your manager is <@{mgr}>. Registered IGN: " + (f"**{ign}**" if ign else "none - set it with `/team join`."),
            ephemeral=True)

    @team.command(name="csn", description="(Manager) See your team's chest-shop sales (latest CSN month)")
    async def csn(self, interaction: discord.Interaction):
        members = db.get_team(str(interaction.user.id))
        if not members:
            return await interaction.response.send_message("Your team is empty.", ephemeral=True)
        lines = []
        grand = 0.0
        for w in members:
            ign = db.get_ign(w) or "no IGN"
            try:
                mids = _owner_markets_for_user(w)
            except Exception:
                mids = []
            wnet = 0.0
            latest = None
            for mid in mids:
                months = (db.csn_get_market(mid) or {}).get("months", {}) or {}
                if not months:
                    continue
                mk = max(months.keys())
                wnet += float(months[mk].get("net", 0) or 0)
                latest = mk if (latest is None or mk > latest) else latest
            grand += wnet
            tag = f" [{latest}]" if latest else ""
            body = f"net {wnet:,.0f}{tag}" if mids else "no shop linked"
            lines.append(f"- <@{w}> (`{ign}`) - {body}")
        embed = discord.Embed(title=f"Team CSN sales ({len(members)})",
                              description="\n".join(lines), color=0x22FF7A)
        embed.set_footer(text=f"Latest-month net per worker; team total {grand:,.0f}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


    @team.command(name="webhook", description="(Manager) Bind a Discord webhook for your team's performance feed")
    @app_commands.describe(url="A Discord webhook URL (Channel Settings -> Integrations -> Webhooks)")
    async def webhook(self, interaction: discord.Interaction, url: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        url = url.strip()
        if "/api/webhooks/" not in url or not url.lower().startswith("https://"):
            return await interaction.response.send_message(
                "That doesn't look like a Discord webhook URL (Channel Settings -> Integrations -> Webhooks).",
                ephemeral=True)
        db.set_team_settings(str(interaction.user.id), webhook_url=url)
        await interaction.response.send_message(
            "Webhook bound. Live events + the weekly digest for your team will post there.", ephemeral=True)

    @team.command(name="channel", description="(Manager) Bind a channel for your team's performance feed")
    @app_commands.describe(channel="Channel to post your team's performance into")
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        db.set_team_settings(str(interaction.user.id), channel_id=str(channel.id))
        await interaction.response.send_message(
            f"Bound to {channel.mention}. Live events + the weekly digest will post there.", ephemeral=True)

    @team.command(name="unbind", description="(Manager) Stop posting your team's performance anywhere")
    async def unbind(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        db.set_team_settings(str(interaction.user.id), webhook_url="", channel_id="")
        await interaction.response.send_message("Unbound. No more team performance posts.", ephemeral=True)

    @team.command(name="perf", description="Your team's performance leaderboard")
    @app_commands.describe(days="Days to look back (default 7)")
    async def perf(self, interaction: discord.Interaction, days: int = 7):
        days = max(1, min(int(days or 7), 365))
        embed = _team_perf_embed(str(interaction.user.id), days)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @team.command(name="leaderboard", description="See which teams are performing best (compete!)")
    @app_commands.describe(days="Days to look back (default 7)")
    async def leaderboard(self, interaction: discord.Interaction, days: int = 7):
        days = max(1, min(int(days or 7), 365))
        board = _all_teams_leaderboard(days)
        if not board:
            return await interaction.response.send_message("No team activity yet.", ephemeral=True)
        lines = []
        for i, tm in enumerate(board[:10], 1):
            medal = ["\U0001F947", "\U0001F948", "\U0001F949"][i - 1] if i <= 3 else f"{i}."
            lines.append(
                f"{medal} <@{tm['manager_id']}>'s team - **{int(tm['total']):,}c** "
                f"({tm['orders']} orders, sales {int(tm['sales_coins']):,}c)")
        embed = discord.Embed(title=f"\U0001F3C6 Team leaderboard - last {days}d",
                              description="\n".join(lines), color=0x22FF7A)
        await interaction.response.send_message(embed=embed)



async def setup(bot):
    await bot.add_cog(TeamCog(bot))
