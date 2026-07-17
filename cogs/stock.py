"""Stock-exchange commands (/stock). The pricing/trade engine, loops, UI views
and dividend logic stay in Restocker_main and are bound from the core module."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
STOCK_DIVIDEND_PCT = core.STOCK_DIVIDEND_PCT
STOCK_LIMIT_ORDERS_ENABLED = core.STOCK_LIMIT_ORDERS_ENABLED
StockPanelView = core.StockPanelView
_build_market_dashboard_embed = core._build_market_dashboard_embed
_build_stock_panel_embed = core._build_stock_panel_embed
_check_limit_orders = core._check_limit_orders
_exec_stock_buy = core._exec_stock_buy
_exec_stock_sell = core._exec_stock_sell
_etf_invest = core._etf_invest
_etf_redeem = core._etf_redeem
_etf_info_embed = core._etf_info_embed
_etf_nav = core._etf_nav
_get_market = core._get_market
_is_market_manager = core._is_market_manager
_load_markets = core._load_markets
_public_market_autocomplete = core._public_market_autocomplete
_recompute_share_price = core._recompute_share_price
_remember_holder_name = core._remember_holder_name
is_manager = core.is_manager
_market_backing = core._market_backing
_get_insurance_fund = core._get_insurance_fund
_add_insurance_fund = core._add_insurance_fund
add_coins = core.add_coins
STOCK_BACK_CASH_PCT = core.STOCK_BACK_CASH_PCT
STOCK_BACK_ASSET_PCT = core.STOCK_BACK_ASSET_PCT
STOCK_BACK_FUND_PCT = core.STOCK_BACK_FUND_PCT
save_yaml = core.save_yaml

class StockCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    stock = app_commands.Group(name="stock", description="Buy and sell shares of markets that have gone public, priced off their real CSN profit")

    @stock.command(name="list", description="See every market currently listed on the stock exchange")
    async def stock_list(self, interaction: discord.Interaction):
        import Restocker_db as _db
        public = _db.get_public_markets()
        if not public:
            return await interaction.response.send_message(
                "📭 No markets are public yet. `/market go_public` to list one.", ephemeral=True
            )
        data = _load_markets()
        markets = data.get("markets", {})
        lines = []
        for mid, listing in sorted(public.items(), key=lambda kv: -kv[1]["share_price"]):
            name = markets.get(mid, {}).get("name", mid)
            lines.append(
                f"**{name}** (`{mid}`) — `{listing['share_price']:,.2f}` 🪙/share "
                f"· `{listing['shares_outstanding']:,.0f}` shares · {listing['pe_multiplier']:,.1f}x P/E"
            )
        embed = discord.Embed(title="📈 Stock Exchange", description="\n".join(lines), color=0x3498DB)
        await interaction.response.send_message(embed=embed)

    @stock.command(name="price", description="Check a market's current share price and recent pricing history")
    @app_commands.describe(market_id="The public market to check")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_price(self, interaction: discord.Interaction, market_id: str):
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing or not listing.get("active"):
            return await interaction.response.send_message(f"❌ `{market_id}` isn't public.", ephemeral=True)
        market = _get_market(market_id) or {}
        history = _db.get_price_history(market_id, limit=5)
        embed = discord.Embed(title=f"📊 {market.get('name', market_id)} — `{market_id}`", color=0x3498DB)
        embed.add_field(name="Share Price", value=f"`{listing['share_price']:,.2f}` 🪙", inline=True)
        embed.add_field(name="Shares Outstanding", value=f"`{listing['shares_outstanding']:,.0f}`", inline=True)
        embed.add_field(name="P/E Multiplier", value=f"`{listing['pe_multiplier']:,.1f}x`", inline=True)
        embed.add_field(name="Last Priced", value=str(listing.get("last_priced_month") or "—"), inline=True)
        # Backing summary folded in here (the standalone /stock backing command was retired
        # 2026-07-15). _market_backing() still powers /stock delist.
        try:
            b = _market_backing(market_id)
            backed = f"**{b['total_pct']:.1f}%** of cap (target {b['target_pct']:.0f}%)"
            if not b["ok"]:
                backed += " · ⚠ under-backed"
            embed.add_field(name="Backed", value=backed, inline=False)
        except Exception:
            pass
        try:
            _assets = float(_db.get_config(f"asset_value:{market_id}") or 0.0)
        except Exception:
            _assets = 0.0
        if _assets > 0:
            _floor = (_assets + float(_db.get_treasury(market_id) or 0.0)) / max(1.0, float(listing.get("shares_outstanding") or 1.0))
            embed.add_field(name="Book Value",
                            value=f"assets `{int(_assets):,}` 🪙 → price floor `{_floor:,.2f}` 🪙/share",
                            inline=False)
        if history:
            lines = [f"`{h['price']:,.2f}` 🪙 — {h['reason'] or '?'} ({h['logged_at']})" for h in history]
            embed.add_field(name="Recent Price Changes", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed)

    @stock.command(name="buy", description="Buy shares of a public market using your server currency")
    @app_commands.describe(market_id="The public market to invest in", shares="How many shares to buy")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_buy(self,
        interaction: discord.Interaction,
        market_id: str,
        shares: app_commands.Range[int, 1, 1_000_000],
    ):
        ok, msg = _exec_stock_buy(interaction.user.id, market_id, shares, interaction.user.display_name)
        await interaction.response.send_message(msg, ephemeral=not ok)

    @stock.command(name="sell", description="Sell shares of a public market back for server currency")
    @app_commands.describe(market_id="The market you hold shares in", shares="How many shares to sell")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_sell(self,
        interaction: discord.Interaction,
        market_id: str,
        shares: app_commands.Range[int, 1, 1_000_000],
    ):
        ok, msg = _exec_stock_sell(interaction.user.id, market_id, shares, interaction.user.display_name)
        await interaction.response.send_message(msg, ephemeral=not ok)

    @stock.command(name="panel", description="Open an interactive live trading panel for a market")
    @app_commands.describe(market_id="The public market to trade")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_panel(self, interaction: discord.Interaction, market_id: str):
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing or not listing.get("active"):
            return await interaction.response.send_message(f"❌ `{market_id}` isn't public.", ephemeral=True)
        embed = _build_stock_panel_embed(market_id)
        await interaction.response.send_message(embed=embed, view=StockPanelView(market_id))

    @stock.command(name="dashboard", description="(Manager) Post a live, auto-updating market dashboard in this channel")
    async def stock_dashboard(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        msg = await interaction.channel.send(embed=_build_market_dashboard_embed())
        try:
            await msg.pin()
        except Exception:
            pass
        save_yaml("stock_dashboard.yml", {"channel_id": interaction.channel.id, "message_id": msg.id})
        await interaction.followup.send(
            "✅ Live market dashboard posted and pinned here — it refreshes every 5 minutes.", ephemeral=True
        )

    @stock.command(name="portfolio", description="See your stock holdings and unrealized profit/loss")
    @app_commands.describe(user="(Manager) View another user's portfolio")
    async def stock_portfolio(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user
        if user and user.id != interaction.user.id and not is_manager(interaction):
            return await interaction.response.send_message(
                "⛔ Managers only can view someone else's portfolio.", ephemeral=True
            )

        import Restocker_db as _db
        holdings = _db.get_portfolio(target.id)
        if not holdings:
            return await interaction.response.send_message(f"📭 {target.mention} doesn't own any shares.", ephemeral=True)

        data = _load_markets()
        markets = data.get("markets", {})
        lines = []
        total_value = 0.0
        total_cost = 0.0
        for h in holdings:
            listing = _db.get_market_shares(h["market_id"])
            price = float(listing["share_price"]) if listing else 0.0
            value = price * h["shares"]
            total_value += value
            total_cost += h["cost_basis"]
            name = markets.get(h["market_id"], {}).get("name", h["market_id"])
            pl = value - h["cost_basis"]
            lines.append(
                f"**{name}** (`{h['market_id']}`) — `{h['shares']:,.0f}` shares @ `{price:,.2f}` 🪙 "
                f"= `{value:,.0f}` 🪙 ({'📈' if pl >= 0 else '📉'} `{pl:+,.0f}` 🪙)"
            )

        embed = discord.Embed(title=f"💼 {target.display_name}'s Portfolio", color=0x9B59B6)
        embed.add_field(name="Holdings", value="\n".join(lines), inline=False)
        embed.add_field(name="Total Value", value=f"`{total_value:,.0f}` 🪙", inline=True)
        embed.add_field(name="Total Cost Basis", value=f"`{total_cost:,.0f}` 🪙", inline=True)
        embed.add_field(name="Unrealized P/L", value=f"`{(total_value - total_cost):+,.0f}` 🪙", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=(target.id == interaction.user.id))

    # /stock holders removed 2026-07-15 (economy not advanced enough yet). The DB helper
    # get_holders() stays — set_params still uses it. Restore the command from git history.

    @stock.command(
        name="set_params",
        description="(Manager) Tune a public market's shares outstanding / P-E / treasury",
    )
    @app_commands.describe(
        market_id="The market to tune",
        shares_outstanding="New total shares outstanding",
        pe_multiplier="New price multiplier applied to monthly net profit per share",
        treasury="Company cash on hand (e.g. the Lands balance) — shows as Treasury and backs the shares",
        assets="Book value of company assets (hive fleet, factories). Price floor = (assets + treasury) ÷ shares. 0 clears.",
    )
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_set_params(self,
        interaction: discord.Interaction,
        market_id: str,
        shares_outstanding: Optional[app_commands.Range[float, 1.0, 100_000_000.0]] = None,
        pe_multiplier: Optional[app_commands.Range[float, 0.1, 1000.0]] = None,
        treasury: Optional[app_commands.Range[float, 0.0, 1_000_000_000_000.0]] = None,
        assets: Optional[app_commands.Range[float, 0.0, 1_000_000_000_000.0]] = None,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing:
            return await interaction.response.send_message(f"❌ `{market_id}` has never been public.", ephemeral=True)
        if treasury is not None:
            _db.upsert_market_shares(market_id, treasury_coins=float(treasury))
        if assets is not None:
            if float(assets) > 0:
                _db.set_config(f"asset_value:{market_id}", str(float(assets)))
            else:
                _db.delete_config(f"asset_value:{market_id}")
        if shares_outstanding is None and pe_multiplier is None:
            if treasury is None and assets is None:
                return await interaction.response.send_message(
                    "❌ Provide at least one of `shares_outstanding`, `pe_multiplier`, `treasury`, or `assets`.",
                    ephemeral=True)
            # Treasury / book value changed — deliberate management action, so re-anchor
            # the price fully onto the new fundamental (no per-event clamp).
            price = _recompute_share_price(market_id, reason="params_changed", full_move=True)
            bits = []
            if treasury is not None:
                bits.append(f"treasury **{int(treasury):,}** 🪙")
            if assets is not None:
                bits.append(f"asset book value **{int(assets):,}** 🪙" if float(assets) > 0
                            else "asset book value **cleared**")
            msg = f"✅ `{market_id}` updated: " + " · ".join(bits)
            if assets is not None and float(assets) > 0:
                msg += "\nPrice floor = (assets + treasury) ÷ shares outstanding."
            if price is not None:
                msg += f"\nShare price after re-anchor: `{price:,.2f}` 🪙"
            return await interaction.response.send_message(msg)
        if shares_outstanding is not None:
            held = sum(float(h.get("shares") or 0) for h in _db.get_holders(market_id))
            if float(shares_outstanding) < held:
                return await interaction.response.send_message(
                    f"❌ Holders already own `{held:,.0f}` shares — shares outstanding can't go below that. "
                    f"Buy shares back first or pick a number ≥ `{held:,.0f}`.", ephemeral=True)

        _db.upsert_market_shares(market_id, shares_outstanding=shares_outstanding, pe_multiplier=pe_multiplier)
        price = _recompute_share_price(market_id, reason="params_changed")
        shown_price = price if price is not None else listing["share_price"]
        await interaction.response.send_message(f"✅ `{market_id}` updated. New share price: `{shown_price:,.2f}` 🪙.")

    # /stock limit_buy, limit_sell, limit_list, limit_cancel removed 2026-07-15 — limit/trigger
    # orders are more than this economy needs yet ("we're not that far"). The engine is left
    # intact (STOCK_LIMIT_ORDERS_ENABLED flag, _db.add_limit_order / get_limit_order /
    # get_user_limit_orders / cancel_limit_order, _check_limit_orders), so restoring the four
    # commands later is just a git revert of this block.

    @stock.command(name="dividends", description="Show (or set) a market's shareholder dividend payout")
    @app_commands.describe(market_id="Public market",
                           set_pct="(Manager/Owner) Set this market's dividend % of monthly net (0 disables)")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_dividends(self, interaction: discord.Interaction, market_id: str, set_pct: Optional[float] = None):
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing:
            return await interaction.response.send_message(f"❌ `{market_id}` isn't listed.", ephemeral=True)
        if set_pct is not None:
            if not _is_market_manager(interaction, market_id):
                return await interaction.response.send_message("⛔ Managers or this market's owner only.", ephemeral=True)
            set_pct = max(0.0, min(100.0, float(set_pct)))
            _db.upsert_market_shares(market_id, dividend_pct=set_pct)
            return await interaction.response.send_message(
                f"✅ `{market_id}` dividend rate set to `{set_pct:.1f}%` of monthly net "
                f"({'paid to shareholders on each CSN report' if set_pct > 0 else 'dividends off for this market'}).",
                ephemeral=True)
        market = _get_market(market_id) or {}
        ov = listing.get("dividend_pct")
        eff = float(ov) if ov is not None else STOCK_DIVIDEND_PCT
        last = _db.get_last_dividend(market_id)
        embed = discord.Embed(title=f"💸 {market.get('name', market_id)} — Dividends", color=0x9B59B6)
        embed.add_field(name="Payout rate", value=(f"`{eff:.1f}%` of monthly net" if eff > 0 else "Off"), inline=True)
        embed.add_field(name="Source", value=("market override" if ov is not None else "server default"), inline=True)
        embed.add_field(name="Last paid month", value=str(listing.get("last_dividend_month") or "—"), inline=True)
        if last:
            embed.add_field(name="Last distribution",
                            value=f"`{int(last['total_paid']):,}` 🪙 to `{last['holders']}` holders "
                                  f"(`{float(last['per_share']):,.2f}`/share) — {last['month']}", inline=False)
        embed.set_footer(text="Dividends pay to shareholders pro-rata automatically on each CSN report.")
        await interaction.response.send_message(embed=embed)


    # ── ABX Index Fund (investable ETF) ──────────────────────────────────────
    @stock.command(name="invest_index",
                   description="Invest coins into the ABX Index — buys the whole market basket by cap weight")
    @app_commands.describe(coins="How many coins to invest into the index")
    async def invest_index(self, interaction: discord.Interaction,
                           coins: app_commands.Range[int, 1, 1_000_000_000]):
        await interaction.response.defer(ephemeral=True)
        r = _etf_invest(interaction.user.id, coins, interaction.user.display_name)
        await interaction.followup.send(r["msg"], ephemeral=True)

    @stock.command(name="sell_index",
                   description="Redeem ABX Index units back for coins (sells the basket at market)")
    @app_commands.describe(units="How many units to redeem, or leave blank to redeem ALL")
    async def sell_index(self, interaction: discord.Interaction, units: Optional[float] = None):
        await interaction.response.defer(ephemeral=True)
        r = _etf_redeem(interaction.user.id, units if units is not None else "all",
                        interaction.user.display_name)
        await interaction.followup.send(r["msg"], ephemeral=True)

    @stock.command(name="index_fund",
                   description="See the ABX Index fund: NAV, size, weights, and your stake")
    async def index_fund(self, interaction: discord.Interaction):
        import Restocker_db as _db
        embed = _etf_info_embed()
        try:
            held = float(_db.get_etf_units(str(interaction.user.id)) or 0)
        except Exception:
            held = 0.0
        if held > 0:
            nav = _etf_nav()["nav"]
            embed.add_field(
                name="Your stake",
                value=f"{held:,.4f} units · ~`{int(held * nav):,}` coins at current NAV",
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


    # /stock backing removed 2026-07-15 — its headline number (total backed % vs target) now
    # shows inside /stock price. _market_backing() stays (delist still uses it); restore the
    # full breakdown command from git history if the detail is wanted again.

    # /stock golive + /stock import_captable removed 2026-07-16 — one-shot V Tech/GEX
    # merger tooling, applied on the live bot that day (100,000 shares @ 1,000; see
    # golive_gex.py in git history). Post-merger the bot's book is authoritative, so a
    # raw Crimson re-import would clobber the converted holdings — deliberately gone.

    @stock.command(name="set_label",
                   description="(Manager) Name the COMPANY a stock represents — shown on the exchange instead of the market name")
    @app_commands.describe(
        market_id="The market the stock lives on (e.g. main)",
        label="Company name shown for the stock, e.g. 'V Tech'. Leave blank to reset to the market's name.")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_set_label(self, interaction: discord.Interaction, market_id: str,
                              label: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        if not _db.get_market_shares(market_id):
            return await interaction.response.send_message(
                f"❌ `{market_id}` isn't a listed stock.", ephemeral=True)
        text = (label or "").strip()[:60]
        if not text:
            _db.delete_config(f"stock_label:{market_id}")
            return await interaction.response.send_message(
                f"✅ Stock label cleared — the exchange shows `{market_id}`'s own market name again.",
                ephemeral=True)
        _db.set_config(f"stock_label:{market_id}", text)
        await interaction.response.send_message(
            f"✅ The stock on `{market_id}` now displays as **{text}** on the exchange, cap table "
            f"and index — the market itself keeps its own name in the ledger and reports.",
            ephemeral=True)

    @stock.command(name="apply_roles",
                   description="(Manager) Give every current shareholder of a market a role")
    @app_commands.describe(market_id="The listed market (import its cap table first)",
                           role="Role to assign (default: find or create 'Shareholder')")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_apply_roles(self, interaction: discord.Interaction, market_id: str,
                                role: Optional[discord.Role] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Run this in the server.", ephemeral=True)
        import Restocker_db as _db
        holders = [h for h in (_db.get_holders(market_id) or [])
                   if float(h.get("shares") or 0) > 0]
        if not holders:
            return await interaction.response.send_message(
                f"❌ `{market_id}` has no holders on record.",
                ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if role is None:
            role = discord.utils.get(guild.roles, name="Shareholder")
            if role is None:
                try:
                    role = await guild.create_role(name="Shareholder", reason=f"{market_id} shareholders")
                except Exception as e:
                    return await interaction.followup.send(f"❌ Couldn't create the role: {e}", ephemeral=True)
        import asyncio as _aio
        added, had, absent, failed = [], [], [], []
        for h in holders:
            uid = int(h["user_id"])
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except Exception:
                    member = None
            if member is None:
                absent.append(str(uid))
                continue
            if role in member.roles:
                had.append(member)
                continue
            try:
                await member.add_roles(role, reason=f"{market_id} shareholder")
                added.append(member)
            except Exception:
                failed.append(member)
            await _aio.sleep(0.4)
        msg = (f"🏷️ **{role.mention}** applied to `{market_id}` holders.\n"
               f"• Added: **{len(added)}**" + (" — " + ", ".join(m.mention for m in added[:20]) if added else "") +
               f"\n• Already had it: **{len(had)}**")
        if absent:
            msg += f"\n• Not on this server: **{len(absent)}** — " + ", ".join(f"<@{u}>" for u in absent[:15])
        if failed:
            msg += (f"\n• ⚠ Couldn't assign to {len(failed)} (move the bot's role above {role.mention}): "
                    + ", ".join(m.mention for m in failed[:10]))
        await interaction.followup.send(msg[:1900], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @stock.command(name="delist",
                   description="(Manager/Owner) Bankrupt + delist a market, paying shareholders from its backing")
    @app_commands.describe(market_id="Market to delist", confirm="Set true to actually pay out + remove the stock")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def delist(self, interaction: discord.Interaction, market_id: str, confirm: bool = False):
        if not (is_manager(interaction) or _is_market_manager(interaction, market_id)):
            return await interaction.response.send_message("Managers / market owner only.", ephemeral=True)
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing or not listing.get("active"):
            return await interaction.response.send_message(f"`{market_id}` isn't a listed stock.", ephemeral=True)
        m = _get_market(market_id) or {}
        name = m.get("name", market_id)
        holders = _db.get_holders(market_id)
        total_shares = sum(float(h.get("shares") or 0) for h in holders)
        b = _market_backing(market_id)
        pool = int(b["cashable"])  # treasury + this market's fund share = real coins payable
        if not confirm:
            return await interaction.response.send_message(
                f"⚠️ Delisting **{name}** pays ~`{pool:,}` coins (cash `{int(b['cash']):,}` + fund "
                f"`{int(b['fund_share']):,}`) pro-rata to **{len(holders)}** holder(s), then removes the stock. "
                f"Asset backing (`{int(b['assets']):,}`) is honored off-exchange by the owner. "
                f"Re-run with `confirm:true`.", ephemeral=True)
        # Re-entrancy guard: holders/pool were snapshotted above, and defer() yields the event
        # loop — a second `/stock delist confirm:true` dispatched in that window would pass the
        # same active-listing check and pay EVERY holder twice from the same snapshot. Claim the
        # market synchronously (no await between check and set) before the first await.
        busy = getattr(type(self), "_delisting_now", None)
        if busy is None:
            busy = type(self)._delisting_now = set()
        if market_id in busy:
            return await interaction.response.send_message(
                "⏳ A delist for this market is already in progress.", ephemeral=True)
        busy.add(market_id)
        try:
            await interaction.response.defer()   # payouts can exceed the 3s interaction window
            return await self._delist_payout(interaction, market_id, name, holders, total_shares, pool, b)
        finally:
            busy.discard(market_id)

    async def _delist_payout(self, interaction, market_id, name, holders, total_shares, pool, b):
        import Restocker_db as _db
        if total_shares <= 0 or pool <= 0:
            _db.upsert_market_shares(market_id, active=0)
            return await interaction.followup.send(
                f"🪦 **{name}** delisted. No payout ({'no holders' if total_shares<=0 else 'no cash backing'}).")
        paid = 0
        failed = []
        for h in holders:
            sh = float(h.get("shares") or 0)
            amt = int(pool * (sh / total_shares))
            try:
                if amt > 0:
                    add_coins(int(h["user_id"]), amt, counts_as_principal=True)
                    paid += amt
                # Only clear the holding AFTER the payout succeeded — a failed credit
                # must never cost a shareholder their shares.
                _db.adjust_holding(h["user_id"], market_id, delta_shares=-sh,
                                   delta_cost_basis=-float(h.get("cost_basis") or 0))
            except Exception:
                failed.append(str(h.get("user_id")))
        # remove exactly what we paid from the backing sources (treasury first, then fund)
        from_treasury = min(int(b["cash"]), paid)
        from_fund = paid - from_treasury
        try:
            if from_treasury > 0:
                _db.adjust_treasury(market_id, -float(from_treasury), allow_negative=False)
            if from_fund > 0:
                _add_insurance_fund(-float(from_fund))
        except Exception:
            pass
        note = ""
        if failed:
            # Keep the listing ACTIVE so re-running /stock delist retries the unpaid
            # holders (paid holders' shares are already cleared, so the retry only
            # sees the remainder and the remaining backing).
            note = (f"\n⚠️ {len(failed)} holder(s) could not be paid — their shares were KEPT and the "
                    f"listing stays active: " + ", ".join(f"<@{u}>" for u in failed[:10])
                    + ". Re-run `/stock delist confirm:true` to retry them.")
        else:
            _db.upsert_market_shares(market_id, active=0)
        await interaction.followup.send(
            f"🪦 **{name}** declared bankrupt{' & delisted' if not failed else ''}. Paid `{paid:,}` coins to "
            f"**{len(holders) - len(failed)}** shareholder(s) pro-rata "
            f"(cash `{from_treasury:,}` + fund `{from_fund:,}`).{note}")


async def setup(bot):
    await bot.add_cog(StockCog(bot))
