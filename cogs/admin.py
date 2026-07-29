"""Admin maintenance commands (/admin)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional
import asyncio

import Restocker_db as _db

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]

def _s(*a, **k) -> str:
    """Return the first positional arg. The wipe body ended every branch with
    interaction.response.send_message(...); turning it into a plain function makes those
    return values, and this keeps that rewrite mechanical rather than hand-edited."""
    return a[0] if a else ""


def _wipe_may_touch(user, market_id) -> bool:
    """Manager, or the owner of THIS market. Replaces the interaction-based
    _is_market_manager check now that the wipe runs without an interaction."""
    try:
        if core._ai_is_manager(user):
            return True
    except Exception:
        pass
    try:
        owner = core._market_owner_id(market_id)
        return bool(owner) and int(owner) == int(getattr(user, "id", 0) or 0)
    except Exception:
        return False

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

    # /admin was retired — every subcommand is an AI tool now. The cog stays as the
    # home for the implementations (wipe_target, _rebuild_one, _nuke_by_clone, …).

    async def wipe_target(self, user, target: str, confirm: str = "",
                          market_id=None, limit_per_user: int = 0) -> str:
        """Destructive wipe. Was /admin wipe; the slash surface was retired, so this
        returns a STRING instead of replying to an interaction.

        The safety was never the command surface — it is the confirm PHRASE: the market
        id for market/market_csn/market_sales, or CONFIRM for stock/employee_dms. That is
        preserved exactly, so nothing can be wiped by asking loosely."""
        t = str(target or "").strip().lower()
        import Restocker_db as _db

        if t == "stock":
            if confirm.strip().upper() != "CONFIRM":
                return _s("⚠️ This **permanently deletes ALL stock data** — every listing, holding, trade "
                    "and price-history row (markets become unlisted). Coins are **not** refunded.\n"
                    "Run again with **confirm: CONFIRM** to proceed.")
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
                return _s(f"❌ Reset failed: {e}")
            for f in ("stock_names.yml", "stock_dashboard.yml"):
                try:
                    save_yaml(f, {})
                except Exception:
                    pass
            summary = ", ".join(f"`{k}`={v}" for k, v in counts.items())
            return _s(f"🧹 **Stock data wiped.** Rows deleted: {summary}. Markets are now unlisted.")

        if t == "market":
            if not market_id:
                return _s("❌ `market_id` is required for this target.")
            if confirm.strip().lower() != market_id.strip().lower():
                return _s(f"❌ Confirmation didn't match. Put `{market_id}` in the `confirm` field to delete.")
            data = _load_markets()
            markets = data.get("markets") or {}
            if market_id not in markets:
                return _s(f"❌ Market `{market_id}` not found.")
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
            return _s(f"🗑️ Market deleted — {mkt_name} (`{market_id}`). "
                      f"Items removed: {items_deleted}. CSN history: "
                      + ("cleared." if csn_deleted else "file not found."))

        if t == "market_csn":
            if not market_id:
                return _s("❌ `market_id` is required for this target.")
            if not _wipe_may_touch(user, market_id):
                return _s("⛔ Managers or this market's owner only.")
            history = _load_csn_for_market(market_id)
            months = history.get("months", {}) or {}
            targets = [mk for mk, md in months.items() if isinstance(md, dict) and md.get("items")]
            if not targets:
                return _s(f"✅ No CSN-sourced months in `{market_id}` — nothing to delete.")
            if confirm.strip().lower() != market_id.strip().lower():
                preview = "\n".join(
                    f"• `{mk}` — {months[mk].get('label', mk)} "
                    f"(`{len(months[mk].get('items', {}))}` items · net `{int(months[mk].get('net', 0)):,}`)"
                    for mk in sorted(targets))
                return _s(f"🔍 **Dry run** — `{len(targets)}` CSN month(s) in `{market_id}` would be deleted "
                    f"(manual earnings kept):\n{preview}\n\nPut `{market_id}` in `confirm` to delete.")
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
            return _s(f"🗑️ Deleted `{len(targets)}` CSN month(s) from `{market_id}`. Manual earnings kept.")

        if t == "market_sales":
            # Clear the per-item sales breakdown (the SOLD/CSN column + CSN-derived items)
            # but KEEP each month's income/spent/net totals. Use when a market shows bogus
            # "sold" data but the earnings figures should stay.
            if not market_id:
                return _s("❌ `market_id` is required for this target.")
            if not _wipe_may_touch(user, market_id):
                return _s("⛔ Managers or this market's owner only.")
            history = _load_csn_for_market(market_id)
            months = history.get("months", {}) or {}
            affected = [mk for mk, md in months.items()
                        if isinstance(md, dict) and (md.get("items") or {})]
            if not affected:
                return _s(f"✅ No per-item sales data in `{market_id}` — nothing to clear.")
            item_rows = sum(len(months[mk].get("items", {})) for mk in affected)
            if confirm.strip().lower() != market_id.strip().lower():
                return _s(f"🔍 **Dry run** — would clear `{item_rows}` per-item sales row(s) across "
                    f"`{len(affected)}` month(s) in `{market_id}`, **keeping** each month's "
                    f"income/spent/net totals.\nPut `{market_id}` in `confirm` to proceed.")
            for mk in affected:
                months[mk]["items"] = {}
            _save_csn_for_market(market_id, history)
            try:
                _recompute_share_price(market_id, reason="admin_wipe_sales")
            except Exception:
                pass
            return _s(f"🗑️ Cleared `{item_rows}` per-item sales row(s) from `{len(affected)}` month(s) in "
                f"`{market_id}`. Monthly earnings totals kept; the dashboard's SOLD column refreshes shortly.")

        if t == "employee_dms":
            if confirm.strip().upper() != "CONFIRM":
                return _s("⚠️ This deletes **all DMs this bot sent to Employees**. Run again with "
                    "**confirm: CONFIRM** to proceed.")
            base = self.bot.get_channel(WORKER_CHANNEL_ID)
            if not base or not base.guild:
                return _s("❌ Can't find the guild via WORKER_CHANNEL_ID.")
            guild = base.guild
            role = discord.utils.get(guild.roles, name=EMPLOYEE_ROLE_NAME)
            if not role:
                return _s(f"❌ Role not found: {EMPLOYEE_ROLE_NAME}")
            bot_user = self.bot.user
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
            return _s(f"✅ Done. Deleted **{total_deleted}** bot DM(s). "
                f"Employees: **{users_ok}** ok, **{users_failed}** failed.")

        return _s("❌ Unknown target.")





    @staticmethod
    def _is_noise_attachment(att) -> bool:
        """A CSN upload that carries no information — a stock scan that captured nothing,
        or an empty profiles capture. Was nested inside the old /admin csn_cleanup command;
        promoted to a method so the AI's csn_cleanup tool can share it."""
        n = (att.filename or "").lower()
        if n.startswith("csn_stock_") and att.size < 300:     # header-only CSV
            return True
        if n.startswith("csn_profiles") and att.size <= 6:    # literally "{}"
            return True
        return False

    async def _nuke_by_clone(self, channel):
        """Wipe a channel INSTANTLY by cloning it and deleting the original.

        Deleting messages one-by-one is ~0.7s each once they're older than 14 days
        (Discord forbids bulk-delete past that), so a busy channel can't be cleared
        inside an interaction's 15-minute token. Cloning is a single API call and
        keeps name, topic, permissions, category and slowmode.

        Returns (new_channel, [market_ids_rebound]).
        """
        new = await channel.clone(reason="Restocker: channel purge")
        try:
            await new.edit(position=channel.position)
        except Exception:
            pass
        old_id = int(channel.id)
        await channel.delete(reason="Restocker: channel purge")

        rebound = []
        try:
            data = _load_markets() or {}
            for k, v in (data.get("markets") or {}).items():
                if isinstance(v, dict) and str(v.get("report_channel_id") or "") == str(old_id):
                    v["report_channel_id"] = int(new.id)
                    rebound.append(k)
            if rebound:
                _save_markets(data)
                log.info("[purge] rebound %s to new channel %s", ", ".join(rebound), new.id)
        except Exception as e:
            log.warning("[purge] rebind failed: %s", e)
        return new, rebound

    async def _rebuild_one(self, interaction, m, mid, confirm, keep_humans, limit):
        """Rebuild one market's channel. Returns (deleted, posted, human_readable_note)."""
        import io as _io
        chan_id = m.get("report_channel_id")
        channel = self.bot.get_channel(int(chan_id)) if chan_id else None
        if channel is None:
            return 0, 0, "❌ no bound channel"
        try:
            perms = channel.permissions_for(channel.guild.me)
        except Exception:
            perms = None
        if perms is not None and not perms.manage_messages:
            return 0, 0, f"❌ no Manage Messages in {channel.mention}"

        months = (_load_csn_for_market(mid) or {}).get("months", {}) or {}
        # A month is worth reposting if it has an item breakdown OR any money moved.
        # Markets imported from the earnings sheet (e.g. Greyhames/`main`) carry
        # income/spent with no item rows — filtering on items alone silently skipped them.
        def _has_data(v):
            if not isinstance(v, dict):
                return False
            if v.get("items"):
                return True
            return bool(float(v.get("income", 0) or 0) or float(v.get("spent", 0) or 0))
        keys = sorted(k for k, v in months.items() if _has_data(v))
        limit = max(50, min(int(limit or 500), 2000))

        victims = []
        try:
            async for msg in channel.history(limit=limit):
                if keep_humans and not (msg.webhook_id or (msg.author and msg.author.bot)):
                    continue
                victims.append(msg)
        except Exception as e:
            return 0, 0, f"⚠️ history unreadable: {e}"

        if not confirm:
            how = ("wipe everything by recreating the channel (instant, new channel ID, "
                   "pins/history lost)" if not keep_humans else
                   f"delete {len(victims)} bot/webhook message(s), keeping human ones")
            return len(victims), len(keys), (
                f"{channel.mention}: would {how}; then post {len(keys)} report(s) "
                f"({', '.join(keys) or 'no months'})")

        deleted = 0
        if not keep_humans:
            # Instant: recreate the channel instead of deleting message-by-message.
            try:
                channel, _rb = await self._nuke_by_clone(channel)
                deleted = len(victims)
            except Exception as e:
                return 0, 0, f"⚠️ wipe failed: {e}"
        else:
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
                log.warning("[rebuild_all] %s delete phase: %s", mid, e)

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
                if items:      # nothing to tabulate for sheet-imported months
                    xb = core._build_csn_xlsx(title, name, mk, items, income, spent, market_id=mid)
                    if xb:
                        files = [discord.File(_io.BytesIO(xb), filename=f"report_{mid}_{mk}.xlsx")]
                await channel.send(embed=embed, files=files)
                posted += 1
                await asyncio.sleep(1.5)
            except Exception as e:
                log.warning("[rebuild_all] %s %s post failed: %s", mid, mk, e)
        log.info("[rebuild_all] %s: deleted %d, posted %d", mid, deleted, posted)
        return deleted, posted, f"{channel.mention}: deleted {deleted}, posted {posted}"



    async def _hive_feeds(self):
        """[(channel_id, market_id)] for every channel bound as a hive harvest feed."""
        out = []
        for k, v in (_db.get_config_prefix("hive_feed:") or {}).items():
            try:
                out.append((int(k.split(":", 1)[1]), str(v)))
            except Exception:
                continue
        return out

    def _hive_month_embed(self, site_name, mid, mk, md):
        value = float(md.get("value", 0) or 0)
        paid = float(md.get("paid_value", 0) or 0)
        owed = max(0.0, value - paid)
        qty = int(md.get("qty", 0) or 0)
        igns = md.get("by_ign") or {}
        items = md.get("by_item") or {}

        desc = (f"🍯 **{qty:,}** pieces harvested · worth **{int(value):,}**🪙\n"
                f"💸 paid out **{int(paid):,}**🪙"
                + (f" · ⏳ **{int(owed):,}**🪙 still owed" if owed >= 1 else " · ✅ all settled")
                + f"\n👷 {len(igns)} harvester(s) · {len(items)} item type(s)")
        e = discord.Embed(title=f"🐝 {site_name} · {mk}", description=desc,
                          color=0x3FB950 if owed < 1 else 0xE3B341)

        top = sorted(igns.items(), key=lambda kv: -kv[1]["value"])[:10]
        if top:
            w = max(len(n[:16]) for n, _ in top)
            rows = [f"{i+1:>2}. {n[:16]:<{w}} {v['qty']:>6,}x {int(v['value']):>9,}🪙"
                    for i, (n, v) in enumerate(top)]
            e.add_field(name="👷 Harvesters", value="```\n" + "\n".join(rows) + "\n```", inline=False)

        ti = sorted(items.items(), key=lambda kv: -kv[1]["value"])[:6]
        if ti:
            w = max(len(core._pretty_item_name(n)[:18]) for n, _ in ti)
            rows = [f"{core._pretty_item_name(n)[:18]:<{w}} {v['qty']:>6,}x {int(v['value']):>9,}🪙"
                    for n, v in ti]
            e.add_field(name="📦 Harvested", value="```\n" + "\n".join(rows) + "\n```", inline=False)
        e.set_footer(text=f"Hive site • {site_name}")
        return e


    async def fix_month_close(self, month=None, market_id=None, repost: bool = False) -> str:
        """Rebuild the month-closing posts from CURRENT data, editing in place where a
        post already exists. Was /admin fix_month_close; the slash surface was retired,
        so this returns a summary STRING for the AI instead of replying to an interaction."""
        import re as _re
        month = (month or "").strip().lower()
        all_months = month in ("", "all", "*")
        if not all_months and not _re.match(r"^\d{4}-\d{2}$", month):
            return "❌ Month must look like `2026-06`, or use `all`."
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
        return body[:1900]




async def setup(bot):
    await bot.add_cog(AdminCog(bot))
