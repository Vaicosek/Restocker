"""Order / worker / manager UI (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands, Embed
from discord.ui import View, Button, Select

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
EMPLOYEE_ROLE_NAME = core.EMPLOYEE_ROLE_NAME
MANAGER_ROLE_NAME = core.MANAGER_ROLE_NAME
TICKETS_CATEGORY_ID = core.TICKETS_CATEGORY_ID
WORKER_CHANNEL_ID = core.WORKER_CHANNEL_ID
_apply_claim = core._apply_claim
_award_loyalty_points = core._award_loyalty_points
_channel_link = core._channel_link
_clear_all_hive_pickups = core._clear_all_hive_pickups
_close_ui_in_place = core._close_ui_in_place
_coins_for_pieces = core._coins_for_pieces
_commit_ticket_slot = core._commit_ticket_slot
_disable_view_children = core._disable_view_children
_ensure_order_dm_panel = core._ensure_order_dm_panel
_finish_claim = core._finish_claim
_get_latest_batch = core._get_latest_batch
_is_blocked_claimer = core._is_blocked_claimer
_load_items = core._load_items
_loyalty_payout_bonus_pct = core._loyalty_payout_bonus_pct
_loyalty_points_for_order = core._loyalty_points_for_order
_market_loyalty_cfg = core._market_loyalty_cfg
_mutate_order = core._mutate_order
_open_assist_ticket = core._open_assist_ticket
_order_is_claimed_closed = core._order_is_claimed_closed
_priority_guard = core._priority_guard
_release_ticket_slot = core._release_ticket_slot
_release_verify_reservation = core._release_verify_reservation
_reserve_ticket_slot = core._reserve_ticket_slot
_save_items = core._save_items
_send_funds_report = core._send_funds_report
_total_funds_coins = core._total_funds_coins
add_coins = core.add_coins
_pay_manager_override = core._pay_manager_override
_pay_manager_points_override = core._pay_manager_points_override
_log_team_event = core._log_team_event
_team_live = core._team_live
apply_weekly_interest = core.apply_weekly_interest
build_order_embed = core.build_order_embed
build_orders_pages = core.build_orders_pages
cleanup_batch_dms_for_closed_order = core.cleanup_batch_dms_for_closed_order
dm_claimants = core.dm_claimants
ephemeral_kwargs = core.ephemeral_kwargs
fmt_qty = core.fmt_qty
is_manager = core.is_manager
load_orders = core.load_orders
remaining_to_assign = core.remaining_to_assign
save_orders = core.save_orders
update_order_messages = core.update_order_messages
utcnow_iso = core.utcnow_iso


def _order_id_from_message(interaction) -> int | None:
    """Recover the order id from the message a button was clicked on.

    Persistent views are registered as OrderView(0), so after a bot restart the
    per-message order id stored on the view instance is lost (every old card would
    resolve to id 0 -> "Order not found"). We stored each card's message id in
    order["messages"] (channel card = message_id, DM card = dms[user_id]), so we
    look the order up by the clicked message's id. This reads fresh state on every
    call and never mutates shared view state, so it's race-safe across concurrent
    clicks on the shared persistent instance."""
    msg = getattr(interaction, "message", None)
    mid = getattr(msg, "id", None)
    if not mid:
        return None
    try:
        mid = int(mid)
    except Exception:
        return None
    try:
        data = load_orders()
    except Exception:
        return None
    for o in data.get("orders", []) or []:
        if not isinstance(o, dict):
            continue
        msgs = o.get("messages") or {}
        try:
            if msgs.get("message_id") is not None and int(msgs["message_id"]) == mid:
                return int(o["id"])
        except Exception:
            pass
        for v in (msgs.get("dms") or {}).values():
            try:
                if v is not None and int(v) == mid:
                    return int(o["id"])
            except Exception:
                pass
    return None


def _claim_of(order: dict, user_id) -> dict | None:
    """Return the caller's claim on this order, or None. Compares by INT id because
    claims persisted to SQLite come back with a string user_id, and a raw
    ("123" == 123) would silently fail. Use this everywhere claim ownership matters."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    for c in (order.get("claims") or []):
        try:
            if int(c.get("user_id")) == uid:
                return c
        except (TypeError, ValueError):
            continue
    return None


class CloseTicketView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger)
    async def close_ticket(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.send_message("✅ Ticket closed. Deleting channel…", ephemeral=True)
        try:
            await interaction.channel.delete(reason="Ticket closed by manager.")
        except Exception as e:
            try:
                await interaction.response.send_message(f"⚠️ Could not delete channel: {e}", ephemeral=True)
            except Exception:
                pass


class WorkerView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="👷 Join Workers",
        style=discord.ButtonStyle.green,
        custom_id="vw_join_workers")
    async def join(self, interaction: discord.Interaction, button: Button):
        if not interaction.guild:

            channel = interaction.client.get_channel(WORKER_CHANNEL_ID)
            guild = channel.guild if channel else None
        else:
            guild = interaction.guild

        if not guild:
            return await interaction.response.send_message(
                "⚠️ I can’t find the server right now. Try again later.", ephemeral=True
            )

        role = discord.utils.get(guild.roles, name=EMPLOYEE_ROLE_NAME)
        if not role:
            role = await guild.create_role(name=EMPLOYEE_ROLE_NAME)

        member = guild.get_member(interaction.user.id)
        if not member:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except Exception:
                return await interaction.response.send_message(
                    "❌ You’re not a member of this server.", ephemeral=True
                )

        if role in member.roles:
            return await interaction.response.send_message("⚠️ You are already a Worker.", ephemeral=True)

        await member.add_roles(role)
        await interaction.response.send_message("✅ You are now a Worker!", ephemeral=True)
        try:
            await interaction.user.send("👋 Welcome aboard! You’ll now get Worker notifications.")
        except discord.Forbidden:
            pass


class RemindByIdModal(discord.ui.Modal, title="Send Reminder"):
    def __init__(self):
        super().__init__(timeout=None)

    order_id = discord.ui.TextInput(
        label="Order ID",
        placeholder="e.g., 6",
        required=True,
        max_length=10,
    )

    note = discord.ui.TextInput(
        label="Manager note (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
        placeholder="Any extra instructions…",
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))


        try:
            oid = int(str(self.order_id.value).strip())
        except Exception:
            return await interaction.response.send_message("❌ Order ID must be a number.", **ephemeral_kwargs(interaction))

        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)
        if not order or order["status"] in ["fulfilled", "cancelled"]:
            return await interaction.response.send_message("⚠️ That order is closed or doesn’t exist.", **ephemeral_kwargs(interaction))

        note = (self.note.value or "").strip() or None


        sent, targeted = await dm_claimants(
            interaction.client, order, min_age_minutes=None, note=note
        )
        await interaction.response.send_message(
            f"🔔 Sent **{sent}/{targeted}** reminder DMs for order #{order['id']}.",
            **ephemeral_kwargs(interaction),
        )


class PartialFulfillModal(discord.ui.Modal, title="Add Produced Amount"):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    amount = discord.ui.TextInput(
        label="How many did you produce now?",
        placeholder="e.g., 18",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            add_n = int(str(self.amount.value).strip())
            if add_n <= 0:
                raise ValueError()
        except Exception:
            return await interaction.response.send_message("❌ Enter a positive integer.", **ephemeral_kwargs(interaction))

        data0 = load_orders()
        order0 = next((o for o in data0.get("orders", []) if o.get("id") == self.order_id), None)
        if not order0:
            return await interaction.response.send_message("❌ Order not found.", **ephemeral_kwargs(interaction))

        items_data = _load_items()
        items = items_data.setdefault("items", {})
        info = items.get(order0.get("item", ""))
        if not isinstance(info, dict):
            return await interaction.response.send_message("❌ Item not found in items.yml.", **ephemeral_kwargs(interaction))
        info["stock"] = int(info.get("stock", 0) or 0) + int(add_n)
        _save_items(items_data)

        def _add_produced(order):
            order.setdefault("requested", order.get("amount", 0))
            order.setdefault("produced", 0)
            if order.get("claimed_by") is None:
                order["claimed_by"] = str(interaction.user)
                if str(order.get("status", "")).lower() not in ("fulfilled", "cancelled"):
                    order["status"] = "open"
            order["produced"] = min(int(order.get("requested", 0) or 0),
                                    int(order.get("produced", 0) or 0) + int(add_n))
            return max(0, int(order.get("requested", 0) or 0) - int(order.get("produced", 0) or 0))

        order, remaining = await _mutate_order(self.order_id, _add_produced)
        if order is None:
            return await interaction.response.send_message("❌ Order not found.", **ephemeral_kwargs(interaction))
        try:
            await update_order_messages(interaction.client, order)
        except Exception:
            pass
        await interaction.response.send_message(
            f"➕ Added {add_n} produced. Remaining for this request: {remaining}.",
            **ephemeral_kwargs(interaction)
        )


class ClaimPartModal(discord.ui.Modal, title="Claim part of this order"):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    qty = discord.ui.TextInput(
        label="How many will you take?",
        placeholder="e.g., 12",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(**ephemeral_kwargs(interaction))
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == self.order_id), None)
        if order:
            guard = await _priority_guard(interaction, order)
            if guard:
                return await interaction.followup.send(guard, **ephemeral_kwargs(interaction))
        try:
            qty = int(str(self.qty.value).strip())
            if qty <= 0:
                raise ValueError()
        except Exception:
            return await interaction.followup.send("❌ Enter a positive integer.", **ephemeral_kwargs(interaction))
        res = await _apply_claim(interaction, self.order_id, qty)
        return await _finish_claim(interaction, self.order_id, res)


class ReleaseClaimModal(discord.ui.Modal, title="Release claimed amount"):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    qty = discord.ui.TextInput(
        label="How many to release?",
        placeholder="e.g., 10",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(**ephemeral_kwargs(interaction))
        try:
            n = int(str(self.qty.value).strip())
            if n <= 0:
                raise ValueError()
        except Exception:
            return await interaction.followup.send("❌ Enter a positive integer.", **ephemeral_kwargs(interaction))

        out = {}

        def _release(order):
            if str(order.get("status")) in ("fulfilled", "cancelled"):
                out["err"] = "⚠️ This order is closed or missing."
                return False
            claims = order.get("claims", [])
            me = _claim_of(order, interaction.user.id)
            if not me:
                out["err"] = "⚠️ You don't have a claim on this order."
                return False
            if n >= int(me.get("qty", 0) or 0):
                released = int(me.get("qty", 0) or 0)
                claims.remove(me)
                out["msg"] = f"↩️ Released all {released} from order #{order['id']}."
            else:
                me["qty"] = int(me.get("qty", 0) or 0) - n
                out["msg"] = f"↩️ Released {n} from order #{order['id']}. You still hold {me['qty']}."
            if not claims and int(order.get("produced", 0) or 0) < int(order.get("requested", 0) or 0):
                order["status"] = "open"
                order["claimed_by"] = None
            return True

        order, ok = await _mutate_order(self.order_id, _release)
        if order is None:
            return await interaction.followup.send("⚠️ This order is closed or missing.", **ephemeral_kwargs(interaction))
        if ok is False:
            return await interaction.followup.send(out.get("err", "⚠️ Couldn't release."), **ephemeral_kwargs(interaction))
        await update_order_messages(interaction.client, order)
        await interaction.followup.send(out.get("msg", "↩️ Released."), **ephemeral_kwargs(interaction))


class RemindModal(discord.ui.Modal, title="Send Reminder"):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    note = discord.ui.TextInput(
        label="Manager note (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
        placeholder="Any extra instructions…",
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))

        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == self.order_id), None)
        if not order or order["status"] in ["fulfilled", "cancelled"]:
            return await interaction.response.send_message("⚠️ This order is closed or missing.", **ephemeral_kwargs(interaction))

        note = (self.note.value or "").strip() or None


        sent, targeted = await dm_claimants(
            interaction.client, order, min_age_minutes=None, note=note
        )
        await interaction.response.send_message(
            f"🔔 Sent **{sent}/{targeted}** reminder DMs for order #{order['id']}.",
            **ephemeral_kwargs(interaction),
        )


class ManagerReviewView(View):
    def __init__(self, order_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.channel_id = channel_id

    def _resolve(self, interaction):
        """Recover (order_id, channel_id) for THIS click. The persistent view is
        registered as ManagerReviewView(0, 0), so after a bot restart the ids stored
        on the instance are lost. The Approve/Reject message always lives in the
        order's verification channel, so we take the channel from the interaction and
        find the order whose verification_ticket_id points at it. Reads fresh state,
        mutates nothing on the shared instance -> race-safe under concurrent clicks."""
        ch_id = getattr(interaction, "channel_id", None) or self.channel_id
        oid = self.order_id
        try:
            valid = oid and any(
                isinstance(o, dict) and int(o.get("id", 0) or 0) == int(oid)
                for o in load_orders().get("orders", []) or []
            )
        except Exception:
            valid = bool(oid)
        if not valid and ch_id:
            try:
                for o in load_orders().get("orders", []) or []:
                    if not isinstance(o, dict):
                        continue
                    vt = o.get("verification_ticket_id")
                    if vt and int(vt) == int(ch_id):
                        oid = int(o["id"])
                        break
            except Exception:
                pass
        return oid, ch_id

    async def _load(self, interaction: discord.Interaction):
        oid, ch_id = self._resolve(interaction)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)
        chan = interaction.client.get_channel(ch_id)
        return data, order, chan

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.green, custom_id="mrv_approve")
    async def approve(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception:
            pass

        oid, ch_id = self._resolve(interaction)
        chan = interaction.client.get_channel(ch_id)

        def _claim_approval(order):
            if str(order.get("status", "")).lower() == "fulfilled":
                return False
            requested = int(order.get("requested", 0) or 0)
            old_produced = int(order.get("produced", 0) or 0)
            order["produced"] = requested
            order["status"] = "fulfilled"
            order["verification_ticket_id"] = None
            return {"remaining_to_stock": max(0, requested - old_produced)}

        order, res = await _mutate_order(oid, _claim_approval)
        if order is None:
            try:
                return await interaction.followup.send("❌ Order missing.", ephemeral=True)
            except Exception:
                return
        if res is False:
            try:
                return await interaction.followup.send("⚠️ This order was already approved.", ephemeral=True)
            except Exception:
                return

        try:
            items_data = _load_items()
        except Exception:
            items_data = {"items": {}}

        claims = list(order.get("claims") or [])
        paid_lines = []
        unpaid_lines = []
        total_paid = 0
        for c in claims:
            try:
                uid = int(c.get("user_id", 0) or 0)
                qty = int(c.get("qty", 0) or 0)
            except Exception:
                continue
            if uid <= 0 or qty <= 0:
                continue
            payout = _coins_for_pieces(order, qty, items_data)
            if payout <= 0:
                # NEVER silently skip. A 0 payout means the item has no catalog price (or its
                # name drifted from the catalog key), and the worker would be paid nothing with
                # no trace while the order still closed as fulfilled. Surface it loudly so a
                # manager can fix the price and run /admin repair_payouts.
                unpaid_lines.append(
                    f"• <@{uid}> — **NOT PAID** for {qty} pcs: no catalog price for "
                    f"`{order.get('item','?')}`")
                log.warning("[pay] order #%s: no price for %r — worker %s NOT paid for %s pcs",
                            order.get("id"), order.get("item"), uid, qty)
                continue
            # Per-market reward: the owning market can grant a points multiplier and/or a
            # flat coin bonus per fulfilled order to incentivise its restockers.
            _mkt_mult, _mkt_bonus = _market_loyalty_cfg(order.get("market_id"))
            bonus_pct = _loyalty_payout_bonus_pct(uid)
            bonus_coins = int(payout * bonus_pct / 100) if bonus_pct > 0 else 0
            total_payout = payout + bonus_coins + _mkt_bonus
            # Tag the ledger with the order so payouts are auditable after the fact — without
            # this, coin_ledger rows are anonymous and "was order #N paid?" is unanswerable.
            new_bal, _principal = add_coins(uid, total_payout, counts_as_principal=True,
                                            reason=f"order#{order['id']}")
            total_paid += total_payout
            bonus_str = f" (+{bonus_coins} loyalty bonus)" if bonus_coins > 0 else ""
            if _mkt_bonus > 0:
                bonus_str += f" (+{_mkt_bonus} market bonus)"
            paid_lines.append(f"• <@{uid}> +**{total_payout}**{bonus_str} (new bal: {new_bal})")
            lp = _loyalty_points_for_order(order, items_data)
            lp_scaled = max(1, int(lp * qty / max(1, int(order.get("requested", 1) or 1))))
            if _mkt_mult != 1.0:
                lp_scaled = max(1, int(lp_scaled * _mkt_mult))
            new_pts, old_tier, new_tier = _award_loyalty_points(uid, lp_scaled, reason=f"order#{order['id']}")
            try:
                _mgr_id, _ov = _pay_manager_override(uid, total_payout, f"order#{order['id']}")
            except Exception:
                _mgr_id, _ov = None, 0
            try:
                _mgr_pid, _opts = _pay_manager_points_override(uid, lp_scaled, f"order#{order['id']}")
            except Exception:
                _mgr_pid, _opts = None, 0
            if (_ov > 0 or _opts > 0) and (_mgr_id or _mgr_pid):
                _mid = _mgr_id or _mgr_pid
                _bits = []
                if _ov > 0:
                    _bits.append(f"+**{_ov}** coins")
                if _opts > 0:
                    _bits.append(f"+**{_opts}** pts")
                _ovstr = " & ".join(_bits)
                paid_lines.append(f"  \u21B3 manager <@{_mid}> {_ovstr} override")
                try:
                    _mgr_obj = await interaction.client.fetch_user(int(_mid))
                    await _mgr_obj.send(
                        f"\U0001F4BC Team override: {_ovstr} from <@{uid}>'s "
                        f"Order #{order['id']} fulfillment.")
                except Exception:
                    pass
            try:
                _log_team_event(uid, "order", coins=total_payout, qty=qty, detail=f"order#{order['id']}")
                if _ov > 0 or _opts > 0:
                    _log_team_event(uid, "override", coins=_ov, points=_opts, detail=f"order#{order['id']}")
                _live = f"\u2705 <@{uid}> fulfilled Order #{order['id']} (+{total_payout}c)"
                if _ov > 0 or _opts > 0:
                    _live += f" \u2192 you +{_ov}c/+{_opts}pts"
                await _team_live(uid, _live)
            except Exception:
                pass
            tier_up_msg = (f"\n🏆 Tier up! You're now **{new_tier['name']}**!"
                           if new_tier["tier"] > old_tier["tier"] else "")
            try:
                user_obj = await interaction.client.fetch_user(uid)
                await user_obj.send(
                    f"✅ Your claim on **Order #{order['id']} — {order.get('item', '')}** was approved.\n"
                    f"💰 Paid: **{total_payout} coins**{bonus_str} (for {fmt_qty(order, qty)}).\n"
                    f"New balance: **{new_bal}**.\n"
                    f"⭐ Loyalty: **+{lp_scaled} pts** → {new_pts:.0f} total ({new_tier['name']}){tier_up_msg}"
                )
            except Exception:
                pass

        remaining_to_stock = int((res or {}).get("remaining_to_stock", 0) or 0)
        if remaining_to_stock > 0:
            try:
                items_data2 = _load_items()
                items2 = items_data2.setdefault("items", {})
                info = items2.get(order.get("item", ""))
                if isinstance(info, dict):
                    info["stock"] = int(info.get("stock", 0) or 0) + int(remaining_to_stock)
                    _save_items(items_data2)
            except Exception:
                pass

        try:
            await update_order_messages(interaction.client, order)
        except Exception:
            pass

        msg = "✅ Approved & marked fulfilled."
        if paid_lines:
            msg += f"\n\n💸 Paid total: **{total_paid} coins**\n" + "\n".join(paid_lines[:15])
            if len(paid_lines) > 15:
                msg += f"\n… and {len(paid_lines) - 15} more"
        elif not unpaid_lines:
            msg += "\n\n⚠️ No valid claims found to pay."
        if unpaid_lines:
            # Loud, actionable — this is the failure mode that quietly cost Dr 6 orders.
            msg += ("\n\n🚨 **Some workers were NOT paid** — the item has no catalog price, so the "
                    "payout computed to 0:\n" + "\n".join(unpaid_lines[:10]) +
                    f"\n\nFix the price with `/set_price item:{order.get('item','?')} coin:<amount>` "
                    f"then run `/admin repair_payouts` to pay them retroactively.")
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

        try:
            if chan:
                await chan.send("✅ Approved by Managers. Channel will be deleted.")
                await chan.delete(reason="Order approved.")
        except Exception:
            pass
    @discord.ui.button(label="❌ Reject (needs fix)", style=discord.ButtonStyle.danger, custom_id="mrv_reject")
    async def reject(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        oid, ch_id = self._resolve(interaction)
        chan = interaction.client.get_channel(ch_id)
        captured = {}

        def _reject(order):
            captured["ids"] = [c.get("user_id") for c in (order.get("claims") or []) if c.get("user_id")]
            captured["item"] = order.get("item", "")
            order["status"] = "open"
            order["verification_ticket_id"] = None
            order["claims"] = []
            order["claimed_by"] = None
            return True

        order, ok = await _mutate_order(oid, _reject)
        if order is None:
            return await interaction.response.send_message("Order missing.", ephemeral=True)
        await update_order_messages(interaction.client, order)
        await interaction.response.send_message("Rejected, cleared all claims & reopened the order.", ephemeral=True)

        for uid in captured.get("ids", []):
            try:
                user = await interaction.client.fetch_user(int(uid))
                await user.send(
                    f"❌ Your claim on order #{order['id']} — {captured.get('item','')} was removed after manager review. "
                    f"The order has been reopened publicly."
                )
            except Exception:
                pass

        try:
            if chan:
                await chan.send("❌ Rejected by Managers. Channel will be deleted.")
                await chan.delete(reason="Order rejected.")
        except Exception:
            pass


class EscalateModal(discord.ui.Modal, title="Escalate Order (repost)"):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id_int = int(order_id)

        self.note = discord.ui.TextInput(
            label="Reason / note (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
            placeholder="Why are you escalating this order?",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))

        oid = int(self.order_id_int)

        data = load_orders()
        order = next((o for o in data.get("orders", []) if int(o.get("id", 0) or 0) == oid), None)
        if not order:
            return await interaction.response.send_message("❌ Order not found.", **ephemeral_kwargs(interaction))

        if str(order.get("status", "")).lower() in ("fulfilled", "cancelled"):
            return await interaction.response.send_message("⚠️ This order is closed.", **ephemeral_kwargs(interaction))


        claimers = sorted({int(c.get("user_id")) for c in (order.get("claims") or []) if c.get("user_id")})
        claimers = [u for u in claimers if u > 0]


        bl = order.get("blocked_claimers") or []
        if not isinstance(bl, list):
            bl = []
        for uid in claimers:
            s = str(int(uid))
            if s not in bl:
                bl.append(s)
        order["blocked_claimers"] = bl


        order["claims"] = []
        order["claimed_by"] = None
        if str(order.get("status", "")).lower() not in ("fulfilled", "cancelled"):
            order["status"] = "open"


        order.setdefault("messages", {})
        msgs = order["messages"]
        if not isinstance(msgs, dict):
            msgs = {}
            order["messages"] = msgs


        try:
            ch_id = msgs.get("channel_id")
            m_id = msgs.get("message_id")
            if ch_id and m_id:
                ch = interaction.client.get_channel(int(ch_id))
                if ch:
                    try:
                        old = await ch.fetch_message(int(m_id))
                        await old.delete()
                    except Exception:
                        pass
        except Exception:
            pass


        msgs["message_id"] = None
        msgs["worker_ping_message_id"] = None
        msgs["channel_id"] = None


        dms = msgs.get("dms") or {}
        if isinstance(dms, dict) and dms:
            for uid in claimers:
                key = str(int(uid))
                mid = dms.get(key)
                if not mid:
                    continue
                try:
                    user = interaction.client.get_user(uid) or await interaction.client.fetch_user(uid)
                    if user:
                        dm = user.dm_channel or await user.create_dm()
                        try:
                            dm_msg = await dm.fetch_message(int(mid))
                            await dm_msg.delete()
                        except Exception:
                            pass
                except Exception:
                    pass
                dms.pop(key, None)
            msgs["dms"] = dms

        note = (self.note.value or "").strip()
        if note:
            order["escalate_note"] = note
        order["escalated_at"] = utcnow_iso()
        order["escalated_by"] = str(interaction.user)

        ok = save_orders(data)
        if not ok:
            return await interaction.response.send_message("❌ Failed to save orders.yml.", **ephemeral_kwargs(interaction))


        try:
            await update_order_messages(interaction.client, order, allow_post=True)
        except Exception:
            pass

        mentions = ", ".join(f"<@{u}>" for u in claimers) if claimers else "—"
        return await interaction.response.send_message(
            f"🚨 Escalated order **#{oid}**. Reposted for workers to claim again.\n"
            f"Blocked previous claimers: {mentions}",
            **ephemeral_kwargs(interaction)
        )


class EscalatePickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

        data = load_orders()


        candidates = [
            o for o in (data.get("orders", []) or [])
            if str(o.get("status", "")).lower() not in ("fulfilled", "cancelled")
        ]
        candidates.sort(key=lambda o: int(o.get("id", 0) or 0), reverse=True)
        candidates = candidates[:25]

        options = []
        if not candidates:
            options = [discord.SelectOption(label="No eligible orders", value="none", default=True)]
        else:
            for o in candidates:
                oid = int(o.get("id", 0) or 0)
                item = str(o.get("item", "") or "")
                st = str(o.get("status", "") or "").upper()
                rem = remaining_to_assign(o)

                options.append(
                    discord.SelectOption(
                        label=f"#{oid} {item}"[:100],
                        description=f"{st} · rem {fmt_qty(o, rem)}"[:100],
                        value=str(oid),
                    )
                )

        self._selected_id: int | None = None

        self.select = discord.ui.Select(
            placeholder="Pick an order to escalate…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="mp_escalate_pick",
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        v = (self.select.values or ["none"])[0]
        self._selected_id = None if v == "none" else int(v)
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="🚨 Escalate selected…", style=discord.ButtonStyle.danger, custom_id="mp_escalate_go")
    async def escalate_go(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        if not self._selected_id:
            return await interaction.response.send_message("Pick an order first.", ephemeral=True)

        return await interaction.response.send_modal(EscalateModal(int(self._selected_id)))


class OrderView(View):
    def __init__(self, order_id: int):
        super().__init__(timeout=None)
        self.order_id = order_id

    def _oid(self, interaction) -> int:
        """Resolve the order id for THIS click. Prefer recovery from the clicked
        message (correct even after a restart, when the shared persistent instance
        has order_id=0); fall back to the instance's own id for fresh in-memory
        views. Never mutates shared state, so it's safe under concurrent clicks."""
        return _order_id_from_message(interaction) or self.order_id

    @discord.ui.button(label="✅ Claim all", style=discord.ButtonStyle.green, custom_id="order_claim_all")
    async def claim_all(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(**ephemeral_kwargs(interaction))
        oid = self._oid(interaction)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)
        if order:
            guard = await _priority_guard(interaction, order)
            if guard:
                return await interaction.followup.send(guard, **ephemeral_kwargs(interaction))
        res = await _apply_claim(interaction, oid, "all")
        return await _finish_claim(interaction, oid, res)
    @discord.ui.button(label="🧩 Claim part…", style=discord.ButtonStyle.secondary, custom_id="order_claim_part")
    async def claim_part(self, interaction: discord.Interaction, button: Button):
        oid = self._oid(interaction)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)

        if not order:
            dummy = discord.Embed(title="⚠️ Order not found", description="This order no longer exists.")
            return await _close_ui_in_place(
                interaction,
                embed=dummy,
                view=_disable_view_children(OrderView(oid)),
                note=None
            )

        if _order_is_claimed_closed(order):
            try:
                items_data = _load_items()
            except Exception:
                items_data = {"items": {}}
            embed = build_order_embed(order, items_data)
            view = _disable_view_children(OrderView(oid))
            return await _close_ui_in_place(interaction, embed=embed, view=view, note=None)

        guard = await _priority_guard(interaction, order)
        if guard:
            return await interaction.response.send_message(guard, **ephemeral_kwargs(interaction))

        if _is_blocked_claimer(order, interaction.user.id):
            return await interaction.response.send_message(
                "❌ You cannot claim this order anymore (it was escalated away from you).",
                **ephemeral_kwargs(interaction)
            )

        return await interaction.response.send_modal(ClaimPartModal(oid))

    @discord.ui.button(label="↩️ Release claim", style=discord.ButtonStyle.secondary, custom_id="order_release_claim")
    async def release_claim(self, interaction: discord.Interaction, button: Button):
        oid = self._oid(interaction)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)
        if not order:
            dummy = discord.Embed(title="⚠️ Order not found", description="This order no longer exists.")
            return await _close_ui_in_place(
                interaction, embed=dummy,
                view=_disable_view_children(OrderView(oid)), note=None
            )
        # only someone who actually holds a claim can release it
        me = _claim_of(order, interaction.user.id)
        if not me:
            return await interaction.response.send_message(
                "⚠️ You don't have a claim on this order to release.",
                **ephemeral_kwargs(interaction)
            )
        return await interaction.response.send_modal(ReleaseClaimModal(oid))

    @discord.ui.button(
        label="🧪 Request recipe/materials",
        style=discord.ButtonStyle.primary,
        custom_id="ov_request_recipe"
    )
    async def request_recipe_materials(self, interaction: discord.Interaction, button: Button):
        return await self._request_recipe_impl(interaction)

    async def _request_recipe_impl(self, interaction: discord.Interaction):
        oid = self._oid(interaction)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)
        if not order:
            dummy = discord.Embed(title="⚠️ Order not found", description="This order no longer exists.")
            view = _disable_view_children(OrderView(oid))
            return await _close_ui_in_place(interaction, embed=dummy, view=view, note=None)
        if _order_is_claimed_closed(order):
            try:
                items_data = _load_items()
            except Exception:
                items_data = {"items": {}}
            embed = build_order_embed(order, items_data)
            view = _disable_view_children(OrderView(oid))
            return await _close_ui_in_place(interaction, embed=embed, view=view, note=None)
        base = interaction.client.get_channel(WORKER_CHANNEL_ID)
        if not base or not base.guild:
            return await interaction.response.send_message(
                "⚠️ Bot is not attached to the worker guild.", **ephemeral_kwargs(interaction))
        guild = base.guild
        try:
            member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
        except Exception:
            return await interaction.response.send_message(
                "⚠️ I can't find you in the guild.", **ephemeral_kwargs(interaction))

        state, existing_id = await _reserve_ticket_slot(oid, "assist_ticket_ids", interaction.user.id)
        if state == "gone":
            return await interaction.response.send_message("❌ Order not found.", **ephemeral_kwargs(interaction))
        if state == "pending":
            return await interaction.response.send_message(
                "⏳ Your ticket is already being created — give it a moment.", **ephemeral_kwargs(interaction))
        if state == "exists":
            chan = guild.get_channel(int(existing_id)) if existing_id else None
            if chan is not None:
                return await interaction.response.send_message(
                    f"🧵 Your assist ticket is already open: {chan.mention}", **ephemeral_kwargs(interaction))
            await _release_ticket_slot(oid, "assist_ticket_ids", interaction.user.id)
            state, existing_id = await _reserve_ticket_slot(oid, "assist_ticket_ids", interaction.user.id)
            if state != "reserved":
                return await interaction.response.send_message(
                    "🧵 Your assist ticket is already being set up.", **ephemeral_kwargs(interaction))

        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)
        chan_id = await _open_assist_ticket(interaction, order, member)
        if not chan_id:
            await _release_ticket_slot(oid, "assist_ticket_ids", interaction.user.id)
            return await interaction.followup.send(
                "❌ Could not open an assist ticket. Tell a manager.", **ephemeral_kwargs(interaction))
        await _commit_ticket_slot(oid, "assist_ticket_ids", interaction.user.id, chan_id)
        link = f"https://discord.com/channels/{guild.id}/{chan_id}"
        return await interaction.followup.send(
            f"🧵 Opened your **Recipe/Materials** ticket: {link}", **ephemeral_kwargs(interaction))

    @discord.ui.button(
        label="🔑 Request trust",
        style=discord.ButtonStyle.secondary,
        custom_id="ov_request_trust"
    )
    async def request_trust(self, interaction: discord.Interaction, button: Button):
        return await self._request_trust_impl(interaction)

    async def _request_trust_impl(self, interaction: discord.Interaction):
        oid = self._oid(interaction)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)
        if not order:
            dummy = discord.Embed(title="⚠️ Order not found", description="This order no longer exists.")
            view = _disable_view_children(OrderView(oid))
            return await _close_ui_in_place(interaction, embed=dummy, view=view, note=None)
        if _order_is_claimed_closed(order):
            try:
                items_data = _load_items()
            except Exception:
                items_data = {"items": {}}
            embed = build_order_embed(order, items_data)
            view = _disable_view_children(OrderView(oid))
            return await _close_ui_in_place(interaction, embed=embed, view=view, note=None)
        base = interaction.client.get_channel(WORKER_CHANNEL_ID)
        if not base or not base.guild:
            return await interaction.response.send_message(
                "⚠️ Bot is not attached to the worker guild.", **ephemeral_kwargs(interaction))
        guild = base.guild
        try:
            member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
        except Exception:
            return await interaction.response.send_message(
                "⚠️ I can't find you in the guild.", **ephemeral_kwargs(interaction))

        state, existing_id = await _reserve_ticket_slot(oid, "trust_ticket_ids", interaction.user.id)
        if state == "gone":
            return await interaction.response.send_message("❌ Order not found.", **ephemeral_kwargs(interaction))
        if state == "pending":
            return await interaction.response.send_message(
                "⏳ Your trust ticket is already being created — give it a moment.", **ephemeral_kwargs(interaction))
        if state == "exists":
            chan = guild.get_channel(int(existing_id)) if existing_id else None
            if chan is not None:
                return await interaction.response.send_message(
                    f"🔑 Your trust request ticket is already open: {chan.mention}", **ephemeral_kwargs(interaction))
            await _release_ticket_slot(oid, "trust_ticket_ids", interaction.user.id)
            state, existing_id = await _reserve_ticket_slot(oid, "trust_ticket_ids", interaction.user.id)
            if state != "reserved":
                return await interaction.response.send_message(
                    "🔑 Your trust ticket is already being set up.", **ephemeral_kwargs(interaction))

        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)
        chan_id = await _open_assist_ticket(interaction, order, member, kind="trust")
        if not chan_id:
            await _release_ticket_slot(oid, "trust_ticket_ids", interaction.user.id)
            return await interaction.followup.send(
                "❌ Could not open a trust ticket. Tell a manager.", **ephemeral_kwargs(interaction))
        await _commit_ticket_slot(oid, "trust_ticket_ids", interaction.user.id, chan_id)
        link = f"https://discord.com/channels/{guild.id}/{chan_id}"
        try:
            ign = ""
            try:
                import Restocker_db as _db_ign2
                ign = _db_ign2.get_ign(str(interaction.user.id)) or ""
            except Exception:
                ign = ""
            ign_txt = f" (IGN `{ign}`)" if ign else ""
            note = (f"🔑 **Trust request** — {member.mention}{ign_txt} needs claim trust to grind "
                    f"**order #{order['id']} — {order.get('item','')}**.\nTicket: {link}")
            mgr_role = discord.utils.get(guild.roles, name=MANAGER_ROLE_NAME)
            wch = interaction.client.get_channel(WORKER_CHANNEL_ID)
            if wch:
                can_ping = bool(mgr_role) and (mgr_role.mentionable or getattr(guild.me.guild_permissions, "mention_roles", False))
                prefix = f"{mgr_role.mention} " if can_ping else ""
                allowed = discord.AllowedMentions(roles=[mgr_role] if can_ping else [], users=[member])
                try:
                    await wch.send(prefix + note, allowed_mentions=allowed)
                except Exception:
                    pass
            if mgr_role:
                for mgr in list(mgr_role.members):
                    if getattr(mgr, "bot", False):
                        continue
                    try:
                        await mgr.send(note)
                    except Exception:
                        pass
        except Exception:
            pass
        return await interaction.followup.send(
            f"🔑 Opened your **Trust / Claim-access** ticket: {link}",
            **ephemeral_kwargs(interaction))

    @discord.ui.button(label="➕ Add produced", style=discord.ButtonStyle.secondary, custom_id="order_add_produced")
    async def add_produced(self, interaction: discord.Interaction, button: Button):
        oid = self._oid(interaction)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)

        if not order:
            dummy = discord.Embed(title="⚠️ Order not found", description="This order no longer exists.")
            return await _close_ui_in_place(
                interaction,
                embed=dummy,
                view=_disable_view_children(OrderView(oid)),
                note=None
            )

        if _order_is_claimed_closed(order):
            try:
                items_data = _load_items()
            except Exception:
                items_data = {"items": {}}
            embed = build_order_embed(order, items_data)
            view = _disable_view_children(OrderView(oid))
            return await _close_ui_in_place(interaction, embed=embed, view=view, note=None)

        if not is_manager(interaction):
            has_claim = _claim_of(order, interaction.user.id) is not None
            if not has_claim:
                return await interaction.response.send_message(
                    "⚠️ You must have a claim on this order to add produced.",
                    **ephemeral_kwargs(interaction)
                )
        return await interaction.response.send_modal(PartialFulfillModal(oid))

    async def fulfill_impl(self, interaction: discord.Interaction):
        return await self._fulfill_core(interaction)

    @discord.ui.button(label="📎 Fulfilled (submit proof)", style=discord.ButtonStyle.blurple, custom_id="order_fulfill")
    async def fulfill(self, interaction: discord.Interaction, button: Button):
        return await self._fulfill_core(interaction)

    async def _fulfill_core(self, interaction: discord.Interaction):

        async def reply(content: str, *, ephemeral: bool = True):
            # Ephemeral replies only work inside a guild interaction. In a DM
            # (direct-assigned orders) passing ephemeral=True makes Discord reject
            # the response, so the worker would see nothing. Drop it when there's
            # no guild so the worker always gets feedback.
            eph = ephemeral and interaction.guild is not None
            try:
                if interaction.response.is_done():
                    return await interaction.followup.send(content, ephemeral=eph)
                return await interaction.response.send_message(content, ephemeral=eph)
            except Exception:
                return None

        oid = self._oid(interaction)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)
        if not order:
            return await reply("❌ Order not found.", ephemeral=True)
        if order.get("status") == "fulfilled":
            return await reply("⚠️ Already fulfilled.", ephemeral=True)
        if order.get("verification_ticket_id"):
            cid = int(order["verification_ticket_id"])
            link = _channel_link(interaction.guild.id, cid) if interaction.guild else f"<#{cid}>"
            return await reply(f"🧵 Verification already open: {link}", ephemeral=True)
        if not is_manager(interaction):
            has_claim = _claim_of(order, interaction.user.id) is not None
            if not has_claim:
                return await reply("⚠️ You must have a claim on this order to submit proof.", ephemeral=True)

        base = interaction.client.get_channel(WORKER_CHANNEL_ID)
        if not base or not base.guild:
            return await reply("❌ WORKER_CHANNEL_ID invalid (bot not attached to worker guild).", ephemeral=True)
        guild = base.guild
        category = guild.get_channel(TICKETS_CATEGORY_ID)
        if not category or category.type != discord.ChannelType.category:
            return await reply("❌ TICKETS_CATEGORY_ID must be a Category.", ephemeral=True)

        def _reserve(o):
            st = str(o.get("status", "")).lower()
            if st in ("fulfilled", "cancelled") or o.get("verification_ticket_id"):
                return False
            o["status"] = "awaiting_verification"
            o["verification_ticket_id"] = -1
            return True
        order, reserved = await _mutate_order(oid, _reserve)
        if order is None:
            return await reply("❌ Order not found.", ephemeral=True)
        if reserved is False:
            existing = order.get("verification_ticket_id")
            if existing and int(existing) > 0 and interaction.guild:
                return await reply(
                    f"🧵 Verification already open: {_channel_link(interaction.guild.id, int(existing))}",
                    ephemeral=True)
            return await reply("⚠️ This order is already being verified or is closed.", ephemeral=True)

        # Creating the verification channel is a slow Discord API call. Defer now so the
        # final reply is a followup (15-min window) instead of racing the 3s interaction
        # limit and 10062-ing — which reply() would swallow, leaving the worker with
        # nothing. Fast rejections above already answered via interaction.response.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)
        except Exception:
            pass

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, manage_channels=True
            ),
        }
        mgr_role = discord.utils.get(guild.roles, name=MANAGER_ROLE_NAME)
        if mgr_role:
            overwrites[mgr_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
            )
        name = f"order-{order['id']}-verify"

        try:
            chan = await guild.create_text_channel(
                name=name, category=category, overwrites=overwrites,
                reason=f"Order #{order['id']} verification"
            )
        except Exception as e:
            await _mutate_order(oid, _release_verify_reservation)
            return await reply(f"❌ Could not create channel: {e}", ephemeral=True)

        requested = int(order.get("requested", order.get("amount", 0)) or 0)
        produced = int(order.get("produced", 0) or 0)
        remaining = max(0, requested - produced)
        intro = (
            f"**Order #{order['id']} — {order.get('item', '')}**\n"
            f"Requested: {fmt_qty(order, requested, prefer_original_amount=True)}, "
            f"Produced: {fmt_qty(order, produced)}, "
            f"Remaining: {fmt_qty(order, remaining)}\n\n"
            f"{interaction.user.mention}, please upload **picture proof** of the finished order.\n"
            "Managers can Approve/Reject below."
        )
        try:
            await chan.send(intro, view=ManagerReviewView(order['id'], chan.id))
        except Exception as e:
            await _mutate_order(oid, _release_verify_reservation)
            try:
                await chan.delete(reason="verification setup failed")
            except Exception:
                pass
            return await reply(f"⚠️ Created channel but couldn't send intro: {e}", ephemeral=True)

        def _set_ticket(o):
            o["verification_ticket_id"] = int(chan.id)
            o["status"] = "awaiting_verification"
            return True
        o2, _ = await _mutate_order(oid, _set_ticket)
        order = o2 or order

        try:
            await update_order_messages(interaction.client, order)
        except Exception:
            pass

        if mgr_role:
            link = _channel_link(guild.id, chan.id)
            for m in list(mgr_role.members):
                try:
                    await m.send(
                        f"🧵 New verification channel for **Order #{order['id']} — {order.get('item', '')}**.\n{link}"
                    )
                except Exception:
                    continue

        return await reply(
            f"🧵 Created verification channel: {_channel_link(guild.id, chan.id)}\nPlease upload your proof there.",
            ephemeral=True
        )


class CoinPriceModal(discord.ui.Modal):
    def __init__(self, *, item_name: str, current_price: int | None):
        super().__init__(title="Set coin price (per piece)")

        self.item_name = item_name

        self.item = discord.ui.TextInput(
            label="Item name (exact)",
            placeholder="e.g. Blue moon: Unluck 10",
            required=True,
            max_length=100,
            default=item_name
        )
        self.price = discord.ui.TextInput(
            label="Price per piece (integer)",
            placeholder="e.g. 60",
            required=True,
            max_length=12,
            default="" if current_price is None else str(int(current_price))
        )

        self.add_item(self.item)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        item_name = str(self.item.value).strip()
        try:
            new_price = int(str(self.price.value).strip())
            if new_price < 0:
                raise ValueError()
        except Exception:
            return await interaction.response.send_message("❌ Price must be an integer ≥ 0.", ephemeral=True)

        try:
            items_data = _load_items()
        except Exception as e:
            return await interaction.response.send_message(f"❌ Failed to load items.yml: {e}", ephemeral=True)

        items = items_data.setdefault("items", {})
        if item_name not in items:
            low = item_name.lower()
            suggestions = [k for k in items.keys() if low in str(k).lower()][:8]
            hint = ""
            if suggestions:
                hint = "\n\nDid you mean:\n" + "\n".join(f"• {s}" for s in suggestions)
            return await interaction.response.send_message(
                f"❌ Item not found: **{item_name}**{hint}",
                ephemeral=True
            )

        info = items.get(item_name) or {}
        if not isinstance(info, dict):
            info = {}
            items[item_name] = info

        old = info.get("coin", None)
        info["coin"] = int(new_price)

        _save_items(items_data)

        old_txt = "unset" if old is None else str(old)
        await interaction.response.send_message(
            f"✅ Updated price (per piece)\n"
            f"• Item: **{item_name}**\n"
            f"• Old: **{old_txt}** → New: **{new_price}**",
            ephemeral=True
        )


class CoinPriceSearchModal(discord.ui.Modal):
    def __init__(self, on_query):
        super().__init__(title="Search item")
        self._on_query = on_query

        self.q = discord.ui.TextInput(
            label="Search text",
            placeholder="type part of item name…",
            required=True,
            max_length=60
        )
        self.add_item(self.q)

    async def on_submit(self, interaction: discord.Interaction):
        query = str(self.q.value).strip()
        await self._on_query(interaction, query)


class ItemPricePickerView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self):
        super().__init__(timeout=180)
        self.query: str = ""
        self.page: int = 0
        self.selected_name: str | None = None
        self._items_cache: dict[str, dict] = {}

        self.select = discord.ui.Select(
            placeholder="Pick an item to edit…",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Loading…", value="__loading__")],
            custom_id="coinprice_item_select"
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    def _load_item_names(self) -> list[str]:
        items_data = _load_items()
        items = items_data.get("items", {}) or {}
        self._items_cache = items if isinstance(items, dict) else {}
        names = [k for k in self._items_cache.keys() if isinstance(k, str)]
        names.sort(key=lambda s: s.lower())
        return names

    def _filtered_names(self) -> list[str]:
        names = self._load_item_names()
        if not self.query:
            return names
        q = self.query.lower()
        return [n for n in names if q in n.lower()]

    def _page_slice(self, names: list[str]) -> list[str]:
        start = self.page * self.PAGE_SIZE
        end = start + self.PAGE_SIZE
        return names[start:end]

    def _rebuild_options(self):
        names = self._filtered_names()
        if not names:
            self.select.options = [
                discord.SelectOption(label="No matches", value="__none__", default=True)
            ]
            return

        max_page = max(0, (len(names) - 1) // self.PAGE_SIZE)
        self.page = max(0, min(self.page, max_page))

        chunk = self._page_slice(names)
        opts = []
        for n in chunk:
            info = self._items_cache.get(n) if isinstance(self._items_cache, dict) else None
            cur = None
            if isinstance(info, dict):
                cur = info.get("coin", None)

            desc = "price: unset" if cur is None else f"price: {int(cur)}"
            opts.append(discord.SelectOption(label=n[:100], value=n, description=desc[:100]))

        if self.selected_name and any(o.value == self.selected_name for o in opts):
            for o in opts:
                o.default = (o.value == self.selected_name)

        self.select.options = opts

    async def refresh(self, interaction: discord.Interaction):
        try:
            self._rebuild_options()
        except Exception as e:
            self.select.options = [discord.SelectOption(label=f"Error: {e}", value="__err__", default=True)]

        embed = discord.Embed(
            title="💲 Set coin price",
            description=(
                f"Search: **{self.query or '—'}**\n"
                f"Page: **{self.page + 1}**\n\n"
                "Pick an item from the dropdown, then it will open a pre-filled form."
            ),
            color=discord.Color.gold()
        )


        try:
            if not interaction.response.is_done():
                return await interaction.response.edit_message(embed=embed, view=self)
            if interaction.message:
                return await interaction.message.edit(embed=embed, view=self)
        except Exception:
            pass

    async def _on_select(self, interaction: discord.Interaction):
        val = (self.select.values or ["__none__"])[0]
        if val.startswith("__"):
            return await interaction.response.defer(ephemeral=True)

        self.selected_name = val

        info = self._items_cache.get(val) if isinstance(self._items_cache, dict) else None
        cur = None
        if isinstance(info, dict):
            cur = info.get("coin", None)

        await interaction.response.send_modal(CoinPriceModal(item_name=val, current_price=cur))

    @discord.ui.button(label="🔎 Search", style=discord.ButtonStyle.secondary, custom_id="coinprice_search")
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        async def _apply(inter2: discord.Interaction, q: str):
            self.query = q
            self.page = 0
            self.selected_name = None
            await self.refresh(inter2)

        await interaction.response.send_modal(CoinPriceSearchModal(_apply))

    @discord.ui.button(label="⬅ Prev", style=discord.ButtonStyle.secondary, custom_id="coinprice_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await self.refresh(interaction)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary, custom_id="coinprice_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.refresh(interaction)

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger, custom_id="coinprice_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            try:
                child.disabled = True
            except Exception:
                pass


        try:
            if not interaction.response.is_done():
                return await interaction.response.edit_message(view=self)
            if interaction.message:
                return await interaction.message.edit(view=self)
        except Exception:
            pass


class OrdersBrowser(View):
    def __init__(self, orders: list[dict], *, viewer_id: int | None = None):
        super().__init__(timeout=None)

        self.viewer_id = int(viewer_id) if viewer_id is not None else None

        def _viewer_has_claim(o: dict) -> bool:
            if self.viewer_id is None:
                return False
            for c in (o.get("claims") or []):
                try:
                    if int(c.get("user_id", 0) or 0) == self.viewer_id:
                        return True
                except Exception:
                    continue
            return False


        filtered: list[dict] = []
        for o in (orders or []):
            st = str(o.get("status", "")).lower()
            if st in ("fulfilled", "cancelled"):
                continue
            if not _order_is_claimed_closed(o):
                filtered.append(o)
                continue
            if st == "claimed" and _viewer_has_claim(o):
                filtered.append(o)

        self.orders = filtered

        options = []
        for o in self.orders:
            oid = int(o.get("id", 0) or 0)
            item = str(o.get("item", ""))
            st = str(o.get("status", "open")).capitalize()
            rem_txt = f"rem {fmt_qty(o, remaining_to_assign(o))}"
            options.append(
                discord.SelectOption(
                    label=f"#{oid} {item}"[:100],
                    description=f"{st} · {rem_txt}"[:100],
                    value=str(oid),
                )
            )

        if not options:
            options = [discord.SelectOption(label="No open/claimed orders", value="none", default=True)]

        self.order_select = Select(
            placeholder="Pick an order...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ob_order_select",
        )
        self.order_select.callback = self._on_select
        self._selected_id: int | None = None
        self.add_item(self.order_select)

    def selected_id(self) -> int | None:
        return self._selected_id

    async def _ack(self, interaction: discord.Interaction, *, ephemeral: bool = True):
        use_ephemeral = bool(interaction.guild) and bool(ephemeral)

        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=use_ephemeral)
        except Exception:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("✅", ephemeral=use_ephemeral)
            except Exception:
                pass

    async def _on_select(self, interaction: discord.Interaction):
        vals = self.order_select.values or []
        if not vals or vals[0] == "none":
            self._selected_id = None
        else:
            self._selected_id = int(vals[0])
        await self._ack(interaction, ephemeral=True)

    @discord.ui.button(label="✅ Claim selected", style=discord.ButtonStyle.success, custom_id="ob_claim_selected")
    async def claim_selected(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)

        oid = self.selected_id()
        if not oid:
            return await interaction.followup.send("Pick an order first.", ephemeral=True)

        data = load_orders()
        order = next((o for o in data["orders"] if int(o.get("id", 0) or 0) == int(oid)), None)

        if not order:
            missing = discord.Embed(
                title="⚠️ Order not found",
                description="This order no longer exists.",
                color=discord.Color.dark_grey()
            )
            _disable_view_children(self)
            return await _close_ui_in_place(interaction, embed=missing, view=self, note=None)

        if _order_is_claimed_closed(order):
            try:
                items_data = _load_items()
            except Exception:
                items_data = {"items": {}}
            embed = build_order_embed(order, items_data)
            _disable_view_children(self)
            return await _close_ui_in_place(interaction, embed=embed, view=self, note=None)

        guard = await _priority_guard(interaction, order)
        if guard:
            return await interaction.followup.send(guard, ephemeral=True)


        if _is_blocked_claimer(order, interaction.user.id):
            return await interaction.followup.send(
                "❌ You cannot claim this order anymore (it was escalated away from you).",
                ephemeral=True
            )

        res = await _apply_claim(interaction, int(oid), "all")
        if res.get("code") == "blocked":
            return await interaction.followup.send(
                "❌ You cannot claim this order anymore (it was escalated away from you).",
                ephemeral=True)
        order = res.get("order") or order
        if not res.get("ok"):
            try:
                items_data = _load_items()
            except Exception:
                items_data = {"items": {}}
            embed = build_order_embed(order, items_data)
            _disable_view_children(self)
            try:
                await update_order_messages(interaction.client, order)
            except Exception:
                pass
            return await _close_ui_in_place(interaction, embed=embed, view=self, note=None)

        await _ensure_order_dm_panel(interaction.client, order, interaction.user)
        await update_order_messages(interaction.client, order)

        if res.get("closed"):
            await cleanup_batch_dms_for_closed_order(interaction.client, int(order["id"]))
            try:
                items_data = _load_items()
            except Exception:
                items_data = {"items": {}}
            embed = build_order_embed(order, items_data)
            _disable_view_children(self)
            return await _close_ui_in_place(interaction, embed=embed, view=self, note=None)

        try:
            items_data = _load_items()
        except Exception:
            items_data = {"items": {}}
        claimed = int(res.get("claimed", 0))
        est_coins = _coins_for_pieces(order, claimed, items_data)

        return await interaction.followup.send(
            f"✅ Claimed {fmt_qty(order, claimed)} on order #{order['id']}.\n"
            f"📩 I moved this order to your DMs (worker channel stays clean).\n"
            f"💰 Estimated payout: **≈ {est_coins} coins**.",
            ephemeral=True
        )

    @discord.ui.button(label="🧪 Request recipe/materials", style=discord.ButtonStyle.primary, custom_id="ob_request_recipe")
    async def request_recipe_materials(self, interaction: discord.Interaction, button: Button):
        oid = self.selected_id()
        if not oid:
            return await interaction.response.send_message("Pick an order first.", ephemeral=True)
        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == oid), None)
        if not order or order.get("status") in ("fulfilled", "cancelled"):
            return await interaction.response.send_message("⚠️ That order is closed.", ephemeral=True)
        base = interaction.client.get_channel(WORKER_CHANNEL_ID)
        if not base or not base.guild:
            return await interaction.response.send_message("⚠️ Bot is not attached to the worker guild.", ephemeral=True)
        guild = base.guild
        try:
            member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
        except Exception:
            return await interaction.response.send_message("⚠️ I can't find you in the guild.", ephemeral=True)

        state, existing_id = await _reserve_ticket_slot(oid, "assist_ticket_ids", interaction.user.id)
        if state == "gone":
            return await interaction.response.send_message("❌ Order not found.", ephemeral=True)
        if state == "pending":
            return await interaction.response.send_message("⏳ Your ticket is already being created — give it a moment.", ephemeral=True)
        if state == "exists":
            chan = guild.get_channel(int(existing_id)) if existing_id else None
            if chan is not None:
                return await interaction.response.send_message(f"🧵 Your assist ticket is already open: {chan.mention}", ephemeral=True)
            await _release_ticket_slot(oid, "assist_ticket_ids", interaction.user.id)
            state, existing_id = await _reserve_ticket_slot(oid, "assist_ticket_ids", interaction.user.id)
            if state != "reserved":
                return await interaction.response.send_message("🧵 Your assist ticket is already being set up.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        chan_id = await _open_assist_ticket(interaction, order, member)
        if not chan_id:
            await _release_ticket_slot(oid, "assist_ticket_ids", interaction.user.id)
            return await interaction.followup.send("❌ Could not open an assist ticket. Tell a manager.", ephemeral=True)
        await _commit_ticket_slot(oid, "assist_ticket_ids", interaction.user.id, chan_id)
        link = f"https://discord.com/channels/{guild.id}/{chan_id}"
        return await interaction.followup.send(f"🧵 Opened your **Recipe/Materials** ticket: {link}", ephemeral=True)

    @discord.ui.button(label="🧩 Claim part…", style=discord.ButtonStyle.secondary, custom_id="ob_claim_part")
    async def claim_part(self, interaction: discord.Interaction, button: Button):
        oid = self.selected_id()
        if not oid:
            return await interaction.response.send_message("Pick an order first.", ephemeral=True)
        return await interaction.response.send_modal(ClaimPartModal(oid))

    @discord.ui.button(label="📎 Fulfilled (submit proof) on selected", style=discord.ButtonStyle.primary, custom_id="ob_fulfilled_selected")
    async def fulfilled_selected(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True, thinking=True)

        oid = self.selected_id()
        if not oid:
            return await interaction.followup.send("Pick an order first.", ephemeral=True)

        try:
            return await OrderView(int(oid))._fulfill_core(interaction)
        except Exception as e:
            try:
                return await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)
            except Exception:
                return

    @discord.ui.button(label="📋 My claims", style=discord.ButtonStyle.secondary, custom_id="ob_my_claims")
    async def my_claims_btn(self, interaction: discord.Interaction, button: Button):
        data = load_orders()
        mine = []

        for o in data.get("orders", []) or []:

            if str(o.get("status", "")).lower() in ("fulfilled", "cancelled"):
                continue

            for c in (o.get("claims") or []):
                if int(c.get("user_id", 0) or 0) == int(interaction.user.id):
                    mine.append((o, int(c.get("qty", 0) or 0)))

        if not mine:
            return await interaction.response.send_message("📭 You have no claims.", ephemeral=True)

        mine.sort(key=lambda x: int(x[0].get("id", 0) or 0), reverse=True)

        lines = []
        for (o, qty) in mine[:25]:
            st = str(o.get("status", "open")).capitalize()
            rem = remaining_to_assign(o)
            lines.append(
                f"• **#{o['id']}** {o.get('item', '')} · you claimed {fmt_qty(o, qty)} · "
                f"status **{st}** · remaining {fmt_qty(o, rem)}"
            )

        return await interaction.response.send_message("🧾 **Your claims:**\n" + "\n".join(lines), ephemeral=True)


class OrdersPaginator(View):
    def __init__(self, pages):
        super().__init__(timeout=120)
        self.pages = pages
        self.index = 0

    async def update(self, interaction):
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="⏪", style=discord.ButtonStyle.secondary)
    async def first(self, interaction, button):
        self.index = 0
        await self.update(interaction)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.primary)
    async def prev(self, interaction, button):
        if self.index > 0:
            self.index -= 1
            await self.update(interaction)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction, button):
        if self.index < len(self.pages) - 1:
            self.index += 1
            await self.update(interaction)

    @discord.ui.button(label="⏩", style=discord.ButtonStyle.secondary)
    async def last(self, interaction, button):
        self.index = len(self.pages) - 1
        await self.update(interaction)

    @discord.ui.button(label="🔔 Remind…", style=discord.ButtonStyle.gray)
    async def remind(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))
        await interaction.response.send_modal(RemindByIdModal())


class ManagerPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="📋 View Orders", style=discord.ButtonStyle.primary)
    async def view_orders(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        pages = build_orders_pages()
        if not pages:
            return await interaction.response.send_message("📭 No orders found.", ephemeral=True)

        await interaction.response.send_message(embed=pages[0], view=OrdersPaginator(pages), ephemeral=True)

    @discord.ui.button(label="🚨 Escalate order…", style=discord.ButtonStyle.danger)
    async def escalate_order_btn(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        embed = discord.Embed(
            title="🚨 Escalate an order",
            description="Select an order from the dropdown, then click **Escalate selected…**.\n"
                        "No typing Order IDs.",
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, view=EscalatePickView(), ephemeral=True)

    @discord.ui.button(label="🧹 Prune Fulfilled/Cancelled", style=discord.ButtonStyle.danger)
    async def prune_closed(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        data = load_orders()
        before = list(data.get("orders", []) or [])


        keep = [
            o for o in before
            if str(o.get("status", "")).lower() not in ("fulfilled", "cancelled")
        ]

        removed = len(before) - len(keep)
        data["orders"] = keep

        ok = save_orders(data, prune=True)
        if not ok:
            return await interaction.followup.send("❌ Failed to prune (could not write orders.yml).", ephemeral=True)

        return await interaction.followup.send(
            f"🧹 Removed **{removed}** fulfilled/cancelled order(s).",
            ephemeral=True
        )

    @discord.ui.button(label="🐝 Hive pickup status", style=discord.ButtonStyle.secondary)
    async def hive_status_btn(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        bid, batch = _get_latest_batch()
        if not batch:
            return await interaction.response.send_message("📭 No active hive pickup batch.", ephemeral=True)

        lines = []
        for site, info in (batch.get("sites") or {}).items():
            if info:
                lines.append(f"• **{site}** → {info.get('user_tag', 'unknown')}")
            else:
                lines.append(f"• **{site}** → ❌ unclaimed")

        embed = discord.Embed(
            title=f"📦 Hive Pickup Status — Batch #{bid}",
            description="\n".join(lines) if lines else "—",
            color=discord.Color.orange()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🧹 Clear hive pickups", style=discord.ButtonStyle.danger)
    async def hive_clear_btn(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        _clear_all_hive_pickups()
        try:
            interaction.client._active_hive_batch = None
        except Exception:
            pass

        return await interaction.response.send_message(
            "🧹 All hive pickup batches and claims have been cleared.",
            ephemeral=True
        )

    @discord.ui.button(label="💲 Set coin price", style=discord.ButtonStyle.primary)
    async def set_coin_price_btn(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        v = ItemPricePickerView()

        try:
            v._rebuild_options()
        except Exception:
            pass

        embed = discord.Embed(
            title="💲 Set coin price",
            description="Pick an item from the dropdown to edit it (opens a pre-filled form).",
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, view=v, ephemeral=True)

    @discord.ui.button(label="📩 Funds report now", style=discord.ButtonStyle.secondary)
    async def funds_report_now_btn(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ok = await _send_funds_report(interaction.client)
            total = _total_funds_coins()
            await interaction.followup.send(
                ("✅ Report sent." if ok else "⚠️ Failed to send report.")
                + f" Total funds: **{total} coins**.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Funds report failed: {e}", ephemeral=True)

    @discord.ui.button(label="💰 Apply interest now", style=discord.ButtonStyle.secondary)
    async def interest_now_btn(self, interaction: discord.Interaction, button: Button):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            applied_users, total_paid = apply_weekly_interest(force=True)
            await interaction.followup.send(
                f"✅ Interest applied. Users credited: **{applied_users}** · Total paid: **{total_paid} coins**.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Interest failed: {e}", ephemeral=True)


class FillMissingPricesModal(discord.ui.Modal):
    def __init__(self, missing_items: list[str]):
        super().__init__(title="Set coin price (per piece)", timeout=300)
        self.missing_items = list(missing_items)
        self.item_name = self.missing_items[0] if self.missing_items else ""

        self.item = discord.ui.TextInput(
            label="Item (missing price)",
            default=self.item_name,
            required=True,
            max_length=100
        )
        self.price = discord.ui.TextInput(
            label="Price per piece (integer)",
            placeholder="e.g. 60",
            required=True,
            max_length=12
        )

        self.add_item(self.item)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        item_name = str(self.item.value).strip()
        try:
            price = int(str(self.price.value).strip())
            if price <= 0:
                raise ValueError()
        except Exception:
            return await interaction.response.send_message("❌ Price must be an integer > 0.", ephemeral=True)

        data = _load_items()
        items = data.setdefault("items", {})
        if item_name not in items:
            return await interaction.response.send_message(f"❌ Item not found: **{item_name}**", ephemeral=True)

        info = items.get(item_name)
        if not isinstance(info, dict):
            info = {"stock": 0, "coin": 0}
            items[item_name] = info

        info["coin"] = int(price)
        _save_items(data)

        remaining = [x for x in self.missing_items[1:] if x != item_name]


        await interaction.response.send_message(
            f"✅ Set **{item_name}** → **{price} coins/piece**.",
            ephemeral=True
        )


        if remaining:
            try:
                await interaction.followup.send_modal(FillMissingPricesModal(remaining))
            except Exception:
                await interaction.followup.send(
                    f"⚠️ Couldn’t open the next modal automatically.\n"
                    f"Run `/coinprices_fill_missing` again to continue (**{len(remaining)}** left).",
                    ephemeral=True
                )
        else:
            await interaction.followup.send("🎉 All missing prices are filled.", ephemeral=True)

