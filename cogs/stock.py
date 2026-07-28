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


async def _shares_amount_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest share amounts for /stock buy & sell: your MAX (affordable coins capped by
    free float on buy; your whole holding on sell) plus round sizes with their cost."""
    import Restocker_db as _db
    out = []
    price = 0.0
    try:
        market_id = str(getattr(interaction.namespace, "market_id", "") or "")
        uid = str(interaction.user.id)
        cmd = interaction.command.name if interaction.command else ""
        listing = _db.get_market_shares(market_id) if market_id else None
        price = float((listing or {}).get("share_price") or 0)
        if cmd == "sell":
            cap = int(float((_db.get_holding(uid, market_id) or {}).get("shares") or 0))
            tag = "your whole holding"
        else:
            bal = float((_db.get_balance(uid) or {}).get("coins") or 0)
            so = float((listing or {}).get("shares_outstanding") or 0)
            held = sum(float(h.get("shares") or 0) for h in _db.get_holders(market_id)) if market_id else 0.0
            flt = max(0, int(so - held))
            afford = int(bal // price) if price > 0 else 0
            cap = min(afford, flt) if flt else afford
            tag = f"your max (float {flt:,} · afford {afford:,})"
        if current.strip().isdigit() and int(current) > 0:
            n = int(current)
            out.append(app_commands.Choice(
                name=(f"{n:,}" + (f" ≈ {int(n * price):,} ¢" if price > 0 else ""))[:100], value=n))
        if cap >= 1 and (not current or str(cap).startswith(current.strip())
                         or not current.strip().isdigit()):
            out.append(app_commands.Choice(
                name=(f"{cap:,} — {tag}" + (f" ≈ {int(cap * price):,} ¢" if price > 0 else ""))[:100],
                value=cap))
        for n in (1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000):
            if n >= (cap or 10**9):
                break
            if current and current.strip().isdigit() and not str(n).startswith(current.strip()):
                continue
            out.append(app_commands.Choice(
                name=(f"{n:,}" + (f" ≈ {int(n * price):,} ¢" if price > 0 else ""))[:100], value=n))
    except Exception:
        pass
    # de-dup by value, keep order
    seen, uniq = set(), []
    for c in out:
        if c.value not in seen:
            seen.add(c.value)
            uniq.append(c)
    return uniq[:25]


class StockCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    stock = app_commands.Group(name="stock", description="Buy and sell shares of markets that have gone public, priced off their real CSN profit")

    @stock.command(name="drip",
                   description="Dividend reinvestment: auto-buy shares with your dividends instead of taking coins")
    @app_commands.describe(enabled="On = dividends & GEX.PR payouts buy more shares automatically")
    async def stock_drip(self, interaction: discord.Interaction, enabled: bool):
        import Restocker_db as _db
        uid = str(interaction.user.id)
        if enabled:
            _db.set_config(f"drip:{uid}", "1")
            msg = ("🌱 **DRIP on** — from now on your dividends and GEX.PR payouts "
                   "auto-buy shares at market (whole shares; remainder stays as coins). "
                   "Compounding, the eighth wonder of Abexilas.")
        else:
            _db.delete_config(f"drip:{uid}")
            msg = "💰 **DRIP off** — payouts arrive as coins again."
        await interaction.response.send_message(msg, ephemeral=True)

    @stock.command(name="buyback",
                   description="(Manager) Retire free-float shares — fewer shares against the same cap raises the floor")
    @app_commands.describe(market_id="Which listing", shares="How many unissued (free-float) shares to retire")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_buyback(self, interaction: discord.Interaction, market_id: str,
                            shares: app_commands.Range[int, 1, 10_000_000]):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing or not listing.get("active"):
            return await interaction.response.send_message("❌ Not a listed market.", ephemeral=True)
        so = float(listing.get("shares_outstanding") or 0)
        held = sum(float(h.get("shares") or 0) for h in _db.get_holders(market_id))
        free_float = max(0.0, so - held)
        if shares > free_float:
            return await interaction.response.send_message(
                f"❌ Only `{free_float:,.0f}` unissued share(s) in the float — holders' shares "
                f"can't be retired (they'd have to sell first).", ephemeral=True)
        old_price = float(listing.get("share_price") or 0)
        _db.upsert_market_shares(market_id, shares_outstanding=so - shares)
        new_price = _recompute_share_price(market_id, reason="buyback", full_move=True)
        await interaction.response.send_message(
            f"🔥 Retired `{shares:,}` share(s) of `{market_id}`: "
            f"`{so:,.0f}` → `{so - shares:,.0f}` outstanding.\n"
            f"Price floor per share: `{old_price:,.2f}` → `{new_price:,.2f}` 🪙 "
            f"(same cap, fewer shares — every holder's slice got bigger).", ephemeral=False)

    @stock.command(name="buy", description="Buy shares of a public market using your server currency")
    @app_commands.describe(market_id="The public market to invest in",
                           shares="How many shares to buy (suggestions show your max and the cost)")
    @app_commands.autocomplete(market_id=_public_market_autocomplete, shares=_shares_amount_autocomplete)
    async def stock_buy(self,
        interaction: discord.Interaction,
        market_id: str,
        shares: app_commands.Range[int, 1, 1_000_000],
    ):
        ok, msg = _exec_stock_buy(interaction.user.id, market_id, shares, interaction.user.display_name)
        await interaction.response.send_message(msg, ephemeral=not ok)

    @stock.command(name="sell", description="Sell shares of a public market back for server currency")
    @app_commands.describe(market_id="The market you hold shares in",
                           shares="How many shares to sell (suggestions show your holding)")
    @app_commands.autocomplete(market_id=_public_market_autocomplete, shares=_shares_amount_autocomplete)
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
        sellable="Assets currently FOR SALE (hive batches, claims) — counts as liquid backing, not valuation. 0 clears.",
    )
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def stock_set_params(self,
        interaction: discord.Interaction,
        market_id: str,
        shares_outstanding: Optional[app_commands.Range[float, 1.0, 100_000_000.0]] = None,
        pe_multiplier: Optional[app_commands.Range[float, 0.1, 1000.0]] = None,
        treasury: Optional[app_commands.Range[float, 0.0, 1_000_000_000_000.0]] = None,
        assets: Optional[app_commands.Range[float, 0.0, 1_000_000_000_000.0]] = None,
        sellable: Optional[app_commands.Range[float, 0.0, 1_000_000_000_000.0]] = None,
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
        if sellable is not None:
            if float(sellable) > 0:
                _db.set_config(f"sellable_assets:{market_id}", str(float(sellable)))
            else:
                _db.delete_config(f"sellable_assets:{market_id}")
        if shares_outstanding is None and pe_multiplier is None:
            if treasury is None and assets is None and sellable is None:
                return await interaction.response.send_message(
                    "❌ Provide at least one of `shares_outstanding`, `pe_multiplier`, `treasury`, "
                    "`assets`, or `sellable`.",
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
            if sellable is not None:
                bits.append(f"sellable (for-sale) assets **{int(sellable):,}** 🪙 → liquid backing"
                            if float(sellable) > 0 else "sellable assets **cleared**")
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
        # AUDIT FIX (critical): (1) re-snapshot AFTER the defer — a sell timed into
        # the defer window used to collect the sale AND the delist payout on shares
        # it no longer held; (2) never int()-cast holder ids — the ABX index fund
        # account ("ABX_INDEX_FUND") crashed the loop AFTER the treasury was drained,
        # leaving a zombie active listing. The fund is paid like any holder; its
        # payout becomes fund cash, and NAV carries it to unit holders.
        holders = _db.get_holders(market_id)
        total_shares = sum(float(h.get("shares") or 0) for h in holders)
        b = _market_backing(market_id)
        pool = int(b["cashable"])
        if total_shares <= 0 or pool <= 0:
            _db.upsert_market_shares(market_id, active=0)
            return await interaction.followup.send(
                f"🪦 **{name}** delisted. No payout ({'no holders' if total_shares<=0 else 'no cash backing'}).")
        paid = 0
        failed = []
        for h in holders:
            sh = float(h.get("shares") or 0)
            cb = float(h.get("cost_basis") or 0)
            amt = int(pool * (sh / total_shares))
            uid = str(h.get("user_id"))
            try:
                # Claim-first: clear the holding, THEN pay — a concurrent sell can't
                # double-dip. If the credit fails, the holding is restored.
                _db.adjust_holding(uid, market_id, delta_shares=-sh, delta_cost_basis=-cb)
            except Exception:
                failed.append(uid)
                continue
            try:
                if amt > 0:
                    add_coins(uid, amt, counts_as_principal=True)
                    paid += amt
            except Exception:
                try:
                    _db.adjust_holding(uid, market_id, delta_shares=sh, delta_cost_basis=cb)
                except Exception:
                    pass
                failed.append(uid)
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
