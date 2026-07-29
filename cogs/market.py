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
        view = MarketSettingsView(mid, interaction.user.id)
        await interaction.response.send_message(
            embed=await build_embed(mid, interaction.user), view=view, ephemeral=True)



    @market.command(name="add", description="(Manager) Register a new market")
    @app_commands.describe(
        market_id="Short unique ID for this market (e.g. sapidorf, amazonia)",
        name="Display name (e.g. 'Sapidorf Market')",
        owner="The Discord user who owns/operates this market (optional)",
        fee_pct="Platform fee % on this market's earnings. Default: 3.0",
    )
    async def market_add(self, 
        interaction: discord.Interaction,
        market_id: str,
        name: str,
        owner: Optional[discord.Member] = None,
        fee_pct: app_commands.Range[float, 0.0, 50.0] = PLATFORM_FEE_PCT,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        market_id = market_id.lower().strip()
        if not re.match(r"^[a-z0-9_-]{1,32}$", market_id):
            return await interaction.response.send_message(
                "❌ Market ID must be lowercase letters, digits, hyphens, or underscores only (max 32 chars).",
                ephemeral=True,
            )

        data = _load_markets()
        markets = data.setdefault("markets", {})
        if market_id in markets:
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` already exists. Use `/market info` to view it.", ephemeral=True
            )

        csn_file = CSN_HISTORY_FILE if market_id == DEFAULT_MARKET_ID else f"csn_history_{market_id}.yml"
        markets[market_id] = {
            "name":              name.strip(),
            "owner_id":          owner.id if owner else None,
            "manager_ids":       [],
            "platform_fee_pct":  round(fee_pct, 4),
            "csn_history_file":  csn_file,
            "active":            True,
            "created_at":        utcnow_iso(),
            "created_by":        interaction.user.id,
        }
        _save_markets(data)

        embed = discord.Embed(title="🏪 Market Registered", color=0x2ECC71)
        embed.add_field(name="Market ID", value=f"`{market_id}`", inline=True)
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="Owner", value=owner.mention if owner else "*None set*", inline=True)
        embed.add_field(name="Platform Fee", value=f"`{fee_pct}%`", inline=True)
        embed.add_field(name="CSN History File", value=f"`{csn_file}`", inline=True)
        embed.set_footer(text=f"Use /csn market_id:{market_id} to record sales data for this market.")
        await interaction.response.send_message(embed=embed)


    @market.command(name="info", description="View details and earnings summary for a market")
    @app_commands.describe(market_id="The market to view")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_info(self, interaction: discord.Interaction, market_id: str):
        m = _get_market(market_id)
        if m is None:
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found. Use `/market list` to see registered markets.",
                ephemeral=True,
            )
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)
                or _is_market_manager(interaction, market_id)):
            return await interaction.response.send_message(
                "⛔ You need to be a manager, market owner, or market manager to view this.", ephemeral=True
            )
        await interaction.response.defer(thinking=True, ephemeral=True)  # shows the private code

        history = _load_csn_for_market(market_id)
        months = history.get("months") or {}
        recent = sorted(months.items())[-3:] if months else []

        owner_id = m.get("owner_id")
        mgr_ids = m.get("manager_ids") or []

        embed = discord.Embed(
            title=f"🏪 {m.get('name', market_id)} [{market_id}]",
            color=0x3498DB,
        )
        embed.add_field(name="Owner", value=f"<@{owner_id}>" if owner_id else "*Not set*", inline=True)
        embed.add_field(name="Status", value="🟢 Active" if m.get("active", True) else "🔴 Inactive", inline=True)

        # Mod-connection + rewards (owner-relevant setup — this response is ephemeral).
        code = (m.get("leader_code") or "").strip()
        embed.add_field(name="Market ID", value=f"`{market_id}`", inline=True)
        embed.add_field(name="Market Code", value=f"`{code}`" if code else "*Not set — /market_code*", inline=True)
        rc = m.get("report_channel_id")
        embed.add_field(name="Report Channel", value=(f"<#{rc}>" if rc else "*Not bound*"), inline=True)
        # The third thing an owner needs for the mod, alongside id + code. Resolved from the
        # bound channel's own webhook (created if missing), so it's always the RIGHT one for
        # this market. Spoiler-tagged: this response is ephemeral, but it's still a secret.
        _hook = None
        if rc:
            try:
                _ch = self.bot.get_channel(int(rc)) or await self.bot.fetch_channel(int(rc))
                _hook = await core._csn_webhook_for(_ch, m.get("name", market_id))
            except Exception as _he:
                log.debug("[market info] webhook lookup failed for %s: %s", market_id, _he)
        embed.add_field(
            name="CSN Webhook",
            value=(f"||{_hook}||" if _hook
                   else ("*couldn't resolve — I may lack Manage Webhooks in that channel*"
                         if rc else "*bind a channel first (`/bind_market`)*")),
            inline=False)
        try:
            _pm, _cb, _pct = _market_loyalty_cfg(market_id)
        except Exception:
            _pm, _cb, _pct = 1.0, 0, 0.0
        _loy = []
        if _pm != 1.0:
            _loy.append(f"**{_pm:g}×** pts")
        if _cb > 0:
            _loy.append(f"**+{_cb:,}c** / order")
        if _pct > 0:
            _loy.append(f"**+{_pct:g}%** / order")
        embed.add_field(name="Restock Rewards", value=(" · ".join(_loy) if _loy else "normal (1×, no bonus)"), inline=True)
        embed.add_field(
            name="Site Managers",
            value=", ".join(f"<@{uid}>" for uid in mgr_ids) if mgr_ids else "*None*",
            inline=False,
        )
        embed.add_field(name="CSN History File", value=f"`{m.get('csn_history_file', '?')}`", inline=True)
        embed.add_field(name="Months Tracked", value=f"`{len(months)}`", inline=True)

        if recent:
            lines = []
            for mk, md in reversed(recent):
                net = int(md.get("net", 0))
                arrow = "📈" if net >= 0 else "📉"
                lines.append(f"{arrow} **{md.get('label', mk)}** — net `{net:+,}` 🪙")
            embed.add_field(name="📅 Recent Months", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"Created: {m.get('created_at', '?')[:10]}  ·  only you can see this")
        await interaction.followup.send(embed=embed, ephemeral=True)







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


    @app_commands.command(
        name="market_set_location",
        description="(Manager/Owner) Set where workers deliver goods for a market (the /la spawn warp)",
    )
    @app_commands.describe(
        market_id="The market",
        location="e.g. '/la spawn BNL'. Leave blank to reset to the default '/la spawn <market_id>'.",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_set_location(self, interaction: discord.Interaction,
                                  market_id: str, location: Optional[str] = None):
        if not (is_manager(interaction) or _is_market_manager(interaction, market_id)):
            return await interaction.response.send_message(
                "⛔ Managers or this market's owner only.", ephemeral=True)
        markets = (_load_markets() or {}).get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found.", ephemeral=True)
        import Restocker_db as _db
        loc = (location or "").strip()[:100]
        if not loc:
            _db.delete_config(f"sell_loc:{market_id}")
            return await interaction.response.send_message(
                f"✅ Delivery location for **{markets[market_id].get('name', market_id)}** reset to the "
                f"default `/la spawn {market_id}`. It shows on worker order cards, `/orders`, and the website.",
                ephemeral=True)
        _db.set_config(f"sell_loc:{market_id}", loc)
        await interaction.response.send_message(
            f"✅ Workers restocking **{markets[market_id].get('name', market_id)}** will now be told to "
            f"deliver to `{loc}`. It shows on their order cards, `/orders`, and the website.",
            ephemeral=True)

    @app_commands.command(
        name="market_rollup",
        description="(Manager/Owner) Roll one market's profit into another market's stock (holding company)",
    )
    @app_commands.describe(
        child_market="The market whose profit rolls UP",
        parent_stock="The stock market it rolls INTO (leave blank to detach and make it independent)",
        share_pct="% of the child's profit that rolls up (default 100; use 60 for a partner market you keep 60% of)",
    )
    @app_commands.autocomplete(child_market=_market_autocomplete, parent_stock=_market_autocomplete)
    async def market_rollup(self, interaction: discord.Interaction, child_market: str,
                            parent_stock: Optional[str] = None,
                            share_pct: Optional[app_commands.Range[float, 0.0, 100.0]] = None):
        if not (is_manager(interaction) or _is_market_manager(interaction, child_market)):
            return await interaction.response.send_message(
                "⛔ Managers or this market's owner only.", ephemeral=True)
        markets = (_load_markets() or {}).get("markets") or {}
        if child_market not in markets:
            return await interaction.response.send_message(
                f"❌ Market `{child_market}` not found.", ephemeral=True)
        parent = (parent_stock or "").strip()
        # AUDIT FIX (high): rolling a market into (or out of) a public stock changes
        # THAT STOCK's fundamental — a child-side owner could re-point a loss-maker
        # into `main` to walk its price down, buy cheap, detach, and sell the bounce.
        # Any rollup change now also requires authority over the PARENT stock.
        def _parent_ok(pmid):
            return is_manager(interaction) or _is_market_manager(interaction, pmid)
        if not parent:
            _cur_parent = None
            try:
                _cur_parent = core._market_rollup_parent(child_market)
            except Exception:
                pass
            if _cur_parent and not _parent_ok(_cur_parent):
                return await interaction.response.send_message(
                    f"⛔ Detaching from **{_cur_parent}** changes that stock's valuation — "
                    f"only its owner/managers (or a server manager) can do that.", ephemeral=True)
            core._set_market_rollup(child_market, None)
            return await interaction.response.send_message(
                f"✅ **{markets[child_market].get('name', child_market)}** detached — it now prices its own "
                f"stock off its own profit again.", ephemeral=True)
        if parent not in markets:
            return await interaction.response.send_message(
                f"❌ Parent market `{parent}` not found.", ephemeral=True)
        if parent == child_market:
            return await interaction.response.send_message(
                "❌ A market can't roll into itself.", ephemeral=True)
        if not _parent_ok(parent):
            return await interaction.response.send_message(
                f"⛔ Rolling into **{parent}** changes that stock's valuation — only its "
                f"owner/managers (or a server manager) can accept the rollup.", ephemeral=True)
        pct = 100.0 if share_pct is None else float(share_pct)
        core._set_market_rollup(child_market, parent, pct)
        await interaction.response.send_message(
            f"✅ **{markets[child_market].get('name', child_market)}**'s profit now rolls into "
            f"**{markets[parent].get('name', parent)}**'s stock at **{pct:g}%**. "
            f"That stock's price is now driven by its own net plus this (and any other rolled-in market).",
            ephemeral=True)









async def setup(bot):
    await bot.add_cog(MarketCog(bot))
