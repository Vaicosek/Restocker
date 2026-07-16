"""Admin maintenance commands (/admin)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional
import asyncio

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
DEFAULT_MARKET_ID = core.DEFAULT_MARKET_ID
EMPLOYEE_ROLE_NAME = core.EMPLOYEE_ROLE_NAME
WORKER_CHANNEL_ID = core.WORKER_CHANNEL_ID
_is_market_manager = core._is_market_manager
_get_market = core._get_market
_load_csn_for_market = core._load_csn_for_market
_load_markets = core._load_markets
_market_autocomplete = core._market_autocomplete
_recompute_share_price = core._recompute_share_price
_save_csn_for_market = core._save_csn_for_market
_save_markets = core._save_markets
is_manager = core.is_manager
log = core.log
save_yaml = core.save_yaml


# ── Shared repair planners ───────────────────────────────────────────────────
# Both the individual /admin commands and /admin repair_all build their plans here,
# so the two can never drift apart.

def _order_is_fulfilled(o: dict) -> bool:
    status = str(o.get("status", "")).lower()
    if "fulfil" in status or status in ("complete", "done", "closed"):
        return True
    req, prod = int(o.get("requested", 0) or 0), int(o.get("produced", 0) or 0)
    return req > 0 and prod >= req


def _order_worker_pairs(o: dict) -> list:
    """[(user_id, qty)] credited for this order — from claims, else the whole order to
    claimed_by. Empty means the order is ORPHANED: fulfilled with nobody attached."""
    pairs = [(str(c.get("user_id") or ""), int(c.get("qty") or 0))
             for c in (o.get("claims") or [])]
    if not pairs and o.get("claimed_by"):
        pairs = [(str(o.get("claimed_by")), int(o.get("produced") or o.get("requested") or 0))]
    return [(u, q) for (u, q) in pairs if u and q > 0]


def _payout_repair_plan(_db, items_data, orders) -> list:
    """Orders the OLD exact-match price lookup zeroed (so the worker was silently skipped),
    that the tolerant lookup can now price. Returns [(order, uid, qty, owed)].

    Safe to re-run: anything already priced fine back then was paid and is excluded, and
    anything already repaired carries a `repair:order#N` ledger row and is excluded too."""
    catalog = (items_data or {}).get("items", {}) or {}

    def _old_price(item_name):
        try:
            return int((catalog.get(item_name) or {}).get("coin", 0) or 0)
        except Exception:
            return 0

    plan = []
    for o in orders:
        if not _order_is_fulfilled(o):
            continue
        if _old_price(o.get("item", "")) > 0:
            continue                                   # priced fine then → already paid
        for uid, qty in _order_worker_pairs(o):
            try:
                if _db.coin_ledger_has(uid, f"repair:order#{o.get('id')}"):
                    continue                           # already repaired
            except Exception:
                continue                               # can't verify → never risk double-pay
            try:
                owed = int(core._coins_for_pieces(o, qty, items_data))
            except Exception:
                owed = 0
            if owed > 0:
                plan.append((o, uid, qty, owed))
    return plan


def _team_backfill_plan(_db, items_data, orders) -> tuple:
    """Team-ledger rows dropped at approval time. Returns (to_write, per_worker_summary).
    Idempotent — skips anything already in the ledger."""
    plan, to_write = {}, []
    for o in orders:
        if not _order_is_fulfilled(o):
            continue
        for wid, qty in _order_worker_pairs(o):
            detail = f"order#{o.get('id')}"
            if _db.team_perf_exists(wid, detail, "order"):
                continue
            mgr = _db.get_manager_of(wid)
            if mgr:
                manager_id = str(mgr)
            elif _db.get_team(wid):
                manager_id = wid
            else:
                continue                               # no team → nothing to attribute to
            try:
                coins = int(core._coins_for_pieces(o, qty, items_data))
            except Exception:
                coins = 0
            if coins <= 0:
                continue
            to_write.append((manager_id, wid, qty, coins, detail))
            p = plan.setdefault(wid, {"orders": 0, "coins": 0})
            p["orders"] += 1
            p["coins"] += coins
    return to_write, plan


def _orphaned_orders(orders) -> list:
    """Fulfilled orders with NO worker attached at all — they can never be paid or credited
    automatically because nothing records who did the work. These need /admin repair_order."""
    return [o for o in orders if _order_is_fulfilled(o) and not _order_worker_pairs(o)]


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    admin = app_commands.Group(name="admin", description="(Managers) Destructive maintenance — guarded by confirm", default_permissions=discord.Permissions(manage_guild=True))

    @admin.command(name="wipe", description="(Managers) Destructive wipe — requires confirm")
    @app_commands.describe(
        target="What to wipe",
        confirm="Safety phrase: the market ID for market/market_csn, or 'CONFIRM' for stock/employee_dms",
        market_id="Required for the 'market' and 'market_csn' targets",
        limit_per_user="employee_dms only — messages to scan per user (0 = all)",
    )
    @app_commands.choices(target=[
        app_commands.Choice(name="All stock-exchange data", value="stock"),
        app_commands.Choice(name="A market — full wipe (registration, items, CSN)", value="market"),
        app_commands.Choice(name="A market's CSN-sourced months (keep manual earnings)", value="market_csn"),
        app_commands.Choice(name="A market's per-item sales (keep monthly earnings totals)", value="market_sales"),
        app_commands.Choice(name="Employee bot DMs", value="employee_dms"),
    ])
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def admin_wipe(self, 
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        confirm: str = "",
        market_id: Optional[str] = None,
        limit_per_user: app_commands.Range[int, 0, 5000] = 0,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        t = target.value
        import Restocker_db as _db

        if t == "stock":
            if confirm.strip().upper() != "CONFIRM":
                return await interaction.response.send_message(
                    "⚠️ This **permanently deletes ALL stock data** — every listing, holding, trade "
                    "and price-history row (markets become unlisted). Coins are **not** refunded.\n"
                    "Run again with **confirm: CONFIRM** to proceed.", ephemeral=True)
            counts = {}
            try:
                with _db.db() as conn:
                    for tbl in ("stock_holdings", "stock_trade_log", "stock_price_log", "market_shares"):
                        try:
                            counts[tbl] = conn.execute(f"DELETE FROM {tbl}").rowcount
                        except Exception as e:
                            counts[tbl] = f"err: {e}"
            except Exception as e:
                return await interaction.response.send_message(f"❌ Reset failed: {e}", ephemeral=True)
            for f in ("stock_names.yml", "stock_dashboard.yml"):
                try:
                    save_yaml(f, {})
                except Exception:
                    pass
            summary = ", ".join(f"`{k}`={v}" for k, v in counts.items())
            return await interaction.response.send_message(
                f"🧹 **Stock data wiped.** Rows deleted: {summary}. Markets are now unlisted.", ephemeral=True)

        if t == "market":
            if not market_id:
                return await interaction.response.send_message(
                    "❌ `market_id` is required for this target.", ephemeral=True)
            if confirm.strip().lower() != market_id.strip().lower():
                return await interaction.response.send_message(
                    f"❌ Confirmation didn't match. Put `{market_id}` in the `confirm` field to delete.", ephemeral=True)
            data = _load_markets()
            markets = data.get("markets") or {}
            if market_id not in markets:
                return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)
            mkt_name = markets[market_id].get("name", market_id)
            csn_file = markets[market_id].get("csn_history_file") or f"csn_history_{market_id}.yml"
            del markets[market_id]
            _save_markets(data)
            items_deleted = 0
            try:
                with _db.db() as conn:
                    items_deleted = conn.execute("DELETE FROM items WHERE market_id=?", (market_id,)).rowcount
            except Exception as e:
                log.warning("[admin_wipe market] items delete failed: %s", e)
            csn_deleted = False
            try:
                import os as _os
                if _os.path.exists(csn_file):
                    _os.remove(csn_file)
                    csn_deleted = True
            except Exception as e:
                log.warning("[admin_wipe market] csn file delete failed: %s", e)
            embed = discord.Embed(title=f"🗑️ Market Deleted — {mkt_name}", color=0xE74C3C)
            embed.add_field(name="Market ID", value=f"`{market_id}`", inline=True)
            embed.add_field(name="Items removed", value=str(items_deleted), inline=True)
            embed.add_field(name="CSN history", value="✅ cleared" if csn_deleted else "⚠️ file not found", inline=True)
            return await interaction.response.send_message(embed=embed)

        if t == "market_csn":
            if not market_id:
                return await interaction.response.send_message(
                    "❌ `market_id` is required for this target.", ephemeral=True)
            if not _is_market_manager(interaction, market_id):
                return await interaction.response.send_message(
                    "⛔ Managers or this market's owner only.", ephemeral=True)
            history = _load_csn_for_market(market_id)
            months = history.get("months", {}) or {}
            targets = [mk for mk, md in months.items() if isinstance(md, dict) and md.get("items")]
            if not targets:
                return await interaction.response.send_message(
                    f"✅ No CSN-sourced months in `{market_id}` — nothing to delete.", ephemeral=True)
            if confirm.strip().lower() != market_id.strip().lower():
                preview = "\n".join(
                    f"• `{mk}` — {months[mk].get('label', mk)} "
                    f"(`{len(months[mk].get('items', {}))}` items · net `{int(months[mk].get('net', 0)):,}`)"
                    for mk in sorted(targets))
                return await interaction.response.send_message(
                    f"🔍 **Dry run** — `{len(targets)}` CSN month(s) in `{market_id}` would be deleted "
                    f"(manual earnings kept):\n{preview}\n\nPut `{market_id}` in `confirm` to delete.", ephemeral=True)
            for mk in targets:
                months.pop(mk, None)
            _save_csn_for_market(market_id, history)
            if market_id == DEFAULT_MARKET_ID:
                try:
                    with _db.db() as conn:
                        for mk in targets:
                            conn.execute("DELETE FROM csn_history WHERE month=?", (mk,))
                except Exception as e:
                    log.warning("[admin_wipe market_csn] DB cleanup failed: %s", e)
            try:
                _recompute_share_price(market_id, reason="admin_wipe_csn")
            except Exception:
                pass
            return await interaction.response.send_message(
                f"🗑️ Deleted `{len(targets)}` CSN month(s) from `{market_id}`. Manual earnings kept.", ephemeral=True)

        if t == "market_sales":
            # Clear the per-item sales breakdown (the SOLD/CSN column + CSN-derived items)
            # but KEEP each month's income/spent/net totals. Use when a market shows bogus
            # "sold" data but the earnings figures should stay.
            if not market_id:
                return await interaction.response.send_message(
                    "❌ `market_id` is required for this target.", ephemeral=True)
            if not _is_market_manager(interaction, market_id):
                return await interaction.response.send_message(
                    "⛔ Managers or this market's owner only.", ephemeral=True)
            history = _load_csn_for_market(market_id)
            months = history.get("months", {}) or {}
            affected = [mk for mk, md in months.items()
                        if isinstance(md, dict) and (md.get("items") or {})]
            if not affected:
                return await interaction.response.send_message(
                    f"✅ No per-item sales data in `{market_id}` — nothing to clear.", ephemeral=True)
            item_rows = sum(len(months[mk].get("items", {})) for mk in affected)
            if confirm.strip().lower() != market_id.strip().lower():
                return await interaction.response.send_message(
                    f"🔍 **Dry run** — would clear `{item_rows}` per-item sales row(s) across "
                    f"`{len(affected)}` month(s) in `{market_id}`, **keeping** each month's "
                    f"income/spent/net totals.\nPut `{market_id}` in `confirm` to proceed.", ephemeral=True)
            for mk in affected:
                months[mk]["items"] = {}
            _save_csn_for_market(market_id, history)
            try:
                _recompute_share_price(market_id, reason="admin_wipe_sales")
            except Exception:
                pass
            return await interaction.response.send_message(
                f"🗑️ Cleared `{item_rows}` per-item sales row(s) from `{len(affected)}` month(s) in "
                f"`{market_id}`. Monthly earnings totals kept; the dashboard's SOLD column refreshes shortly.",
                ephemeral=True)

        if t == "employee_dms":
            if confirm.strip().upper() != "CONFIRM":
                return await interaction.response.send_message(
                    "⚠️ This deletes **all DMs this bot sent to Employees**. Run again with "
                    "**confirm: CONFIRM** to proceed.", ephemeral=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            base = interaction.client.get_channel(WORKER_CHANNEL_ID)
            if not base or not base.guild:
                return await interaction.followup.send("❌ Can't find the guild via WORKER_CHANNEL_ID.", ephemeral=True)
            guild = base.guild
            role = discord.utils.get(guild.roles, name=EMPLOYEE_ROLE_NAME)
            if not role:
                return await interaction.followup.send(f"❌ Role not found: {EMPLOYEE_ROLE_NAME}", ephemeral=True)
            bot_user = interaction.client.user
            total_deleted = users_ok = users_failed = 0
            for member in list(role.members):
                if member.bot:
                    continue
                try:
                    dm = member.dm_channel or await member.create_dm()
                    hist_limit = None if int(limit_per_user) == 0 else int(limit_per_user)
                    async for msg in dm.history(limit=hist_limit, oldest_first=False):
                        if msg.author.id != bot_user.id:
                            continue
                        try:
                            await msg.delete()
                            total_deleted += 1
                        except discord.Forbidden:
                            break
                        except discord.HTTPException:
                            pass
                        await asyncio.sleep(0.35)
                    users_ok += 1
                    await asyncio.sleep(0.6)
                except Exception:
                    users_failed += 1
                    await asyncio.sleep(0.6)
                    continue
            return await interaction.followup.send(
                f"✅ Done. Deleted **{total_deleted}** bot DM(s). "
                f"Employees: **{users_ok}** ok, **{users_failed}** failed.", ephemeral=True)

        return await interaction.response.send_message("❌ Unknown target.", ephemeral=True)


    @admin.command(
        name="migrate_stock",
        description="(Managers) Move recent live stock from one market to another (fix mis-routed scans)")
    @app_commands.describe(
        from_market="Source market the scans landed in (usually the default, e.g. main)",
        to_market="Destination market id (e.g. viridianmarket)",
        since_minutes="Only move rows updated within the last N minutes (0 = move ALL rows)",
    )
    @app_commands.autocomplete(from_market=_market_autocomplete, to_market=_market_autocomplete)
    async def migrate_stock(self, interaction: discord.Interaction,
                            from_market: str, to_market: str, since_minutes: int = 60):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        if not _get_market(to_market):
            return await interaction.response.send_message(
                f"❌ Destination market `{to_market}` isn't registered. Create it with "
                f"`/market add market_id:{to_market}` first.", ephemeral=True)
        from datetime import datetime, timezone, timedelta
        since_iso = None
        if since_minutes and since_minutes > 0:
            since_iso = (datetime.now(timezone.utc) - timedelta(minutes=int(since_minutes))).isoformat()
        import Restocker_db as _db
        moved = _db.migrate_market_stock(from_market, to_market, since_iso)
        window = f"updated in the last {since_minutes} min" if since_iso else "ALL rows"
        await interaction.response.send_message(
            f"✅ Moved **{moved}** stock item(s) from `{from_market}` → `{to_market}` ({window}).\n"
            f"Check `/inventory stock market_id:{to_market}`"
            + (" or the website's STOCK column." if moved else " (nothing matched — widen `since_minutes`?)."),
            ephemeral=True)


    @admin.command(
        name="backfill_team_perf",
        description="(Managers) Recover past fulfillments missing from the team ledger")
    @app_commands.describe(
        apply="false = dry-run preview (default); true = actually write the ledger rows")
    async def backfill_team_perf(self, interaction: discord.Interaction, apply: bool = False):
        # Recovers team-ledger rows that were dropped when an order was approved BEFORE the
        # worker was linked to a team (or when a manager self-fulfilled). Attributes each to
        # the worker's CURRENT team. Idempotent — skips any order already in the ledger, so
        # it never double-counts.
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import Restocker_db as _db
        try:
            items_data = core._load_items()
            orders = _db.load_orders()
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Couldn't load orders: {e}", ephemeral=True)
        plan, to_write = {}, []
        for o in orders:
            req  = int(o.get("requested", 0) or 0)
            prod = int(o.get("produced", 0) or 0)
            status = str(o.get("status", "")).lower()
            fulfilled = ("fulfil" in status or status in ("complete", "done", "closed")
                         or (req > 0 and prod >= req))
            if not fulfilled:
                continue
            claims = o.get("claims") or []
            if claims:
                pairs = [(str(c.get("user_id") or ""), int(c.get("qty") or 0)) for c in claims]
            elif o.get("claimed_by"):
                pairs = [(str(o.get("claimed_by")), prod or req)]   # no per-user rows: whole order
            else:
                pairs = []
            for wid, qty in pairs:
                if not wid or qty <= 0:
                    continue
                detail = f"order#{o.get('id')}"
                # Idempotent: if this order is already in the ledger for this worker, skip
                # (never double-count). Only genuinely-dropped rows get recovered.
                if _db.team_perf_exists(wid, detail, "order"):
                    continue
                # Attribute to the worker's CURRENT team: their manager if they're a worker,
                # else themselves if they own a team. No team affiliation → can't attribute.
                mgr = _db.get_manager_of(wid)
                if mgr:
                    manager_id = str(mgr)
                elif _db.get_team(wid):
                    manager_id = wid
                else:
                    continue
                try:
                    coins = int(core._coins_for_pieces(o, qty, items_data))
                except Exception:
                    coins = 0
                if coins <= 0:
                    continue
                to_write.append((manager_id, wid, qty, coins, detail))
                p = plan.setdefault(wid, {"orders": 0, "coins": 0})
                p["orders"] += 1; p["coins"] += coins
        if not to_write:
            return await interaction.followup.send(
                "✅ Nothing to backfill — no manager self-fulfillments are missing from the ledger.",
                ephemeral=True)
        lines = []
        for mid, p in sorted(plan.items(), key=lambda kv: -kv[1]["coins"]):
            try:
                ign = _db.get_ign(mid) or mid
            except Exception:
                ign = mid
            lines.append(f"• **{ign}** — {p['orders']} order(s) → +{p['coins']:,} coins")
        summary = "\n".join(lines)
        if not apply:
            return await interaction.followup.send(
                f"**Dry run** — {len(to_write)} ledger row(s) would be written:\n{summary}\n\n"
                f"Review the amounts, then re-run with `apply:true` to commit.", ephemeral=True)
        n = 0
        for manager_id, wid, qty, coins, detail in to_write:
            try:
                _db.record_team_perf(manager_id, wid, "order", coins=float(coins), qty=qty, detail=detail)
                n += 1
            except Exception as e:
                log.warning("[backfill] write failed for %s %s: %s", wid, detail, e)
        await interaction.followup.send(
            f"✅ Backfilled **{n}** ledger row(s):\n{summary}\n\n"
            f"They now show on the team leaderboard (7-day window).", ephemeral=True)

    @admin.command(
        name="repair_payouts",
        description="(Managers) Find & repay workers who were paid 0 by the price-lookup bug")
    @app_commands.describe(
        apply="false = dry-run preview (default); true = actually pay the workers")
    async def repair_payouts(self, interaction: discord.Interaction, apply: bool = False):
        """Repairs orders broken by the old exact-match price lookup.

        An order is only touched when it is *provably* a victim of that bug:
          • it is fulfilled, and has a claim, and
          • the OLD exact-key lookup priced it at 0 (so the worker was silently skipped), and
          • the NEW tolerant lookup prices it > 0 (so we know what they were owed).

        That pairing is what makes this safe to run repeatedly: an order that priced fine
        under the old logic was already paid, fails the filter, and is never re-paid. It is
        NOT a blanket "pay everyone again" — it can't double-pay.
        """
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import Restocker_db as _db
        try:
            items_data = core._load_items()
            orders = _db.load_orders()
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Couldn't load orders: {e}", ephemeral=True)

        catalog = (items_data or {}).get("items", {}) or {}

        def _old_price(item_name):
            """Exactly the old buggy lookup: exact key only, int()-truncated."""
            try:
                info = catalog.get(item_name) or {}
                return int(info.get("coin", 0) or 0)
            except Exception:
                return 0

        plan = []          # (order, uid, qty, owed)
        for o in orders:
            status = str(o.get("status", "")).lower()
            if "fulfil" not in status and status not in ("complete", "done", "closed"):
                continue
            item = o.get("item", "")
            if _old_price(item) > 0:
                continue                       # priced fine before → was paid → never touch
            claims = o.get("claims") or []
            pairs = [(str(c.get("user_id") or ""), int(c.get("qty") or 0)) for c in claims]
            if not pairs and o.get("claimed_by"):
                pairs = [(str(o.get("claimed_by")), int(o.get("produced") or o.get("requested") or 0))]
            for uid, qty in pairs:
                if not uid or qty <= 0:
                    continue
                # Idempotency: a previous repair tags the ledger with repair:order#N. Without
                # this check the filter below stays true forever and a second run would pay
                # the same worker again.
                try:
                    if _db.coin_ledger_has(uid, f"repair:order#{o.get('id')}"):
                        continue
                except Exception:
                    continue                   # can't verify → don't risk a double payment
                try:
                    owed = int(core._coins_for_pieces(o, qty, items_data))
                except Exception:
                    owed = 0
                if owed > 0:                   # tolerant lookup found a real price → was underpaid
                    plan.append((o, uid, qty, owed))

        if not plan:
            return await interaction.followup.send(
                "✅ Nothing to repair — no fulfilled order was paid 0 by the price bug.\n"
                "(If a worker is still short, their item may have **no catalog price at all** — "
                "set one with `/item_set_price`, then re-run this.)", ephemeral=True)

        lines, total = [], 0
        for o, uid, qty, owed in plan[:20]:
            try:
                ign = _db.get_ign(uid) or uid
            except Exception:
                ign = uid
            lines.append(f"• **#{o.get('id')}** {o.get('item','?')} — {ign} × {qty} pcs → **+{owed:,}c**")
            total += owed
        more = f"\n… and {len(plan) - 20} more" if len(plan) > 20 else ""
        summary = "\n".join(lines) + more

        if not apply:
            return await interaction.followup.send(
                f"**Dry run** — {len(plan)} unpaid claim(s), **{sum(p[3] for p in plan):,} coins** owed:\n"
                f"{summary}\n\nRe-run with `apply:true` to pay them.", ephemeral=True)

        paid_n, paid_c = 0, 0
        for o, uid, qty, owed in plan:
            try:
                uid_i = int(uid)
            except Exception:
                continue
            detail = f"order#{o.get('id')}"
            try:
                _mkt_mult, _mkt_bonus = core._market_loyalty_cfg(o.get("market_id"))
                bonus_pct = core._loyalty_payout_bonus_pct(uid_i)
                bonus = int(owed * bonus_pct / 100) if bonus_pct > 0 else 0
                total_payout = owed + bonus + _mkt_bonus
                core.add_coins(uid_i, total_payout, counts_as_principal=True,
                               reason=f"repair:{detail}")
                paid_n += 1
                paid_c += total_payout
            except Exception as e:
                log.warning("[repair] payout failed for %s %s: %s", uid, detail, e)
                continue
            # Team ledger — idempotent, so this is safe even if backfill already ran.
            try:
                if not _db.team_perf_exists(uid, detail, "order"):
                    mgr = _db.get_manager_of(uid)
                    manager_id = str(mgr) if mgr else (uid if _db.get_team(uid) else None)
                    if manager_id:
                        _db.record_team_perf(manager_id, uid, "order",
                                             coins=float(total_payout), qty=qty, detail=detail)
            except Exception as e:
                log.warning("[repair] team ledger failed for %s %s: %s", uid, detail, e)
            try:
                u = await interaction.client.fetch_user(uid_i)
                await u.send(
                    f"💰 **Payment correction** — Order **#{o.get('id')}** ({o.get('item','')}) "
                    f"was approved but a pricing bug paid you **0 coins**.\n"
                    f"You've now been paid **{total_payout:,} coins** for {qty} pcs. Sorry about that!")
            except Exception:
                pass

        await interaction.followup.send(
            f"✅ Repaired **{paid_n}** claim(s) — paid **{paid_c:,} coins** total.\n{summary}\n\n"
            f"Workers were DM'd. Ledger rows are tagged `repair:order#N`.", ephemeral=True)

    @admin.command(
        name="repair_all",
        description="(Managers) Run every repair at once — payouts, team ledger, brew names")
    @app_commands.describe(apply="false = dry-run preview (default); true = actually apply everything")
    async def repair_all(self, interaction: discord.Interaction, apply: bool = False):
        """One button for the lot: repays workers the price bug zeroed, recovers dropped team
        ledger rows, cleans brew names, and flags orphaned orders that need a human."""
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import Restocker_db as _db
        try:
            items_data = core._load_items()
            orders = _db.load_orders()
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Couldn't load orders: {e}", ephemeral=True)

        pay_plan = _payout_repair_plan(_db, items_data, orders)
        team_rows, team_sum = _team_backfill_plan(_db, items_data, orders)
        orphans = _orphaned_orders(orders)
        pay_total = sum(p[3] for p in pay_plan)

        def _ign(uid):
            try:
                return _db.get_ign(uid) or uid
            except Exception:
                return uid

        parts = []
        # 1) payouts
        if pay_plan:
            lines = [f"• **#{o.get('id')}** {o.get('item','?')} — {_ign(u)} × {q} → **+{c:,}c**"
                     for o, u, q, c in pay_plan[:10]]
            parts.append(f"**💰 Unpaid claims ({len(pay_plan)}, {pay_total:,}c)**\n" + "\n".join(lines)
                         + (f"\n… +{len(pay_plan)-10} more" if len(pay_plan) > 10 else ""))
        else:
            parts.append("**💰 Payouts** — nothing owed. ✅")
        # 2) team ledger
        if team_rows:
            lines = [f"• **{_ign(w)}** — {p['orders']} order(s) → +{p['coins']:,}c"
                     for w, p in sorted(team_sum.items(), key=lambda kv: -kv[1]["coins"])[:10]]
            parts.append(f"**📊 Team ledger ({len(team_rows)} row(s))**\n" + "\n".join(lines))
        else:
            parts.append("**📊 Team ledger** — nothing missing. ✅")
        # 3) orphans (informational — needs a human decision)
        if orphans:
            lines = [f"• **#{o.get('id')}** {o.get('item','?')} — fulfilled, nobody attached"
                     for o in orphans[:10]]
            parts.append(f"**🚨 Orphaned orders ({len(orphans)})** — can't auto-repair, no worker "
                         f"on record:\n" + "\n".join(lines)
                         + "\nUse `/admin repair_order` to attach the worker who actually did it.")

        if not apply:
            return await interaction.followup.send(
                "**Dry run — nothing changed.**\n\n" + "\n\n".join(parts)
                + "\n\nRe-run with `apply:true` to commit.", ephemeral=True)

        # ── apply ──
        paid_n = paid_c = 0
        for o, uid, qty, owed in pay_plan:
            try:
                uid_i = int(uid)
            except Exception:
                continue
            detail = f"order#{o.get('id')}"
            try:
                _mult, _mkt_bonus = core._market_loyalty_cfg(o.get("market_id"))
                bonus_pct = core._loyalty_payout_bonus_pct(uid_i)
                bonus = int(owed * bonus_pct / 100) if bonus_pct > 0 else 0
                total_payout = owed + bonus + _mkt_bonus
                core.add_coins(uid_i, total_payout, counts_as_principal=True,
                               reason=f"repair:{detail}")
                paid_n += 1
                paid_c += total_payout
            except Exception as e:
                log.warning("[repair_all] payout failed %s %s: %s", uid, detail, e)
                continue
            try:
                u = await interaction.client.fetch_user(uid_i)
                await u.send(f"💰 **Payment correction** — Order **#{o.get('id')}** "
                             f"({o.get('item','')}) was approved but a pricing bug paid you 0. "
                             f"You've now been paid **{total_payout:,} coins**. Sorry about that!")
            except Exception:
                pass

        team_n = 0
        for manager_id, wid, qty, coins, detail in team_rows:
            try:
                _db.record_team_perf(manager_id, wid, "order", coins=float(coins),
                                     qty=qty, detail=detail)
                team_n += 1
            except Exception as e:
                log.warning("[repair_all] ledger write failed %s %s: %s", wid, detail, e)

        try:
            brews_n = core._purge_garbage_brew_aliases()
        except Exception as e:
            log.warning("[repair_all] brew clean failed: %s", e)
            brews_n = 0

        done = [f"💰 Paid **{paid_n}** claim(s) — **{paid_c:,} coins**",
                f"📊 Wrote **{team_n}** team-ledger row(s)",
                f"🧪 Cleaned **{brews_n}** brew name(s)"]
        if orphans:
            done.append(f"🚨 **{len(orphans)}** orphaned order(s) still need `/admin repair_order`")
        await interaction.followup.send("✅ **Repair complete.**\n" + "\n".join(done), ephemeral=True)

    @admin.command(
        name="repair_order",
        description="(Managers) Attach a worker to an orphaned order and pay them")
    @app_commands.describe(
        order_id="The order that has nobody attached (see /admin repair_all)",
        worker="The person who actually did the work",
        qty="How many pieces they made (leave 0 to use the order's full amount)",
        apply="false = preview (default); true = attach, pay and credit")
    async def repair_order(self, interaction: discord.Interaction, order_id: int,
                           worker: discord.User, qty: int = 0, apply: bool = False):
        """For orders that closed with no claim on record — nothing else can attribute them,
        so a manager names the worker. Idempotent via the repair:order#N ledger tag."""
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import Restocker_db as _db
        try:
            items_data = core._load_items()
            orders = _db.load_orders()
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Couldn't load orders: {e}", ephemeral=True)

        o = next((x for x in orders if int(x.get("id", 0) or 0) == int(order_id)), None)
        if not o:
            return await interaction.followup.send(f"❌ No order #{order_id}.", ephemeral=True)

        uid = str(worker.id)
        detail = f"order#{order_id}"
        try:
            if _db.coin_ledger_has(uid, f"repair:{detail}"):
                return await interaction.followup.send(
                    f"⚠️ {worker.mention} was already repaired for order #{order_id} — refusing to "
                    f"pay twice.", ephemeral=True)
        except Exception:
            return await interaction.followup.send(
                "⚠️ Couldn't verify the ledger — refusing to pay in case it double-pays.",
                ephemeral=True)

        pieces = int(qty) if qty > 0 else int(o.get("produced") or o.get("requested") or 0)
        if pieces <= 0:
            return await interaction.followup.send("❌ Quantity must be > 0.", ephemeral=True)
        try:
            owed = int(core._coins_for_pieces(o, pieces, items_data))
        except Exception:
            owed = 0
        if owed <= 0:
            return await interaction.followup.send(
                f"❌ `{o.get('item','?')}` has no catalog price, so the payout would be 0.\n"
                f"Set one with `/item_set_price item:{o.get('item','?')} coin:<amount>` and re-run.",
                ephemeral=True)

        _mult, _mkt_bonus = core._market_loyalty_cfg(o.get("market_id"))
        bonus_pct = core._loyalty_payout_bonus_pct(worker.id)
        bonus = int(owed * bonus_pct / 100) if bonus_pct > 0 else 0
        total_payout = owed + bonus + _mkt_bonus

        if not apply:
            return await interaction.followup.send(
                f"**Dry run** — order **#{order_id}** ({o.get('item','?')})\n"
                f"• Worker: {worker.mention}\n• Pieces: **{pieces}**\n"
                f"• Payout: **{owed:,}** + {bonus:,} loyalty + {_mkt_bonus:,} market = "
                f"**{total_payout:,} coins**\n\nRe-run with `apply:true` to commit.",
                ephemeral=True)

        try:
            core.add_coins(worker.id, total_payout, counts_as_principal=True,
                           reason=f"repair:{detail}")
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Payout failed: {e}", ephemeral=True)
        try:
            if not _db.team_perf_exists(uid, detail, "order"):
                mgr = _db.get_manager_of(uid)
                manager_id = str(mgr) if mgr else (uid if _db.get_team(uid) else None)
                if manager_id:
                    _db.record_team_perf(manager_id, uid, "order", coins=float(total_payout),
                                         qty=pieces, detail=detail)
        except Exception as e:
            log.warning("[repair_order] ledger failed: %s", e)
        try:
            await worker.send(
                f"💰 **Payment correction** — you've been paid **{total_payout:,} coins** for "
                f"Order **#{order_id}** ({o.get('item','')}), which closed without your claim "
                f"recorded. Sorry about that!")
        except Exception:
            pass
        log.info("[repair_order] %s attached %s to order#%s for %s coins",
                 interaction.user, uid, order_id, total_payout)
        await interaction.followup.send(
            f"✅ Attached {worker.mention} to order **#{order_id}** and paid "
            f"**{total_payout:,} coins** ({pieces} pcs). They've been DM'd.", ephemeral=True)

    @admin.command(name="ai_audit", description="(Managers) Recent AI tool actions — who ran what")
    @app_commands.describe(limit="How many recent entries (default 15)", sensitive_only="Only moderation/destructive actions")
    async def ai_audit(self, interaction: discord.Interaction, limit: int = 15, sensitive_only: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import json as _json, Restocker_db as _db
        from datetime import datetime, timezone
        try:
            raw = _db.get_config("ai_audit_log")
            arr = _json.loads(raw) if raw else []
        except Exception:
            arr = []
        if sensitive_only:
            arr = [e for e in arr if e.get("sens")]
        if not arr:
            return await interaction.response.send_message("📋 No AI tool actions logged yet.", ephemeral=True)
        limit = max(1, min(int(limit or 15), 40))
        recent = arr[-limit:][::-1]
        lines = []
        for e in recent:
            try:
                ts = datetime.fromtimestamp(int(e.get("ts", 0)), timezone.utc).strftime("%m-%d %H:%M")
            except Exception:
                ts = "?"
            flag = "⚠️ " if e.get("sens") else ""
            args = (e.get("args") or "").strip()
            if len(args) > 80:
                args = args[:79] + "…"
            lines.append(f"{flag}`{ts}` <@{e.get('uid')}> → **{e.get('tool')}** `{args}`")
        body = "🧾 **AI tool audit** (most recent first)\n" + "\n".join(lines)
        await interaction.response.send_message(body[:1950], ephemeral=True)


    @admin.command(name="purge_brews",
                   description="(Managers) Clean brew names — strip ads, state tags, quality bars, durations")
    async def purge_brews(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        try:
            n = core._purge_garbage_brew_aliases()
        except Exception as e:
            return await interaction.response.send_message(f"⚠️ Failed: {e}", ephemeral=True)
        await interaction.response.send_message(
            (f"🧪 Cleaned **{n}** brew name(s) — stripped ads, 'Barrel aged'/'Distilled', "
             f"the quality bar, durations, emoji and flavour text, keeping only real effects. "
             f"Names also clean live on reports and the website." if n
             else "✅ All brew names already clean."),
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
