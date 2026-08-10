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



async def _bank_month_autocomplete(interaction: discord.Interaction, current: str):
    """Offer the last 12 closed months as a picker — nobody should be typing "2026-07"
    into a field that only ever accepts twelve sensible values. Label is the readable
    form ("July 2026"), value stays YYYY-MM so build_bank_earnings_report is unchanged.
    Months already sent are marked, because sending one twice is the mistake this
    command makes easy."""
    from datetime import datetime as _dt, timezone as _tz
    out = []
    try:
        import Restocker_db as _db
    except Exception:
        _db = None
    now = _dt.now(_tz.utc)
    y, m = now.year, now.month
    cur = (current or "").strip().lower()
    for _ in range(12):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        key = f"{y:04d}-{m:02d}"
        label = f"{_dt(y, m, 1).strftime('%B')} {y}"
        if _db is not None:
            try:
                if str(_db.get_config(f"bank_report:{key}") or "").strip():
                    label += " · already sent"
            except Exception:
                pass
        if cur and cur not in label.lower() and cur not in key:
            continue
        out.append(app_commands.Choice(name=label[:100], value=key))
    return out[:25]


class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    market = app_commands.Group(name="market", description="Manage multiple markets — register, track earnings, and configure per-market settings")
    # Owners kept looking for their own shop under something that sounded like theirs,
    # not under the registry of every market on the server. Same panel, findable name.
    my = app_commands.Group(name="my", description="Your own market — settings, staff, treasury, listing")

    @market.command(
        name="bank_report",
        description="(Managers) Send the V Tech earnings statement for a month to the bank")
    @app_commands.describe(
        month="Which month (blank = the month that just closed)",
        preview="Show it here without sending (default: preview)")
    @app_commands.autocomplete(month=_bank_month_autocomplete)
    async def bank_report(self, interaction: discord.Interaction,
                          month: Optional[str] = None, preview: bool = True):
        """Manual counterpart to the monthly loop, for catching up a late statement.
        Defaults to PREVIEW because the destination is a third party's channel — sending
        should be a deliberate second step, not the thing that happens if you mistype."""
        import re as _re
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        if month:
            month = str(month).strip()
            if not _re.fullmatch(r"\d{4}-\d{2}", month):
                return await interaction.followup.send(
                    "Month must look like `2026-07`.", ephemeral=True)
        else:
            _first = _dt.now(_tz.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            month = (_first - _td(days=1)).strftime("%Y-%m")

        try:
            body = core.build_bank_earnings_report(month)
        except Exception as e:
            return await interaction.followup.send(f"❌ Couldn't build it: `{e}`", ephemeral=True)

        if preview:
            return await interaction.followup.send(
                f"**Preview — nothing sent.** Re-run with `preview:False` to send.\n\n{body}",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

        _wh = core._bank_report_webhook()
        if not _wh:
            return await interaction.followup.send(
                "❌ No bank webhook configured. Set `BANK_REPORT_WEBHOOK` in .env "
                "(preferred — keeps it out of git) and restart.", ephemeral=True)
        import aiohttp as _ah
        try:
            async with _ah.ClientSession() as _s:
                async with _s.post(_wh, json={"content": body[:1900],
                                              "allowed_mentions": {"parse": []}}) as _r:
                    if _r.status not in (200, 204):
                        return await interaction.followup.send(
                            f"❌ Webhook returned `{_r.status}` — nothing was sent.", ephemeral=True)
        except Exception as e:
            return await interaction.followup.send(f"❌ Send failed: `{e}`", ephemeral=True)

        try:
            import Restocker_db as _db
            _db.set_config(f"bank_report:{month}", "done")   # stop the loop re-sending it
        except Exception:
            pass
        await interaction.followup.send(f"✅ Sent the **{month}** statement to the bank.",
                                        ephemeral=True)

    @market.command(name="sales",
                    description="A month's sales for a market — full item breakdown + dashboard link")
    @app_commands.describe(market_id="Which market", month="YYYY-MM (blank = latest month on record)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_sales(self, interaction: discord.Interaction,
                           market_id: str = None, month: str = None):
        """Replaces /csn_history, which could only ever read the MAIN market's history
        file and showed all-time totals rather than a month. This is per-market and
        month-scoped, and links to the report page for the day-by-day detail that does
        not fit in an embed."""
        mid = (market_id or "").strip() or DEFAULT_MARKET_ID
        markets = (_load_markets().get("markets", {}) or {})
        if mid not in markets:
            return await interaction.response.send_message(
                f"❌ Market `{mid}` not found.", ephemeral=True)
        if not (is_manager(interaction) or _is_market_owner(interaction, mid)
                or _is_market_manager(interaction, mid)):
            return await interaction.response.send_message(
                "⛔ Managers, this market's owner, or its site managers only.", ephemeral=True)
        await interaction.response.defer(thinking=True)

        months = (_load_csn_for_market(mid).get("months") or {})
        if not months:
            return await interaction.followup.send(
                f"📭 No sales recorded for **{markets[mid].get('name', mid)}** yet.")
        mk = (month or "").strip() or sorted(months)[-1]
        md = months.get(mk)
        if not isinstance(md, dict):
            return await interaction.followup.send(
                f"❌ No data for `{mk}`. On record: " + ", ".join(f"`{k}`" for k in sorted(months)[-12:]))

        inc = int(md.get("income", 0) or 0)
        spent = int(md.get("spent", 0) or 0)
        net = int(md.get("net", 0) or 0)
        e = discord.Embed(
            title=f"📈 {markets[mid].get('name', mid)} — {md.get('label', mk)}",
            color=0x2ECC71 if net >= 0 else 0xE74C3C)
        e.add_field(name="Income", value=f"`{inc:,}` 🪙", inline=True)
        e.add_field(name="Spent", value=f"`{spent:,}` 🪙", inline=True)
        e.add_field(name="Net", value=f"**`{net:,}`** 🪙", inline=True)

        items = md.get("items") or {}
        rows = sorted(items.items(), key=lambda kv: -float(kv[1].get("net_coins", 0) or 0))
        # Embed fields cap at 1024 chars, so page the breakdown instead of truncating it
        # to a "top 10" the way the old command did — this is the WHOLE month.
        chunk, size, part = [], 0, 1
        for iname, v in rows:
            line = (f"`{int(v.get('sold_qty', 0) or 0):>6,}` × {iname} — "
                    f"`{int(v.get('net_coins', 0) or 0):,}` 🪙")
            if size + len(line) + 1 > 1000 or len(chunk) >= 18:
                e.add_field(name=f"Items ({part})", value="\n".join(chunk), inline=False)
                chunk, size, part = [], 0, part + 1
                if part > 6:
                    break
            chunk.append(line); size += len(line) + 1
        if chunk and part <= 6:
            e.add_field(name=(f"Items ({part})" if part > 1 else "Items"),
                        value="\n".join(chunk), inline=False)
        if part > 6:
            e.set_footer(text=f"{len(rows)} items total — full list on the dashboard.")
        else:
            e.set_footer(text=f"{len(rows)} item(s) · {len(months)} month(s) on record")

        url = getattr(core, "DASHBOARD_URL", "")
        view = None
        if url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Day-by-day on the dashboard", style=discord.ButtonStyle.link,
                url=f"{url}/report/{mid}/{mk}"))
        await interaction.followup.send(embed=e, view=view)

    @my.command(name="market",
                description="(Owner/Manager) One panel for everything about your market")
    @app_commands.describe(market_id="Market to open on (blank = the first you can manage)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def market_settings(self, interaction: discord.Interaction, market_id: str = None):
        """One panel replacing edit/loyalty/set_*/add_manager/remove_manager/go_public/
        go_private/treasury/treasury_withdraw/remove_item/vtech_group/delete."""
        from views.market_settings import MarketSettingsView, build_embed, _may_view as _may_manage
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
                "⛔ You need to be this market's owner, a site manager, a server manager, "
                "or hold its leader role.",
                ephemeral=True)
        view = MarketSettingsView(mid, interaction.user.id, interaction.user)
        await interaction.response.send_message(
            embed=await build_embed(mid, interaction.user), view=view, ephemeral=True)












    # Top-level (NOT under /market) — the /market group is at Discord's 25-subcommand cap,
    # and channel-binding is discoverable under a `/bind…` search here.













async def setup(bot):
    await bot.add_cog(MarketCog(bot))
