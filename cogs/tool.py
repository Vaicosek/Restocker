"""Tool/equipment alias commands (/tool) — link raw CSN tool codes like
'Diamond Pickaxe#ahc' to a clean display name, exactly like /brew links potions.
Shares the same alias store, so the names resolve everywhere CSN reports do."""
import sys

import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
_load_brew_aliases = core._load_brew_aliases
_save_brew_aliases = core._save_brew_aliases
is_manager = core.is_manager


class ToolCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    tool = app_commands.Group(name="tool",
                              description="Name tool/equipment codes like Diamond Pickaxe#ahc")

    @tool.command(name="set", description="Map a tool code to a clean name")
    @app_commands.describe(code="The raw code, e.g. Diamond Pickaxe#ahc",
                           name="What it is, e.g. Diamond Pickaxe (Efficiency V)")
    async def tool_set(self, interaction: discord.Interaction, code: str, name: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("❌ Managers only.", ephemeral=True)
        code = code.strip(); name = name.strip()
        if not code or not name:
            return await interaction.response.send_message("❌ Both code and name must be non-empty.", ephemeral=True)
        aliases = _load_brew_aliases()
        old = aliases.get(code)
        aliases[code] = name
        _save_brew_aliases(aliases)
        msg = (f"✏️ Updated **{code}** → **{name}** (was: *{old}*)" if old
               else f"✅ Linked **{code}** → **{name}**")
        await interaction.response.send_message(msg, ephemeral=True)

    @tool.command(name="remove", description="Remove a tool code alias")
    @app_commands.describe(code="The raw code to un-map")
    async def tool_remove(self, interaction: discord.Interaction, code: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("❌ Managers only.", ephemeral=True)
        code = code.strip()
        aliases = _load_brew_aliases()
        if code not in aliases:
            return await interaction.response.send_message(f"❌ No alias found for `{code}`.", ephemeral=True)
        removed = aliases.pop(code)
        _save_brew_aliases(aliases)
        await interaction.response.send_message(f"🗑️ Removed **{code}** (was: *{removed}*)", ephemeral=True)

    @tool.command(name="list", description="Show all code → name links (tools + brews)")
    async def tool_list(self, interaction: discord.Interaction):
        aliases = _load_brew_aliases()
        if not aliases:
            return await interaction.response.send_message(
                "📭 No aliases yet. Use `/tool set code:<Diamond Pickaxe#ahc> name:<Diamond Pickaxe Eff V>`.",
                ephemeral=True)
        by_name: dict = {}
        for code, name in sorted(aliases.items(), key=lambda x: x[1].lower()):
            by_name.setdefault(name, []).append(code)
        lines = [f"**{name}** ← " + " / ".join(f"`{c}`" for c in codes)
                 for name, codes in sorted(by_name.items(), key=lambda x: x[0].lower())]
        embed = discord.Embed(title=f"🔧 Item aliases ({len(aliases)} codes → {len(by_name)} names)",
                              color=0x9B59B6)
        chunk, used, idx = [], 0, 1
        for line in lines:
            if used + len(line) + 1 > 1000:
                embed.add_field(name=f"Aliases (part {idx})", value="\n".join(chunk), inline=False)
                chunk, used, idx = [], 0, idx + 1
            chunk.append(line); used += len(line) + 1
        if chunk:
            embed.add_field(name=("Aliases" if idx == 1 else f"Aliases (part {idx})"),
                            value="\n".join(chunk), inline=False)
        embed.set_footer(text="Shared with /brew — one name can have multiple codes; merged in reports")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ToolCog(bot))
