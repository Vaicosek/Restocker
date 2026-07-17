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
load_orders = core.load_orders
save_orders = core.save_orders
update_order_messages = core.update_order_messages
utcnow_iso = core.utcnow_iso
_load_items = core._load_items
_create_restock_orders = core._create_restock_orders
_is_future_item = core._is_future_item
log = core.log
PRIORITY_HOURS = core.PRIORITY_HOURS

def _web_items_text(order: dict) -> str:
    """'• Diamond Sword × 2' lines from a web order's items_json, for customer DMs."""
    try:
        import json as _json
        raw = order.get("items_json") or "[]"
        items = _json.loads(raw) if isinstance(raw, str) else (raw or [])
        lines = [f"• {i.get('name', '?')} × {i.get('qty', 1)}" for i in items if isinstance(i, dict)]
        return "\n".join(lines) or "—"
    except Exception:
        return "—"


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
            order = _db.get_web_order(self.order_id)      # needed for the customer's DM
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

        # Tell the customer. They ordered on the website and would otherwise never hear
        # anything back — we store discord_id precisely so we can reach them.
        dm_ok = False
        try:
            if order and order.get("discord_id"):
                customer = await bot.fetch_user(int(order["discord_id"]))
                await customer.send(
                    f"❌ Your web order **#{self.order_id}** was declined.\n"
                    f"{_web_items_text(order)}\n\n"
                    f"Ask a manager if you think that's a mistake.")
                dm_ok = True
        except Exception:
            pass

        await interaction.response.send_message(
            f"❌ Order #{self.order_id} declined."
            + ("" if dm_ok else " ⚠️ Couldn't DM the customer (DMs closed?)."),
            ephemeral=True,
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

        # Block self-approval — same guard futures already has. A manager who orders through
        # the website must not be able to wave their own order through; another manager reviews
        # it. Decline is unaffected (you may always cancel your own order).
        if str(order.get("discord_id") or "") == str(interaction.user.id):
            await interaction.response.send_message(
                "⚠️ You can't approve your **own** web order — another manager has to review it.",
                ephemeral=True)
            return

        # Idempotency: approving twice would create a second set of restock orders. The buttons
        # are stripped on approve, but a stale message or two managers racing could still fire
        # this path, so re-check the live status before doing anything.
        if str(order.get("status") or "").lower() == "approved":
            await interaction.response.send_message(
                f"ℹ️ Order #{self.order_id} is already approved — not creating duplicate orders.",
                ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            import Restocker_db as _db
            _db.update_web_order_status(
                self.order_id,
                status="approved",
                reviewed_by=str(interaction.user.id),
                notify_msg_id=str(interaction.message.id) if interaction.message else None,
            )
        except Exception as e:
            await interaction.followup.send(f"⚠️ DB error: {e}", ephemeral=True)
            return

        # Turn the approved web order into REAL restock orders workers can claim, instead of
        # just pinging @Employee to eyeball an embed. Everything downstream (claiming, payouts,
        # team credit) then works exactly as it does for any other order.
        created, skipped = 0, []
        try:
            import json as _json
            raw = order.get("items_json") or "[]"
            wanted = _json.loads(raw) if isinstance(raw, str) else (raw or [])
            catalog = (_load_items() or {}).get("items", {}) or {}
            to_order = []
            for it in wanted:
                if not isinstance(it, dict):
                    continue
                name = str(it.get("name") or "").strip()
                try:
                    qty = int(it.get("qty") or 0)
                except (TypeError, ValueError):
                    qty = 0
                if not name or qty <= 0:
                    continue
                info = catalog.get(name)
                if info is None:                       # renamed/removed since they ordered
                    skipped.append(f"{name} (not in catalog)")
                    continue
                if _is_future_item(name):              # _create_restock_orders drops these silently
                    skipped.append(f"{name} (Future item — needs /futures_order)")
                    continue
                to_order.append((name, qty, info))
            if to_order:
                created = int(_create_restock_orders(to_order) or 0)
        except Exception as e:
            log.warning("[web-order] couldn't create restock orders for #%s: %s", self.order_id, e)
            skipped.append(f"error: {e}")

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

        # Confirm to the customer — without this they order on the site and hear nothing.
        dm_ok = False
        try:
            if order.get("discord_id"):
                customer = await bot.fetch_user(int(order["discord_id"]))
                await customer.send(
                    f"✅ Your web order **#{self.order_id}** was approved!\n"
                    f"{_web_items_text(order)}\n\n"
                    f"Our workers are on it — you'll be contacted when it's ready.")
                dm_ok = True
        except Exception:
            pass

        msg = f"✅ Order #{self.order_id} approved."
        if created:
            msg += f"\n🧰 Created **{created}** restock order(s) — workers can claim them now."
        elif not skipped:
            msg += "\n⚠️ No restock orders created (nothing orderable in this request)."
        if worker_mention:
            msg += f" {worker_mention} pinged."
        msg += "\n" + ("👤 Customer notified." if dm_ok
                       else "⚠️ Couldn't DM the customer (DMs closed?).")
        if skipped:
            msg += ("\n\n🚨 **Not turned into orders:**\n• " + "\n• ".join(skipped[:8])
                    + "\nThese need handling by hand.")
        await interaction.followup.send(msg[:1900], ephemeral=True)


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
        from datetime import datetime, timezone as _tz, timedelta

        try:
            order = _db.get_futures_order(self.order_id)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ DB error: {e}", ephemeral=True)
            return

        if not order:
            await interaction.response.send_message("⚠️ Order not found.", ephemeral=True)
            return

        # Block self-approval: an internal review has to be done by a DIFFERENT manager,
        # even if the customer happens to hold the manager role (that's how #10 got
        # self-approved). Decline is unaffected.
        if str(order.get("user_id") or "") == str(interaction.user.id):
            await interaction.response.send_message(
                "⚠️ You can't approve your **own** futures order — another manager has to review it.",
                ephemeral=True)
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

        item       = order.get("item", "?")
        qty        = int(order.get("quantity") or 0)
        username   = order.get("username", "?")
        enchants   = order.get("enchants") or ""
        notes      = order.get("notes") or ""

        # Turn the approved futures order into a REAL claimable work order (orders table
        # + OrderView), posted to the worker channel through the normal flow — so workers
        # can claim / partial-claim / fulfill → verify → get paid exactly like any order,
        # instead of a static embed they can't interact with. THIS is the worker UI that
        # ships only after a manager approves.
        posted_ok = False
        _new_order_id = None
        try:
            info = (_load_items().get("items") or {}).get(item) or {}
            _sv = info.get("stackable")
            if _sv is None:                       # infer: tools/armor/sets don't stack
                _nl = str(item).lower()
                _nonstack = ("pickaxe", "axe", "shovel", "sword", "hoe", "helmet", "chestplate",
                             "leggings", "boots", "set", "bow", "trident", "shield", "elytra",
                             "fishing rod")
                stackable = not any(k in _nl for k in _nonstack)
            else:
                stackable = bool(_sv)
            try:
                stack_size = int(info.get("stack_size") or (64 if stackable else 1))
            except Exception:
                stack_size = 64 if stackable else 1

            _now = datetime.now(_tz.utc)
            data_orders = load_orders()
            _new_order_id = max([o.get("id", 0) for o in (data_orders.get("orders") or [])], default=0) + 1
            work_order = {
                "id": _new_order_id, "shop": "", "item": item,
                "requested": qty, "produced": 0,
                "status": "open", "claimed_by": None, "claims": [],
                "created_at": utcnow_iso(),
                "messages": {"channel_id": None, "message_id": None, "dms": {}},
                "unit_type": "pieces", "amount": qty,
                "stackable": bool(stackable), "stack_size": stack_size, "barrel_slots": 54,
                # Card is posted directly below, and we handle the @Employee ping
                # ourselves, so mark both announce sides done to keep the background
                # loop from re-posting or double-notifying.
                "worker_announced": True,
                "employee_announced": True,
                "employee_announce_at": None,
                "priority_until": (_now + timedelta(hours=PRIORITY_HOURS)).isoformat(),
                "priority_role": EMPLOYEE_ROLE_NAME,
                # traceability back to the futures request
                "source": "futures", "futures_id": int(self.order_id),
                "customer_id": str(order.get("user_id") or ""),
            }
            data_orders.setdefault("orders", []).append(work_order)
            save_orders(data_orders)
            await update_order_messages(bot, work_order, allow_post=True)
            posted_ok = True
        except Exception as e:
            print(f"⚠️ Could not create claimable work order from futures #{self.order_id}: {e}")

        # Post a short context line under the card (customer + required enchants/notes,
        # which the standard order card doesn't show) and @Employee ping only on
        # "Approve & Ping".
        if posted_ok:
            _target = bot.get_channel(WORKER_CHANNEL_ID) if WORKER_CHANNEL_ID else None
            _target = _target or interaction.channel
            if _target is not None:
                bits = [f"🔮 Order **#{_new_order_id}** is a **futures** job for **{username}** — {qty}x {item}."]
                if enchants:
                    bits.append(f"**Required:** {enchants}")
                if notes:
                    bits.append(f"**Notes:** {notes}")
                prefix = ""
                allowed = discord.AllowedMentions.none()
                if ping_workers and interaction.guild:
                    _role = discord.utils.get(interaction.guild.roles, name=EMPLOYEE_ROLE_NAME)
                    if _role:
                        prefix = _role.mention + " "
                        allowed = discord.AllowedMentions(roles=[_role])
                try:
                    await _target.send(prefix + "  ·  ".join(bits), allowed_mentions=allowed)
                except Exception:
                    pass

        # DM the customer a proper embed
        try:
            if order.get("user_id"):
                customer = await bot.fetch_user(int(order["user_id"]))
                dm_embed = discord.Embed(
                    title="✅ Futures Order Approved",
                    description="Your order has been reviewed and sent to the workers!",
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
            (f"✅ Futures #{self.order_id} approved → posted as claimable order **#{_new_order_id}** "
             f"in the worker channel"
             if posted_ok else
             f"⚠️ Futures #{self.order_id} marked approved, but I couldn't post the worker order — "
             f"check WORKER_CHANNEL_ID")
            + (f" · {EMPLOYEE_ROLE_NAME} pinged." if (ping_workers and posted_ok) else "."),
            ephemeral=True,
        )


def _futures_bulk_preview_embed(bulk: dict) -> discord.Embed:
    """The review card for a bulk futures order: customer, market, and every parsed line."""
    status = str(bulk.get("status") or "pending")
    color = {"fulfilled": discord.Color.green(), "declined": discord.Color.red(),
             "cancelled": discord.Color.dark_grey()}.get(status, discord.Color.gold())
    lines = bulk.get("lines") or []
    total_units = sum(int(l.get("qty") or 0) for l in lines)
    body = []
    for i, l in enumerate(lines, 1):
        unit = l.get("unit") or "pieces"
        ench = f" · _{l.get('enchants')}_" if l.get("enchants") else ""
        done = " ✅" if l.get("work_order_id") else ""
        body.append(f"`{i:>2}.` **{int(l.get('qty') or 0)}** {unit} — {l.get('item','?')}{ench}{done}")
    desc = "\n".join(body) if body else "_(no lines parsed — cancel and re-paste)_"
    embed = discord.Embed(title=f"🔮 Bulk Futures #{bulk.get('id')} — {status.capitalize()}",
                          description=desc, color=color)
    embed.add_field(name="Customer", value=f"<@{bulk.get('customer_id')}>", inline=True)
    if bulk.get("market_id"):
        embed.add_field(name="Their market", value=f"`{bulk.get('market_id')}`", inline=True)
    embed.add_field(name="Lines / units", value=f"{len(lines)} · {total_units:,}", inline=True)
    if status == "pending":
        embed.set_footer(text="Review the parsed lines, then Approve & Fulfill to create the work orders.")
    return embed


class FuturesBulkModal(discord.ui.Modal, title="Bulk futures — paste item list"):
    """One paragraph field: paste the customer's order, one item per line. On submit we parse
    it, save the bulk order, and post the review card with Approve & Fulfill."""

    def __init__(self, customer_id: int, customer_name: str, market_id: str, created_by: int):
        super().__init__(timeout=600)
        self._customer_id = int(customer_id)
        self._customer_name = customer_name or ""
        self._market_id = market_id or ""
        self._created_by = int(created_by)
        self.items = discord.ui.TextInput(
            label="Items (one per line)",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=3500,
            placeholder="2 barrels Warlord Potion (Str 2 + Speed 2)\nRegen Potion 2 barrels\nSword Sharp V Fire Aspect II x10",
        )
        self.notes = discord.ui.TextInput(
            label="Notes (optional)", style=discord.TextStyle.paragraph,
            required=False, max_length=500, placeholder="Consignment: pays worker cost now, rest on resale.")
        self.add_item(self.items)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        import Restocker_db as _db
        parsed = core._parse_futures_bulk_text(str(self.items.value or ""))
        if not parsed:
            return await interaction.response.send_message(
                "❌ Couldn't read any items from that. One item per line, e.g. `2 barrels Warlord Potion`.",
                ephemeral=True)
        try:
            bulk_id = _db.create_futures_bulk(
                self._customer_id, self._customer_name, self._market_id,
                self._created_by, str(self.notes.value or ""))
            for p in parsed:
                _db.add_futures_bulk_line(bulk_id, p["item"], p["qty"], p.get("unit", "pieces"),
                                          enchants="", raw_line=p.get("raw", ""))
        except Exception as e:
            return await interaction.response.send_message(f"⚠️ DB error: {e}", ephemeral=True)
        bulk = _db.get_futures_bulk(bulk_id)
        await interaction.response.send_message(
            embed=_futures_bulk_preview_embed(bulk), view=FuturesBulkView(bulk_id))
        # Record the message id so the persistent view can recover the bulk after a restart.
        try:
            msg = await interaction.original_response()
            _db.update_futures_bulk_status(bulk_id, "pending", notify_msg_id=str(msg.id))
        except Exception:
            pass


class FuturesBulkView(discord.ui.View):
    """Approve & Fulfill / Cancel for a bulk futures order. Persistent: recovers the bulk id
    from the message it lives on, so the buttons survive a restart."""

    def __init__(self, bulk_id: int = 0):
        super().__init__(timeout=None)
        self.bulk_id = int(bulk_id or 0)

    def _resolve(self, interaction):
        if self.bulk_id:
            return self.bulk_id
        try:
            import Restocker_db as _db
            b = _db.get_futures_bulk_by_msg(getattr(interaction, "message", None) and interaction.message.id)
            return int(b["id"]) if b else 0
        except Exception:
            return 0

    @discord.ui.button(label="✅ Approve & Fulfill", style=discord.ButtonStyle.success,
                       custom_id="futures_bulk_fulfill")
    async def fulfill(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        bulk_id = self._resolve(interaction)
        bulk = _db.get_futures_bulk(bulk_id) if bulk_id else None
        if not bulk:
            return await interaction.response.send_message("⚠️ Bulk order not found.", ephemeral=True)
        if str(bulk.get("status")) == "fulfilled":
            return await interaction.response.send_message("⚠️ Already fulfilled.", ephemeral=True)
        if str(bulk.get("status")) in ("declined", "cancelled"):
            return await interaction.response.send_message(f"⚠️ This order is {bulk.get('status')}.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            created = core._create_futures_bulk_work_orders(bulk_id)
        except Exception as e:
            return await interaction.followup.send(f"⚠️ Couldn't create work orders: {e}", ephemeral=True)
        _db.update_futures_bulk_status(bulk_id, "fulfilled", reviewed_by=str(interaction.user.id))
        try:
            await interaction.message.edit(embed=_futures_bulk_preview_embed(_db.get_futures_bulk(bulk_id)),
                                           view=None)
        except Exception:
            pass
        try:
            cid = str(bulk.get("customer_id") or "")
            if cid:
                cust = await interaction.client.fetch_user(int(cid))
                await cust.send(f"✅ Your bulk futures order **#{bulk_id}** was approved — "
                                f"**{len(created)}** item(s) are now queued for our workers to craft.")
        except Exception:
            pass
        await interaction.followup.send(
            f"✅ Fulfilled bulk futures **#{bulk_id}** — created **{len(created)}** claimable work "
            f"order(s). They post to the worker channel on the next announce slot.", ephemeral=True)

    @discord.ui.button(label="🗑 Cancel", style=discord.ButtonStyle.danger,
                       custom_id="futures_bulk_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        bulk_id = self._resolve(interaction)
        bulk = _db.get_futures_bulk(bulk_id) if bulk_id else None
        if not bulk:
            return await interaction.response.send_message("⚠️ Bulk order not found.", ephemeral=True)
        if str(bulk.get("status")) == "fulfilled":
            return await interaction.response.send_message(
                "⚠️ Already fulfilled — the work orders exist. Cancel those individually if needed.",
                ephemeral=True)
        _db.update_futures_bulk_status(bulk_id, "cancelled", reviewed_by=str(interaction.user.id))
        try:
            await interaction.message.edit(embed=_futures_bulk_preview_embed(_db.get_futures_bulk(bulk_id)),
                                           view=None)
        except Exception:
            pass
        await interaction.response.send_message(f"🗑 Cancelled bulk futures #{bulk_id}.", ephemeral=True)


async def post_futures_bulk_review(bulk_id: int):
    """Post a bulk futures request's review card (embed + Approve & Fulfill / Cancel) to the
    futures channel so a manager can action it. Used when the order comes from the WEBSITE
    (no Discord interaction to reply to). Records the message id so the persistent view can
    recover the deal after a restart. Runs on the bot loop."""
    import Restocker_db as _db
    bulk = _db.get_futures_bulk(int(bulk_id))
    if not bulk:
        return
    ch = None
    for cid in (getattr(core, "FUTURES_CHANNEL_ID", 0), getattr(core, "WEB_ORDERS_CHANNEL_ID", 0),
                getattr(core, "FUNDS_REPORT_CHANNEL_ID", 0)):
        if cid:
            ch = bot.get_channel(cid)
            if ch:
                break
    if ch is None:
        try:
            log.warning("[futures web] no channel configured to post bulk #%s", bulk_id)
        except Exception:
            pass
        return
    mgr = discord.utils.get(ch.guild.roles, name=getattr(core, "MANAGER_ROLE_NAME", "")) if ch.guild else None
    ping = mgr.mention if mgr else ""
    content = (f"{ping} — new **web** futures request from <@{bulk.get('customer_id')}> "
               f"for `{bulk.get('market_id') or '—'}`") if ping else "New web futures request"
    try:
        msg = await ch.send(content=content, embed=_futures_bulk_preview_embed(bulk),
                            view=FuturesBulkView(int(bulk_id)))
        _db.update_futures_bulk_status(int(bulk_id), "pending", notify_msg_id=str(msg.id))
    except Exception as e:
        try:
            log.warning("[futures web] post failed for #%s: %s", bulk_id, e)
        except Exception:
            pass


class InvestorSyncModal(discord.ui.Modal, title="Sync investors — paste GEX.PR cap table"):
    """Paste the Crimson Banking cap-table export for GEX.PR (the CSV block:
    account_id,discord_id,name,shares). Rebuilds the investor register: shares aggregate by
    Discord id (one person can hold via several entities) and share_pct is derived so it
    always sums to 100. Same format the future Crimson API will return."""

    def __init__(self):
        super().__init__(timeout=600)
        self.blob = discord.ui.TextInput(
            label="Cap-table export (CSV lines)",
            style=discord.TextStyle.paragraph, required=True, max_length=3900,
            placeholder="10001,429708337039278101,Crimson Vault,221,\n10869,180182849381466113,Maestro Inc.,111,")
        self.add_item(self.blob)

    async def on_submit(self, interaction: discord.Interaction):
        import Restocker_db as _db
        rows = core._parse_crimson_captable(str(self.blob.value or ""))
        if not rows:
            return await interaction.response.send_message(
                "❌ Couldn't parse any holder lines — paste the raw CSV block from the "
                "Crimson cap-table report (`account,discord_id,name,shares,`).", ephemeral=True)
        # Liquidated holders (left for good — /investor liquidate) are dropped, but the
        # pcts stay derived from the FULL total: the company keeps their payout slice
        # instead of the remaining investors absorbing it.
        total = sum(sh for _, _, sh in rows)
        liq = core._liquidated_holders()
        dropped = [(uid, nm, sh) for uid, nm, sh in rows if str(uid) in liq]
        kept = [(uid, nm, sh) for uid, nm, sh in rows if str(uid) not in liq]
        n = _db.replace_investors(kept, total_shares=total if dropped else None)
        lines = [f"• <@{uid}> **{nm}** — {sh:,.0f} pref · {100.0*sh/total:.1f}%"
                 for uid, nm, sh in sorted(kept, key=lambda r: -r[2])]
        msg = (f"✅ Investor register synced — **{n}** investor(s), {total:,.0f} preferred shares "
               f"(entities aggregated by Discord account):\n" + "\n".join(lines)[:1500])
        if dropped:
            liq_sh = sum(sh for _, _, sh in dropped)
            msg += (f"\n🧹 Liquidated (company keeps their {100.0*liq_sh/total:.1f}% slice): "
                    + ", ".join(f"{nm} ({sh:,.0f})" for _, nm, sh in dropped))
        await interaction.response.send_message(msg[:1900], ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())


class PayoutReviewView(discord.ui.View):
    """Withdrawal Approve/Reject. RESTART-PERSISTENT: registered with bot.add_view and
    stable custom_ids, so buttons on old tickets keep working after redeploys. A fresh
    process recovers the requester/amount from the ticket message itself ('Requester:
    <@id>', 'Amount: **N coins**'); the ticket channel is where the click happens."""

    _inflight: set = set()          # message ids being processed — blocks double-click double-pay

    def __init__(self, user_id: int = 0, amount: int = 0, channel_id: int = 0):
        super().__init__(timeout=None)
        self.user_id = int(user_id or 0)
        self.amount = int(amount or 0)
        self.channel_id = int(channel_id or 0)

    def _resolve(self, interaction: discord.Interaction):
        """(user_id, amount, channel) — live instance fields, else parsed from the message."""
        uid, amt = self.user_id, self.amount
        if not uid or not amt:
            import re as _re
            txt = (interaction.message.content if interaction.message else "") or ""
            m_u = _re.search(r"Requester:\s*<@!?(\d+)>", txt)
            m_a = _re.search(r"Amount:\s*\*\*([\d,]+)\s*coins", txt)
            if m_u:
                uid = int(m_u.group(1))
            if m_a:
                amt = int(m_a.group(1).replace(",", ""))
        chan = None
        if self.channel_id:
            chan = interaction.client.get_channel(self.channel_id)
        if chan is None:
            chan = interaction.channel      # the review message lives inside the ticket channel
        return uid, amt, chan

    @discord.ui.button(label="✅ Approve & mark paid", style=discord.ButtonStyle.green,
                       custom_id="payout_review:approve")
    async def approve(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        mid = int(interaction.message.id) if interaction.message else 0
        if mid in type(self)._inflight:
            return await interaction.response.send_message(
                "⏳ Already being processed by another manager.", ephemeral=True)
        type(self)._inflight.add(mid)
        try:
            # Ack within the 3s window FIRST — balance load + DM + channel ops can exceed it.
            await interaction.response.defer(ephemeral=True, thinking=True)
            uid, amount, chan = self._resolve(interaction)
            if not uid or amount <= 0:
                return await interaction.followup.send(
                    "❌ Couldn't read the requester/amount from this ticket — pay manually and "
                    "close the channel.", ephemeral=True)
            data = _load_balances()
            u = _get_user_bal(data["users"], uid)
            if u["coins"] < amount:
                return await interaction.followup.send(
                    f"❌ User has only **{u['coins']}** coins; requested **{amount}**.", ephemeral=True)

            coins, principal = deduct_coins(uid, amount, reduce_principal=True)

            try:
                user = await interaction.client.fetch_user(uid)
                await user.send(
                    f"💳 Your withdrawal of **{amount} coins** has been **paid**. "
                    f"New coin balance: **{coins}**.")
            except Exception:
                pass

            await interaction.followup.send(
                f"✅ Marked **{amount} coins** as paid and deducted from balance.", ephemeral=True)

            try:
                if chan:
                    await chan.send("✅ Payment marked complete. Closing this ticket…")
                    await chan.delete(reason="Payout approved")
            except Exception:
                pass
        finally:
            type(self)._inflight.discard(mid)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger,
                       custom_id="payout_review:reject")
    async def reject(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid, _amount, chan = self._resolve(interaction)

        try:
            if uid:
                user = await interaction.client.fetch_user(uid)
                await user.send("❌ Your coins withdrawal request was **rejected** by managers.")
        except Exception:
            pass

        await interaction.followup.send("❌ Rejected. Ticket will be closed.", ephemeral=True)

        try:
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

