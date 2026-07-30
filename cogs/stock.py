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
    """Suggest share amounts for share trades: your MAX (affordable coins capped by
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








    # /stock limit_buy, limit_sell, limit_list, limit_cancel removed 2026-07-15 — limit/trigger
    # orders are more than this economy needs yet ("we're not that far"). The engine is left
    # intact (STOCK_LIMIT_ORDERS_ENABLED flag, _db.add_limit_order / get_limit_order /
    # get_user_limit_orders / cancel_limit_order, _check_limit_orders), so restoring the four
    # commands later is just a git revert of this block.



    # ── ABX Index Fund (investable ETF) ──────────────────────────────────────





async def setup(bot):
    await bot.add_cog(StockCog(bot))



# ─────────────────────────────────────────────────────────────────────────────
#  Bankruptcy delist, extracted from the retired /stock delist so MarketSettings
#  can call it. Bodies unchanged except `self` -> module-level state.
#  The re-entrancy guard MUST stay module-level: it is what stops two confirms
#  dispatched inside the defer window from paying every holder twice.
# ─────────────────────────────────────────────────────────────────────────────
_DELISTING_NOW: set = set()

async def run_stock_delist(interaction: discord.Interaction, market_id: str, confirm: bool = False):
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
    # Module-level, not per-cog: the panel and any future caller must share ONE guard.
    busy = _DELISTING_NOW
    if market_id in busy:
        return await interaction.response.send_message(
            "⏳ A delist for this market is already in progress.", ephemeral=True)
    busy.add(market_id)
    try:
        await interaction.response.defer()   # payouts can exceed the 3s interaction window
        return await _run_delist_payout(interaction, market_id, name, holders, total_shares, pool, b)
    finally:
        busy.discard(market_id)

async def _run_delist_payout(interaction, market_id, name, holders, total_shares, pool, b):
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
