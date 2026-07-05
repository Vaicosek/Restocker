"""Order / worker commands (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from datetime import datetime
from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
ANNOUNCE_DELAY_MINUTES = core.ANNOUNCE_DELAY_MINUTES
EMPLOYEE_ROLE_NAME = core.EMPLOYEE_ROLE_NAME
MANAGER_ROLE_NAME = core.MANAGER_ROLE_NAME
ManagerPanelView = core.ManagerPanelView
PRIORITY_HOURS = core.PRIORITY_HOURS
WORKER_CHANNEL_ID = core.WORKER_CHANNEL_ID
WorkerView = core.WorkerView
_coin_rates_for_order = core._coin_rates_for_order
_coins_for_pieces = core._coins_for_pieces
_load_items = core._load_items
_order_is_claimed_closed = core._order_is_claimed_closed
_priority_active = core._priority_active
any_item_autocomplete = core.any_item_autocomplete
ephemeral_kwargs = core.ephemeral_kwargs
fmt_qty = core.fmt_qty
is_manager = core.is_manager
load_orders = core.load_orders
next_batch_slot = core.next_batch_slot
order_id_autocomplete = core.order_id_autocomplete
orders_cmd = core.orders_cmd
parse_iso = core.parse_iso
remaining_to_assign = core.remaining_to_assign
save_orders = core.save_orders
timedelta = core.timedelta
timezone = core.timezone
unit_to_pieces = core.unit_to_pieces
update_order_messages = core.update_order_messages
utcnow_iso = core.utcnow_iso
_ensure_order_dm_panel = core._ensure_order_dm_panel

import re as _re

_GEAR_TOOLS = {   # keyword -> canonical piece (longest matched first: chestplate before chest)
    "chestplate": "Chestplate", "leggings": "Leggings", "pickaxe": "Pickaxe",
    "shovel": "Shovel", "helmet": "Helmet", "boots": "Boots", "sword": "Sword",
    "spade": "Shovel", "chest": "Chestplate", "legs": "Leggings", "pants": "Leggings",
    "helm": "Helmet", "pick": "Pickaxe", "axe": "Axe",
}
_GEAR_ENCH = [   # (regex, canonical enchant) — matched loosely; sorted after to match the mod
    (r"eff(?:iciency)?\s*(?:v|5)\b", "Efficiency V"),
    (r"eff(?:iciency)?\s*(?:iv|4)\b", "Efficiency IV"),
    (r"fort(?:une)?(?:\s*(?:iii|3))?\b", "Fortune III"),
    (r"silk(?:\s*touch)?\b", "Silk Touch"),
    (r"sharp(?:ness)?\s*(?:v|5)\b", "Sharpness V"),
    (r"fire\s*asp(?:ect)?(?:\s*(?:ii|2))?\b", "Fire Aspect II"),
    (r"(?:knock\s*back|kb)(?:\s*(?:ii|2))?\b", "Knockback II"),
    (r"prot(?:ection)?\s*(?:iv|4)\b", "Protection IV"),
    (r"unbreak(?:ing)?\s*(?:iii|3)\b", "Unbreaking III"),
]


def _resolve_gear(text: str):
    """Loose plain-text ('eff 5 unbreak 3 axe fort 3') -> canonical Diamond gear name matching
    the mod/catalog ('Diamond Axe - Efficiency V, Fortune III, Unbreaking III'), or None.
    Unbreaking III is auto-added (it's core on every enchanted item)."""
    t = (text or "").lower()
    tool = None
    for kw in sorted(_GEAR_TOOLS, key=len, reverse=True):
        if _re.search(rf"\b{kw}\b", t):
            tool = _GEAR_TOOLS[kw]
            break
    if not tool:
        return None
    ench = []
    for rx, canon in _GEAR_ENCH:
        if _re.search(rx, t) and canon not in ench:
            ench.append(canon)
    if not any(e != "Unbreaking III" for e in ench):
        return None   # need a real enchant besides Unbreaking to be confident
    if "Unbreaking III" not in ench:
        ench.append("Unbreaking III")
    ench.sort()
    return f"Diamond {tool} - {', '.join(ench)}"

class OrdersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="orders", description="Show open production requests")
    async def orders(self, interaction: discord.Interaction):
        return await orders_cmd(interaction)

    @app_commands.command(name="cancel_order", description="(Managers) Cancel an existing restock order by ID")


    @app_commands.describe(order_id="The ID of the order to cancel")


    @app_commands.autocomplete(order_id=order_id_autocomplete)
    async def cancel_order(self, interaction: discord.Interaction, order_id: int):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ You need the @Managers role to cancel orders.", **ephemeral_kwargs(interaction))

        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == order_id), None)
        if not order:
            return await interaction.response.send_message(f"❌ Order #{order_id} not found.", **ephemeral_kwargs(interaction))
        if order["status"] == "fulfilled":
            return await interaction.response.send_message(
                f"⚠️ Order #{order_id} is already fulfilled and cannot be cancelled.", **ephemeral_kwargs(interaction)
            )
        if order["status"] == "cancelled":
            return await interaction.response.send_message(f"⚠️ Order #{order_id} is already cancelled.", **ephemeral_kwargs(interaction))

        order["status"] = "cancelled"
        save_orders(data)
        await update_order_messages(interaction.client, order)
        await interaction.response.send_message(f"❌ Order #{order_id} has been cancelled.", **ephemeral_kwargs(interaction))

    @app_commands.command(
        name="order",
        description="(Managers) Order an item from workers — everyone, or DM one specific worker"
    )
    @app_commands.describe(
        item_key="Pick an existing catalog item (type to search)",
        amount="How many (in the unit you choose)",
        unit_type="Choose pieces, stacks, or barrels",
        worker="Optional: assign directly to ONE worker (DMs only them, no mass ping). Blank = ask all workers.",
        stackable="Optional — auto-detected from the catalog per item; only set to override"
    )
    @app_commands.choices(unit_type=[
        app_commands.Choice(name="Pieces", value="pieces"),
        app_commands.Choice(name="Stacks", value="stacks"),
        app_commands.Choice(name="Barrels", value="barrels"),
    ])
    @app_commands.autocomplete(item_key=any_item_autocomplete)
    async def order(self,
        interaction: discord.Interaction,
        item_key: str,
        amount: int,
        unit_type: str,
        worker: Optional[discord.Member] = None,
        stackable: Optional[bool] = None,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message(
                "⛔ You need the @Managers role to create orders.",
                **ephemeral_kwargs(interaction)
            )
        if worker is not None and worker.bot:
            return await interaction.response.send_message(
                "❌ Pick a real worker (not a bot).", **ephemeral_kwargs(interaction)
            )

        if amount <= 0:
            return await interaction.response.send_message(
                "❌ Amount must be a positive integer.",
                **ephemeral_kwargs(interaction)
            )

        item = (item_key or "").strip()
        if not item:
            return await interaction.response.send_message(
                "❌ Invalid item selection.",
                **ephemeral_kwargs(interaction)
            )

        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)


        try:
            shops = _load_items()
        except Exception:
            return await interaction.followup.send(
                "❌ items file couldn’t be read.",
                **ephemeral_kwargs(interaction)
            )

        items = (shops.get("items") or {})
        if item not in items:
            return await interaction.followup.send(
                f"❌ Item **{item}** no longer exists.",
                **ephemeral_kwargs(interaction)
            )

        info = items.get(item) or {}
        if not isinstance(info, dict):
            info = {}

        try:
            price_piece = int(info.get("coin", 0) or 0)
        except Exception:
            price_piece = 0

        if price_piece <= 0:
            return await interaction.followup.send(
                f"❌ **{item}** has **no coin price** set.\n"
                f"Set it in your items file under: `items -> {item} -> coin` (PER PIECE).",
                **ephemeral_kwargs(interaction)
            )

        # Auto-detect stackability from the catalog when the manager didn't pick it,
        # so gear (stackable=False, size 1) vs blocks/items (stackable=True, real stack
        # size) is handled per-item without the manager having to know each one.
        cat_stackable = bool(info.get("stackable", True))
        try:
            cat_stack_size = int(info.get("stack_size") or (64 if cat_stackable else 1))
        except Exception:
            cat_stack_size = 64 if cat_stackable else 1
        if stackable is None:
            stackable = cat_stackable
            stack_size = cat_stack_size
        else:
            stackable = bool(stackable)
            stack_size = 64 if stackable else 1

        unit = str(unit_type).lower().strip()
        if unit not in ("pieces", "stacks", "barrels"):
            unit = "pieces"

        requested_pieces = unit_to_pieces(int(amount), unit, stackable=stackable)

        data_orders = load_orders()
        new_id = (max([o.get("id", 0) for o in data_orders.get("orders", [])] or [0]) + 1)
        now_utc = datetime.now(timezone.utc)

        if worker is not None:
            # Direct order: pre-assign the whole thing to this one worker and mark it
            # announced, so the worker-channel batch/ping loops never broadcast it — it
            # only ever hits the assigned worker's DM, via the normal fulfil→approve→pay path.
            order = {
                "id": new_id, "shop": "", "item": item,
                "requested": requested_pieces, "produced": 0,
                "status": "claimed", "claimed_by": str(worker),
                "claims": [{"user_id": worker.id, "user_tag": str(worker),
                            "qty": requested_pieces, "claimed_at": utcnow_iso()}],
                "created_at": utcnow_iso(),
                "messages": {"channel_id": None, "message_id": None, "dms": {}},
                "unit_type": unit, "amount": int(amount),
                "stackable": bool(stackable), "stack_size": stack_size, "barrel_slots": 54,
                "employee_announce_at": None, "employee_announced": True, "worker_announced": True,
                "priority_until": None,
            }
        else:
            # Broadcast: goes on the worker board and pings the pool after the batch delay.
            announce_at = next_batch_slot(ANNOUNCE_DELAY_MINUTES)
            order = {
                "id": new_id, "shop": "", "item": item,
                "requested": requested_pieces, "produced": 0,
                "status": "open", "claimed_by": None, "claims": [],
                "created_at": utcnow_iso(),
                "messages": {"channel_id": None, "message_id": None, "dms": {}},
                "unit_type": unit, "amount": int(amount),
                "stackable": bool(stackable), "stack_size": stack_size, "barrel_slots": 54,
                "employee_announce_at": announce_at.isoformat(),
                "employee_announced": False, "worker_announced": False,
                "priority_until": (now_utc + timedelta(hours=PRIORITY_HOURS)).isoformat(),
                "priority_role": EMPLOYEE_ROLE_NAME,
            }

        data_orders.setdefault("orders", []).append(order)
        save_orders(data_orders)

        pp, ps, pb, pieces_per_barrel = _coin_rates_for_order(order, shops)
        total = _coins_for_pieces(order, requested_pieces, shops)

        if worker is not None:
            dmed = True
            try:
                await _ensure_order_dm_panel(interaction.client, order, worker)
                await worker.send(
                    f"📦 You’ve been **directly assigned Order #{new_id}** — "
                    f"**{amount} {unit}** of **{item}**.\n"
                    f"Produce it, then hit **📎 Fulfilled (submit proof)** on the order card above. "
                    f"You’ll be paid and earn loyalty points once a manager approves it."
                )
            except Exception:
                dmed = False
            tail = ("📩 Sent straight to their DMs (no mass ping)." if dmed
                    else "⚠️ Couldn’t DM them (DMs closed) — they can still open it from `/orders` (it shows under their claims).")
            await interaction.followup.send(
                f"✅ Direct order #{new_id} assigned to {worker.mention}: **{amount} {unit}** of **{item}**.\n"
                f"💰 Estimated payout: ≈ **{total} coins** (+loyalty) on approval.\n{tail}",
                **ephemeral_kwargs(interaction))
        else:
            await interaction.followup.send(
                f"✅ Order #{new_id} created for **{item}**\n"
                f"Requested: **{amount} {unit}** · Stackable: **{stackable}**\n"
                f"(Stored internally as **{requested_pieces} pieces**)\n\n"
                f"💰 **Estimated payout:** ≈ **{total} coins**\n"
                f"• Per item (piece): **{pp:.2f}**\n"
                f"• Per barrel: **{pb:.2f}** (barrel = {pieces_per_barrel} pcs)\n"
                f"• Price basis: **piece**\n\n"
                f"⏱️ Workers ping + Employee DM will go out in **{ANNOUNCE_DELAY_MINUTES} min**.",
                **ephemeral_kwargs(interaction)
            )

    @app_commands.command(name="order_bulk",
        description="(Managers) Create many orders at once from a pasted list")
    @app_commands.describe(
        orders="One per line: `Item name | quantity`  (e.g. Diamond Shovel - Fortune III, Unbreaking III, Efficiency V | 500)",
        unit_type="Unit for every line (default pieces)",
    )
    @app_commands.choices(unit_type=[
        app_commands.Choice(name="Pieces", value="pieces"),
        app_commands.Choice(name="Stacks", value="stacks"),
        app_commands.Choice(name="Barrels", value="barrels"),
    ])
    async def order_bulk(self, interaction: discord.Interaction, orders: str, unit_type: str = "pieces"):
        if not is_manager(interaction):
            return await interaction.response.send_message(
                "⛔ You need the @Managers role to create orders.", **ephemeral_kwargs(interaction))
        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)
        import re as _re
        unit = str(unit_type).lower().strip()
        if unit not in ("pieces", "stacks", "barrels"):
            unit = "pieces"
        shops = _load_items()
        items = (shops.get("items") or {})
        lines = [l.strip() for l in orders.replace("\\n", "\n").split("\n") if l.strip()]
        data_orders = load_orders()
        base_id = max([o.get("id", 0) for o in data_orders.get("orders", [])] or [0])
        now_utc = datetime.now(timezone.utc)
        announce_at = next_batch_slot(ANNOUNCE_DELAY_MINUTES)
        created, unpriced, failed = [], [], []
        for line in lines:
            # Prefer "name | qty" (safe — item names contain commas); fall back to "name x qty" / "name qty".
            name = qty = None
            if "|" in line:
                a, b = line.rsplit("|", 1)
                name = a.strip()
                digs = _re.sub(r"[^\d]", "", b)
                qty = int(digs) if digs else 0
            else:
                m = _re.match(r"^(.*?)\s+x?\s*(\d[\d,]*)\s*$", line, _re.I)
                if m:
                    name = m.group(1).strip(); qty = int(m.group(2).replace(",", ""))
            if not name or not qty or qty <= 0:
                failed.append(line[:60]); continue
            info = items.get(name)
            if not isinstance(info, dict):
                _rg = _resolve_gear(name)        # plain-text? "eff 5 unbreak 3 axe fort 3" -> canonical
                if _rg and isinstance(items.get(_rg), dict):
                    name, info = _rg, items.get(_rg)
            if isinstance(info, dict):
                try:
                    price = int(info.get("coin", 0) or 0)
                except Exception:
                    price = 0
                stackable = bool(info.get("stackable", True))
                try:
                    stack_size = int(info.get("stack_size") or (64 if stackable else 1))
                except Exception:
                    stack_size = 64 if stackable else 1
            else:
                price, stackable, stack_size = 0, False, 1   # lenient: unknown item still posts (price 0)
                unpriced.append(name)
            requested_pieces = unit_to_pieces(int(qty), unit, stackable=stackable)
            base_id += 1
            data_orders.setdefault("orders", []).append({
                "id": base_id, "shop": "", "item": name,
                "requested": requested_pieces, "produced": 0,
                "status": "open", "claimed_by": None, "claims": [],
                "created_at": utcnow_iso(),
                "messages": {"channel_id": None, "message_id": None, "dms": {}},
                "unit_type": unit, "amount": int(qty),
                "stackable": bool(stackable), "stack_size": stack_size, "barrel_slots": 54,
                "employee_announce_at": announce_at.isoformat(),
                "employee_announced": False, "worker_announced": False,
                "priority_until": (now_utc + timedelta(hours=PRIORITY_HOURS)).isoformat(),
                "priority_role": EMPLOYEE_ROLE_NAME,
            })
            created.append(f"#{base_id} {name} × {qty} {unit}" + (" ⚠️unpriced" if price <= 0 else ""))
        if created:
            save_orders(data_orders)
        msg = f"✅ Created **{len(created)}** order(s)."
        if created:
            msg += "\n" + "\n".join(created[:20]) + (f"\n…and {len(created)-20} more." if len(created) > 20 else "")
        if unpriced:
            msg += (f"\n\n⚠️ {len(unpriced)} item(s) not in the catalog — posted at **price 0** "
                    f"(set a price before approving): " + ", ".join(f"`{u}`" for u in unpriced[:8]))
        if failed:
            msg += f"\n\n❌ Couldn't parse {len(failed)} line(s): " + " · ".join(f"`{f}`" for f in failed[:6])
        msg += f"\n\n⏱️ Cards post to the worker channel in ~{ANNOUNCE_DELAY_MINUTES} min."
        await interaction.followup.send(msg[:1950], **ephemeral_kwargs(interaction))

    @app_commands.command(name="ping_unclaimed", description="(Managers) Ping the Workers about unclaimed orders.")


    @app_commands.describe(limit="Ping only the N oldest unclaimed orders (0 = all)")
    @app_commands.default_permissions(manage_guild=True)
    async def ping_unclaimed(self, interaction: discord.Interaction, limit: int = 0):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))

        data = load_orders()
        unclaimed = [
            o for o in data.get("orders", [])

            if not _order_is_claimed_closed(o)
            and not o.get("claims")
        ]


        unclaimed = [o for o in unclaimed if not _priority_active(o)]

        if not unclaimed:
            return await interaction.response.send_message("✅ Nothing to ping: no unclaimed orders.", **ephemeral_kwargs(interaction))

        unclaimed.sort(key=lambda o: parse_iso(o.get("created_at", utcnow_iso())))
        if limit and limit > 0:
            unclaimed = unclaimed[:limit]

        channel = interaction.client.get_channel(WORKER_CHANNEL_ID)
        if not channel:
            return await interaction.response.send_message("⚠️ WORKER_CHANNEL_ID is not set to a valid channel.", **ephemeral_kwargs(interaction))

        role = discord.utils.get(channel.guild.roles, name=EMPLOYEE_ROLE_NAME)

        lines = []
        for o in unclaimed:
            rem = remaining_to_assign(o)
            lines.append(f"• **#{o['id']}** {o.get('item','')} · rem {fmt_qty(o, rem)}")

        mention = (role.mention + " ") if role else ""
        await channel.send(
            f"{mention}⏰ **Unclaimed orders need attention:**\n" + "\n".join(lines),
            allowed_mentions=discord.AllowedMentions(roles=True)
        )
        return await interaction.response.send_message(f"🔔 Pinged {len(unclaimed)} unclaimed order(s).", **ephemeral_kwargs(interaction))

    @app_commands.command(
        name="orders_resend",
        description="(Managers) Re-broadcast all open, unclaimed orders — fixes orders stuck as 'announced'."
    )
    @app_commands.default_permissions(manage_guild=True)
    async def orders_resend(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))
        data = load_orders()
        now_iso = utcnow_iso()
        n = 0
        for o in data.get("orders", []):
            if not isinstance(o, dict):
                continue
            # skip closed/cancelled and anything already claimed by a worker
            if _order_is_claimed_closed(o):
                continue
            if o.get("claims"):
                continue
            # clear the "already announced" flags and mark it due NOW so the
            # worker_announce_loop rebroadcasts the card + @Employee ping next tick
            o["worker_announced"] = False
            o["employee_announced"] = False
            o["employee_announce_at"] = now_iso
            n += 1
        save_orders(data)
        await interaction.response.send_message(
            f"🔁 Re-queued **{n}** open order(s) — they'll post to the worker channel within ~1 min.\n"
            f"(If nothing shows up, the announce loop can't reach the channel — tell me.)",
            **ephemeral_kwargs(interaction)
        )

    @app_commands.command(name="manager_panel", description="Open the Manager control panel")
    @app_commands.default_permissions(manage_guild=True)
    async def manager_panel(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        embed = discord.Embed(
            title="🛠️ Manager Panel",
            description=(
                "Use the buttons below:\n"
                "• **View Orders** → private list\n"
                "• **Prune Fulfilled/Cancelled** → removes closed orders\n"
                "• **Hive pickup status / clear** → hive pickup cleanup\n"
                "• **Set coin price** → edit item coin prices (**PER PIECE**)\n"
                "• **Funds report / Interest** → finance tools\n"
            ),
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, view=ManagerPanelView(), ephemeral=True)

    @app_commands.command(
        name="orders_clear_all",
        description="(Managers) DELETE ALL orders (testing only)."
    )


    @app_commands.describe(
        confirm="Type YES to confirm (required)"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def orders_clear_all(self, interaction: discord.Interaction, confirm: str):
        if not is_manager(interaction):
            return await interaction.response.send_message(
                "⛔ Managers only.",
                **ephemeral_kwargs(interaction)
            )

        if confirm.strip().upper() != "YES":
            return await interaction.response.send_message(
                "❌ Confirmation failed.\nType `YES` exactly to delete all orders.",
                **ephemeral_kwargs(interaction)
            )

        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)

        data = load_orders()
        orders = list(data.get("orders", []))
        total = len(orders)

        deleted_msgs = 0
        deleted_channels = 0


        for o in orders:

            try:
                msg_meta = o.get("messages") or {}
                ch_id = msg_meta.get("channel_id")
                msg_id = msg_meta.get("message_id")
                if ch_id and msg_id:
                    ch = interaction.client.get_channel(int(ch_id))
                    if ch:
                        msg = await ch.fetch_message(int(msg_id))
                        await msg.delete()
                        deleted_msgs += 1
            except Exception:
                pass


            try:
                vid = o.get("verification_ticket_id")
                if vid:
                    ch = interaction.client.get_channel(int(vid))
                    if ch:
                        await ch.delete(reason="Orders cleared (testing)")
                        deleted_channels += 1
            except Exception:
                pass


        data["orders"] = []
        save_orders(data)

        await interaction.followup.send(
            f"🧨 **ALL ORDERS DELETED**\n\n"
            f"• Orders removed: **{total}**\n"
            f"• Public messages deleted: **{deleted_msgs}**\n"
            f"• Verification channels deleted: **{deleted_channels}**\n\n"
            f"Ready for fresh testing ✅",
            **ephemeral_kwargs(interaction)
        )


async def setup(bot):
    await bot.add_cog(OrdersCog(bot))
