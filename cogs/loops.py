"""Background task loops (extracted from Restocker_main). Started in cog_load;
each loop's before_loop waits until the bot is ready, so starting pre-connect is fine."""
import sys
import discord
from discord.ext import commands, tasks

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import asyncio
import time
import os as _os
import socket as _socket
import secrets as _secrets

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]

# ── Multi-instance guard ──────────────────────────────────────────────────────
# Two bot processes on the same token cause duplicate reports/payouts (the original
# "it sends the same thing" bug). Each instance writes a heartbeat to the shared DB;
# if another instance's heartbeat is fresh AND advancing between our checks (i.e.
# genuinely live, not a just-restarted stale key), we DM the owners once. Warning
# only — never a hard refuse, so a legit restart can never lock the bot out.
_INSTANCE_ID = f"{_socket.gethostname()}:{_os.getpid()}:{_secrets.token_hex(3)}"
_INSTANCE_SEEN: dict = {}
_INSTANCE_WARNED = {"done": False}
EMPLOYEE_BATCH_LOOP_SECONDS = core.EMPLOYEE_BATCH_LOOP_SECONDS
EMPLOYEE_ROLE_NAME = core.EMPLOYEE_ROLE_NAME
LOYALTY_DECAY_IDLE_DAYS = core.LOYALTY_DECAY_IDLE_DAYS
LOYALTY_DECAY_PCT_WEEKLY = core.LOYALTY_DECAY_PCT_WEEKLY
LOYALTY_IGN_DEADLINE_DAYS = core.LOYALTY_IGN_DEADLINE_DAYS
OrdersBrowser = core.OrdersBrowser
WORKER_CHANNEL_ID = core.WORKER_CHANNEL_ID
_build_market_dashboard_embed = core._build_market_dashboard_embed
_coin_rates_for_order = core._coin_rates_for_order
_coins_for_pieces = core._coins_for_pieces
_get_employee_batch_lock = core._get_employee_batch_lock
_get_ui_store = core._get_ui_store
_get_worker_announce_lock = core._get_worker_announce_lock
_load_balances = core._load_balances
_load_items = core._load_items
_load_markets = core._load_markets
_order_is_claimed_closed = core._order_is_claimed_closed
_revert_price_toward_fundamental = core._revert_price_toward_fundamental
_save_balances = core._save_balances
_send_funds_report = core._send_funds_report
_track_batch_dm_message = core._track_batch_dm_message
apply_weekly_interest = core.apply_weekly_interest
bot = core.bot
MANAGER_DM_IDS = getattr(core, "MANAGER_DM_IDS", set())
cleanup_claimed_order_dms_scan = core.cleanup_claimed_order_dms_scan
fmt_coin = core.fmt_coin
fmt_qty = core.fmt_qty
hashlib = core.hashlib
load_orders = core.load_orders
load_yaml = core.load_yaml
log = core.log
parse_iso = core.parse_iso
remaining_to_assign = core.remaining_to_assign
safe_dm = core.safe_dm
save_orders = core.save_orders
save_yaml = core.save_yaml
update_order_messages = core.update_order_messages
_team_perf_embed = core._team_perf_embed
_team_post = core._team_post

class LoopsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @tasks.loop(seconds=15)
    async def worker_announce_loop(self, ):
        async with _get_worker_announce_lock():
            try:


                channel = bot.get_channel(WORKER_CHANNEL_ID)
                if not channel or not channel.guild:
                    return

                role = discord.utils.get(channel.guild.roles, name=EMPLOYEE_ROLE_NAME)
                mention = role.mention if role else ""

                now = datetime.now(timezone.utc)

                data = load_orders()
                orders_list = data.get("orders", []) or []

                ready = [
                    o for o in orders_list
                    if isinstance(o, dict)
                    and not _order_is_claimed_closed(o)
                    and not bool(o.get("worker_announced", False))
                    and o.get("employee_announce_at")
                    and parse_iso(o["employee_announce_at"]) <= now
                ]

                if not ready:
                    return

                ready.sort(key=lambda o: int(o.get("id", 0) or 0))

                ui = data.setdefault("ui", {})
                last_sig = ui.get("last_worker_batch_sig")
                last_ts = ui.get("last_worker_batch_ts")

                ids_sig = ",".join(str(int(o["id"])) for o in ready)
                now_ts = int(time.time())

                if last_sig == ids_sig and last_ts and now_ts - int(last_ts) < 180:
                    return

                ui["last_worker_batch_sig"] = ids_sig
                ui["last_worker_batch_ts"] = now_ts

                for o in ready:
                    o["worker_announced"] = True

                if not save_orders(data):
                    for o in ready:
                        o["worker_announced"] = False
                    return

                bulk_threshold = int(getattr(core, "ORDER_BULK_CARD_THRESHOLD", 12) or 12)
                if len(ready) > bulk_threshold:
                    # BULK MODE — a full-market refill (100+ orders) posts ONE grouped
                    # board instead of a hundred rate-limited embeds. Claims run through
                    # /orders and the website; cards for these orders are never posted.
                    try:
                        await core._post_bulk_order_board(bot, channel, ready)
                    except Exception as _be:
                        print(f"[worker_announce_loop] bulk board failed: {_be}")
                else:
                    for o in ready:
                        try:
                            await update_order_messages(bot, o, allow_post=True)
                        except Exception:
                            pass

                # After all order cards are posted, fan the whole open-order set out to the SW
                # Trade Network as ONE consolidated thread. Self-throttled (≤ every
                # NETWORK_MIN_INTERVAL_MIN min, only when the open set changed) to respect the
                # network's 3-posts/hour cap. Best-effort.
                if getattr(core, "NETWORK_AUTOPOST", False) and getattr(core, "NETWORK_FORUM_CHANNEL_ID", 0):
                    try:
                        await core._post_orders_batch_to_network(bot)
                    except Exception:
                        pass

                lines = []
                _items_data_ping = _load_items().get("items", {})
                _markets_ping    = _load_markets().get("markets", {})
                for o in ready[:25]:
                    rem       = remaining_to_assign(o)
                    item_name = o.get('item', '')
                    item_info = _items_data_ping.get(item_name, {})
                    mid       = item_info.get("market_id", "main")
                    mkt_name  = (_markets_ping.get(mid) or {}).get("name", mid.capitalize())
                    lines.append(f"• **#{o['id']}** {item_name} · rem {fmt_qty(o, rem)} `[{mkt_name}]`")

                header     = "New restock requests:" if len(ready) > 1 else "New restock request:"
                content    = f"{mention} 🔔 **{header}**\n" + "\n".join(lines)
                dm_content = f"🔔 **{header}**\n" + "\n".join(lines)

                content_hash = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()

                data2 = load_orders()
                ui2 = data2.setdefault("ui", {})
                last_hash = ui2.get("last_worker_ping_hash")
                last_hash_ts = ui2.get("last_worker_ping_ts")
                now_ts2 = int(time.time())

                if last_hash == content_hash and last_hash_ts and now_ts2 - int(last_hash_ts) < 180:
                    return

                me = getattr(bot, "user", None)
                if me:
                    try:
                        dupes = []
                        async for msg in channel.history(limit=10, oldest_first=False):
                            if msg.author and msg.author.id == me.id and (msg.content or "") == content:
                                dupes.append(msg)
                        if dupes:
                            dupes.sort(key=lambda m: m.created_at, reverse=True)
                            for extra in dupes[1:]:
                                try:
                                    await extra.delete()
                                except Exception:
                                    pass

                            # Reload before saving — the history scan above awaited, so data2
                            # may be stale (see the main save below for the full rationale).
                            _d = load_orders()
                            _u = _d.setdefault("ui", {})
                            _u["last_worker_ping_hash"] = content_hash
                            _u["last_worker_ping_ts"] = now_ts2
                            save_orders(_d)
                            return
                    except Exception:
                        pass

                await channel.send(content, allowed_mentions=discord.AllowedMentions(roles=True))

                import asyncio as _aio_wa
                worker_role = discord.utils.get(channel.guild.roles, name=EMPLOYEE_ROLE_NAME)
                if worker_role:
                    for member in list(worker_role.members):
                        if member.bot:
                            continue
                        try:
                            await safe_dm(member, dm_content)
                        except Exception:
                            pass
                        # Throttle: 60+ DMs in a tight burst is what gets a bot rate-limited.
                        await _aio_wa.sleep(0.4)

                # Do NOT save the stale pre-fanout snapshot (data2) — save_orders upserts every
                # order row, so a claim made during the minutes-long DM loop would be reverted
                # to unclaimed (→ double claim / double payout). Reload fresh and write ONLY
                # the ui dedup keys this loop actually owns.
                data3 = load_orders()
                ui3 = data3.setdefault("ui", {})
                ui3["last_worker_ping_hash"] = content_hash
                ui3["last_worker_ping_ts"] = now_ts2
                save_orders(data3)

            except Exception as e:
                print(f"[worker_announce_loop] error: {e}")

    @worker_announce_loop.before_loop
    async def _wait_ready_worker_announce(self, ):
        await bot.wait_until_ready()

    @tasks.loop(minutes=10)
    async def claimed_dm_cleanup_loop(self, ):
        try:
            await cleanup_claimed_order_dms_scan(bot)
        except Exception:
            return

    @claimed_dm_cleanup_loop.before_loop
    async def _wait_ready_claimed_dm_cleanup(self, ):
        await bot.wait_until_ready()

    @tasks.loop(seconds=EMPLOYEE_BATCH_LOOP_SECONDS)
    async def employee_batch_dispatch_loop(self, ):
        async with _get_employee_batch_lock():
            try:
                await asyncio.sleep(3)

                worker_channel = bot.get_channel(WORKER_CHANNEL_ID)
                if not worker_channel or not getattr(worker_channel, "guild", None):
                    return
                guild = worker_channel.guild

                employee_role = discord.utils.get(guild.roles, name=EMPLOYEE_ROLE_NAME)
                if not employee_role:
                    return

                now = datetime.now(timezone.utc)

                data = load_orders()
                orders_list = data.get("orders", []) or []

                ready = []
                for o in orders_list:
                    if not isinstance(o, dict):
                        continue
                    st = str(o.get("status", "")).lower()
                    if st in ("fulfilled", "cancelled"):
                        continue
                    if bool(o.get("employee_announced", False)):
                        continue
                    if not o.get("employee_announce_at"):
                        continue
                    if parse_iso(o["employee_announce_at"]) <= now:
                        ready.append(o)

                if not ready:
                    return

                ready.sort(key=lambda o: int(o.get("id", 0) or 0))
                show = ready[:25]

                try:
                    items_data = _load_items()
                except Exception:
                    items_data = {"items": {}}

                lines: list[str] = []
                for o in show:
                    rem = remaining_to_assign(o)
                    price_piece, _, price_barrel, _ppb = _coin_rates_for_order(o, items_data)
                    total_rem = _coins_for_pieces(o, int(rem), items_data)

                    lines.append(
                        f"• **#{o['id']}** {o.get('item','')}\n"
                        f"rem {fmt_qty(o, rem)} · {fmt_coin(price_piece)}c/piece · {fmt_coin(price_barrel)}c/barrel · ≈ {fmt_coin(total_rem)}c"
                    )

                embed = discord.Embed(
                    title="📦 New Production Requests (batch)",
                    description="\n".join(lines),
                    color=discord.Color.orange()
                )

                for o in ready:
                    o["employee_announced"] = True
                if not save_orders(data):
                    log.error("[employee_batch_dispatch_loop] save_orders failed; NOT sending to avoid duplicates.")
                    return

                data = load_orders()
                store = _get_ui_store(data)

                sent = 0
                edited = 0
                failed = 0

                members = [m for m in list(employee_role.members) if not getattr(m, "bot", False)]

                # Collect DM-tracking updates locally and apply them to a FRESH load after the
                # fan-out: saving the pre-fanout snapshot would upsert every stale order row,
                # reverting any claim made during the minutes this loop spends DMing (→ double
                # claim / double payout). The stale `store` reads above are fine (read-only).
                import asyncio as _aio_eb
                dm_tracks = []
                for member in members:
                    await _aio_eb.sleep(0.4)   # throttle — 60+ DMs in a burst = rate-limit bait
                    uid_str = str(int(member.id))
                    tracked = store.get(uid_str)
                    tracked_ids = tracked if isinstance(tracked, list) else ([tracked] if tracked else [])
                    tracked_ids = [int(x) for x in tracked_ids if str(x).isdigit()]
                    last_id = tracked_ids[-1] if tracked_ids else None

                    try:
                        dm = member.dm_channel or await member.create_dm()
                        view = OrdersBrowser(show, viewer_id=int(member.id))

                        if last_id:
                            try:
                                msg = await dm.fetch_message(int(last_id))
                                await msg.edit(embed=embed, view=view)
                                edited += 1

                                for old_id in tracked_ids[:-1]:
                                    try:
                                        old_msg = await dm.fetch_message(int(old_id))
                                        await old_msg.delete()
                                    except Exception:
                                        pass

                                dm_tracks.append((member.id, int(last_id)))
                                continue
                            except Exception:
                                pass

                        msg = await dm.send(embed=embed, view=view)
                        sent += 1

                        for old_id in tracked_ids:
                            try:
                                old_msg = await dm.fetch_message(int(old_id))
                                await old_msg.delete()
                            except Exception:
                                pass

                        dm_tracks.append((member.id, int(msg.id)))

                    except discord.Forbidden:
                        failed += 1
                    except Exception:
                        failed += 1

                fresh = load_orders()
                for _mid, _msgid in dm_tracks:
                    _track_batch_dm_message(fresh, _mid, _msgid)
                save_orders(fresh)
                log.info("[employee_batch_dispatch_loop] edited=%d sent=%d failed=%d ready=%d", edited, sent, failed, len(ready))

            except Exception as e:
                log.error("[employee_batch_dispatch_loop] error: %s", e, exc_info=True)

    @employee_batch_dispatch_loop.before_loop
    async def _before_employee_batch_dispatch_loop(self, ):
        await bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def dividend_report_flush_loop(self, ):
        """Post queued payout events (GEX.PR pool + shareholder dividends) to
        #dividend-reports. The payout engines run in sync code and can only queue;
        this loop is the async half. Empties the queue on success, keeps it on failure."""
        try:
            import json as _json
            import Restocker_db as _db
            raw = _db.get_config("pending_dividend_posts")
            if not raw:
                return
            try:
                q = _json.loads(raw)
            except Exception:
                _db.set_config("pending_dividend_posts", "[]")
                return
            if not isinstance(q, list) or not q:
                return
            ch_id = int(getattr(core, "DIVIDEND_REPORTS_CHANNEL_ID", 0) or 0)
            channel = bot.get_channel(ch_id) if ch_id else None
            if channel is None:
                return                      # channel not visible yet — keep the queue
            markets = core._load_markets().get("markets", {}) or {}
            remaining = []
            for entry in q:
                try:
                    mid = str(entry.get("market_id") or "")
                    label = None
                    try:
                        import Restocker_db as _db2
                        label = _db2.get_config(f"stock_label:{mid}")
                    except Exception:
                        pass
                    mname = label or (markets.get(mid) or {}).get("name", mid)
                    month = entry.get("month", "?")
                    if entry.get("type") == "investor_pool":
                        lines = [f"Net profit `{float(entry.get('net') or 0):,.0f}` 🪙 · "
                                 f"pool **{float(entry.get('pool_pct') or 0):g}%** = "
                                 f"`{float(entry.get('pool') or 0):,.0f}` 🪙"]
                        for uid, amt in (entry.get("paid") or [])[:20]:
                            lines.append(f"• <@{uid}> — **{int(amt):,}** 🪙")
                        emb = discord.Embed(
                            title=f"💰 GEX.PR profit share — {mname} · {month}",
                            description="\n".join(lines)[:4000],
                            color=discord.Color.gold())
                        emb.set_footer(text="Paid automatically when monthly results record")
                    else:
                        emb = discord.Embed(
                            title=f"📈 Shareholder dividend — {mname} · {month}",
                            description=(f"Total **{int(entry.get('total') or 0):,}** 🪙 · "
                                         f"`{float(entry.get('per_share') or 0):,.4f}`/share · "
                                         f"{int(entry.get('holders') or 0)} holder(s)"),
                            color=discord.Color.green())
                        emb.set_footer(text="Paid from the market treasury to all common shareholders")
                    await channel.send(embed=emb)
                    await asyncio.sleep(0.5)
                except Exception as _pe:
                    remaining.append(entry)
                    log.warning("[dividend_flush] post failed: %s", _pe)
            _db.set_config("pending_dividend_posts", _json.dumps(remaining))
        except Exception as e:
            log.warning("[dividend_flush] loop error: %s", e)

    @dividend_report_flush_loop.before_loop
    async def _before_dividend_report_flush_loop(self, ):
        await bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def weekly_interest_loop(self, ):
        try:
            applied_users, total_paid = apply_weekly_interest(force=False)
            if applied_users <= 0 or total_paid <= 0:
                return
        except Exception:
            return

    @weekly_interest_loop.before_loop
    async def _wait_ready_interest(self, ):
        await bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def weekly_funds_report_loop(self, ):
        # Fully guarded: an unhandled exception would permanently stop this loop.
        try:
            data = _load_balances()
            meta = data.setdefault("meta", {})
            last = meta.get("last_funds_report_week")
            now = datetime.now(timezone.utc)
            iso_week = f"{now.isocalendar().year}-W{now.isocalendar().week}"
            if last == iso_week:
                return
            if now.weekday() != 0:
                return
            # NOTE: no hour gate. tasks.loop(hours=24) ticks at whatever time-of-day the bot
            # booted, so an "early-UTC only" window could simply never coincide with the tick
            # and the report would never send. Weekday + the week-key above are sufficient.
            ok = await _send_funds_report(bot)
            if ok:
                meta["last_funds_report_week"] = iso_week
                _save_balances(data)
        except Exception as e:
            log.warning("[weekly_funds_report_loop] %s", e)

    @weekly_funds_report_loop.before_loop
    async def _wait_ready_weekly_report(self, ):
        await bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def loyalty_decay_loop(self, ):
        """Apply point decay to users inactive for > LOYALTY_DECAY_IDLE_DAYS."""
        try:
            import Restocker_db as _db_decay
            now = datetime.now(timezone.utc)
            # LOYALTY_DECAY_PCT_WEEKLY is a WEEKLY rate but this loop ticks DAILY — without a
            # week guard an idle user lost 20% per DAY (0.8^7 ≈ 79%/week instead of 20%).
            # Same once-per-week key pattern as apply_weekly_interest.
            iso_week = f"{now.isocalendar().year}-W{now.isocalendar().week}"
            if _db_decay.get_config("last_loyalty_decay_week") == iso_week:
                return
            idle_threshold = (now - timedelta(days=LOYALTY_DECAY_IDLE_DAYS)).isoformat()
            all_loy = _db_decay.get_all_loyalty()
            updates = []
            for row in all_loy:
                last = row.get("last_activity")
                if not last:
                    continue
                if last >= idle_threshold:
                    continue
                pts = float(row.get("points", 0))
                if pts <= 0:
                    continue
                new_pts = max(0.0, pts * (1.0 - LOYALTY_DECAY_PCT_WEEKLY / 100.0))
                if abs(new_pts - pts) > 0.5:
                    updates.append((new_pts, row["user_id"]))
            if updates:
                _db_decay.update_loyalty_points_bulk(updates)
                log.info("[loyalty] Decay applied to %d users", len(updates))
            _db_decay.set_config("last_loyalty_decay_week", iso_week)
        except Exception as e:
            log.warning("[loyalty] decay_loop failed: %s", e)

    @loyalty_decay_loop.before_loop
    async def _before_loyalty_decay(self, ):
        await bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def ign_deadline_loop(self, ):
        """Remove employee role from users who didn't register IGN within deadline."""
        try:
            import Restocker_db as _db_ign_dl
            now = datetime.now(timezone.utc).isoformat()
            overdue = [p for p in _db_ign_dl.get_all_ign_pending() if p["deadline"] < now]
            for pending in overdue:
                uid = int(pending["user_id"])
                # SAFETY: never strip someone who DID register. Pending-row cleanup is spread
                # across several registration paths and one missed row here would cost a real
                # employee their role — re-check the registry and self-heal the stale row.
                if _db_ign_dl.get_ign(str(uid)):
                    _db_ign_dl.delete_ign_pending(str(uid))
                    continue
                guild = bot.get_guild(int(pending["guild_id"]))
                if not guild:
                    continue
                member = guild.get_member(uid)
                if member:
                    role = guild.get_role(int(pending["role_id"]))
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="IGN not registered within 3 days")
                            await member.send(
                                f"⚠️ Your **{role.name}** role was removed because you didn't register "
                                f"your in-game username within {LOYALTY_IGN_DEADLINE_DAYS} days.\n"
                                f"Contact a manager to be reinstated."
                            )
                            log.info("[ign] Removed role %s from %s (deadline passed)", role.name, member)
                        except Exception:
                            pass
                _db_ign_dl.delete_ign_pending(str(uid))
        except Exception as e:
            log.warning("[ign] deadline_loop failed: %s", e)

    @ign_deadline_loop.before_loop
    async def _before_ign_deadline(self, ):
        await bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def stock_reversion_loop(self, ):
        """Daily mean-reversion pass over every public market."""
        try:
            import Restocker_db as _db
            for mid in list(_db.get_public_markets().keys()):
                try:
                    _revert_price_toward_fundamental(mid)
                except Exception as e:
                    log.warning("[stock_reversion_loop] %s: %s", mid, e)
        except Exception as e:
            log.warning("[stock_reversion_loop] %s", e)

    @stock_reversion_loop.before_loop
    async def _before_stock_reversion(self, ):
        await bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def stock_dashboard_loop(self, ):
        """Keep the registered market dashboard message fresh."""
        try:
            core._snapshot_market_index(force=True)   # 5-min heartbeat for the Abexilas index
        except Exception:
            pass
        try:
            import Restocker_db as _dbrb
            if _dbrb.get_config("etf_rebalance_pending") == "1":
                _dbrb.set_config("etf_rebalance_pending", "0")
                if core._etf_nav().get("units", 0) > 0:
                    core._etf_rebalance("composition_change")
        except Exception as e:
            log.warning("[etf-rebalance loop] %s", e)
        try:
            state = load_yaml("stock_dashboard.yml", {}) or {}
            ch_id, msg_id = state.get("channel_id"), state.get("message_id")
            if not ch_id or not msg_id:
                return
            channel = bot.get_channel(int(ch_id))
            if channel is None:
                return
            try:
                msg = await channel.fetch_message(int(msg_id))
            except discord.NotFound:
                save_yaml("stock_dashboard.yml", {})
                return
            await msg.edit(embed=_build_market_dashboard_embed())
        except Exception as e:
            log.warning("[stock_dashboard_loop] %s", e)

    @stock_dashboard_loop.before_loop
    async def _wait_ready_stock_dashboard(self, ):
        await bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def team_digest_loop(self, ):
        # Whole body guarded: an unhandled exception (e.g. a transient "database is locked"
        # from get_config/get_team_settings) would permanently stop this loop — tasks.loop
        # only auto-retries connection errors. Also: no hour gate — the daily tick lands at
        # boot time-of-day, so a narrow window could never coincide and the digest would
        # never send. Monday + the week-key are sufficient.
        try:
            import Restocker_db as _db
            now = datetime.now(timezone.utc)
            iso_week = f"{now.isocalendar().year}-W{now.isocalendar().week}"
            if _db.get_config("last_team_digest_week") == iso_week:
                return
            if now.weekday() != 0:      # Mondays only
                return
            try:
                managers = sorted({r["manager_id"] for r in _db.get_all_team_perf(None)})
            except Exception:
                managers = []
            posted = 0
            for mgr in managers:
                try:
                    st = _db.get_team_settings(mgr)
                    if not st or not ((st.get("webhook_url") or "").strip() or (st.get("channel_id") or "").strip()):
                        continue
                    embed = _team_perf_embed(mgr, 7)
                    ok = await _team_post(mgr, content="📅 Weekly team performance digest", embed=embed)
                    if ok:
                        posted += 1
                except Exception as e:
                    log.warning("[team-digest] %s failed: %s", mgr, e)
            _db.set_config("last_team_digest_week", iso_week)
            log.info("[team-digest] posted %d team digest(s)", posted)
        except Exception as e:
            log.warning("[team-digest] loop error: %s", e)

    @team_digest_loop.before_loop
    async def _wait_ready_team_digest(self, ):
        await bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def db_backup_loop(self, ):
        import os, glob, asyncio as _aio
        # Ticks hourly but only writes once per DB_BACKUP_EVERY_HOURS-hour window (default 3 →
        # ~8 snapshots/day), gated by a time-bucket key so a "database is locked" hiccup can't
        # permanently stop it. The first tick after a (re)start backs up if the current window
        # hasn't been captured yet — so you get a fresh restore point right after every restart,
        # which is exactly when things tend to go wrong.
        try:
            import Restocker_db as _db
            now = datetime.now(timezone.utc)
            every = max(1, int(getattr(core, "DB_BACKUP_EVERY_HOURS", 3)))
            bucket = now.strftime("%Y-%m-%d") + f"_{now.hour // every}"
            if _db.get_config("last_db_backup_bucket") == bucket:
                return
            os.makedirs("backups", exist_ok=True)
            dest = os.path.join("backups", f"restocker_{now.strftime('%Y%m%d_%H%M%S')}.db")
            await _aio.to_thread(_db.backup_database, dest)
            keep = int(getattr(core, "DB_BACKUP_KEEP", 56))   # ~1 week at 8/day (3-hourly)
            files = sorted(glob.glob(os.path.join("backups", "restocker_*.db")))
            for f in files[:-keep] if keep > 0 else []:
                try:
                    os.remove(f)
                except Exception:
                    pass
            _db.set_config("last_db_backup_bucket", bucket)
            log.info("[db-backup] wrote %s (every %dh, retain %d)", dest, every, keep)
        except Exception as e:
            log.warning("[db-backup] failed: %s", e)

    @db_backup_loop.before_loop
    async def _wait_ready_db_backup(self, ):
        await bot.wait_until_ready()

    @tasks.loop(seconds=30)
    async def instance_heartbeat_loop(self, ):
        """Detect a second bot process on the same token (root cause of duplicate
        reports/payouts). Each instance writes instance_hb:<id>=<epoch>; we warn the
        owners ONCE if another id's heartbeat is fresh AND advanced since our last look
        (proves it's live, not a just-restarted stale key). Warning only, never a refuse."""
        try:
            import Restocker_db as _db, time as _t
            now = int(_t.time())
            try:
                allcfg = _db.get_all_config() or {}
            except Exception:
                allcfg = {}
            live_others = []
            for k, v in allcfg.items():
                if not str(k).startswith("instance_hb:"):
                    continue
                oid = str(k)[len("instance_hb:"):]
                if oid == _INSTANCE_ID:
                    continue
                try:
                    ots = int(v)
                except (TypeError, ValueError):
                    continue
                if now - ots > 86400:          # prune week-dead keys occasionally
                    try: _db.delete_config(k)
                    except Exception: pass
                    continue
                if now - ots < 75:
                    prev = _INSTANCE_SEEN.get(oid)
                    if prev is not None and ots > prev:   # actively updating -> genuinely live
                        live_others.append(oid)
                    _INSTANCE_SEEN[oid] = ots
            if live_others and not _INSTANCE_WARNED["done"]:
                _INSTANCE_WARNED["done"] = True
                msg = ("⚠️ **Another bot instance appears to be running** on this token "
                       f"(`{live_others[0]}`). This one is `{_INSTANCE_ID}`. Two instances cause "
                       "duplicate reports and double payouts — shut one down (check Wispbyte + any local run).")
                log.warning("[instance] %s", msg.replace("**", ""))
                for _mid in MANAGER_DM_IDS:
                    try:
                        u = await bot.fetch_user(int(_mid))
                        await u.send(msg)
                    except Exception:
                        pass
            try:
                _db.set_config(f"instance_hb:{_INSTANCE_ID}", str(now))
            except Exception:
                pass
        except Exception as e:
            log.debug("[instance] heartbeat failed: %s", e)

    @instance_heartbeat_loop.before_loop
    async def _wait_ready_instance_hb(self, ):
        await bot.wait_until_ready()

    def _all_loops(self):
        return (self.worker_announce_loop, self.claimed_dm_cleanup_loop, self.employee_batch_dispatch_loop,
                self.dividend_report_flush_loop,
                self.weekly_interest_loop, self.weekly_funds_report_loop, self.loyalty_decay_loop,
                self.ign_deadline_loop, self.stock_reversion_loop, self.stock_dashboard_loop,
                self.team_digest_loop, self.db_backup_loop, self.instance_heartbeat_loop)

    def _start_loops(self):
        for _lp in self._all_loops():
            if not _lp.is_running():
                _lp.start()

    async def cog_load(self):
        # NOTE: cog_load runs during load_extension, which happens BEFORE bot.start().
        # Starting the loops here made each before_loop call wait_until_ready() while the
        # client was still uninitialised -> RuntimeError "Client has not been properly
        # initialised" -> every loop died at boot (no order posts, no employee DMs, no
        # dashboard/report/backup loops). So only start here if the bot is ALREADY ready
        # (e.g. a live cog reload); the normal boot path starts them from on_ready below.
        if self.bot.is_ready():
            self._start_loops()

    @commands.Cog.listener()
    async def on_ready(self):
        # Fires after login -> wait_until_ready() now returns instantly, loops run for real.
        self._start_loops()

    def cog_unload(self):
        for _lp in self._all_loops():
            _lp.cancel()


async def setup(bot):
    await bot.add_cog(LoopsCog(bot))
