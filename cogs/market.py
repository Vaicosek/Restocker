"""Market management commands (/market)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional
import math
import os
import re

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
CSN_HISTORY_FILE = core.CSN_HISTORY_FILE
DEFAULT_MARKET_ID = core.DEFAULT_MARKET_ID
MIN_SHARE_PRICE = core.MIN_SHARE_PRICE
PLATFORM_FEE_PCT = core.PLATFORM_FEE_PCT
PLATFORM_FEE_ACTIVE = getattr(core, "PLATFORM_FEE_ACTIVE", False)
_MATPLOTLIB_OK = core._MATPLOTLIB_OK
_generate_earnings_chart = core._generate_earnings_chart
_get_market = core._get_market
_is_market_manager = core._is_market_manager
_is_market_owner = core._is_market_owner
_load_csn_for_market = core._load_csn_for_market
_load_markets = core._load_markets
_load_platform_balance = core._load_platform_balance
_log_manual_restock = core._log_manual_restock
_market_autocomplete = core._market_autocomplete
any_item_autocomplete = core.any_item_autocomplete
_market_loyalty_cfg = core._market_loyalty_cfg
_set_market_loyalty = core._set_market_loyalty
_markets_owned_by = core._markets_owned_by
_vtech_group_markets = core._vtech_group_markets
_set_vtech_group_markets = core._set_vtech_group_markets
_recompute_share_price = core._recompute_share_price
_remove_market_item = core._remove_market_item
_save_markets = core._save_markets
_suggest_item_price = core._suggest_item_price
add_coins = core.add_coins
io = core.io
is_manager = core.is_manager
load_yaml = core.load_yaml
log = core.log
save_yaml = core.save_yaml
utcnow_iso = core.utcnow_iso


class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    market = app_commands.Group(name="market", description="Manage multiple markets — register, track earnings, and configure per-market settings")

    @market.command(name="settings",
                    description="(Owner/Manager) MarketSettings — one panel for everything about a market")
    @app_commands.describe(market_id="Market to open on (blank = the first you can manage)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_settings(self, interaction: discord.Interaction, market_id: str = None):
        """One panel replacing edit/loyalty/set_*/add_manager/remove_manager/go_public/
        go_private/treasury/treasury_withdraw/remove_item/vtech_group/delete."""
        from views.market_settings import MarketSettingsView, build_embed, _may_manage
        mid = (market_id or "").strip()
        markets = (_load_markets().get("markets", {}) or {})
        if not mid:
            mine = [k for k in markets if _may_manage(interaction.user, k)]
            mid = mine[0] if mine else (next(iter(markets), DEFAULT_MARKET_ID))
        if mid not in markets:
            return await interaction.response.send_message(
                f"❌ Market `{mid}` not found.", ephemeral=True)
        if not _may_manage(interaction.user, mid):
            return await interaction.response.send_message(
                "⛔ You need to be this market's owner, a site manager, or a server manager.",
                ephemeral=True)
        view = MarketSettingsView(mid, interaction.user.id, interaction.user)
        await interaction.response.send_message(
            embed=await build_embed(mid, interaction.user), view=view, ephemeral=True)












    # Top-level (NOT under /market) — the /market group is at Discord's 25-subcommand cap,
    # and channel-binding is discoverable under a `/bind…` search here.
    @app_commands.command(
        name="bind_market",
        description="(Manager) Bind a Discord channel to a market so CSN reports route there (no code needed)",
    )
    @app_commands.describe(
        market_id="The market to bind",
        channel="The channel the CSN webhook posts in (defaults to the current channel)",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_set_channel(self,
        interaction: discord.Interaction,
        market_id: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        target = channel or interaction.channel
        if target is None:
            return await interaction.response.send_message(
                "❌ Couldn't determine a channel. Specify one with `channel:`.", ephemeral=True
            )

        data = _load_markets()
        markets = data.get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found.", ephemeral=True
            )

        for mid, m in markets.items():
            if mid != market_id and str(m.get("report_channel_id") or "") == str(target.id):
                return await interaction.response.send_message(
                    f"❌ {target.mention} is already bound to market `{mid}`. "
                    f"Unbind it there first or pick a different channel.",
                    ephemeral=True,
                )

        markets[market_id]["report_channel_id"] = str(target.id)
        _save_markets(data)
        await interaction.response.send_message(
            f"✅ CSN reports posted in {target.mention} will now record to "
            f"**{markets[market_id].get('name', market_id)}** (`{market_id}`).\n"
            f"No in-game Market Code is required for this market anymore — the channel identifies it.",
            ephemeral=True,
        )

    @app_commands.command(
        name="unbind_market",
        description="(Manager) Remove a market's channel binding",
    )
    @app_commands.describe(market_id="The market to unbind")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_unset_channel(self, interaction: discord.Interaction, market_id: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        data = _load_markets()
        markets = data.get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found.", ephemeral=True
            )
        try:
            import Restocker_db as _db_unbind
            with _db_unbind.db() as conn:
                conn.execute(
                    "UPDATE markets SET report_channel_id = NULL WHERE market_id = ?",
                    (market_id,),
                )
        except Exception as e:
            log.error("[market_unset_channel] failed: %s", e)
            return await interaction.response.send_message(
                "❌ Couldn't clear the binding — check the bot logs.", ephemeral=True
            )
        await interaction.response.send_message(
            f"✅ Channel binding removed for **{markets[market_id].get('name', market_id)}** "
            f"(`{market_id}`). It will fall back to the in-game verification code.",
            ephemeral=True,
        )












async def setup(bot):
    await bot.add_cog(MarketCog(bot))
