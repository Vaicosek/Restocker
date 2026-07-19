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
log = core.log
order_id_autocomplete = core.order_id_autocomplete

# AUDIT FIX (high): /config, /network and /ai_allow rebind server-critical channel
# IDs and AI access — but "manager" was checked per-guild, so an admin of ANY guild
# the bot got invited to could re-point the funds report or worker cards to their
# own server. Every command in this cog is now pinned to the home guild.
import os as _os
_HOME_GUILD_ID = int(_os.getenv("HOME_GUILD_ID", "954487497411403806") or 0)
_core_is_manager = core.is_manager


def is_manager(interaction) -> bool:
    if _HOME_GUILD_ID and getattr(interaction, "guild_id", None) != _HOME_GUILD_ID:
        return False
    return _core_is_manager(interaction)

# (friendly name, DB key / module constant) for the channel-type IDs.
_CHANNEL_KEYS = [
    ("Worker order-card channel", "WORKER_CHANNEL_ID"),
    ("Welcome channel",           "WELCOME_CHANNEL_ID"),
    ("Tickets category",          "TICKETS_CATEGORY_ID"),
    ("Funds-report channel",      "FUNDS_REPORT_CHANNEL_ID"),
    ("Web-orders channel",        "WEB_ORDERS_CHANNEL_ID"),
    ("Futures approval channel",  "FUTURES_CHANNEL_ID"),
    ("CSN-report channel",        "CSN_REPORT_CHANNEL_ID"),
    ("Trade-network forum channel", "NETWORK_FORUM_CHANNEL_ID"),
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

    # ── SW Trade Network broadcast (invite / toggle / manual post) — live, no restart ──
    network = app_commands.Group(
        name="network",
        description="(Managers) SW Trade Network cross-server order broadcasting",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @network.command(name="invite", description="Set the Discord invite workers use to claim network orders")
    @app_commands.describe(url="A discord.gg/… invite to your server (leave empty to clear)")
    async def network_invite(self, interaction: discord.Interaction, url: str = ""):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        url = (url or "").strip()
        db.set_config("NETWORK_INVITE_URL", url)
        try:
            setattr(core, "NETWORK_INVITE_URL", url)
        except Exception:
            pass
        await interaction.response.send_message(
            (f"✅ Network claim invite → {url}" if url else "✅ Cleared the network claim invite."),
            ephemeral=True)

    @network.command(name="autopost", description="Turn auto-posting new orders to the trade network on/off")
    @app_commands.describe(enabled="True = auto-post every new order; False = off")
    async def network_autopost(self, interaction: discord.Interaction, enabled: bool):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        db.set_config("NETWORK_AUTOPOST", "true" if enabled else "false")
        try:
            setattr(core, "NETWORK_AUTOPOST", bool(enabled))
        except Exception:
            pass
        warn = "" if getattr(core, "NETWORK_FORUM_CHANNEL_ID", 0) else \
            "\n⚠️ No forum channel set yet — use `/config set_channel` → *Trade-network forum channel*."
        await interaction.response.send_message(
            f"✅ Trade-network auto-post **{'ON' if enabled else 'OFF'}**.{warn}", ephemeral=True)

    @network.command(name="post",
                     description="Post the consolidated open-orders batch to the trade network now (or one order to test)")
    @app_commands.describe(order_id="Optional: a single order ID to test-post. Leave empty to post the full open-orders batch.")
    @app_commands.autocomplete(order_id=order_id_autocomplete)
    async def network_post(self, interaction: discord.Interaction, order_id: int = 0):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        if not getattr(core, "NETWORK_FORUM_CHANNEL_ID", 0):
            return await interaction.response.send_message(
                "❌ Set the forum channel first: `/config set_channel` → *Trade-network forum channel*.",
                ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        # No id → post the whole consolidated batch (bypass the throttle for a manual push).
        if not order_id:
            try:
                posted, note = await core._post_orders_batch_to_network(self.bot, force=True)
            except Exception as e:
                return await interaction.followup.send(f"⚠️ Post failed: {e}", ephemeral=True)
            return await interaction.followup.send(
                (f"✅ {note}" if posted else f"ℹ️ {note}"), ephemeral=True)
        # Otherwise test-post that single order.
        try:
            data = core.load_orders()
            order = next((o for o in (data.get("orders", []) or [])
                          if int(o.get("id", 0) or 0) == int(order_id)), None)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Couldn't load orders: {e}", ephemeral=True)
        if not order:
            return await interaction.followup.send(f"❌ No order #{order_id}.", ephemeral=True)
        try:
            await core._post_order_to_network(self.bot, order)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Post failed: {e}", ephemeral=True)
        await interaction.followup.send(
            f"✅ Test-posted order #{order_id} to the trade-network forum.", ephemeral=True)

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
