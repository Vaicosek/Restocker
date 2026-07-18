"""Money / futures / investor commands (extracted from Restocker_main)."""
import re
import sys
import discord
from discord import app_commands
from discord.ext import commands

from datetime import datetime
from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
FUNDS_REPORT_CHANNEL_ID = core.FUNDS_REPORT_CHANNEL_ID
FuturesOrderView = core.FuturesOrderView
MANAGER_ROLE_ALT = core.MANAGER_ROLE_ALT
MANAGER_ROLE_NAME = core.MANAGER_ROLE_NAME
WEB_ORDERS_CHANNEL_ID = core.WEB_ORDERS_CHANNEL_ID
FUTURES_CHANNEL_ID = core.FUTURES_CHANNEL_ID
WORKER_CHANNEL_ID = core.WORKER_CHANNEL_ID
_get_user_bal = core._get_user_bal
_load_balances = core._load_balances
_open_payout_ticket = core._open_payout_ticket
_owner_markets_for_user = core._owner_markets_for_user
add_coins = core.add_coins
any_item_autocomplete = core.any_item_autocomplete
future_item_autocomplete = core.future_item_autocomplete
_is_future_item = core._is_future_item
bot = core.bot
ephemeral_kwargs = core.ephemeral_kwargs
is_manager = core.is_manager
timezone = core.timezone


async def _liquidate_target_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest anyone the bot knows as an investor or shareholder (register + cached
    holder names), so people who already LEFT Discord are still pickable."""
    import Restocker_db as _db
    seen = {}
    try:
        for uid, name in (core.load_yaml("stock_names.yml", {}) or {}).items():
            seen[str(uid)] = str(name or uid)
    except Exception:
        pass
    try:
        for uid, inv in (_db.get_investors() or {}).items():
            seen[str(uid)] = str(inv.get("name") or seen.get(str(uid)) or uid)
    except Exception:
        pass
    # ACTUAL shareholders too — the reclaim keys off the ID that holds the shares, and a
    # holder missing from stock_names.yml would otherwise be unpickable (an @mention of
    # their Discord account can be a different ID than the one on the cap table).
    try:
        for mid in (_db.get_public_markets() or {}):
            for h in _db.get_holders(mid):
                huid = str(h.get("user_id"))
                label = seen.get(huid) or f"holder …{huid[-4:]}"
                seen[huid] = f"{label} · {float(h.get('shares') or 0):,.0f} sh {mid}"
    except Exception:
        pass
    cur = (current or "").lower()
    out = []
    for uid, name in sorted(seen.items(), key=lambda kv: kv[1].lower()):
        if cur and cur not in name.lower() and cur not in uid:
            continue
        out.append(app_commands.Choice(name=f"{name} ({uid})"[:100], value=uid))
        if len(out) >= 25:
            break
    return out


class MoneyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="balance", description="Show your coin balance (or another user's if Manager).")


    @app_commands.describe(user="(Managers) Optional: check someone else's balance")
    async def balance_cmd(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target = user or interaction.user


        if user is not None and not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only (for checking others).", **ephemeral_kwargs(interaction))

        data = _load_balances()
        u = _get_user_bal(data["users"], target.id)

        await interaction.response.send_message(
            f"💰 Balance for {target.mention}\n"
            f"• Coins: **{u['coins']}**\n"
            f"• Principal: **{u.get('principal', u['coins'])}**",
            **ephemeral_kwargs(interaction)
        )

    @app_commands.command(
        name="futures_order",
        description="(Market owners) Request a custom item crafted to order",
    )
    @app_commands.describe(
        item="The item you want (e.g. Diamond Pickaxe)",
        quantity="How many you want",
        enchants="Required enchants/quality (e.g. 'Fortune III, Unbreaking' or 'Clean — no Silk Touch/Fortune, Unbreaking')",
        notes="Anything else workers/managers should know",
    )
    @app_commands.autocomplete(item=future_item_autocomplete)
    async def futures_order(self,
        interaction: discord.Interaction,
        item: str,
        quantity: int,
        enchants: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        if not (is_manager(interaction) or _owner_markets_for_user(interaction.user.id)):
            return await interaction.response.send_message(
                "📈 Futures orders are for market owners only.", **ephemeral_kwargs(interaction)
            )
        if quantity <= 0:
            return await interaction.response.send_message(
                "❌ Quantity must be a positive integer.", **ephemeral_kwargs(interaction)
            )

        item = (item or "").strip()
        if not item:
            return await interaction.response.send_message(
                "❌ Please specify an item.", **ephemeral_kwargs(interaction)
            )

        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)

        try:
            import Restocker_db as _db
            order_id = _db.save_futures_order(
                user_id=interaction.user.id,
                username=interaction.user.display_name,
                item=item,
                quantity=quantity,
                enchants=enchants or "",
                notes=notes or "",
            )
        except Exception as e:
            return await interaction.followup.send(f"⚠️ DB error: {e}", **ephemeral_kwargs(interaction))

        # Futures approvals go to their own #futures channel; fall back to the
        # web-orders channel, then the funds channel, if it isn't configured.
        channel = None
        if FUTURES_CHANNEL_ID:
            channel = bot.get_channel(FUTURES_CHANNEL_ID)
        if channel is None and WEB_ORDERS_CHANNEL_ID:
            channel = bot.get_channel(WEB_ORDERS_CHANNEL_ID)
        if channel is None:
            channel = bot.get_channel(FUNDS_REPORT_CHANNEL_ID)

        if channel is not None:
            embed = discord.Embed(
                title=f"🔮 New Futures Order #{order_id}",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Customer", value=interaction.user.mention, inline=True)
            embed.add_field(name="Item", value=f"{quantity}x {item}", inline=True)
            if enchants:
                embed.add_field(name="Enchants / Quality", value=enchants, inline=False)
            if notes:
                embed.add_field(name="Notes", value=notes, inline=False)
            embed.set_footer(text="Awaiting manager review")

            mgr_role = discord.utils.get(channel.guild.roles, name=MANAGER_ROLE_NAME) if channel.guild else None
            alt_role  = discord.utils.get(channel.guild.roles, name=MANAGER_ROLE_ALT)  if channel.guild else None
            ping = " ".join(r.mention for r in [mgr_role, alt_role] if r)

            try:
                msg = await channel.send(
                    content=f"{ping} — new futures order!" if ping else "New futures order!",
                    embed=embed,
                    view=FuturesOrderView(order_id),
                )
                try:
                    _db.update_futures_order_status(
                        order_id, status="pending", reviewed_by=None, notify_msg_id=str(msg.id)
                    )
                except Exception:
                    pass
            except Exception as e:
                print(f"⚠️ Could not post futures order notification: {e}")

        await interaction.followup.send(
            f"✅ Futures order #{order_id} submitted for **{quantity}x {item}**"
            + (f" ({enchants})" if enchants else "")
            + " — a manager will review it shortly.",
            **ephemeral_kwargs(interaction)
        )

    @app_commands.command(
        name="futures_bulk",
        description="(Owner/Manager) One bulk futures order from a pasted item list — then Approve & Fulfill")
    @app_commands.describe(
        customer="Who this order is for (the buyer)",
        market_id="The buyer's market — where resales are tracked for consignment billing (optional)")
    @app_commands.autocomplete(market_id=core._market_autocomplete)
    async def futures_bulk(self, interaction: discord.Interaction, customer: discord.Member,
                           market_id: Optional[str] = None):
        if not (is_manager(interaction) or _owner_markets_for_user(interaction.user.id)):
            return await interaction.response.send_message(
                "📈 Bulk futures orders are for market owners / managers only.",
                **ephemeral_kwargs(interaction))
        # A modal is the natural place to paste a multi-line list. It parses on submit and
        # posts the review card with Approve & Fulfill.
        from views.web import FuturesBulkModal
        await interaction.response.send_modal(FuturesBulkModal(
            customer_id=customer.id, customer_name=customer.display_name,
            market_id=market_id or "", created_by=interaction.user.id))

    # ── Investors (/investor ...) — GEX.PR preferred shareholders, profit-share engine ──
    investor = app_commands.Group(
        name="investor",
        description="(Managers) V Tech investors — sync the GEX.PR cap table, pool %, payouts",
        default_permissions=discord.Permissions(manage_guild=True))

    @investor.command(name="sync", description="Paste a Crimson Banking cap-table export to (re)build the investor register")
    async def investor_sync(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        from views.web import InvestorSyncModal
        await interaction.response.send_modal(InvestorSyncModal())

    @investor.command(name="status", description="Investor register, pool %, and recent distributions")
    async def investor_status(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        invs = sorted((_db.get_investors() or {}).values(),
                      key=lambda i: -float(i.get("share_pct") or 0))
        pool = core._investor_pool_pct()
        embed = discord.Embed(title="V Tech investors (GEX.PR)", color=discord.Color.gold())
        embed.add_field(name="Profit pool", value=f"`{pool:g}%` of each V Tech market's monthly net "
                        f"(change: `/investor set_pool`)", inline=False)
        if invs:
            lines = [f"• <@{i['user_id']}> **{i.get('name') or '?'}** — "
                     f"{float(i.get('pref_shares') or 0):,.0f} pref · **{float(i.get('share_pct') or 0):g}%** · "
                     f"received `{float(i.get('total_received') or 0):,.0f}`"
                     for i in invs[:20]]
            embed.add_field(name=f"Register ({len(invs)})", value="\n".join(lines)[:1000], inline=False)
        else:
            embed.add_field(name="Register", value="*empty — run `/investor sync` with the GEX.PR "
                            "cap-table export from Crimson Banking*", inline=False)
        try:
            recent = _db.get_investor_payout_log(6)
        except Exception:
            recent = []
        if recent:
            embed.add_field(name="Recent distributions", value="\n".join(
                f"• <@{r['user_id']}> +`{float(r['amount']):,.0f}` · {r.get('note') or ''}"
                for r in recent)[:1000], inline=False)
        embed.set_footer(text="Distributions run automatically when a V Tech market's monthly "
                              "CSN net records — positive months only, once per market-month.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @investor.command(name="set_pool", description="Set what % of V Tech monthly net goes to investors")
    @app_commands.describe(pct="0–100. The pool is then split by each investor's share.")
    async def investor_set_pool(self, interaction: discord.Interaction,
                                pct: app_commands.Range[float, 0.0, 100.0]):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        _db.set_config("investor_pool_pct", str(float(pct)))
        await interaction.response.send_message(
            f"✅ Investor pool set to **{pct:g}%** of each V Tech market's monthly net. "
            f"Applies to distributions from now on (never retroactive).", ephemeral=True)

    @investor.command(name="payout", description="Manual extra payout to one investor (straight to their coins)")
    @app_commands.describe(user="The investor", amount="Coins to credit", reason="Shows on the log")
    async def investor_payout(self, interaction: discord.Interaction, user: discord.Member,
                              amount: app_commands.Range[int, 1, 1_000_000_000], reason: str = "manual"):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        if not _db.get_investor(str(user.id)):
            return await interaction.response.send_message(
                f"❌ {user.mention} isn't on the investor register — `/investor sync` first.", ephemeral=True)
        core.add_coins(user.id, int(amount), counts_as_principal=False,
                       reason=f"investor:manual:{reason[:80]}")
        _db.add_investor_payout(str(user.id), int(amount), note=f"manual:{reason[:80]}")
        await interaction.response.send_message(
            f"💸 Paid {user.mention} **{amount:,}** coins (investor payout: *{reason}*).", ephemeral=True)
        try:
            await user.send(f"💸 Investor payout: **{amount:,}** coins — *{reason}*.")
        except Exception:
            pass

    @investor.command(name="apply_roles",
                      description="(Manager) Give every registered GEX.PR investor the canonical Investor role")
    @app_commands.describe(role="Role to assign (default: the canonical/most-populated 'Investor' role)")
    async def investor_apply_roles(self, interaction: discord.Interaction,
                                   role: Optional[discord.Role] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Run this in the server.", ephemeral=True)
        import Restocker_db as _db
        invs = _db.get_investors() or {}
        if not invs:
            return await interaction.response.send_message("No investors registered.", ephemeral=True)
        if role is None:
            try:
                _rid = int(_db.get_config("canonical_role:investor") or 0)
                role = guild.get_role(_rid) if _rid else None
            except Exception:
                role = None
            if role is None:
                cands = [r for r in guild.roles if "investor" in r.name.lower()]
                role = max(cands, key=lambda r: len(r.members)) if cands else None
            if role is None:
                try:
                    role = await guild.create_role(name="Investor", reason="GEX.PR investors")
                except Exception as e:
                    return await interaction.response.send_message(f"❌ Couldn't create the role: {e}", ephemeral=True)
        _db.set_config("canonical_role:investor", str(role.id))
        await interaction.response.defer(ephemeral=True, thinking=True)
        import asyncio as _aio
        added, had, absent = [], [], []
        for uid in invs.keys():
            member = guild.get_member(int(uid))
            if member is None:
                try:
                    member = await guild.fetch_member(int(uid))
                except Exception:
                    member = None
            if member is None:
                absent.append(uid)
                continue
            if role in member.roles:
                had.append(member)
                continue
            try:
                await member.add_roles(role, reason="GEX.PR investor register")
                added.append(member)
            except Exception:
                absent.append(uid)
            await _aio.sleep(0.3)
        await interaction.followup.send(
            f"✅ {role.mention}: +{len(added)} added · {len(had)} already had it · "
            f"{len(absent)} not in server / failed.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @investor.command(name="liquidate",
                      description="Mark a gone-for-good holder for liquidation — their equity returns to the company")
    @app_commands.describe(
        action="add = mark, remove = unmark, list = show everyone marked",
        user_id="Who (works for people who already left Discord — pick from the list or paste an ID)",
        note="Why, e.g. 'perma banned' — shows on the list")
    @app_commands.choices(action=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="list", value="list")])
    @app_commands.autocomplete(user_id=_liquidate_target_autocomplete)
    async def investor_liquidate(self, interaction: discord.Interaction, action: str,
                                 user_id: Optional[str] = None, note: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        cur = core._liquidated_holders()
        if action == "list":
            if not cur:
                return await interaction.response.send_message("Nobody is marked for liquidation.", ephemeral=True)
            names = core.load_yaml("stock_names.yml", {}) or {}
            lines = [f"• <@{uid}> **{names.get(uid, '?')}**" + (f" — *{why}*" if why else "")
                     for uid, why in cur.items()]
            return await interaction.response.send_message(
                "🧹 **Marked for liquidation** (equity reroutes to the company on the next "
                "cap-table import / investor sync):\n" + "\n".join(lines)[:1800],
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        uid = re.sub(r"\D", "", str(user_id or ""))
        if not uid:
            return await interaction.response.send_message(
                "❌ Pick a holder from the list (or paste their Discord ID).", ephemeral=True)
        _db = __import__("Restocker_db")
        import json as _json
        if action == "remove":
            if uid not in cur:
                return await interaction.response.send_message(f"<@{uid}> wasn't marked.", ephemeral=True,
                                                               allowed_mentions=discord.AllowedMentions.none())
            core._set_liquidated_holder(uid, remove=True)
            # Full undo: if this liquidation reclaimed equity, give it back from the snapshot.
            restored_note = ""
            try:
                raw = _db.get_config(f"liq_snapshot:{uid}")
                if raw:
                    snap = _json.loads(raw)
                    back = []
                    for mid, sh, cb in snap.get("holdings", []):
                        _db.adjust_holding(uid, mid, float(sh), float(cb))
                        try:
                            _db.log_stock_trade(uid, mid, "unliquidated", float(sh), 0.0, 0.0)
                        except Exception:
                            pass
                        back.append(f"`{mid}` **{float(sh):,.0f}** sh")
                    pref = snap.get("pref")
                    if pref:
                        invs = _db.get_investors() or {}
                        pct_sum = sum(float(v.get("share_pct") or 0) for v in invs.values())
                        pref_sum = sum(float(v.get("pref_shares") or 0) for v in invs.values())
                        # pcts still derive from the pre-drop total, so this recovers it exactly
                        full_total = (pref_sum / (pct_sum / 100.0)) if pct_sum > 0 else None
                        rows = [(k, (v.get("name") or ""), float(v.get("pref_shares") or 0))
                                for k, v in invs.items()]
                        rows.append((uid, str(pref[0] or ""), float(pref[1] or 0)))
                        _db.replace_investors(rows, total_shares=full_total)
                        back.append(f"GEX.PR **{float(pref[1] or 0):,.0f}** pref")
                    _db.delete_config(f"liq_snapshot:{uid}")
                    if back:
                        restored_note = "\n↩️ Restored: " + ", ".join(back)
            except Exception as e:
                restored_note = f"\n⚠ Snapshot restore failed: {e}"
            return await interaction.response.send_message(
                f"✅ <@{uid}> unmarked — future imports treat their shares normally again." + restored_note,
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        core._set_liquidated_holder(uid, note)
        # Apply IMMEDIATELY — an inactive holder loses their equity now, not on some
        # future cap-table re-import (which is no longer a routine operation). Everything
        # taken is snapshotted so action:remove is a full undo.
        reclaimed = []
        try:
            for h in _db.get_portfolio(uid):
                sh = float(h.get("shares") or 0)
                if sh <= 0:
                    continue
                cb = float(h.get("cost_basis") or 0.0)
                _db.adjust_holding(uid, h["market_id"], -sh, -cb)
                try:
                    _db.log_stock_trade(uid, h["market_id"], "liquidated", sh, 0.0, 0.0)
                except Exception:
                    pass
                reclaimed.append((h["market_id"], sh, cb))
        except Exception:
            pass
        pref_note = ""
        pref_snap = None
        try:
            invs = _db.get_investors() or {}
            if str(uid) in invs:
                pct_sum = sum(float(v.get("share_pct") or 0) for v in invs.values())
                pref_sum = sum(float(v.get("pref_shares") or 0) for v in invs.values())
                full_total = (pref_sum / (pct_sum / 100.0)) if pct_sum > 0 else (pref_sum or 1.0)
                gone = invs[str(uid)]
                pref_snap = [gone.get("name") or "", float(gone.get("pref_shares") or 0)]
                rows = [(k, (v.get("name") or ""), float(v.get("pref_shares") or 0))
                        for k, v in invs.items() if k != str(uid)]
                _db.replace_investors(rows, total_shares=full_total)
                pref_note = (f"\n📜 GEX.PR: dropped **{pref_snap[1]:,.0f}** preferred "
                             f"shares ({float(gone.get('share_pct') or 0):.1f}%) — the company keeps that "
                             f"payout slice (other investors' % unchanged).")
        except Exception:
            pref_snap = None
        if reclaimed or pref_snap:
            try:
                _db.set_config(f"liq_snapshot:{uid}", _json.dumps(
                    {"holdings": [[m, s, c] for m, s, c in reclaimed], "pref": pref_snap}))
            except Exception:
                pass
        common_note = ""
        if reclaimed:
            common_note = ("\n🧹 Reclaimed now: "
                           + ", ".join(f"`{mid}` **{sh:,.0f}** sh" for mid, sh, _cb in reclaimed)
                           + " — returned to the company (free float).")
        elif not pref_snap:
            common_note = ("\n⚠ **Nothing to reclaim under this ID** (`" + uid + "`) — no shares, no "
                           "GEX.PR stake. If they're on the cap table, their shares sit under a "
                           "DIFFERENT account: pick the entry from this command's autocomplete list "
                           "(it shows real holders with their share counts) instead of @mentioning.")
        await interaction.response.send_message(
            f"🧹 <@{uid}> marked for liquidation" + (f" (*{note}*)" if note else "") + "."
            + common_note + pref_note +
            "\nFuture cap-table imports and investor syncs will keep them out automatically. "
            "Mistake? `action:remove` restores everything taken here.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ── Platform fees (/fees ...) — infrastructure is live, charging is OFF by default ──
    fees = app_commands.Group(
        name="fees",
        description="(Managers) Platform fees — status, on/off switch, manual charges",
        default_permissions=discord.Permissions(manage_guild=True))

    @fees.command(name="status", description="Are fees on? Balance, default rate, recent charges")
    async def fees_status(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        active = core._fees_active()
        try:
            raw = _db.get_config("fees_active")
            source = "runtime (/fees toggle)" if (raw is not None and str(raw).strip() != "") else "env default"
        except Exception:
            source = "env default"
        bal = 0.0
        try:
            bal = float(_db.get_platform_balance() or 0)
        except Exception:
            pass
        embed = discord.Embed(
            title="🏦 Platform fees",
            color=discord.Color.green() if active else discord.Color.dark_grey())
        embed.add_field(name="Charging", value=("🟢 ACTIVE" if active else "⚫ OFF (dormant)")
                        + f" · via {source}", inline=False)
        embed.add_field(name="Default rate", value=f"`{core.PLATFORM_FEE_PCT:g}%` "
                        f"(per-market override: `/market edit fee_pct:`)", inline=True)
        embed.add_field(name="Balance collected", value=f"`{bal:,.0f}` 🪙", inline=True)
        try:
            rows = _db.get_platform_balance_log(5)
        except Exception:
            rows = []
        if rows:
            embed.add_field(name="Recent charges", value="\n".join(
                f"• `{r.get('month','?')}` {r.get('market_id') or '—'} · "
                f"**{float(r.get('amount') or 0):,.0f}** · {r.get('note') or ''}"
                for r in rows)[:1000], inline=False)
        embed.set_footer(text="Charge points wired (all no-ops while OFF): monthly CSN net · "
                              "futures margin collections · /fees charge (manual, e.g. tool rental)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @fees.command(name="toggle", description="Turn platform-fee charging ON or OFF (runtime — no restart)")
    @app_commands.describe(active="True = start charging at each wired point; False = fully dormant")
    async def fees_toggle(self, interaction: discord.Interaction, active: bool):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        _db.set_config("fees_active", "1" if active else "0")
        if active:
            msg = ("🟢 **Platform fees are now LIVE.** From now on: each market's monthly CSN net "
                   f"is charged its fee_pct (default {core.PLATFORM_FEE_PCT:g}%), and futures margin "
                   "collections are charged on `/futures pay`. Past months are NOT charged retroactively.")
        else:
            msg = "⚫ Platform fees switched **off** — all charge points are dormant again."
        await interaction.response.send_message(msg, ephemeral=True)

    @fees.command(name="charge", description="Manually charge a user a fee (e.g. tool/factory rental) into the platform balance")
    @app_commands.describe(user="Who pays", amount="Coins to charge", reason="What it's for (shows on the ledger)")
    async def fees_charge(self, interaction: discord.Interaction, user: discord.Member,
                          amount: app_commands.Range[int, 1, 1_000_000_000], reason: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        if user.bot:
            return await interaction.response.send_message("❌ Pick a real user.", ephemeral=True)
        import Restocker_db as _db
        bal = int(_db.get_balance(str(user.id)).get("coins") or 0)
        if bal < amount:
            return await interaction.response.send_message(
                f"❌ {user.mention} only has `{bal:,}` coins — can't charge `{amount:,}`. "
                f"(No partial charges; agree a smaller amount.)", ephemeral=True)
        reason = (reason or "").strip()[:120]
        core.deduct_coins(user.id, int(amount), reason=f"fee:{reason}")
        core._credit_platform_balance(int(amount), market_id="", note=f"manual:{reason} <@{user.id}>")
        await interaction.response.send_message(
            f"🏦 Charged {user.mention} **{amount:,}** 🪙 → platform balance (*{reason}*).", ephemeral=True)
        try:
            await user.send(f"🏦 You were charged **{amount:,}** coins by V Tech: *{reason}*.")
        except Exception:
            pass

    # ── Consignment futures management (/futures ...) ───────────────────────────────────
    futures = app_commands.Group(
        name="futures",
        description="(Owner/Manager) Manage consignment futures deals — price, bill, and collect")

    def _fut_ok(self, interaction) -> bool:
        return is_manager(interaction) or bool(_owner_markets_for_user(interaction.user.id))

    @staticmethod
    def _fut_deal_ok(interaction, bulk) -> tuple[bool, str]:
        """Money-affecting deal actions (price / sold / pay) need more than 'owns any market':
        the deal's CUSTOMER is by design a market owner too, so without this check the debtor
        could run `/futures pay` or `/futures sold qty:0` on their own deal and write off their
        entire margin owed with no coins moving. Managers, or the deal's creator (the supplier)
        — and never the customer unless they're a manager."""
        uid = str(interaction.user.id)
        if is_manager(interaction):
            return True, ""
        if uid == str(bulk.get("customer_id") or ""):
            return False, "⛔ You're the customer on this deal — its pricing/billing is managed by the supplier."
        if uid == str(bulk.get("created_by") or ""):
            return True, ""
        return False, "⛔ Only a manager or the deal's creator can do that."

    @futures.command(name="deals", description="List consignment / bulk futures deals")
    @app_commands.describe(customer="Only show this customer's deals (optional)")
    async def futures_deals(self, interaction: discord.Interaction,
                            customer: Optional[discord.Member] = None):
        if not self._fut_ok(interaction):
            return await interaction.response.send_message("⛔ Owners / managers only.", ephemeral=True)
        import Restocker_db as _db
        rows = _db.list_futures_bulk(customer_id=(str(customer.id) if customer else None), limit=25)
        if not rows:
            return await interaction.response.send_message("📭 No bulk futures deals yet.", ephemeral=True)
        lines = []
        for b in rows:
            full = _db.get_futures_bulk(b["id"])
            o = core._futures_bulk_owed(full)
            tag = ("✅" if b["status"] == "fulfilled" else
                   "🕒" if b["status"] == "pending" else "🗑")
            extra = f" · ⚠{o['unpriced']} unpriced" if o["unpriced"] else ""
            lines.append(
                f"{tag} **#{b['id']}** <@{b['customer_id']}> · {len(full['lines'])} lines · "
                f"owed `{o['owed_so_far']:.0f}` paid `{o['paid']:.0f}` → **`{o['remaining']:.0f}`** left{extra}")
        embed = discord.Embed(title="🔮 Consignment futures deals",
                              description="\n".join(lines)[:4000], color=discord.Color.gold())
        embed.set_footer(text="/futures view <id> for line detail · /futures bill <id> to invoice")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @futures.command(name="view", description="Full per-line breakdown of one deal")
    @app_commands.describe(deal_id="Bulk deal # from /futures deals")
    async def futures_view(self, interaction: discord.Interaction, deal_id: int):
        if not self._fut_ok(interaction):
            return await interaction.response.send_message("⛔ Owners / managers only.", ephemeral=True)
        import Restocker_db as _db
        bulk = _db.get_futures_bulk(deal_id)
        if not bulk:
            return await interaction.response.send_message(f"❌ No deal #{deal_id}.", ephemeral=True)
        o = core._futures_bulk_owed(bulk)
        rows = []
        for i, l in enumerate(o["lines"], 1):
            if l["priced"]:
                margin = (l["full_price"] or 0) - (l["worker_cost"] or 0)
                rows.append(f"`{i:>2}.` {l['item']} ×{l['qty']} → `{l['item_key']}` · "
                            f"cost `{l['worker_cost']:.0f}`/full `{l['full_price']:.0f}` "
                            f"(margin `{margin:.0f}`) · resold {l['resold']}/{l['qty']} · owed `{l['owed']:.0f}`")
            else:
                rows.append(f"`{i:>2}.` {l['item']} ×{l['qty']} · ⚠ unpriced — "
                            f"`/futures price {deal_id} {i} <item>`")
        embed = discord.Embed(
            title=f"🔮 Deal #{deal_id} — {str(bulk.get('status','')).capitalize()}",
            description="\n".join(rows)[:4000], color=discord.Color.gold())
        embed.add_field(name="Customer", value=f"<@{bulk.get('customer_id')}>", inline=True)
        if bulk.get("market_id"):
            embed.add_field(name="Market", value=f"`{bulk.get('market_id')}`", inline=True)
        embed.add_field(name="Upfront (break-even)", value=f"`{o['upfront']:.0f}` 🪙", inline=True)
        embed.add_field(name="Margin owed so far", value=f"`{o['owed_so_far']:.0f}` 🪙", inline=True)
        embed.add_field(name="Paid back", value=f"`{o['paid']:.0f}` 🪙", inline=True)
        embed.add_field(name="Remaining", value=f"**`{o['remaining']:.0f}`** 🪙", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @futures.command(name="price", description="Price one line — link it to a catalog item")
    @app_commands.describe(
        deal_id="Bulk deal #", line="Line number from /futures view",
        item="Catalog item to link (its sell price + break-even are used, and CSN resales match it)",
        worker_cost="Override per-unit break-even (optional)",
        full_price="Override per-unit full price (optional)")
    @app_commands.autocomplete(item=any_item_autocomplete)
    async def futures_price(self, interaction: discord.Interaction, deal_id: int, line: int, item: str,
                            worker_cost: Optional[float] = None, full_price: Optional[float] = None):
        if not self._fut_ok(interaction):
            return await interaction.response.send_message("⛔ Owners / managers only.", ephemeral=True)
        import Restocker_db as _db
        bulk = _db.get_futures_bulk(deal_id)
        if not bulk:
            return await interaction.response.send_message(f"❌ No deal #{deal_id}.", ephemeral=True)
        ok, why = self._fut_deal_ok(interaction, bulk)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=True)
        lines = bulk.get("lines") or []
        if line < 1 or line > len(lines):
            return await interaction.response.send_message(
                f"❌ Line must be 1–{len(lines)} (see `/futures view {deal_id}`).", ephemeral=True)
        ln = lines[line - 1]
        res = core._price_futures_bulk_line(ln["id"], item, bulk.get("market_id") or "",
                                            worker_cost, full_price,
                                            getattr(core, "FUTURES_MIN_MARGIN", 0))
        if not res.get("ok"):
            if res.get("reason") == "no_price":
                return await interaction.response.send_message(
                    f"❌ `{item}` has no sell price — set one with `/item_set_price` or pass `full_price:`.",
                    ephemeral=True)
            # low_margin — cheap item, not worth putting on consignment
            return await interaction.response.send_message(
                f"⛔ Margin too small for consignment: **{res['margin']:.0f}** /unit "
                f"(break-even {res['worker_cost']:.0f}, full {res['full_price']:.0f}) — the minimum is "
                f"**{res['min_margin']:.0f}**.\nFutures is for high-margin items (gear, brews), not cheap "
                f"blocks. Raise the sell price, lower the worker cost, or override with `full_price:`. "
                f"(The floor is `FUTURES_MIN_MARGIN`.)", ephemeral=True)
        await interaction.response.send_message(
            f"💲 Priced line **{line}** of deal #{deal_id} → `{item}`\n"
            f"• break-even **{res['worker_cost']:.0f}** · full **{res['full_price']:.0f}** · "
            f"margin **{res['margin']:.0f}** /unit\n"
            f"• CSN baseline set at **{res['baseline']}** sold (only resales after now bill).",
            ephemeral=True)

    @futures.command(name="sold", description="Manually set how many of a line the customer has resold")
    @app_commands.describe(deal_id="Bulk deal #", line="Line number",
                           qty="Resold count (use -1 to clear the override and fall back to CSN)")
    async def futures_sold(self, interaction: discord.Interaction, deal_id: int, line: int, qty: int):
        if not self._fut_ok(interaction):
            return await interaction.response.send_message("⛔ Owners / managers only.", ephemeral=True)
        import Restocker_db as _db
        bulk = _db.get_futures_bulk(deal_id)
        if not bulk:
            return await interaction.response.send_message(f"❌ No deal #{deal_id}.", ephemeral=True)
        ok, why = self._fut_deal_ok(interaction, bulk)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=True)
        lines = bulk.get("lines") or []
        if line < 1 or line > len(lines):
            return await interaction.response.send_message(
                f"❌ Line must be 1–{len(lines)}.", ephemeral=True)
        ln = lines[line - 1]
        if qty < 0:
            _db.set_futures_bulk_line_sold(ln["id"], None)
            return await interaction.response.send_message(
                f"↩️ Cleared manual resold on line {line} — back to CSN auto-tracking.", ephemeral=True)
        _db.set_futures_bulk_line_sold(ln["id"], qty)
        await interaction.response.send_message(
            f"✍️ Line {line}: resold set to **{qty}** (manual override).", ephemeral=True)

    @futures.command(name="bill", description="Post the current invoice for a deal (and DM the customer)")
    @app_commands.describe(deal_id="Bulk deal #", dm="Also DM the customer the invoice (default: yes)")
    async def futures_bill(self, interaction: discord.Interaction, deal_id: int, dm: bool = True):
        if not self._fut_ok(interaction):
            return await interaction.response.send_message("⛔ Owners / managers only.", ephemeral=True)
        import Restocker_db as _db
        bulk = _db.get_futures_bulk(deal_id)
        if not bulk:
            return await interaction.response.send_message(f"❌ No deal #{deal_id}.", ephemeral=True)
        o = core._futures_bulk_owed(bulk)
        rows = []
        for i, l in enumerate(o["lines"], 1):
            if l["priced"] and l["resold"] > 0:
                rows.append(f"• {l['item']} — resold {l['resold']}/{l['qty']} × margin → **{l['owed']:.0f}** 🪙")
        detail = "\n".join(rows) if rows else "_No resales tracked yet._"
        embed = discord.Embed(
            title=f"🧾 Invoice — consignment deal #{deal_id}",
            description=detail, color=discord.Color.gold())
        embed.add_field(name="Owed so far", value=f"`{o['owed_so_far']:.0f}` 🪙", inline=True)
        embed.add_field(name="Paid", value=f"`{o['paid']:.0f}` 🪙", inline=True)
        embed.add_field(name="Remaining", value=f"**`{o['remaining']:.0f}`** 🪙", inline=True)
        embed.set_footer(text=f"Upfront break-even {o['upfront']:.0f} paid at deal · "
                              f"max margin if all resells {o['total_margin']:.0f}"
                              + (f" · ⚠ {o['unpriced']} line(s) unpriced" if o['unpriced'] else ""))
        await interaction.response.send_message(embed=embed, ephemeral=True)
        if dm and o["remaining"] > 0:
            try:
                cust = await interaction.client.fetch_user(int(bulk.get("customer_id")))
                await cust.send(
                    f"🧾 **Invoice — your consignment order #{deal_id}**\n"
                    f"Based on what you've resold so far you owe **{o['remaining']:.0f}** coins "
                    f"(margin {o['owed_so_far']:.0f} − paid {o['paid']:.0f}).\n"
                    f"Pay whenever you can — thanks!")
            except Exception:
                pass

    @futures.command(name="pay", description="Record a payment the customer made against a deal")
    @app_commands.describe(deal_id="Bulk deal #", amount="Coins the customer paid back")
    async def futures_pay(self, interaction: discord.Interaction, deal_id: int, amount: int):
        if not self._fut_ok(interaction):
            return await interaction.response.send_message("⛔ Owners / managers only.", ephemeral=True)
        if amount <= 0:
            return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        import Restocker_db as _db
        bulk = _db.get_futures_bulk(deal_id)
        if not bulk:
            return await interaction.response.send_message(f"❌ No deal #{deal_id}.", ephemeral=True)
        ok, why = self._fut_deal_ok(interaction, bulk)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=True)
        new_paid = _db.record_futures_bulk_payment(deal_id, amount)
        o = core._futures_bulk_owed(_db.get_futures_bulk(deal_id))
        # Platform fee on collected consignment margin — dormant until /fees toggle (returns
        # 0 while fees are off). Ledgers V Tech's cut of the margin actually collected.
        _fee = core._charge_platform_fee(amount, market_id=bulk.get("market_id"),
                                         note=f"futures:deal#{deal_id}")
        await interaction.response.send_message(
            f"💰 Recorded **{amount:,}** 🪙 on deal #{deal_id}. "
            f"Paid total **{new_paid:.0f}** · remaining **{o['remaining']:.0f}**."
            + (f"\n🏦 Platform fee ledgered: **{_fee:,}** 🪙." if _fee else ""), ephemeral=True)

    @app_commands.command(name="my_futures_orders", description="Check the status of your submitted futures orders")
    async def my_futures_orders(self, interaction: discord.Interaction):
        import Restocker_db as _db
        rows = _db.list_futures_orders(user_id=interaction.user.id, limit=20)
        if not rows:
            return await interaction.response.send_message(
                "📭 You haven't submitted any futures orders.", **ephemeral_kwargs(interaction)
            )

        status_emoji = {"pending": "⏳", "approved": "✅", "declined": "❌"}
        lines = []
        for r in rows:
            emoji = status_emoji.get(r["status"], "❔")
            enchant_txt = f" ({r['enchants']})" if r.get("enchants") else ""
            lines.append(f"{emoji} **#{r['id']}** {r['quantity']}x {r['item']}{enchant_txt} — *{r['status']}*")

        await interaction.response.send_message(
            "🔮 **Your futures orders:**\n" + "\n".join(lines), **ephemeral_kwargs(interaction)
        )

    @app_commands.command(name="futures_orders", description="(Managers) List futures orders by status")
    @app_commands.describe(status="Filter by status (default: pending)")
    @app_commands.choices(status=[
        app_commands.Choice(name="Pending", value="pending"),
        app_commands.Choice(name="Approved", value="approved"),
        app_commands.Choice(name="Declined", value="declined"),
        app_commands.Choice(name="All", value="all"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def futures_orders_cmd(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        import Restocker_db as _db
        status_val = status.value if status else "pending"
        rows = _db.list_futures_orders(status=None if status_val == "all" else status_val, limit=25)
        if not rows:
            return await interaction.response.send_message(
                f"📭 No futures orders with status `{status_val}`.", ephemeral=True
            )

        lines = []
        for r in rows:
            enchant_txt = f" ({r['enchants']})" if r.get("enchants") else ""
            lines.append(f"**#{r['id']}** {r['quantity']}x {r['item']}{enchant_txt} — {r['username']} — *{r['status']}*")

        embed = discord.Embed(
            title=f"🔮 Futures Orders ({status_val})", description="\n".join(lines[:25]), color=0xE67E22
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="withdraw_request",
        description="Request a coins withdrawal (opens a manager ticket)."
    )


    @app_commands.describe(
        amount="How many coins you want paid out",
        note="Optional note for managers (payment method, availability, etc.)"
    )
    async def withdraw_request(self, 
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1_000_000_000],
        note: Optional[str] = None
    ):

        data = _load_balances()
        u = _get_user_bal(data["users"], interaction.user.id)
        if u["coins"] < amount:
            return await interaction.response.send_message(
                f"❌ You have **{u['coins']}** coins but requested **{amount}**.",
                **ephemeral_kwargs(interaction)
            )


        base = interaction.client.get_channel(WORKER_CHANNEL_ID)
        if not base or not base.guild:
            return await interaction.response.send_message("⚠️ Bot is not attached to the worker guild.", **ephemeral_kwargs(interaction))

        # Defer before opening the ticket: _open_payout_ticket() creates a Discord
        # channel (a slow API call), which can exceed the 3s interaction window and
        # cause "Unknown interaction" (10062) when we reply afterwards.
        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)

        member = base.guild.get_member(interaction.user.id) or await base.guild.fetch_member(interaction.user.id)

        chan_id = await _open_payout_ticket(interaction, member, int(amount), (note or "").strip() or None)

        if not chan_id:
            return await interaction.followup.send("❌ Could not open a payout ticket. Tell a manager.", **ephemeral_kwargs(interaction))

        link = f"https://discord.com/channels/{base.guild.id}/{chan_id}"
        await interaction.followup.send(
            f"📬 Opened your **coins withdrawal** ticket for **{amount}**.\n"
            f"Managers will review and mark it paid here: {link}",
            **ephemeral_kwargs(interaction)
        )

    @app_commands.command(name="deposit", description="(Managers) Add coins to a user's account.")


    @app_commands.describe(user="User to credit", amount="Coins to add (positive)", note="Optional note")
    @app_commands.default_permissions(manage_guild=True)
    async def deposit_cmd(self, 
        interaction: discord.Interaction,
        user: discord.User,
        amount: app_commands.Range[int, 1, 1_000_000_000],
        note: Optional[str] = None,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))

        coins, principal = add_coins(user.id, int(amount), counts_as_principal=True)

        try:
            msg = f"✅ Deposited **{amount} coins** to {user.mention}. New balance: **{coins}**."
            if note and note.strip():
                msg += f"\n📝 Note: {note.strip()}"
            await user.send(msg)
        except Exception:
            pass

        await interaction.response.send_message(
            f"✅ Deposited **{amount} coins** to {user.mention}. Balance is now **{coins}**.",
            **ephemeral_kwargs(interaction),
        )


    @app_commands.command(name="balance_history", description="Your recent coin movements (or another user's if Manager)")
    @app_commands.describe(user="(Managers) Whose history to view", limit="How many entries (default 15)")
    async def balance_history(self, interaction: discord.Interaction,
                              user: discord.Member = None, limit: int = 15):
        target = interaction.user
        if user is not None and user.id != interaction.user.id:
            if not is_manager(interaction):
                return await interaction.response.send_message("Managers only for others' history.", ephemeral=True)
            target = user
        import Restocker_db as _db
        rows = _db.get_coin_ledger(str(target.id), max(1, min(int(limit), 50)))
        if not rows:
            return await interaction.response.send_message(
                f"No recorded coin movements for {target.mention} yet.", ephemeral=True)
        lines = []
        for r in rows:
            d = int(r["delta"])
            sign = "+" if d >= 0 else ""
            when = (r.get("created_at") or "")[:16].replace("T", " ")
            why = (r.get("reason") or "").strip()
            lines.append(f"`{when}` **{sign}{d:,}** → {int(r['balance_after']):,}" + (f"  · {why}" if why else ""))
        embed = discord.Embed(title=f"🧾 Coin history — {target.display_name}",
                              description="\n".join(lines), color=0x22FF7A)
        embed.set_footer(text="Most recent first")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(MoneyCog(bot))
