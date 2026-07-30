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
    ("Payment-proof archive channel", "PAYMENT_PROOF_CHANNEL_ID"),
    ("Fulfillment-proof archive channel", "FULFILL_PROOF_CHANNEL_ID"),
]
_GUILD_KEY = ("Funds-report guild", "FUNDS_REPORT_GUILD_ID")


class ConfigCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot






    # ── Who may talk to the bot's AI ─────────────────────────────────────────────





async def setup(bot):
    await bot.add_cog(ConfigCog(bot))
