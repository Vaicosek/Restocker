"""Enchant-area roster: which employees (by IGN) operate which enchant-table area.

Areas are your /la land areas. For now a manager supplies the IGNs of the people in
each enchant-table area manually; proper binding at onboarding comes later. This cog
is a thin manager-facing registry over Restocker_db's enchant_area_* helpers.

Nested-loaded from cogs/events.py's setup() so main.py's cog tuple stays untouched.
"""
import sys
import re as _re

import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
log = core.log


def _split_igns(raw: str) -> list[str]:
    """Accept IGNs separated by commas, spaces, newlines, or Discord mentions of names."""
    if not raw:
        return []
    parts = _re.split(r"[\s,;]+", raw.strip())
    seen, out = set(), []
    for p in parts:
        p = p.strip().lstrip("@")
        if not p:
            continue
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


async def _area_autocomplete(interaction: discord.Interaction, current: str):
    try:
        import Restocker_db as _db
        areas = sorted({r["area"] for r in _db.enchant_area_list()})
    except Exception:
        areas = []
    cur = (current or "").lower()
    return [app_commands.Choice(name=a, value=a) for a in areas if cur in a.lower()][:25]


class EnchantCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    enchant = app_commands.Group(
        name="enchant_area",
        description="(Managers) Roster of which employees operate which enchant-table area",
        default_permissions=discord.Permissions(manage_guild=True))

    @enchant.command(name="set", description="Bind one or more IGNs to an enchant-table area")
    @app_commands.describe(area="Area name (your /la land area, e.g. 'North Enchant')",
                           igns="IGNs in that area — separated by spaces, commas, or new lines")
    @app_commands.autocomplete(area=_area_autocomplete)
    async def set_area(self, interaction: discord.Interaction, area: str, igns: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        names = _split_igns(igns)
        if not names:
            return await interaction.response.send_message("❌ No IGNs found in that input.", ephemeral=True)
        import Restocker_db as _db
        added, existed, resolved = [], [], 0
        for ign in names:
            try:
                r = _db.enchant_area_add(area, ign, added_by=str(interaction.user.id))
            except Exception as e:
                log.error("[enchant] add failed for %s/%s: %s", area, ign, e)
                continue
            if r == "added":
                added.append(ign)
            elif r == "exists":
                existed.append(ign)
            if _db.get_user_id_by_ign(ign):
                resolved += 1
        area_disp = area.strip()
        lines = [f"🪄 **{area_disp}** enchant area updated."]
        if added:
            lines.append(f"➕ Added ({len(added)}): {', '.join(f'`{n}`' for n in added)}")
        if existed:
            lines.append(f"• Already listed ({len(existed)}): {', '.join(f'`{n}`' for n in existed)}")
        lines.append(f"🔗 {resolved}/{len(names)} IGNs matched a registered Discord account.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @enchant.command(name="list", description="Show enchant-area rosters (all areas, or one)")
    @app_commands.describe(area="Leave blank to list every area")
    @app_commands.autocomplete(area=_area_autocomplete)
    async def list_area(self, interaction: discord.Interaction, area: str = ""):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        rows = _db.enchant_area_list(area.strip() or None)
        if not rows:
            return await interaction.response.send_message(
                "No enchant-area bindings yet — add some with `/enchant_area set`.", ephemeral=True)
        by_area: dict[str, list[dict]] = {}
        for r in rows:
            by_area.setdefault(r["area"], []).append(r)
        embed = discord.Embed(title="🪄 Enchant-area roster", color=0x9b59b6)
        for a, members in sorted(by_area.items(), key=lambda kv: kv[0].lower()):
            val = []
            for m in members:
                tag = f" — <@{m['user_id']}>" if m.get("user_id") else " — _unregistered_"
                val.append(f"`{m['ign']}`{tag}")
            embed.add_field(name=f"{a}  ({len(members)})", value="\n".join(val)[:1024], inline=False)
        embed.set_footer(text="IGNs supplied manually · onboarding auto-binding to come")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @enchant.command(name="remove", description="Remove one IGN from an enchant area")
    @app_commands.describe(area="Area name", ign="The IGN to remove")
    @app_commands.autocomplete(area=_area_autocomplete)
    async def remove_area(self, interaction: discord.Interaction, area: str, ign: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        ok = _db.enchant_area_remove(area, ign)
        msg = (f"🗑 Removed `{ign}` from **{area.strip()}**." if ok
               else f"❌ `{ign}` was not listed in **{area.strip()}**.")
        await interaction.response.send_message(msg, ephemeral=True)

    @enchant.command(name="clear", description="Remove ALL IGNs from an enchant area")
    @app_commands.describe(area="Area name to clear")
    @app_commands.autocomplete(area=_area_autocomplete)
    async def clear_area(self, interaction: discord.Interaction, area: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        n = _db.enchant_area_clear(area)
        await interaction.response.send_message(
            f"🧹 Cleared **{area.strip()}** — removed {n} IGN(s).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(EnchantCog(bot))
