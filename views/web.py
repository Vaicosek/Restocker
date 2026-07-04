"""Web-order / futures / payout UI (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands, Embed
from discord.ui import View, Button, Select

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
EMPLOYEE_ROLE_NAME = core.EMPLOYEE_ROLE_NAME
WORKER_CHANNEL_ID  = core.WORKER_CHANNEL_ID
_disable_view_children = core._disable_view_children
_get_user_bal = core._get_user_bal
_load_balances = core._load_balances
bot = core.bot
deduct_coins = core.deduct_coins
deduct_investor_coins = core.deduct_investor_coins
is_manager = core.is_manager
_log_team_event = core._log_team_event
_team_live = core._team_live

class WebOrderView(discord.ui.View):
    """Persistent view posted to the orders channel when a web order comes in."""

    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = int(order_id)

    @discord.ui.button(label="✅ Approve & Ping Workers", style=discord.ButtonStyle.success,
                       custom_id="web_order_approve_ping")
    async def approve_ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            await interaction.response.send_message("❌ Manager access required.", ephemeral=True)
            return
        await self._do_approve(interaction, ping_workers=True)

    @discord.ui.button(label="Approve (no ping)", style=discord.ButtonStyle.primary,
                       custom_id="web_order_approve_quiet")
    async def approve_quiet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            await interaction.response.send_message("❌ Manager access required.", ephemeral=True)
            return
        await self._do_approve(interaction, ping_workers=False)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger,
                       custom_id="web_order_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            await interaction.response.send_message("❌ Manager access required.", ephemeral=True)
            return

        try:
            import Restocker_db as _db
            _db.update_web_order_status(
                self.order_id,
                status="declined",
                reviewed_by=str(interaction.user.id),
                notify_msg_id=str(interaction.message.id) if interaction.message else None,
            )
        except Exception as e:
            await interaction.response.send_message(f"⚠️ DB error: {e}", ephemeral=True)
            return

        try:
            embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
            if embed:
                embed.color = discord.Color.red()
                embed.set_footer(text=f"Declined by {interaction.user.display_name}")
                await interaction.message.edit(embed=embed, view=None)
        except Exception:
            pass

        await interaction.response.send_message(
            f"❌ Order #{self.order_id} declined.", ephemeral=True
        )

    async def _do_approve(self, interaction: discord.Interaction, *, ping_workers: bool):
        try:
            import Restocker_db as _db
            order = _db.get_web_order(self.order_id)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ DB error: {e}", ephemeral=True)
            return

        if not order:
            await interaction.response.send_message("⚠️ Order not found.", ephemeral=True)
            return

        try:
            import Restocker_db as _db
            _db.update_web_order_status(
                self.order_id,
                status="approved",
                reviewed_by=str(interaction.user.id),
                notify_msg_id=str(interaction.message.id) if interaction.message else None,
            )
        except Exception as e:
            await interaction.response.send_message(f"⚠️ DB error: {e}", ephemeral=True)
            return

        try:
            embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
            if embed:
                embed.color = discord.Color.green()
                embed.set_footer(text=f"Approved by {interaction.user.display_name}")
                await interaction.message.edit(embed=embed, view=None)
        except Exception:
            pass

        worker_mention = ""
        if ping_workers and interaction.guild:
            worker_role = discord.utils.get(interaction.guild.roles, name=EMPLOYEE_ROLE_NAME)
            if worker_role:
                worker_mention = worker_role.mention
                try:
                    await interaction.channel.send(
                        f"📦 {worker_mention} — new web order approved! "
                        f"**Order #{self.order_id}** from **{order.get('discord_username', '?')}**. "
                        f"Please check and fulfil."
                    )
                except Exception:
                    pass

        await interaction.response.send_message(
            f"✅ Order #{self.order_id} approved."
            + (f" {worker_mention} pinged." if worker_mention else ""),
            ephemeral=True,
        )


class FuturesOrderView(discord.ui.View):
    """Persistent view posted when a customer submits a /futures_order request."""

    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = int(order_id)

    @discord.ui.button(label="✅ Approve & Ping Workers", style=discord.ButtonStyle.success,
                       custom_id="futures_order_approve_ping")
    async def approve_ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            await interaction.response.send_message("❌ Manager access required.", ephemeral=True)
            return
        await self._do_approve(interaction, ping_workers=True)

    @discord.ui.button(label="Approve (no ping)", style=discord.ButtonStyle.primary,
                       custom_id="futures_order_approve_quiet")
    async def approve_quiet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            await interaction.response.send_message("❌ Manager access required.", ephemeral=True)
            return
        await self._do_approve(interaction, ping_workers=False)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger,
                       custom_id="futures_order_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            await interaction.response.send_message("❌ Manager access required.", ephemeral=True)
            return

        try:
            import Restocker_db as _db
            order = _db.get_futures_order(self.order_id)
            _db.update_futures_order_status(
                self.order_id,
                status="declined",
                reviewed_by=str(interaction.user.id),
                notify_msg_id=str(interaction.message.id) if interaction.message else None,
            )
        except Exception as e:
            await interaction.response.send_message(f"⚠️ DB error: {e}", ephemeral=True)
            return

        try:
            embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
            if embed:
                embed.color = discord.Color.red()
                embed.set_footer(text=f"Declined by {interaction.user.display_name}")
                await interaction.message.edit(embed=embed, view=None)
        except Exception:
            pass

        try:
            if order and order.get("user_id"):
                customer = await bot.fetch_user(int(order["user_id"]))
                await customer.send(
                    f"❌ Your futures order for **{order.get('quantity')}x {order.get('item')}** was declined."
                )
        except Exception:
            pass

        await interaction.response.send_message(
            f"❌ Futures order #{self.order_id} declined.", ephemeral=True
        )

    async def _do_approve(self, interaction: discord.Interaction, *, ping_workers: bool):
        import Restocker_db as _db
        from datetime import datetime, timezone as _tz

        try:
            order = _db.get_futures_order(self.order_id)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ DB error: {e}", ephemeral=True)
            return

        if not order:
            await interaction.response.send_message("⚠️ Order not found.", ephemeral=True)
            return

        try:
            _db.update_futures_order_status(
                self.order_id,
                status="approved",
                reviewed_by=str(interaction.user.id),
                notify_msg_id=str(interaction.message.id) if interaction.message else None,
            )
        except Exception as e:
            await interaction.response.send_message(f"⚠️ DB error: {e}", ephemeral=True)
            return
        try:
            _wid = str(order.get("user_id") or "")
            if _wid:
                _log_team_event(_wid, "futures", qty=int(order.get("quantity") or 0), detail=f"futures#{self.order_id}")
                await _team_live(_wid, f"🔮 <@{_wid}> futures order #{self.order_id} approved "
                                       f"({order.get('quantity')}x {order.get('item')}).")
        except Exception:
            pass

        # Edit the original manager-review embed to show approved
        try:
            orig_embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
            if orig_embed:
                orig_embed.color = discord.Color.green()
                orig_embed.set_footer(text=f"Approved by {interaction.user.display_name} • {datetime.now(_tz.utc).strftime('%d %b %Y %H:%M UTC')}")
                await interaction.message.edit(embed=orig_embed, view=None)
        except Exception:
            pass

        # Build the order-card embed to post to the worker channel
        item       = order.get("item", "?")
        qty        = order.get("quantity", "?")
        username   = order.get("username", "?")
        enchants   = order.get("enchants") or ""
        notes      = order.get("notes") or ""

        card_embed = discord.Embed(
            title=f"🔮 Futures Order #{self.order_id}",
            color=discord.Color.gold(),
            timestamp=datetime.now(_tz.utc),
        )
        card_embed.add_field(name="Customer", value=f"<@{order['user_id']}>" if order.get("user_id") else username, inline=True)
        card_embed.add_field(name="Item", value=f"{qty}x {item}", inline=True)
        if enchants:
            card_embed.add_field(name="Enchants / Quality", value=enchants, inline=False)
        if notes:
            card_embed.add_field(name="Notes", value=notes, inline=False)
        card_embed.set_footer(text=f"Approved by {interaction.user.display_name}")

        # Post order card to the worker channel
        worker_channel = bot.get_channel(WORKER_CHANNEL_ID) if WORKER_CHANNEL_ID else None
        worker_mention = ""
        if interaction.guild:
            worker_role = discord.utils.get(interaction.guild.roles, name=EMPLOYEE_ROLE_NAME)
            if worker_role and ping_workers:
                worker_mention = worker_role.mention

        if worker_channel:
            try:
                ping_content = (
                    f"{worker_mention} — futures order approved! **#{self.order_id}: {qty}x {item}** for **{username}**. Coordinate fulfilment here."
                    if ping_workers and worker_mention
                    else f"📦 Futures order approved! **#{self.order_id}: {qty}x {item}** for **{username}**."
                )
                await worker_channel.send(
                    content=ping_content,
                    embed=card_embed,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            except Exception as e:
                print(f"⚠️ Could not post futures order card to worker channel: {e}")
        else:
            # Fallback: post in the current channel if worker channel not configured
            try:
                ping_content = (
                    f"{worker_mention} — futures order approved! **#{self.order_id}: {qty}x {item}** for **{username}**."
                    if ping_workers and worker_mention
                    else f"📦 Futures order **#{self.order_id}** approved."
                )
                await interaction.channel.send(
                    content=ping_content,
                    embed=card_embed,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            except Exception:
                pass

        # DM the customer a proper embed
        try:
            if order.get("user_id"):
                customer = await bot.fetch_user(int(order["user_id"]))
                dm_embed = discord.Embed(
                    title="✅ Futures Order Approved",
                    description=f"Your order has been reviewed and sent to the workers!",
                    color=discord.Color.green(),
                    timestamp=datetime.now(_tz.utc),
                )
                dm_embed.add_field(name="Item", value=f"{qty}x {item}", inline=True)
                dm_embed.add_field(name="Order #", value=str(self.order_id), inline=True)
                if enchants:
                    dm_embed.add_field(name="Enchants / Quality", value=enchants, inline=False)
                if notes:
                    dm_embed.add_field(name="Notes", value=notes, inline=False)
                dm_embed.set_footer(text=f"Reviewed by {interaction.user.display_name}")
                await customer.send(embed=dm_embed)
        except Exception:
            pass

        await interaction.response.send_message(
            f"✅ Futures order #{self.order_id} approved — card posted to worker channel"
            + (f", {worker_mention} pinged." if worker_mention else "."),
            ephemeral=True,
        )


class PayoutReviewView(discord.ui.View):
    def __init__(self, user_id: int, amount: int, channel_id: int):
        super().__init__(timeout=None)
        self.user_id = int(user_id)
        self.amount = int(amount)
        self.channel_id = int(channel_id)

    async def _load(self, interaction: discord.Interaction):
        data = _load_balances()
        u = _get_user_bal(data["users"], self.user_id)
        chan = interaction.client.get_channel(self.channel_id)
        return data, u, chan

    @discord.ui.button(label="✅ Approve & mark paid", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)


        data, u, chan = await self._load(interaction)
        if u["coins"] < self.amount:
            return await interaction.response.send_message(
                f"❌ User has only **{u['coins']}** coins; requested **{self.amount}**.", ephemeral=True
            )


        coins, principal = deduct_coins(self.user_id, self.amount, reduce_principal=True)
        u["coins"] = coins
        u["principal"] = principal


        try:
            user = await interaction.client.fetch_user(self.user_id)
            await user.send(
                f"💳 Your withdrawal of **{self.amount} coins** has been **paid**. "
                f"New coin balance: **{coins}**."
            )
        except Exception:
            pass

        await interaction.response.send_message(
            f"✅ Marked **{self.amount} coins** as paid and deducted from balance.", ephemeral=True
        )


        try:
            if chan:
                await chan.send("✅ Payment marked complete. Closing this ticket…")
                await chan.delete(reason="Payout approved")
        except Exception:
            pass

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)


        try:
            user = await interaction.client.fetch_user(self.user_id)
            await user.send("❌ Your coins withdrawal request was **rejected** by managers.")
        except Exception:
            pass

        await interaction.response.send_message("❌ Rejected. Ticket will be closed.", ephemeral=True)

        try:
            chan = interaction.client.get_channel(self.channel_id)
            if chan:
                await chan.send("❌ Rejected by managers. Closing…")
                await chan.delete(reason="Payout rejected")
        except Exception:
            pass


class InvestorWithdrawApprovalView(discord.ui.View):
    def __init__(self, investor_id: int, amount: int, channel: discord.TextChannel):
        super().__init__(timeout=None)
        self.investor_id = investor_id
        self.amount = amount
        self.channel = channel

    @discord.ui.button(label="✅ Approve & Deduct Balance", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        new_bal, _ = deduct_investor_coins(self.investor_id, self.amount)
        _disable_view_children(self)
        embed = discord.Embed(
            title="✅ Withdrawal Approved",
            description=(
                f"**{self.amount:,}** 🪙 deducted from <@{self.investor_id}>'s dividend balance.\n"
                f"New balance: `{new_bal:,}` 🪙\n\n"
                f"*Please log in-game and pay the investor.*"
            ),
            color=0x2ECC71,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        try:
            user = interaction.client.get_user(self.investor_id) or await interaction.client.fetch_user(self.investor_id)
            dm = user.dm_channel or await user.create_dm()
            await dm.send(embed=discord.Embed(
                title="💸 Investor Withdrawal Approved",
                description=f"Your withdrawal of `{self.amount:,}` 🪙 has been approved and will be paid in-game shortly.",
                color=0x2ECC71,
            ))
        except Exception:
            pass

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        _disable_view_children(self)
        embed = discord.Embed(
            title="❌ Withdrawal Rejected",
            description=f"Request for `{self.amount:,}` 🪙 was rejected. Balance unchanged.",
            color=0xE74C3C,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        try:
            user = interaction.client.get_user(self.investor_id) or await interaction.client.fetch_user(self.investor_id)
            dm = user.dm_channel or await user.create_dm()
            await dm.send(embed=discord.Embed(
                title="❌ Investor Withdrawal Rejected",
                description=f"Your withdrawal request for `{self.amount:,}` 🪙 was rejected by a manager.",
                color=0xE74C3C,
            ))
        except Exception:
            pass

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.secondary)
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.send_message("Closing ticket…", ephemeral=True)
        try:
            await self.channel.delete()
        except Exception:
            pass

