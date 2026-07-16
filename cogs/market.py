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

async def _earnings_month_autocomplete(interaction: discord.Interaction, current: str):
    market_id = getattr(interaction.namespace, "market_id", None) or DEFAULT_MARKET_ID
    history = _load_csn_for_market(market_id)
    months = history.get("months") or {}
    out = []
    for mk, md in sorted(months.items(), reverse=True):
        label = md.get("label", mk)
        if current.lower() in mk.lower() or current.lower() in label.lower():
            out.append(app_commands.Choice(name=label, value=mk))
    return out[:25]

class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    market = app_commands.Group(name="market", description="Manage multiple markets — register, track earnings, and configure per-market settings")

    @market.command(name="loyalty",
                    description="(Owner/Manager) Set this market's restock rewards: loyalty points multiplier + coin bonus")
    @app_commands.describe(
        market_id="Which market to configure",
        points_multiplier="Loyalty-point multiplier for orders fulfilled in this market (e.g. 1.5). 1 = normal.",
        coin_bonus="Flat extra coins paid per fulfilled order in this market (e.g. 500). 0 = none.",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_loyalty(self, interaction: discord.Interaction, market_id: str,
                             points_multiplier: float = 1.0, coin_bonus: int = 0):
        if not (is_manager(interaction) or market_id in _markets_owned_by(interaction.user.id)):
            return await interaction.response.send_message(
                "⛔ Only a manager or this market's owner can set its rewards.", ephemeral=True)
        if not _get_market(market_id):
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found. See `/market list`.", ephemeral=True)
        if points_multiplier <= 0:
            return await interaction.response.send_message(
                "❌ points_multiplier must be greater than 0 (1 = normal, 1.5 = +50%).", ephemeral=True)
        if coin_bonus < 0:
            return await interaction.response.send_message(
                "❌ coin_bonus can't be negative.", ephemeral=True)
        _set_market_loyalty(market_id, points_multiplier, coin_bonus)
        mname = (_get_market(market_id) or {}).get("name", market_id)
        parts = []
        if points_multiplier != 1.0:
            parts.append(f"**{points_multiplier:g}×** loyalty points")
        if coin_bonus > 0:
            parts.append(f"**+{coin_bonus:,}** coins per fulfilled order")
        reward = " and ".join(parts) if parts else "normal rewards (no bonus)"
        await interaction.response.send_message(
            f"✅ **{mname}** (`{market_id}`) now grants {reward} to restockers.\n"
            f"Applies when a manager approves an order tagged to this market.", ephemeral=True)

    @market.command(name="vtech_group",
                    description="(Manager) View/add/remove markets in the V Tech group (shared loyalty pool)")
    @app_commands.describe(
        action="view the group, or add/remove one market",
        market_id="Market to add/remove (ignored for 'view')",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="view", value="view"),
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
    ])
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_vtech_group(self, interaction: discord.Interaction, action: str,
                                 market_id: Optional[str] = None):
        """V Tech-owned markets (Greyhames, Bank, Dragonmart, ...) share ONE loyalty pool:
        working any of them credits the FULL point award to the shared V Tech ledger, instead
        of the smaller slice every other market's orders contribute. Configurable here since
        the group can grow — see VTECH_SLICE_PCT for the non-member slice."""
        if not is_manager(interaction):
            return await interaction.response.send_message(
                "⛔ Managers only — this is a V Tech-wide setting, not a single market's.", ephemeral=True)
        current = _vtech_group_markets()
        if action == "view":
            if not current:
                return await interaction.response.send_message(
                    "🏭 V Tech group is empty — no market gets the full-credit slice yet.", ephemeral=True)
            lines = [f"• **{(_get_market(mid) or {}).get('name', mid)}** (`{mid}`)" for mid in sorted(current)]
            return await interaction.response.send_message(
                "🏭 **V Tech group** (shared loyalty pool):\n" + "\n".join(lines), ephemeral=True)
        if not market_id:
            return await interaction.response.send_message(
                "❌ Provide a market_id to add or remove.", ephemeral=True)
        if not _get_market(market_id):
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found. See `/market list`.", ephemeral=True)
        if action == "add":
            current.add(market_id)
            _set_vtech_group_markets(current)
            return await interaction.response.send_message(
                f"✅ Added `{market_id}` to the V Tech group — its orders now credit the shared pool in full.",
                ephemeral=True)
        current.discard(market_id)
        _set_vtech_group_markets(current)
        await interaction.response.send_message(
            f"✅ Removed `{market_id}` from the V Tech group — its orders now credit only a slice.",
            ephemeral=True)

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

    @market.command(name="delete", description="(Manager) Delete a market — removes its dashboard tab, stock, and share listing")
    @app_commands.describe(
        market_id="Exact market ID to delete (e.g. TEST). Resolves case-insensitively if only one variant exists.",
        confirm="Set True to actually delete. Leave off first to preview what will be removed.",
    )
    async def market_delete(self, interaction: discord.Interaction, market_id: str, confirm: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        import Restocker_db as _db
        markets = (_load_markets().get("markets", {}) or {})
        if market_id in markets:
            real_id = market_id                                  # exact match wins
        else:
            cands = [mid for mid in markets if str(mid).strip().lower() == market_id.strip().lower()]
            if len(cands) == 1:
                real_id = cands[0]
            elif len(cands) > 1:
                return await interaction.response.send_message(
                    f"⚠️ Multiple markets match `{market_id}` by case: "
                    + ", ".join(f"`{c}`" for c in cands) + ". Pass the exact one.", ephemeral=True)
            else:
                return await interaction.response.send_message(
                    f"❌ No market `{market_id}`. Use `/market list` to see them.", ephemeral=True)

        try:
            n_stock = sum(1 for r in (_db.get_all_market_stock() or [])
                          if str(r.get("market_id")) == str(real_id))
        except Exception:
            n_stock = 0
        try:
            months = len((_load_csn_for_market(real_id) or {}).get("months", {}) or {})
        except Exception:
            months = 0
        info = markets.get(real_id) if isinstance(markets.get(real_id), dict) else {}
        name = info.get("name") or real_id

        if not confirm:
            return await interaction.response.send_message(
                f"🗑️ **Delete market `{real_id}`** ({name})?\n"
                f"• Stock items: **{n_stock}**\n"
                f"• Sales-history months: **{months}** *(kept for audit)*\n\n"
                f"Removes its dashboard tab, stock, alarms and share listing. "
                f"Re-run with **`confirm:True`** to delete.",
                ephemeral=True)

        counts = _db.delete_market(real_id)
        log.info("[market] %s deleted market '%s' -> %s", interaction.user, real_id, counts)
        await interaction.response.send_message(
            f"✅ Deleted market **`{real_id}`** ({name}) — removed "
            f"{counts.get('market_stock', 0)} stock row(s), {counts.get('stock_alarms', 0)} alarm(s), "
            f"{counts.get('market_shares', 0)} share listing(s)."
            + (f" ⚠️ {months} month(s) of sales history were kept." if months else ""),
            ephemeral=True)

    @market.command(name="list", description="List all registered markets")
    async def market_list(self, interaction: discord.Interaction):
        data = _load_markets()
        markets = data.get("markets") or {}
        if not markets:
            return await interaction.response.send_message(
                "📭 No markets registered yet.\nUse `/market add` to register one.", ephemeral=True
            )
        lines = []
        for mid, m in sorted(markets.items()):
            owner_id = m.get("owner_id")
            owner_str = f"<@{owner_id}>" if owner_id else "*No owner*"
            active_str = "🟢" if m.get("active", True) else "🔴"
            fee_pct = m.get("platform_fee_pct", PLATFORM_FEE_PCT)
            lines.append(
                f"{active_str} **{m.get('name', mid)}** `[{mid}]` — owner: {owner_str} — fee: `{fee_pct}%`"
            )
        embed = discord.Embed(
            title=f"🏪 Markets ({len(markets)})",
            description="\n".join(lines),
            color=0x3498DB,
        )
        embed.set_footer(text="Use /market info market_id:<id> for full details and earnings summary.")
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
        try:
            _pm, _cb = _market_loyalty_cfg(market_id)
        except Exception:
            _pm, _cb = 1.0, 0
        _loy = []
        if _pm != 1.0:
            _loy.append(f"**{_pm:g}×** pts")
        if _cb > 0:
            _loy.append(f"**+{_cb:,}c** / order")
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

    @market.command(name="earnings", description="Monthly earnings report for a market — income, spending, net profit")
    @app_commands.describe(
        market_id="Which market to show earnings for (default: main). Use /market list to see IDs.",
        month="Show one specific month (pick from the autocomplete list) instead of a recent-months summary.",
        months="How many recent months to show when no specific month is picked (max 24). Default: 12",
        charts="Show income vs net bar chart. Default: True",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete, month=_earnings_month_autocomplete)
    async def market_earnings(self, 
        interaction: discord.Interaction,
        market_id: str = DEFAULT_MARKET_ID,
        month: str = "",
        months: app_commands.Range[int, 1, 24] = 12,
        charts: bool = True,
    ):
        mkt = _get_market(market_id)
        if mkt is None:
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found.", ephemeral=True
            )
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)
                or _is_market_manager(interaction, market_id)):
            return await interaction.response.send_message(
                "⛔ Managers, market owners, or site managers only.", ephemeral=True
            )
        await interaction.response.defer(thinking=True)

        history = _load_csn_for_market(market_id)
        all_months = history.get("months") or {}
        if not all_months:
            return await interaction.followup.send(
                f"📭 No earnings history for `{market_id}` yet.\nRun `/csn market_id:{market_id}` to record data."
            )

        fee_pct = float(mkt.get("platform_fee_pct", PLATFORM_FEE_PCT) or PLATFORM_FEE_PCT)
        market_label = mkt.get("name", market_id)

        if month:
            md = all_months.get(month)
            if md is None:
                return await interaction.followup.send(
                    f"📭 No data for month `{month}` in `{market_id}`. Pick a month from the autocomplete list."
                )
            income, spent, net = int(md["income"]), int(md["spent"]), int(md["net"])
            est_fee = int(math.floor(net * fee_pct / 100.0)) if net > 0 else 0
            color = 0x2ECC71 if net >= 0 else 0xE74C3C
            embed = discord.Embed(title=f"📊 {market_label} — {md.get('label', month)}", color=color)
            _fee_line = f"\n**Est. Platform Fee ({fee_pct}%):** `{est_fee:,}` 🪙" if PLATFORM_FEE_ACTIVE else ""
            embed.add_field(
                name="💰 Summary",
                value=(
                    f"**Income:** `{income:,}` 🪙\n"
                    f"**Spent:**  `{spent:,}` 🪙\n"
                    f"**Net:**    `{net:+,}` 🪙"
                    f"{_fee_line}"
                ),
                inline=False,
            )
            items = md.get("items") or {}
            if items:
                top = sorted(items.items(), key=lambda kv: kv[1].get("net_coins", 0), reverse=True)[:10]
                lines = [
                    f"**{name}** — sold `{v.get('sold_qty', 0):,}` · bought `{v.get('bought_qty', 0):,}` · "
                    f"`{v.get('net_coins', 0):+,.0f}` 🪙"
                    for name, v in top
                ]
                embed.add_field(name="🏆 Top Items", value="\n".join(lines), inline=False)

            files = []
            if charts and _MATPLOTLIB_OK:
                png = _generate_earnings_chart([(md.get("label", month), income, net)])
                if png:
                    files = [discord.File(io.BytesIO(png), filename="earnings_chart.png")]
                    embed.set_image(url="attachment://earnings_chart.png")
            elif charts and not _MATPLOTLIB_OK:
                embed.set_footer(text="📊 Interactive charts on the dashboard → dashboard.vaicosmarket.com")

            return await interaction.followup.send(embed=embed, files=files)

        sorted_months = sorted(all_months.items())[-months:]
        total_income = sum(md["income"] for _, md in sorted_months)
        total_spent  = sum(md["spent"]  for _, md in sorted_months)
        total_net    = sum(md["net"]    for _, md in sorted_months)
        est_fee = int(math.floor(total_net * fee_pct / 100.0)) if total_net > 0 else 0

        best_month  = max(sorted_months, key=lambda x: x[1]["net"])
        worst_month = min(sorted_months, key=lambda x: x[1]["net"])

        color = 0x2ECC71 if total_net >= 0 else 0xE74C3C
        embed = discord.Embed(
            title=f"📊 {market_label} — Last {len(sorted_months)} Month{'s' if len(sorted_months) != 1 else ''}",
            color=color,
        )
        _fee_line = f"\n**Est. Platform Fee ({fee_pct}%):** `{est_fee:,}` 🪙" if PLATFORM_FEE_ACTIVE else ""
        embed.add_field(
            name="💰 Total Summary",
            value=(
                f"**Income:** `{int(total_income):,}` 🪙\n"
                f"**Spent:**  `{int(total_spent):,}` 🪙\n"
                f"**Net:**    `{int(total_net):+,}` 🪙"
                f"{_fee_line}"
            ),
            inline=True,
        )
        avg_net = total_net / len(sorted_months) if sorted_months else 0
        embed.add_field(
            name="📈 Averages",
            value=(
                f"**Avg income/mo:** `{int(total_income / len(sorted_months)):,}` 🪙\n"
                f"**Avg net/mo:**    `{int(avg_net):+,}` 🪙\n"
                f"**Months tracked:** `{len(sorted_months)}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="🏆 Best / Worst Month",
            value=(
                f"🟢 **{best_month[1].get('label', best_month[0])}** — `{int(best_month[1]['net']):+,}` 🪙\n"
                f"🔴 **{worst_month[1].get('label', worst_month[0])}** — `{int(worst_month[1]['net']):+,}` 🪙"
            ),
            inline=False,
        )

        rows = []
        for mk, md in reversed(sorted_months):
            net = int(md["net"])
            arrow = "📈" if net >= 0 else "📉"
            rows.append(
                f"{arrow} **{md.get('label', mk)}** — "
                f"`{int(md['income']):,}` in · `{int(md['spent']):,}` out · `{net:+,}` net"
            )
        chunk, used = [], 0
        for row in rows:
            if used + len(row) + 1 > 1020:
                chunk.append("*…(older months truncated)*")
                break
            chunk.append(row)
            used += len(row) + 1
        embed.add_field(name="📅 Month-by-Month", value="\n".join(chunk), inline=False)

        files = []
        if charts:
            if not _MATPLOTLIB_OK:
                embed.set_footer(text="📊 Interactive charts on the dashboard → dashboard.vaicosmarket.com")
            else:
                chart_input = [(md.get("label", mk), md["income"], md["net"]) for mk, md in sorted_months]
                png = _generate_earnings_chart(chart_input)
                if png:
                    files = [discord.File(io.BytesIO(png), filename="earnings_chart.png")]
                    embed.set_image(url="attachment://earnings_chart.png")

        await interaction.followup.send(embed=embed, files=files)

    @market.command(name="set_ticker", description="(Manager) Set a short stock ticker symbol for a market (e.g. GEX)")
    @app_commands.describe(market_id="Market", ticker="Symbol — 1-6 letters/digits")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_set_ticker(self, interaction: discord.Interaction, market_id: str, ticker: str):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message(
                "⛔ Managers or this market's owner only.", ephemeral=True
            )
        sym = "".join(ch for ch in ticker.upper() if ch.isalnum())[:6]
        if not sym:
            return await interaction.response.send_message(
                "❌ A ticker needs at least one letter or digit.", ephemeral=True
            )
        tickers = load_yaml("market_tickers.yml", {}) or {}
        tickers[market_id] = sym
        save_yaml("market_tickers.yml", tickers)
        await interaction.response.send_message(
            f"✅ Ticker for `{market_id}` set to **{sym}**.", ephemeral=True
        )

    @market.command(name="report", description="Your private market report — best sellers, missing stock, earnings")
    @app_commands.describe(market_id="The market to report on")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_report(self, interaction: discord.Interaction, market_id: str):
        mkt = _get_market(market_id)
        if mkt is None:
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found.", ephemeral=True
            )
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)
                or _is_market_manager(interaction, market_id)):
            return await interaction.response.send_message(
                "⛔ Only the market owner or managers can view this report.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True, thinking=True)

        history   = _load_csn_for_market(market_id)
        all_months = history.get("months") or {}

        if not all_months:
            return await interaction.followup.send(
                f"📭 No sales data for `{market_id}` yet. Reports are submitted automatically via the CSN mod.",
                ephemeral=True,
            )

        item_totals: dict[str, dict] = {}
        for mk, md in all_months.items():
            for iname, iv in (md.get("items") or {}).items():
                if not isinstance(iv, dict):
                    continue
                e = item_totals.setdefault(iname, {"sold": 0, "bought": 0})
                e["sold"]   += int(iv.get("sold_qty",   0))
                e["bought"] += int(iv.get("bought_qty", 0))

        item_rows = [
            {"name": n, "sold": v["sold"], "bought": v["bought"], "missing": v["sold"] - v["bought"]}
            for n, v in item_totals.items()
        ]

        best_seller  = max(item_rows, key=lambda r: r["sold"],    default=None)
        most_missing = sorted([r for r in item_rows if r["missing"] > 0], key=lambda r: -r["missing"])[:5]
        surplus      = sorted([r for r in item_rows if r["missing"] < 0], key=lambda r: r["missing"])[:5]

        recent_months = sorted(all_months.items())[-3:]
        total_net    = sum(md.get("net",    0) for _, md in recent_months)
        total_income = sum(md.get("income", 0) for _, md in recent_months)

        color = 0x2ECC71 if total_net >= 0 else 0xE74C3C
        market_name = mkt.get("name", market_id)

        embed = discord.Embed(
            title=f"📊 {market_name} — Private Report",
            color=color,
        )
        embed.set_footer(text="Only visible to you  •  /market report")

        if best_seller:
            embed.add_field(
                name="🏆 Best Seller",
                value=f"**{best_seller['name']}**\n`{best_seller['sold']:,}` sold to customers",
                inline=True,
            )

        embed.add_field(
            name=f"💰 Last {len(recent_months)} Month{'s' if len(recent_months) != 1 else ''}",
            value=(
                f"**Income:** `{int(total_income):,}` 🪙\n"
                f"**Net:** `{int(total_net):+,}` 🪙"
            ),
            inline=True,
        )
        embed.add_field(name="​", value="​", inline=True)

        if most_missing:
            lines = [f"⚠️ **{r['name']}** — missing `{r['missing']:,}`" for r in most_missing]
            embed.add_field(name="📦 Needs Restocking", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📦 Needs Restocking", value="✅ Nothing — stock looks balanced!", inline=False)

        if surplus:
            lines = [f"📦 **{r['name']}** — surplus `{abs(r['missing']):,}`" for r in surplus]
            embed.add_field(name="📈 Over-stocked", value="\n".join(lines), inline=False)

        month_lines = []
        for mk, md in reversed(recent_months):
            net = int(md.get("net", 0))
            arrow = "📈" if net >= 0 else "📉"
            month_lines.append(f"{arrow} **{md.get('label', mk)}** — `{net:+,}` 🪙 net")
        embed.add_field(name="📅 Recent Months", value="\n".join(month_lines), inline=False)

        dashboard_url = os.getenv("DASHBOARD_URL", "").strip()
        if dashboard_url:
            embed.add_field(
                name="🌐 Full Dashboard",
                value=f"[View {market_name} on the website]({dashboard_url})\n*(Earnings tab → select your market)*",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @market.command(name="set_owner", description="(Manager) Set the owner of a market")
    @app_commands.describe(market_id="The market to update", owner="The new owner")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_set_owner(self, interaction: discord.Interaction, market_id: str, owner: discord.Member):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        data = _load_markets()
        markets = data.get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)
        markets[market_id]["owner_id"] = owner.id
        _save_markets(data)
        await interaction.response.send_message(
            f"✅ **{markets[market_id].get('name', market_id)}** owner set to {owner.mention}."
        )

    @market.command(name="edit", description="(Manager) Edit a market's name, fee, or active status")
    @app_commands.describe(
        market_id="The market to edit",
        name="New display name",
        fee_pct="New platform fee % (0–50)",
        active="Whether this market is active",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_edit(self, 
        interaction: discord.Interaction,
        market_id: str,
        name: Optional[str] = None,
        fee_pct: Optional[app_commands.Range[float, 0.0, 50.0]] = None,
        active: Optional[bool] = None,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        data = _load_markets()
        markets = data.get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)
        if name is None and fee_pct is None and active is None:
            return await interaction.response.send_message(
                "❌ Provide at least one field to update: `name`, `fee_pct`, or `active`.", ephemeral=True
            )

        mkt = markets[market_id]
        changes = []
        if name is not None:
            mkt["name"] = name.strip()
            changes.append(f"Name → `{name.strip()}`")
        if fee_pct is not None:
            mkt["platform_fee_pct"] = round(fee_pct, 4)
            changes.append(f"Platform Fee → `{fee_pct}%`")
        if active is not None:
            mkt["active"] = active
            changes.append(f"Active → `{active}`")

        _save_markets(data)

        embed = discord.Embed(title=f"✅ Market Updated — {mkt.get('name', market_id)}", color=0x3498DB)
        embed.add_field(name="Market ID", value=f"`{market_id}`", inline=True)
        embed.add_field(name="Changes", value="\n".join(changes), inline=False)
        await interaction.response.send_message(embed=embed)

    @market.command(name="add_manager", description="(Manager/Owner) Add a site manager to a market")
    @app_commands.describe(market_id="The market", user="The user to add as site manager")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_add_manager(self, interaction: discord.Interaction, market_id: str, user: discord.Member):
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)):
            return await interaction.response.send_message("⛔ Managers or market owners only.", ephemeral=True)
        data = _load_markets()
        markets = data.get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)
        mgr_ids = markets[market_id].setdefault("manager_ids", [])
        if user.id not in mgr_ids:
            mgr_ids.append(user.id)
        _save_markets(data)
        await interaction.response.send_message(
            f"✅ {user.mention} added as site manager for **{markets[market_id].get('name', market_id)}**."
        )

    @market.command(name="remove_manager", description="(Manager/Owner) Remove a site manager from a market")
    @app_commands.describe(market_id="The market", user="The user to remove")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_remove_manager(self, interaction: discord.Interaction, market_id: str, user: discord.Member):
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)):
            return await interaction.response.send_message("⛔ Managers or market owners only.", ephemeral=True)
        data = _load_markets()
        markets = data.get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)
        mgr_ids = markets[market_id].get("manager_ids") or []
        if user.id in mgr_ids:
            mgr_ids.remove(user.id)
            markets[market_id]["manager_ids"] = mgr_ids
            _save_markets(data)
            await interaction.response.send_message(
                f"✅ {user.mention} removed as site manager from **{markets[market_id].get('name', market_id)}**."
            )
        else:
            await interaction.response.send_message(
                f"❌ {user.mention} is not a site manager of `{market_id}`.", ephemeral=True
            )

    @market.command(name="platform_balance", description="(Manager) View total platform fee balance collected")
    async def market_platform_balance(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        data = _load_platform_balance()
        bal = int(data.get("balance", 0) or 0)
        log_entries = list(reversed((data.get("log") or [])[-10:]))

        embed = discord.Embed(title="🏦 Platform Fee Balance", color=0x9B59B6)
        embed.add_field(name="Total Collected", value=f"`{bal:,}` 🪙", inline=False)

        if log_entries:
            lines = []
            for entry in log_entries:
                lines.append(
                    f"• `{entry.get('month', '?')}` [{entry.get('market_id', '?')}] "
                    f"→ `{int(entry.get('amount', 0)):,}` 🪙  {entry.get('note', '')}"
                )
            embed.add_field(name="📋 Recent Fee Collections (last 10)", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📋 Fee Log", value="*No fees collected yet*", inline=False)

        await interaction.response.send_message(embed=embed)

    @market.command(name="set_leader_role", description="(Manager) Set the Discord role that identifies the leader of a market")
    @app_commands.describe(
        market_id="The market to update",
        role="The Discord role whose holder is the market leader",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_set_leader_role(self, 
        interaction: discord.Interaction,
        market_id: str,
        role: discord.Role,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        data = _load_markets()
        markets = data.get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)
        markets[market_id]["discord_role_name"] = role.name
        _save_markets(data)
        await interaction.response.send_message(
            f"✅ Leader role for **{markets[market_id].get('name', market_id)}** set to **{role.name}**.\n"
            f"Whoever holds this role can now run `/market_code market_id:{market_id}` to get their CSN code.",
            ephemeral=True,
        )

    @market.command(
        name="set_channel",
        description="(Manager) Bind a Discord channel to a market so CSN reports route there with NO code needed",
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

    @market.command(
        name="unset_channel",
        description="(Manager) Remove the channel binding for a market",
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

    @market.command(
        name="go_public",
        description="(Manager/Owner) List a market on the stock exchange so its shares can be traded",
    )
    @app_commands.describe(
        market_id="The market to take public",
        shares_outstanding="Total shares to issue (default 1000, only used on first listing)",
        pe_multiplier="Price multiplier applied to monthly net profit per share (default 12)",
        initial_price="Override the computed launch price (optional — otherwise priced off real CSN profit)",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_go_public(self, 
        interaction: discord.Interaction,
        market_id: str,
        shares_outstanding: Optional[float] = None,
        pe_multiplier: Optional[float] = None,
        initial_price: Optional[float] = None,
    ):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message(
                "⛔ Managers or this market's owner/managers only.", ephemeral=True
            )
        market = _get_market(market_id)
        if not market:
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)

        import Restocker_db as _db
        existing = _db.get_market_shares(market_id)
        if existing and existing.get("active"):
            return await interaction.response.send_message(
                f"❌ `{market_id}` is already public at `{existing['share_price']:,.2f}` 🪙/share. "
                f"Use `/stock set_params` to adjust it instead.", ephemeral=True
            )

        _db.upsert_market_shares(
            market_id,
            active=1,
            shares_outstanding=shares_outstanding,
            pe_multiplier=pe_multiplier,
        )
        price = _recompute_share_price(market_id, reason="ipo")
        if initial_price is not None and initial_price > 0:
            price = round(initial_price, 2)
            _db.upsert_market_shares(market_id, share_price=price)
            _db.log_stock_price(market_id, price, "ipo_override")
        listing = _db.get_market_shares(market_id)

        embed = discord.Embed(
            title=f"📈 {market.get('name', market_id)} Just Went Public!",
            description=f"Shares of `{market_id}` are now tradeable with server currency.",
            color=0x2ECC71,
        )
        embed.add_field(name="Share Price", value=f"`{listing['share_price']:,.2f}` 🪙", inline=True)
        embed.add_field(name="Shares Outstanding", value=f"`{listing['shares_outstanding']:,.0f}`", inline=True)
        embed.add_field(name="P/E Multiplier", value=f"`{listing['pe_multiplier']:,.1f}x`", inline=True)
        if price is None:
            embed.add_field(
                name="⚠️ No CSN history yet",
                value=f"Price set to the floor (`{MIN_SHARE_PRICE:,.2f}` 🪙) until a monthly report is recorded.",
                inline=False,
            )
        embed.set_footer(text=f"Buy in with /stock buy market_id:{market_id} shares:<amount>")
        await interaction.response.send_message(embed=embed)

    @market.command(
        name="go_private",
        description="(Manager/Owner) Delist a market from the stock exchange",
    )
    @app_commands.describe(
        market_id="The market to delist",
        confirm="Required if anyone holds shares — type the market ID again to confirm",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_go_private(self, interaction: discord.Interaction, market_id: str, confirm: Optional[str] = None):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message(
                "⛔ Managers or this market's owner/managers only.", ephemeral=True
            )

        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing or not listing.get("active"):
            return await interaction.response.send_message(f"❌ `{market_id}` isn't public.", ephemeral=True)

        holders = _db.get_holders(market_id)
        if holders and (not confirm or confirm.strip().lower() != market_id.strip().lower()):
            total_held = sum(h["shares"] for h in holders)
            return await interaction.response.send_message(
                f"⚠️ {len(holders)} holder(s) own `{total_held:,.2f}` shares of `{market_id}`. "
                f"Delisting freezes their shares at the last price (`{listing['share_price']:,.2f}` 🪙) "
                f"until you go public again. Type `confirm:{market_id}` to proceed anyway.",
                ephemeral=True,
            )

        _db.upsert_market_shares(market_id, active=0)
        market = _get_market(market_id) or {}
        await interaction.response.send_message(
            f"✅ **{market.get('name', market_id)}** (`{market_id}`) delisted from the stock exchange. "
            f"Existing holdings are kept and will unfreeze if it goes public again.",
            ephemeral=True,
        )

    @market.command(name="treasury", description="(Manager/Owner) View a public market's treasury and buyback cover")
    @app_commands.describe(market_id="The public market")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_treasury(self, interaction: discord.Interaction, market_id: str):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message("⛔ Managers or this market's owner only.", ephemeral=True)
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing:
            return await interaction.response.send_message(f"❌ `{market_id}` isn't listed.", ephemeral=True)
        treasury = float(listing.get("treasury_coins") or 0)
        price = float(listing.get("share_price") or 0)
        held = sum(float(h.get("shares") or 0) for h in _db.get_holders(market_id))
        liability = held * price
        excess = max(0.0, treasury - liability)
        market = _get_market(market_id) or {}
        embed = discord.Embed(title=f"🏦 {market.get('name', market_id)} — Treasury", color=0x1ABC9C)
        embed.add_field(name="Treasury", value=f"`{treasury:,.0f}` 🪙", inline=True)
        embed.add_field(name="Buyback cover", value=f"`{liability:,.0f}` 🪙", inline=True)
        embed.add_field(name="Withdrawable", value=f"`{excess:,.0f}` 🪙", inline=True)
        embed.set_footer(text="Buys pay into the treasury; sells are funded from it. Withdraw the excess with /market treasury_withdraw.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @market.command(name="treasury_withdraw", description="(Manager/Owner) Withdraw a market's EXCESS treasury to your wallet")
    @app_commands.describe(market_id="The public market", amount="Coins to withdraw (must be within the withdrawable excess)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_treasury_withdraw(self, interaction: discord.Interaction, market_id: str,
                                       amount: app_commands.Range[int, 1, 1_000_000_000]):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message("⛔ Managers or this market's owner only.", ephemeral=True)
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing:
            return await interaction.response.send_message(f"❌ `{market_id}` isn't listed.", ephemeral=True)
        treasury = float(listing.get("treasury_coins") or 0)
        price = float(listing.get("share_price") or 0)
        held = sum(float(h.get("shares") or 0) for h in _db.get_holders(market_id))
        liability = held * price
        excess = max(0.0, treasury - liability)
        amt = int(amount)
        if amt > excess:
            return await interaction.response.send_message(
                f"❌ Only `{excess:,.0f}` 🪙 is withdrawable (treasury `{treasury:,.0f}` minus buyback cover `{liability:,.0f}`).",
                ephemeral=True)
        applied = _db.adjust_treasury(market_id, -float(amt), allow_negative=False)
        moved = int(round(-applied))
        add_coins(interaction.user.id, moved, counts_as_principal=True)
        market = _get_market(market_id) or {}
        await interaction.response.send_message(
            f"✅ Withdrew `{moved:,}` 🪙 from **{market.get('name', market_id)}**'s treasury to your wallet.", ephemeral=True)

    @market.command(name="remove_item", description="(Manager/Owner) Remove an item your market no longer sells")
    @app_commands.describe(
        market_id="Your market",
        item="Exact item name to remove",
        mode="full = also adjust historical income/net (default); hide = keep totals, just hide it",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="full - adjust totals (moves share price)", value="full"),
        app_commands.Choice(name="hide - keep totals (cosmetic)", value="hide"),
    ])
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_remove_item(self, interaction: discord.Interaction, market_id: str, item: str,
                                 mode: Optional[app_commands.Choice[str]] = None):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message("⛔ Managers or this market's owner only.", ephemeral=True)
        adjust = (mode.value if mode else "full") != "hide"
        r = _remove_market_item(market_id, item, adjust_totals=adjust)
        if not r["months_touched"] and not r["catalog_removed"]:
            return await interaction.response.send_message(f"❌ `{item}` not found in `{market_id}`.", ephemeral=True)
        if adjust:
            extra = f" Adjusted net by `{-r['removed_net']:,.0f}` 🪙 across `{r['months_touched']}` month(s)."
        else:
            extra = f" Hidden from `{r['months_touched']}` month(s); totals kept."
        cat = " Catalog entry removed." if r["catalog_removed"] else ""
        await interaction.response.send_message(f"🗑️ Removed **{item}** from `{market_id}`.{extra}{cat}", ephemeral=True)

    @market.command(name="log_restock", description="(Manager/Owner) Log stock you added by hand so net profit stays accurate")
    @app_commands.describe(market_id="Your market", item="Item name", qty="Units you added", cost="Total coins you paid")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_log_restock(self, interaction: discord.Interaction, market_id: str, item: str,
                                 qty: app_commands.Range[int, 1, 1_000_000],
                                 cost: app_commands.Range[int, 0, 1_000_000_000]):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message("⛔ Managers or this market's owner only.", ephemeral=True)
        r = _log_manual_restock(market_id, item, qty, cost)
        stock = f" Catalog stock now `{r['new_stock']:,}`." if r.get("new_stock") is not None else ""
        s = _suggest_item_price(market_id, item)
        await interaction.response.send_message(
            f"📦 Logged `{qty:,}`x **{item}** at `{cost:,}` 🪙 to `{market_id}` ({r['month']}).{stock}\n"
            f"💡 Optimal sell price ~`{s['optimal']:,}` 🪙 (general avg `{s['standard']:,.0f}`, your cost "
            f"`{s['unit_cost']:,.1f}`/unit, target {s['margin_pct']:.0f}% margin).",
            ephemeral=True)

    @market.command(name="suggest_price", description="(Manager/Owner) Suggested price for an item vs the general market")
    @app_commands.describe(market_id="Your market", item="Item name")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_suggest_price(self, interaction: discord.Interaction, market_id: str, item: str):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message("⛔ Managers or this market's owner only.", ephemeral=True)
        s = _suggest_item_price(market_id, item)
        market = _get_market(market_id) or {}
        embed = discord.Embed(title=f"💡 Price guide — {item}", color=0x2bbf90,
                              description=f"Market: **{market.get('name', market_id)}**")
        embed.add_field(name="Optimal", value=f"`{s['optimal']:,}` 🪙", inline=True)
        embed.add_field(name="General market (standard)", value=f"`{s['standard']:,.0f}` 🪙", inline=True)
        embed.add_field(name="Your realized sell", value=f"`{s['effective']:,.1f}` 🪙", inline=True)
        embed.add_field(name="Your cost/unit", value=f"`{s['unit_cost']:,.1f}` 🪙", inline=True)
        embed.add_field(name="Current catalog", value=f"`{s['current']:,.0f}` 🪙", inline=True)
        embed.add_field(name="Sold in markets", value=f"`{s['markets_selling']}`", inline=True)
        if s["current"] and s["optimal"]:
            diff = s["optimal"] - s["current"]
            if abs(diff) >= max(1, 0.03 * s["current"]):
                verb = "raise" if diff > 0 else "lower"
                embed.add_field(name="Suggested change",
                                value=f"**{verb}** to `{s['optimal']:,}` 🪙 ({diff:+,.0f})", inline=False)
            else:
                embed.add_field(name="Suggested change", value="Looks well-priced ✅", inline=False)
        embed.set_footer(text="A guide from your own + general market history. You set the final price.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @market.command(name="hide_earnings",
                    description="(Manager/Owner) Hide this market's earnings from the public dashboard (stays active)")
    @app_commands.describe(market_id="The market",
                           hide="True = hide earnings + CSN prices from the public dashboard; False = show again")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_hide_earnings(self, interaction: discord.Interaction, market_id: str, hide: bool = True):
        if not _is_market_manager(interaction, market_id):
            return await interaction.response.send_message(
                "⛔ Managers or this market's owner only.", ephemeral=True)
        if _get_market(market_id) is None:
            return await interaction.response.send_message(
                f"❌ Market `{market_id}` not found.", ephemeral=True)
        import Restocker_db as _db
        raw = _db.get_config("earnings_hidden_markets") or ""
        ids = {p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip()}
        if hide:
            ids.add(market_id)
        else:
            ids.discard(market_id)
        _db.set_config("earnings_hidden_markets", ",".join(sorted(ids)))
        name = (_get_market(market_id) or {}).get("name", market_id)
        if hide:
            await interaction.response.send_message(
                f"🙈 **{name}** (`{market_id}`) earnings + CSN prices are now **hidden** from the public dashboard. "
                f"The market stays active and tradeable, and you still see everything in Discord. "
                f"(Refreshes within a few seconds.)", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"👁️ **{name}** (`{market_id}`) earnings are **public** again on the dashboard.", ephemeral=True)



async def setup(bot):
    await bot.add_cog(MarketCog(bot))
