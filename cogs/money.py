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




class MoneyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


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
                    # Give it a billing line so consignment tracks it exactly like a bulk.
                    core._ensure_futures_billing_line(
                        order_id, str(interaction.user.id),
                        getattr(interaction.user, "display_name", str(interaction.user)),
                        item, int(quantity), enchants or "",
                        market_id="", created_by=interaction.user.id)
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


    # ── Investors (/investor ...) — GEX.PR preferred shareholders, profit-share engine ──




async def setup(bot):
    await bot.add_cog(MoneyCog(bot))
