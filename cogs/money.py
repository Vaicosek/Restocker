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
OWNER_ROLE_NAME = core.OWNER_ROLE_NAME
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
            embed.set_footer(text="Awaiting owner review")

            owner_role = discord.utils.get(channel.guild.roles, name=OWNER_ROLE_NAME) if channel.guild else None
            ping = owner_role.mention if owner_role else ""

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

