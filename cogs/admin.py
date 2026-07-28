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
order_id_autocomplete = core.order_id_autocomplete
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
                if (_db.coin_ledger_has(uid, f"repair:order#{o.get('id')}")
                        or _db.coin_ledger_has(uid, f"order#{o.get('id')}")):  # AUDIT FIX: normal payout counts as paid
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
            # Resolve the manager FIRST: team_perf_log rows are keyed on manager_id, so the
            # idempotency check must use the manager, not the worker — checking with wid never
            # matched for managed workers and every re-run double-counted the whole ledger.
            mgr = _db.get_manager_of(wid)
            if mgr:
                manager_id = str(mgr)
            elif _db.get_team(wid):
                manager_id = wid
            else:
                manager_id = None
            if manager_id and _db.team_perf_exists(manager_id, detail, "order"):
                continue
            if not manager_id:
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
                    # AUDIT FIX: stock_limit_orders and stock_alarms were surviving the
                    # wipe — limit orders don't escrow, so pre-wipe buy orders stayed
                    # ARMED and auto-fired against fresh re-listings weeks later,
                    # silently spending users' coins on trades from a previous era.
                    for tbl in ("stock_holdings", "stock_trade_log", "stock_price_log",
                                "market_shares", "stock_limit_orders", "stock_alarms"):
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
                    # AUDIT FIX: the wipe left a LIVE stock listing (active=1, price
                    # frozen forever), holdings, armed limit orders, alarms and stock
                    # rows — a tradeable ghost market, inherited by any future market
                    # that reuses the id. Everything stock-side dies with the market.
                    for tbl in ("market_shares", "stock_holdings", "stock_limit_orders",
                                "stock_alarms", "market_stock"):
                        try:
                            conn.execute(f"DELETE FROM {tbl} WHERE market_id=?", (market_id,))
                        except Exception as e2:
                            log.warning("[admin_wipe market] %s cleanup failed: %s", tbl, e2)
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


    @admin.command(name="dm_setup",
                   description="(Managers) DM market owners their market id, CSN code, webhook + setup steps")
    @app_commands.describe(market_id="One market (blank = every active market with an owner)",
                           confirm="False (default) = preview who'd be DMed. True = actually send.")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def dm_setup(self, interaction: discord.Interaction,
                       market_id: Optional[str] = None, confirm: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        markets = (_load_markets().get("markets", {}) or {})
        targets = ([market_id] if market_id else list(markets.keys()))
        plan, sent, failed = [], [], []
        for mid in targets:
            m = markets.get(mid)
            if not isinstance(m, dict) or not m.get("active", True):
                continue
            owner = m.get("owner_id") or m.get("leader_discord_id")
            if not owner:
                failed.append(f"{mid} (no owner set)")
                continue
            chan_id = m.get("report_channel_id")
            channel = self.bot.get_channel(int(chan_id)) if chan_id else None
            plan.append((mid, m, str(owner), channel))

        if not confirm:
            lines = [f"• `{mid}` → <@{o}>" + (f" · {ch.mention}" if ch else " · ⚠️ no bound channel")
                     for mid, _m, o, ch in plan]
            return await interaction.followup.send(
                f"🔍 Would DM **{len(plan)}** owner(s):\n" + "\n".join(lines[:20])
                + (f"\n(+{len(plan)-20} more)" if len(plan) > 20 else "")
                + (f"\nSkipped: {', '.join(failed)}" if failed else "")
                + "\n\nRe-run with `confirm:True` to send.", ephemeral=True)

        for mid, m, owner, channel in plan:
            name = m.get("name", mid)
            code = (m.get("leader_code") or "—")
            hook = "*(ask a manager — create one in that channel's Integrations)*"
            if channel is not None:
                try:   # reuse an existing bot webhook if we're allowed to see them
                    for w in await channel.webhooks():
                        if w.token:
                            hook = w.url
                            break
                except Exception:
                    pass
            # one shared builder with the AI's dm_market_setup tool
            e = core._build_setup_embed(mid, m, channel,
                                        None if hook.startswith("*(") else hook)
            try:
                user = self.bot.get_user(int(owner)) or await self.bot.fetch_user(int(owner))
                await user.send(embed=e)
                sent.append(mid)
                await asyncio.sleep(1.0)
            except Exception as ex:
                failed.append(f"{mid} (DM failed: {type(ex).__name__})")
        return await interaction.followup.send(
            f"📨 Sent setup DMs for **{len(sent)}**: {', '.join(sent) or '—'}"
            + (f"\nFailed: {', '.join(failed[:10])}" if failed else ""), ephemeral=True)

    @admin.command(name="rebuild_market_channel",
                   description="(Managers) WIPE this market's channel and repost a clean report for every month")
    @app_commands.describe(
        market_id="Market whose bound channel to rebuild (blank = the market bound to THIS channel)",
        confirm="False (default) = preview. True = delete messages and repost.",
        keep_humans="True (default) = only delete bot/webhook messages, keep what people wrote",
        limit="How many messages to scan for deletion (default 500, max 2000)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def rebuild_market_channel(self, interaction: discord.Interaction,
                                     market_id: Optional[str] = None, confirm: bool = False,
                                     keep_humans: bool = True, limit: int = 500):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import io as _io
        markets = (_load_markets().get("markets", {}) or {})
        # resolve the market from the arg, else from the channel we're standing in
        mid = market_id
        if not mid:
            for _k, _m in markets.items():
                if isinstance(_m, dict) and str(_m.get("report_channel_id") or "") == str(interaction.channel_id):
                    mid = _k
                    break
        if not mid or mid not in markets:
            return await interaction.followup.send(
                "❌ No market given and this channel isn't bound to one. Pass `market_id:`.", ephemeral=True)
        m = markets[mid]
        chan_id = m.get("report_channel_id")
        channel = self.bot.get_channel(int(chan_id)) if chan_id else None
        if channel is None:
            return await interaction.followup.send(
                f"❌ `{mid}` has no bound channel (set one with `/bind_market`).", ephemeral=True)
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.manage_messages:
            return await interaction.followup.send(
                f"❌ I need **Manage Messages** in {channel.mention} to clear it.", ephemeral=True)

        months = (_load_csn_for_market(mid) or {}).get("months", {}) or {}
        keys = sorted(k for k, v in months.items() if isinstance(v, dict) and (v.get("items") or {}))
        limit = max(50, min(int(limit or 500), 2000))

        # count what would be deleted
        victims = []
        try:
            async for msg in channel.history(limit=limit):
                if keep_humans and not (msg.webhook_id or (msg.author and msg.author.bot)):
                    continue
                victims.append(msg)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Couldn't read history: {e}", ephemeral=True)

        if not confirm:
            return await interaction.followup.send(
                f"🔍 **Preview — {m.get('name', mid)}** ({channel.mention})\n"
                f"• would delete **{len(victims)}** message(s)"
                + (" (bot/webhook only — human messages kept)" if keep_humans else " (**everything**, humans included)")
                + f"\n• would repost **{len(keys)}** monthly report(s): "
                + (", ".join(keys) or "none")
                + f"\n\nRe-run with `confirm:True` to do it.", ephemeral=True)

        deleted = 0
        # bulk delete is far faster but only works on messages <14 days old
        try:
            fresh = [msg for msg in victims if (discord.utils.utcnow() - msg.created_at).days < 14]
            old = [msg for msg in victims if msg not in fresh]
            for i in range(0, len(fresh), 100):
                try:
                    await channel.delete_messages(fresh[i:i + 100])
                    deleted += len(fresh[i:i + 100])
                except Exception:
                    for msg in fresh[i:i + 100]:
                        try:
                            await msg.delete(); deleted += 1; await asyncio.sleep(0.4)
                        except Exception:
                            pass
            for msg in old:
                try:
                    await msg.delete(); deleted += 1; await asyncio.sleep(0.7)
                except Exception:
                    pass
        except Exception as e:
            log.warning("[rebuild_market_channel] delete phase: %s", e)

        posted = 0
        for mk in keys:
            md = months.get(mk) or {}
            items = md.get("items") or {}
            income = float(md.get("income", 0) or 0)
            spent = float(md.get("spent", 0) or 0)
            name = m.get("name", mid)
            title = f"📕 {name} · {md.get('label', mk)}"
            try:
                embed = core._build_csn_compact_embed(title, items, income, spent, mid, mk)
                embed.set_footer(text=f"Monthly report • {name}")
                files = []
                xb = core._build_csn_xlsx(title, name, mk, items, income, spent, market_id=mid)
                if xb:
                    files = [discord.File(_io.BytesIO(xb), filename=f"report_{mid}_{mk}.xlsx")]
                await channel.send(embed=embed, files=files)
                posted += 1
                await asyncio.sleep(1.5)
            except Exception as e:
                log.warning("[rebuild_market_channel] %s %s post failed: %s", mid, mk, e)
        log.info("[rebuild_market_channel] %s: deleted %d, posted %d", mid, deleted, posted)
        return await interaction.followup.send(
            f"🧹 **{m.get('name', mid)}** rebuilt — deleted **{deleted}**, posted **{posted}** monthly report(s) "
            f"in {channel.mention}.", ephemeral=True)

    @admin.command(name="fix_month_close",
                   description="(Managers) EDIT the existing month-closing posts in place with the CURRENT data")
    @app_commands.describe(month="Month key e.g. 2026-06 — or `all` / blank for EVERY recorded month",
                           market_id="Market to fix (blank = every active market)",
                           repost="True = post a new message instead of editing the old one")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def repost_month_close(self, interaction: discord.Interaction,
                                 month: Optional[str] = None, market_id: Optional[str] = None,
                                 repost: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import re as _re
        month = (month or "").strip().lower()
        all_months = month in ("", "all", "*")
        if not all_months and not _re.match(r"^\d{4}-\d{2}$", month):
            return await interaction.response.send_message(
                "❌ Month must look like `2026-06`, or use `all`.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import io as _io
        markets = (_load_markets().get("markets", {}) or {})
        targets = ([market_id] if market_id else list(markets.keys()))
        done, skipped = [], []
        # (market, month) pairs to fix
        jobs = []
        for mid in targets:
            m = markets.get(mid)
            if not isinstance(m, dict) or not m.get("active", True):
                continue
            recorded = (_load_csn_for_market(mid) or {}).get("months", {}) or {}
            for mk in (sorted(recorded.keys()) if all_months else [month]):
                jobs.append((mid, mk))
        for mid, month in jobs:
            m = markets.get(mid)
            md = (_load_csn_for_market(mid) or {}).get("months", {}).get(month)
            chan_id = m.get("report_channel_id")
            channel = self.bot.get_channel(int(chan_id)) if chan_id else None
            if not isinstance(md, dict) or not (md.get("items") or {}):
                # The month no longer exists (e.g. a mis-routed copy was purged) but the old
                # closing post is still sitting in the channel reporting numbers that aren't
                # real. Delete the orphan instead of silently skipping it.
                if channel is None:
                    skipped.append(f"{mid} {month} (no data)")
                    continue
                try:
                    from datetime import date as _d2
                    lbl = _d2(int(month[:4]), int(month[5:7]), 1).strftime("%B %Y")
                except Exception:
                    lbl = month
                killed = 0
                try:
                    async for msg in channel.history(limit=100):
                        if (msg.author.id == self.bot.user.id and msg.embeds
                                and str(msg.embeds[0].title or "").startswith("📕 Month closed")
                                and lbl in str(msg.embeds[0].title or "")):
                            await msg.delete()
                            killed += 1
                            await asyncio.sleep(0.5)
                except Exception as e:
                    log.warning("[fix_month_close] orphan delete %s %s: %s", mid, month, e)
                skipped.append(f"{mid} {month} ({'deleted stale post' if killed else 'no data'})")
                continue
            if channel is None:
                skipped.append(f"{mid} {month} (unbound)")
                continue
            items = md.get("items") or {}
            income = float(md.get("income", 0) or 0)
            spent = float(md.get("spent", 0) or 0)
            name = m.get("name", mid)
            title = f"📕 Month closed — {name} · {md.get('label', month)}"
            try:
                embed = core._build_csn_compact_embed(title, items, income, spent, mid, month)
                files = []
                xb = core._build_csn_xlsx(title, name, month, items, income, spent, market_id=mid)
                if xb:
                    files = [discord.File(_io.BytesIO(xb), filename=f"closing_{mid}_{month}.xlsx")]

                # Prefer EDITING the bot's own existing closing post for this month — the
                # channel keeps one truthful message instead of a wrong one plus a correction.
                target_msg = None
                if not repost:
                    try:
                        async for msg in channel.history(limit=60):
                            if (msg.author.id == self.bot.user.id and msg.embeds
                                    and str(msg.embeds[0].title or "").startswith("📕 Month closed")
                                    and md.get("label", month) in str(msg.embeds[0].title or "")):
                                target_msg = msg
                                break
                    except Exception:
                        target_msg = None

                if target_msg is not None:
                    embed.set_footer(text=f"Month-end closing report (corrected) • {name}")
                    # attachments= replaces the stale workbook with the rebuilt one
                    await target_msg.edit(embed=embed, attachments=files)
                    done.append(f"{mid} {month} ✏️")
                else:
                    embed.set_footer(text=f"Corrected month-end closing report • {name}")
                    await channel.send(embed=embed, files=files)
                    done.append(f"{mid} {month} 📤")
                await asyncio.sleep(1.5)
            except Exception as e:
                skipped.append(f"{mid} {month} ({e})")
        scope = "ALL months" if all_months else f"`{month}`"
        body = (f"📕 Fixed {scope} — **{len(done)}** post(s): {', '.join(done[:20]) or '—'}"
                + (f" (+{len(done) - 20} more)" if len(done) > 20 else "")
                + (f"\nSkipped {len(skipped)}: {', '.join(skipped[:8])}"
                   + (" …" if len(skipped) > 8 else "") if skipped else ""))
        return await interaction.followup.send(body[:1900], ephemeral=True)

    @admin.command(name="csn_cleanup",
                   description="(Managers) Delete useless CSN webhook noise in THIS channel (empty stock CSVs, {} profiles)")
    @app_commands.describe(
        limit="How many recent messages to scan (default 200, max 500)",
        confirm="False (default) = preview what would be deleted. True = actually delete.")
    async def csn_cleanup(self, interaction: discord.Interaction, limit: int = 200, confirm: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        limit = max(10, min(int(limit or 200), 500))
        channel = interaction.channel

        def _is_noise_attachment(att) -> bool:
            n = (att.filename or "").lower()
            # empty stock scan: header-only CSV is well under 300 bytes
            if n.startswith("csn_stock_") and att.size < 300:
                return True
            # empty brew-profiles capture ("{}")
            if n.startswith("csn_profiles") and att.size <= 6:
                return True
            return False

        victims = []
        try:
            async for msg in channel.history(limit=limit):
                if not (msg.webhook_id or (msg.author and msg.author.bot)):
                    continue                      # never touch human messages
                if not msg.attachments:
                    continue
                if (msg.content or "").strip():
                    continue                      # keep anything with a summary/content
                if all(_is_noise_attachment(a) for a in msg.attachments):
                    victims.append(msg)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ History scan failed: {e}", ephemeral=True)

        if not victims:
            return await interaction.followup.send(
                f"✅ Scanned {limit} messages — no CSN noise found here.", ephemeral=True)

        names = {}
        for m in victims:
            for a in m.attachments:
                key = "empty stock CSV" if a.filename.lower().startswith("csn_stock_") else "empty profiles.json"
                names[key] = names.get(key, 0) + 1
        summary = ", ".join(f"{v}× {k}" for k, v in names.items())

        if not confirm:
            return await interaction.followup.send(
                f"🔍 Would delete **{len(victims)}** message(s) in {channel.mention} ({summary}).\n"
                f"Data posts (monthly reports, non-empty scans, hive lines, lands feed) are untouched.\n"
                f"Re-run with `confirm:True` to delete.", ephemeral=True)

        deleted = failed = 0
        for m in victims:
            try:
                await m.delete()
                deleted += 1
                await asyncio.sleep(0.6)          # stay friendly with the rate limit
            except Exception:
                failed += 1
        await interaction.followup.send(
            f"🧹 Deleted **{deleted}** noise message(s) ({summary})"
            + (f" — {failed} failed (missing Manage Messages?)" if failed else "") + ".",
            ephemeral=True)

