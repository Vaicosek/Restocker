"""Brew-code alias commands (/brew)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
_load_brew_aliases = core._load_brew_aliases
_save_brew_aliases = core._save_brew_aliases
is_manager = core.is_manager

class BrewCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    brew = app_commands.Group(name="brew", description="Manage human-readable names for potion/brew codes like Potion#32L")

    @brew.command(name="set", description="Map a brew code to a human-readable name")


    @app_commands.describe(
        code="The raw code, e.g. Potion#32L",
        name="What this brew actually is, e.g. Speed II Potion",
    )
    async def brew_set(self, interaction: discord.Interaction, code: str, name: str):
        if not is_manager(interaction):
            return await interaction.response.send_message(
                "❌ Managers only.", ephemeral=True
            )
        code = code.strip()
        name = name.strip()
        if not code or not name:
            return await interaction.response.send_message(
                "❌ Both code and name must be non-empty.", ephemeral=True
            )
        aliases = _load_brew_aliases()
        old = aliases.get(code)
        aliases[code] = name
        _save_brew_aliases(aliases)
        if old:
            msg = f"✏️ Updated **{code}** → **{name}** (was: *{old}*)"
        else:
            msg = f"✅ Saved **{code}** → **{name}**"
        await interaction.response.send_message(msg, ephemeral=True)

    @brew.command(name="remove", description="Remove a brew code alias")


    @app_commands.describe(code="The raw code to un-map, e.g. Potion#32L")
    async def brew_remove(self, interaction: discord.Interaction, code: str):
        if not is_manager(interaction):
            return await interaction.response.send_message(
                "❌ Managers only.", ephemeral=True
            )
        code = code.strip()
        aliases = _load_brew_aliases()
        if code not in aliases:
            return await interaction.response.send_message(
                f"❌ No alias found for `{code}`.", ephemeral=True
            )
        removed = aliases.pop(code)
        _save_brew_aliases(aliases)
        await interaction.response.send_message(
            f"🗑️ Removed **{code}** (was: *{removed}*)", ephemeral=True
        )

    @brew.command(name="list", description="Show all brew code → name mappings")
    async def brew_list(self, interaction: discord.Interaction):
        aliases = _load_brew_aliases()
        if not aliases:
            return await interaction.response.send_message(
                "📭 No brew aliases set yet.\n"
                "Use `/brew set code:<Potion#32L> name:<Speed II Potion>` to add one.",
                ephemeral=True,
            )

        by_name: dict = {}
        for code, name in sorted(aliases.items(), key=lambda x: x[1].lower()):
            by_name.setdefault(name, []).append(code)

        lines = []
        for name, codes in sorted(by_name.items(), key=lambda x: x[0].lower()):
            code_str = " / ".join(f"`{c}`" for c in codes)
            lines.append(f"**{name}** ← {code_str}")

        embed = discord.Embed(
            title=f"🧪 Brew Aliases ({len(aliases)} codes → {len(by_name)} names)",
            color=0x9B59B6,
        )
        chunk, used = [], 0
        field_idx   = 1
        for line in lines:
            if used + len(line) + 1 > 1000:
                embed.add_field(name=f"Aliases (part {field_idx})", value="\n".join(chunk), inline=False)
                chunk, used, field_idx = [], 0, field_idx + 1
            chunk.append(line)
            used += len(line) + 1
        if chunk:
            label = "Aliases" if field_idx == 1 else f"Aliases (part {field_idx})"
            embed.add_field(name=label, value="\n".join(chunk), inline=False)

        embed.set_footer(text="Tip: one name can have multiple codes — they'll be merged in reports")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(BrewCog(bot))
