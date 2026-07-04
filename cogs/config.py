"""Runtime configuration (/config) — rebind the server-specific channel / category /
guild IDs for the server the bot is actually running on, without editing .env.

Overrides are stored in the DB (bot_config) and applied at startup by
Restocker_main._apply_config_overrides(). Changing one live updates main's own
reads immediately; a restart fully propagates it to every cog/view (they cache
these IDs at load time)."""
import sys

import discord
from discord import app_commands
from discord.ext import commands

import Restocker_db as db

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
log = core.log

# (friendly name, DB key / module constant) for the channel-type IDs.
_CHANNEL_KEYS = [
    ("Worker order-card channel", "WORKER_CHANNEL_ID"),
    ("Welcome channel",           "WELCOME_CHANNEL_ID"),
    ("Tickets category",          "TICKETS_CATEGORY_ID"),
    ("Funds-report channel",      "FUNDS_REPORT_CHANNEL_ID"),
    ("Web-orders channel",        "WEB_ORDERS_CHANNEL_ID"),
    ("CSN-report channel",        "CSN_REPORT_CHANNEL_ID"),
]
_GUILD_KEY = ("Funds-report guild", "FUNDS_REPORT_GUILD_ID")


class ConfigCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    config = app_commands.Group(
        name="config",
        description="(Managers) Rebind channels/category/guild for this server",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @config.command(name="set_channel", description="Point a bot channel/category at a channel on THIS server")
    @app_commands.describe(which="Which channel/category to bind", channel="Target channel (use a category for Tickets)")
    @app_commands.choices(which=[app_commands.Choice(name=n, value=k) for (n, k) in _CHANNEL_KEYS])
    async def set_channel(self, interaction: discord.Interaction,
                          which: app_commands.Choice[str], channel: discord.abc.GuildChannel):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        key = which.value
        db.set_config(key, int(channel.id))
        try:
            setattr(core, key, int(channel.id))   # live update for main's own reads
        except Exception:
            pass
        await interaction.response.send_message(
            f"✅ **{which.name}** → {channel.mention} (`{channel.id}`).\n"
            f"⚠️ Restart the bot to fully apply (cogs/views cache these at load).",
            ephemeral=True,
        )

    @config.command(name="set_guild", description="Set the funds-report guild (defaults to this server)")
    @app_commands.describe(guild_id="Guild ID — leave empty to use this server")
    async def set_guild(self, interaction: discord.Interaction, guild_id: str = ""):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        gid = (guild_id or "").strip() or str(interaction.guild_id or "")
        if not gid.isdigit():
            return await interaction.response.send_message("❌ Invalid guild id.", ephemeral=True)
        db.set_config("FUNDS_REPORT_GUILD_ID", gid)
        try:
            setattr(core, "FUNDS_REPORT_GUILD_ID", int(gid))
        except Exception:
            pass
        await interaction.response.send_message(
            f"✅ Funds-report guild → `{gid}`. Restart to fully apply.", ephemeral=True)

    @config.command(name="show", description="Show current channel/guild config (override vs .env default)")
    async def show(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        lines = []
        for n, k in _CHANNEL_KEYS + [_GUILD_KEY]:
            cur = getattr(core, k, None)
            ov = db.get_config(k)
            src = "DB override" if ov not in (None, "") else ".env / default"
            if k.endswith("CHANNEL_ID") and cur:
                disp = f"<#{cur}>"
            else:
                disp = f"`{cur}`"
            lines.append(f"**{n}** — {disp}  ·  _{src}_")
        embed = discord.Embed(title="⚙️ Channel configuration",
                              description="\n".join(lines), color=0x22FF7A)
        embed.set_footer(text="Set with /config set_channel · changes apply fully on restart")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config.command(name="reset", description="Clear a DB override (revert to .env default on next restart)")
    @app_commands.describe(which="Which override to clear")
    @app_commands.choices(which=[app_commands.Choice(name=n, value=k) for (n, k) in _CHANNEL_KEYS + [_GUILD_KEY]])
    async def reset(self, interaction: discord.Interaction, which: app_commands.Choice[str]):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        db.delete_config(which.value)
        await interaction.response.send_message(
            f"✅ Cleared **{which.name}** override. Restart to revert to the .env default.", ephemeral=True)

    # ── AI allow-list (who may @mention the bot's AI) — live, no restart needed ──
    ai_allow = app_commands.Group(
        name="ai_allow",
        description="(Managers) Manage who may @mention the bot's AI",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @ai_allow.command(name="add", description="Allow a user to use the bot's AI by @mention")
    @app_commands.describe(user="The user to grant AI chat access")
    async def ai_allow_add(self, interaction: discord.Interaction, user: discord.User):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        r = core._ai_allow_add(user.id)
        if r == "added":
            msg = (f"✅ {user.mention} can now @mention the AI — effective immediately, no restart.\n"
                   f"(This is chat access only; mutating actions still need a manager role.)")
        elif r == "already":
            msg = f"ℹ️ {user.mention} is already allowed."
        else:
            msg = "❌ Invalid user."
        await interaction.response.send_message(msg, ephemeral=True)

    @ai_allow.command(name="remove", description="Revoke a user's AI access")
    @app_commands.describe(user="The user to remove")
    async def ai_allow_remove(self, interaction: discord.Interaction, user: discord.User):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        r = core._ai_allow_remove(user.id)
        if r == "removed":
            msg = f"✅ {user.mention} can no longer use the AI."
        elif r == "env":
            msg = (f"⚠️ {user.mention} is allow-listed in the server `.env` (AI_ALLOWED_USER_IDS), "
                   f"so I can't drop them here — remove them from `.env` and restart.")
        else:
            msg = f"ℹ️ {user.mention} wasn't on the runtime allow-list."
        await interaction.response.send_message(msg, ephemeral=True)

    @ai_allow.command(name="list", description="Show who may use the bot's AI")
    async def ai_allow_list(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        env_ids = sorted(core._AI_ALLOWED_ENV_IDS)
        db_ids = sorted(core._ai_allowed_db_ids())
        blocks = []
        if env_ids:
            blocks.append("**From `.env` (permanent):**\n"
                          + "\n".join(f"• <@{i}> (`{i}`)" for i in env_ids))
        if db_ids:
            blocks.append("**Added via /ai_allow (live):**\n"
                          + "\n".join(f"• <@{i}> (`{i}`)" for i in db_ids))
        embed = discord.Embed(
            title="🤖 AI-allowed users",
            description="\n\n".join(blocks) or "No one is allowed yet.",
            color=0x22FF7A,
        )
        embed.set_footer(text="These IDs may @mention the AI. Actions are still gated by manager roles.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ConfigCog(bot))
