"""Loyalty cog — points, tiers, leaderboard, IGN registration + link audit.

First extracted cog (pilot for the module split). Shared helpers/config are bound
from the running core module via sys.modules, so this works whether the bot is
launched as `python Restocker_main.py` (module __main__) or imported under its
own name — no double-import, no startup-command change required.
"""
import sys
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

# Bind to the already-loaded core module (the running Restocker_main).
core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager             = core.is_manager
_loyalty_tier          = core._loyalty_tier
LOYALTY_TIERS          = core.LOYALTY_TIERS
_award_loyalty_points  = core._award_loyalty_points
LOYALTY_EMPLOYEE_ROLES = core.LOYALTY_EMPLOYEE_ROLES

TIER_EMOJIS = {1: "🪨", 2: "🔨", 3: "⚔️", 4: "💎", 5: "👑"}

_IGN_RE = r"^[A-Za-z0-9_]{3,16}$"


def _mention_list(members: list, cap: int = 30) -> str:
    shown = ", ".join(m.mention for m in members[:cap])
    extra = len(members) - cap
    return shown + (f" … +{extra} more" if extra > 0 else "")


class LoyaltyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    loyalty = app_commands.Group(name="loyalty", description="Loyalty points, tiers, and rewards")

    @loyalty.command(name="stats", description="View your loyalty stats and tier")
    @app_commands.describe(user="View another user's stats (managers only)")
    async def loyalty_stats(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user
        if user and user != interaction.user and not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only for other users.", ephemeral=True)
        import Restocker_db as _db_ls
        rec = _db_ls.get_loyalty(str(target.id))
        pts = float(rec.get("points", 0))
        total = float(rec.get("total_earned", 0))
        tier = _loyalty_tier(pts)
        next_tier = next((t for t in LOYALTY_TIERS if t["min_pts"] > pts), None)
        ign = _db_ls.get_ign(str(target.id)) or "*Not registered*"

        embed = discord.Embed(
            title=f"{TIER_EMOJIS.get(tier['tier'], '⭐')} {target.display_name} — {tier['name']}",
            color=0xF1C40F
        )
        embed.add_field(name="Points", value=f"`{pts:,.0f}`", inline=True)
        embed.add_field(name="All-time Earned", value=f"`{total:,.0f}`", inline=True)
        embed.add_field(name="IGN", value=f"`{ign}`", inline=True)
        embed.add_field(name="Interest Rate", value=f"`{tier['interest_weekly_pct']}%/week`", inline=True)
        embed.add_field(name="Payout Bonus", value=f"`+{tier['payout_bonus_pct']}%`", inline=True)
        if next_tier:
            needed = next_tier["min_pts"] - pts
            embed.add_field(name=f"Next: {TIER_EMOJIS.get(next_tier['tier'],'')} {next_tier['name']}",
                            value=f"`{needed:,.0f}` pts away", inline=True)
        else:
            embed.add_field(name="Tier", value="🏆 Max tier reached!", inline=True)

        tiers_str = "\n".join(
            f"{'→' if t['tier'] == tier['tier'] else '  '} {TIER_EMOJIS.get(t['tier'],'')} **{t['name']}** — "
            f"{t['min_pts']:,} pts · {t['interest_weekly_pct']}%/wk · +{t['payout_bonus_pct']}% payout"
            for t in LOYALTY_TIERS
        )
        embed.add_field(name="All Tiers", value=tiers_str, inline=False)
        await interaction.response.send_message(embed=embed)

    @loyalty.command(name="leaderboard", description="Top loyalty point holders")
    async def loyalty_leaderboard(self, interaction: discord.Interaction):
        import Restocker_db as _db_lb
        rows = _db_lb.get_loyalty_leaderboard(15)
        if not rows:
            return await interaction.response.send_message("No loyalty data yet.", ephemeral=True)
        lines = []
        for i, row in enumerate(rows, 1):
            uid = row["user_id"]
            pts = float(row.get("points", 0))
            tier = _loyalty_tier(pts)
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
            ign = _db_lb.get_ign(uid) or "—"
            lines.append(f"{medal} <@{uid}> (`{ign}`) — **{pts:,.0f}** pts {TIER_EMOJIS.get(tier['tier'],'')} {tier['name']}")
        embed = discord.Embed(title="🏆 Loyalty Leaderboard", description="\n".join(lines), color=0xF1C40F)
        await interaction.response.send_message(embed=embed)

    @loyalty.command(name="register_ign", description="Register your Minecraft in-game username")
    @app_commands.describe(ign="Your Minecraft username (exact, case-sensitive)")
    async def loyalty_register_ign(self, interaction: discord.Interaction, ign: str):
        import re as _re2, Restocker_db as _db_ri
        if not _re2.match(r"^[A-Za-z0-9_]{3,16}$", ign):
            return await interaction.response.send_message(
                "❌ Invalid IGN. Must be 3-16 characters: letters, numbers, underscores.", ephemeral=True)
        existing = _db_ri.get_user_id_by_ign(ign)
        if existing and existing != str(interaction.user.id):
            return await interaction.response.send_message(
                f"❌ `{ign}` is already registered to someone else.", ephemeral=True)
        _db_ri.set_ign(str(interaction.user.id), ign)
        _db_ri.delete_ign_pending(str(interaction.user.id))
        await interaction.response.send_message(f"✅ IGN **{ign}** registered! You're all set.", ephemeral=True)

    @loyalty.command(name="unlinked", description="(Manager) List employees who haven't linked their Minecraft IGN")
    async def loyalty_unlinked(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Run this in a server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        # Make sure the full member list is cached (members intent is on).
        members = guild.members
        if not guild.chunked:
            try:
                members = await guild.chunk()
            except Exception:
                members = guild.members

        import Restocker_db as _db_ul
        employees = [m for m in members if not m.bot
                     and any(r.name in LOYALTY_EMPLOYEE_ROLES for r in m.roles)]
        linked, pending, unlinked = [], [], []
        for m in employees:
            if _db_ul.get_ign(str(m.id)):
                linked.append(m)
            elif _db_ul.get_ign_pending(str(m.id)):
                pending.append(m)
            else:
                unlinked.append(m)

        embed = discord.Embed(
            title="🔗 IGN Link Status — all employees",
            description=(f"Scanned **{len(employees)}** members holding an employee role "
                         f"({', '.join(sorted(LOYALTY_EMPLOYEE_ROLES))})."),
            color=0xE74C3C if unlinked else 0x2ECC71,
        )
        embed.add_field(name=f"✅ Linked ({len(linked)})",
                        value=_mention_list(linked) if linked else "*none*", inline=False)
        if pending:
            embed.add_field(name=f"⏳ Prompted, awaiting reply ({len(pending)})",
                            value=_mention_list(pending), inline=False)
        embed.add_field(name=f"❌ Not linked ({len(unlinked)})",
                        value=_mention_list(unlinked) if unlinked else "*none — everyone is linked!* 🎉",
                        inline=False)
        if unlinked:
            embed.set_footer(text="Unlinked employees' CSN sales credit NO ONE. "
                                  "Link them with /loyalty link <user> <ign>.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @loyalty.command(name="link", description="(Manager) Link a member to their Minecraft IGN")
    @app_commands.describe(user="The Discord member", ign="Their EXACT Minecraft username (case-sensitive)")
    async def loyalty_link(self, interaction: discord.Interaction, user: discord.Member, ign: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import re as _re3, Restocker_db as _db_lk
        ign = ign.strip()
        if not _re3.match(_IGN_RE, ign):
            return await interaction.response.send_message(
                "❌ Invalid IGN. Must be 3-16 characters: letters, numbers, underscores.", ephemeral=True)
        owner = _db_lk.get_user_id_by_ign(ign)
        if owner and owner != str(user.id):
            return await interaction.response.send_message(
                f"❌ `{ign}` is already linked to <@{owner}>. Unlink them first if this is a mistake.",
                ephemeral=True)
        old = _db_lk.get_ign(str(user.id))
        _db_lk.set_ign(str(user.id), ign)
        _db_lk.delete_ign_pending(str(user.id))
        note = f" (was `{old}`)" if old and old != ign else ""
        await interaction.response.send_message(
            f"🔗 Linked {user.mention} → **{ign}**{note}. Their CSN sales now credit them.")

    @loyalty.command(name="unlink", description="(Manager) Remove a member's IGN link")
    @app_commands.describe(user="The Discord member to unlink")
    async def loyalty_unlink(self, interaction: discord.Interaction, user: discord.Member):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db_ulk
        old = _db_ulk.get_ign(str(user.id))
        if not old:
            return await interaction.response.send_message(
                f"{user.mention} has no IGN linked.", ephemeral=True)
        _db_ulk.delete_ign(str(user.id))
        await interaction.response.send_message(f"🔓 Unlinked {user.mention} (was `{old}`).")

    @loyalty.command(name="set_points", description="(Manager) Manually set a user's loyalty points")
    @app_commands.describe(user="The user", points="New point total")
    async def loyalty_set_points(self, interaction: discord.Interaction, user: discord.Member, points: float):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db_sp
        new = _db_sp.set_loyalty_points(str(user.id), points)
        tier = _loyalty_tier(new)
        await interaction.response.send_message(
            f"✅ Set **{user.display_name}**'s loyalty to `{new:,.0f}` pts — {TIER_EMOJIS.get(tier['tier'],'')} **{tier['name']}**"
        )

    @loyalty.command(name="add_points", description="(Manager) Add loyalty points to a user")
    @app_commands.describe(user="The user", points="Points to add", reason="Reason")
    async def loyalty_add_points(self, interaction: discord.Interaction, user: discord.Member, points: float, reason: str = "manual"):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        new_total, old_tier, new_tier = _award_loyalty_points(user.id, int(points), reason=reason)
        tier_up = f" 🏆 Tier up to **{new_tier['name']}**!" if new_tier["tier"] > old_tier["tier"] else ""
        await interaction.response.send_message(
            f"✅ Added **{points:,.0f}** pts to {user.mention} → `{new_total:,.0f}` total{tier_up}"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LoyaltyCog(bot))
