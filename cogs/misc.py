"""Misc / admin commands (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
AUTOROLE_CREATE_IF_MISSING = core.AUTOROLE_CREATE_IF_MISSING
CSN_HISTORY_FILE = core.CSN_HISTORY_FILE
CUSTOMER_ROLE_NAME = core.CUSTOMER_ROLE_NAME
DEFAULT_MARKET_ID = core.DEFAULT_MARKET_ID
MANAGER_ROLE_NAME = core.MANAGER_ROLE_NAME
PLATFORM_FEE_PCT = core.PLATFORM_FEE_PCT
_load_markets = core._load_markets
_save_markets = core._save_markets
_market_autocomplete = core._market_autocomplete
is_manager = core.is_manager
load_yaml = core.load_yaml
save_yaml = core.save_yaml
utcnow_iso = core.utcnow_iso


async def _my_market_autocomplete(interaction: discord.Interaction, current: str):
    """Managers see every market; anyone else sees only the
    markets whose leader role they actually hold (so owners only pick their own)."""
    data = _load_markets()
    markets = data.get("markets", {}) or {}
    mgr = is_manager(interaction)
    member = interaction.user
    guild = interaction.guild
    cur = (current or "").lower()
    out = []
    for k, v in markets.items():
        if not isinstance(v, dict):
            continue
        if not mgr:
            role_name = (v.get("discord_role_name") or "").strip()
            if not role_name or guild is None:
                continue
            role = discord.utils.get(guild.roles, name=role_name)
            if not (role and role in getattr(member, "roles", [])):
                continue
        name = v.get("name", k)
        if cur in k.lower() or cur in str(name).lower():
            out.append(app_commands.Choice(name=f"{name} [{k}]", value=k))
    return out[:25]


class MiscCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="website_login", description="Get a one-time code to log in on the dashboard website")
    async def website_login(self, interaction: discord.Interaction):
        import secrets as _secrets
        import string as _string
        import time as _time
        code = "".join(_secrets.choice(_string.ascii_uppercase + _string.digits) for _ in range(6))
        now = _time.time()
        codes = load_yaml("web_login_codes.yml", {}) or {}
        codes = {k: v for k, v in codes.items()
                 if isinstance(v, dict) and float(v.get("expires", 0)) > now}
        codes[code] = {
            "user_id": str(interaction.user.id),
            "name": interaction.user.display_name,
            "expires": now + 600,
        }
        save_yaml("web_login_codes.yml", codes)
        await interaction.response.send_message(
            f"🔐 Your website login code is **`{code}`**  (valid 10 minutes, one-time).\n"
            f"Open the dashboard, click **Log in**, and paste it to link your account.",
            ephemeral=True,
        )




async def setup(bot):
    await bot.add_cog(MiscCog(bot))
