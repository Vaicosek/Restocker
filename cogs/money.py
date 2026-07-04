"""Money / futures / investor commands (extracted from Restocker_main)."""
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
WORKER_CHANNEL_ID = core.WORKER_CHANNEL_ID
_get_user_bal = core._get_user_bal
_load_balances = core._load_balances
_open_payout_ticket = core._open_payout_ticket
_owner_markets_for_user = core._owner_markets_for_user
add_coins = core.add_coins
any_item_autocomplete = core.any_item_autocomplete
bot = core.bot
ephemeral_kwargs = core.ephemeral_kwargs
is_manager = core.is_manager
timezone = core.timezone

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
    @app_commands.autocomplete(item=any_item_autocomplete)
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

        channel = None
        if WEB_ORDERS_CHANNEL_ID:
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

        member = base.guild.get_member(interaction.user.id) or await base.guild.fetch_member(interaction.user.id)

        chan_id = await _open_payout_ticket(interaction, member, int(amount), (note or "").strip() or None)

        if not chan_id:
            return await interaction.response.send_message("❌ Could not open a payout ticket. Tell a manager.", **ephemeral_kwargs(interaction))

        link = f"https://discord.com/channels/{base.guild.id}/{chan_id}"
        await interaction.response.send_message(
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
