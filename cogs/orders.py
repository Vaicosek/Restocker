"""Order / worker commands (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from datetime import datetime
from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
_market_autocomplete = core._market_autocomplete
cleanup_batch_dms_for_closed_order = core.cleanup_batch_dms_for_closed_order
_purge_worker_ping_messages = core._purge_worker_ping_messages
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
_markets_owned_by = core._markets_owned_by
_get_market = core._get_market
normal_item_autocomplete = core.normal_item_autocomplete
_is_future_item = core._is_future_item
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


# ── enchanted-gear name canonicalization ─────────────────────────────────────
# Old CSN/history data spells the same gear many ways: enchants in a different order,
# "Silk Touch" vs "Silk Touch I", and pre-1.13 lore names ("Damage All V" == Sharpness V,
# "Dig Speed" == Efficiency, "Durability" == Unbreaking). That made the /order autocomplete
# list the same item many times AND offer spellings that fail the catalog lookup
# ("… no longer exists"). Canonicalizing collapses every variant to one form.
_ENCH_SYN = {
    "efficiency": "Efficiency", "dig speed": "Efficiency",
    "fortune": "Fortune", "loot bonus blocks": "Fortune",
    "silk touch": "Silk Touch",
    "sharpness": "Sharpness", "damage all": "Sharpness",
    "smite": "Smite", "damage undead": "Smite",
    "bane of arthropods": "Bane of Arthropods", "damage arthropods": "Bane of Arthropods",
    "fire aspect": "Fire Aspect", "knockback": "Knockback",
    "looting": "Looting", "loot bonus mobs": "Looting",
    "protection": "Protection", "protect": "Protection",
    "unbreaking": "Unbreaking", "durability": "Unbreaking", "mending": "Mending",
    "sweeping edge": "Sweeping Edge", "sweeping": "Sweeping Edge",
    "power": "Power", "punch": "Punch", "flame": "Flame", "infinity": "Infinity",
    "lure": "Lure", "luck of the sea": "Luck of the Sea", "thorns": "Thorns",
    "respiration": "Respiration", "aqua affinity": "Aqua Affinity", "depth strider": "Depth Strider",
    "feather falling": "Feather Falling", "blast protection": "Blast Protection",
    "fire protection": "Fire Protection", "projectile protection": "Projectile Protection",
    "loyalty": "Loyalty", "impaling": "Impaling", "riptide": "Riptide", "channeling": "Channeling",
    "multishot": "Multishot", "quick charge": "Quick Charge", "piercing": "Piercing",
    "soul speed": "Soul Speed", "swift sneak": "Swift Sneak",
}
_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X"}


def _ench_level(rest: str) -> str:
    m = _re.search(r"\b(\d+)\b", rest)
    if m:
        return _ROMAN.get(int(m.group(1)), m.group(1))
    rm = _re.search(r"\b(x|ix|iv|v?i{1,3}|v)\b", rest)
    return rm.group(1).upper() if rm else ""


def _canon_ench(clause: str):
    low = " ".join((clause or "").strip().lower().split())
    if not low:
        return None, False
    for syn in sorted(_ENCH_SYN, key=len, reverse=True):
        if syn in low:
            name = _ENCH_SYN[syn]
            lvl = _ench_level(low.replace(syn, " ", 1))
            return (f"{name} {lvl}" if lvl and lvl != "I" else name), True
    return clause.strip(), False


def _canon_gear_name(name: str) -> str:
    """Collapse enchanted-gear name variants (enchant order, level-I notation, lore synonyms)
    to one canonical form. Names with no recognised enchant pass through unchanged."""
    s = " ".join((name or "").strip().split())
    if " - " not in s:
        return s
    prefix, ench_part = s.split(" - ", 1)
    canon, known = [], 0
    for cl in ench_part.split(","):
        if not cl.strip():
            continue
        c, is_known = _canon_ench(cl)
        if is_known:
            known += 1
        if c and c not in canon:
            canon.append(c)
    if known == 0:
        return s
    canon.sort()
    return f"{prefix} - {', '.join(canon)}"


async def _order_item_autocomplete(interaction: discord.Interaction, current: str):
    """/order item autocomplete: canonicalize gear names + de-duplicate so the same enchanted
    item is offered once (not once per old-data spelling) and the picked value resolves."""
    base = await normal_item_autocomplete(interaction, current)
    seen = {}
    for ch in base:
        canon = _canon_gear_name(ch.value)
        key = canon.lower()
        if key not in seen:
            seen[key] = app_commands.Choice(name=canon[:100], value=canon[:100])
    return list(seen.values())[:25]

_NONSTACK_KEYWORDS = ("pickaxe", "axe", "shovel", "sword", "hoe", "helmet", "chestplate",
                      "leggings", "boots", "set", "bow", "trident", "shield", "elytra", "fishing rod")

def _item_stackable(name: str, info: dict):
    """Auto-detect stackability (managers never set it): use the catalog flag if present,
    else infer from the name — tools/weapons/armor and bundled 'sets' don't stack; everything
    else defaults to stackable. Returns (stackable: bool, stack_size: int)."""
    info = info or {}
    sv = info.get("stackable")
    if sv is None:
        n = (name or "").lower()
        stackable = not any(k in n for k in _NONSTACK_KEYWORDS)
    else:
        stackable = bool(sv)
    try:
        stack_size = int(info.get("stack_size") or (64 if stackable else 1))
    except Exception:
        stack_size = 64 if stackable else 1
    return stackable, stack_size


# Single source of truth for the stock-refill plan (also used by the web ⚡ button and
# skips Future variants). Kept under the old name so the command/view below don't change.
_build_stock_refill_plan = core._stock_refill_plan


class _StockRefillConfirmView(discord.ui.View):
    """Confirm/Cancel gate before creating the drafted stock-refill orders."""
    def __init__(self, to_order, market_id, invoker_id, target_pct):
        super().__init__(timeout=120)
        self._to_order = to_order
        self._market_id = market_id
        self._invoker_id = invoker_id
        self._target_pct = target_pct

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message("This preview isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Create orders", style=discord.ButtonStyle.green)
    async def _confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        now_utc = datetime.now(timezone.utc)
        announce_at = next_batch_slot(ANNOUNCE_DELAY_MINUTES)
        data_orders = load_orders()
        base_id = max([o.get("id", 0) for o in data_orders.get("orders", [])] or [0])
        created = 0
        for item, need, info in self._to_order:
            stackable, stack_size = _item_stackable(item, info)
            base_id += 1
            data_orders.setdefault("orders", []).append({
                "id": base_id, "shop": "", "item": item,
                "requested": int(need), "produced": 0,
                "status": "open", "claimed_by": None, "claims": [],
                "created_at": utcnow_iso(),
                "messages": {"channel_id": None, "message_id": None, "dms": {}},
                "unit_type": "pieces", "amount": int(need),
                "stackable": bool(stackable), "stack_size": stack_size, "barrel_slots": 54,
                "employee_announce_at": announce_at.isoformat(),
                "employee_announced": False, "worker_announced": False,
                "priority_until": (now_utc + timedelta(hours=PRIORITY_HOURS)).isoformat(),
                "priority_role": EMPLOYEE_ROLE_NAME,
                "market_id": self._market_id,
            })
            created += 1
        if created:
            save_orders(data_orders)
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Created **{created}** restock order(s) for `{self._market_id}` "
                    f"(refill to {self._target_pct:g}%). Cards post to the worker channel "
                    f"in ~{ANNOUNCE_DELAY_MINUTES} min.",
            view=self)
        self.stop()

    @discord.ui.button(label="✖ Cancel", style=discord.ButtonStyle.grey)
    async def _cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="✖ Cancelled — no orders created.", view=self)
        self.stop()


_PAYOUT_REQ_RE = _re.compile(r"Requester:\s*<@!?(\d+)>")


def _earnings_rundown(user_id, max_lines: int = 12) -> str:
    # What a user actually DID to earn their coins — DISTINCT approved orders (deduped, since the
    # perf log can double-log a re-approval), each with item, qty, WHO approved it, and WHEN. Falls
    # back to raw order claims (flagged) when nothing was approved. Posted under the withdrawal card.
    try:
        import Restocker_db as _db
    except Exception:
        return ""
    uid = str(user_id)
    # Withdrawal requests need the requester's registered Minecraft IGN so the manager
    # knows exactly which in-game account to pay (and can spot an unregistered account).
    try:
        _ign = _db.get_ign(uid) or ""
    except Exception:
        _ign = ""
    ign_line = (f"\U0001f3ae IGN: `{_ign}`\n" if _ign
                else "\u26a0\ufe0f No IGN registered for this account\n")
    lines = []
    try:
        with _db.db() as conn:
            rows = conn.execute(
                "SELECT detail, MAX(manager_id) mgr, MIN(created_at) first "
                "FROM team_perf_log WHERE worker_id = ? AND kind = 'order' "
                "GROUP BY detail ORDER BY first", (uid,)).fetchall()
            for r in rows[:max_lines]:
                det = str(r["detail"] or "")
                item, qty = "", 0
                if det.lower().startswith("order#"):
                    try:
                        o = conn.execute(
                            "SELECT item, COALESCE(requested, amount, 0) q FROM orders WHERE id = ?",
                            (int(det.split("#", 1)[1]),)).fetchone()
                        if o:
                            item = str(o["item"] or "")
                            qty = int(o["q"] or 0)
                    except Exception:
                        pass
                nm = item[:46] if item else det
                q = f"{qty}× " if qty else ""
                # flag self-approval (a manager signing off their OWN order) — a governance red flag
                appr = f"<@{r['mgr']}>" + (" \u26a0 self-approved" if str(r["mgr"]) == uid else "")
                lines.append(f"• {det} · {q}{nm} · approved by {appr} · {str(r['first'])[:10]}")
            if not rows:
                cr = conn.execute(
                    "SELECT o.item item, COUNT(DISTINCT oc.order_id) n, SUM(oc.qty) q "
                    "FROM order_claims oc JOIN orders o ON o.id = oc.order_id WHERE oc.user_id = ? "
                    "GROUP BY o.item ORDER BY q DESC", (uid,)).fetchall()
                for r in cr[:max_lines]:
                    lines.append(f"• {int(r['q'] or 0)}× {r['item']} ({r['n']} order) — ⚠ no approval logged")
    except Exception:
        return ""
    if not lines:
        return (ign_line + "\u26a0\ufe0f **Work rundown: NOTHING recorded for this account** \u2014 no approved "
                "orders, harvests, or claims. This account has not earned via work \u2014 review "
                "carefully before paying.")
    return ign_line + "\U0001f4cb **Work rundown** \u2014 what they did to earn this:\n" + "\n".join(lines)


def _order_search_matches(query: str, orders: list[dict]) -> list[dict]:
    """Item name / IGN / Discord name-or-ID / order # search — shared by /search_orders and
    its autocomplete (and mirrors the 🔎 Search button's modal in views/orders.py). A bare
    '#<digits>' or plain-digits query does an EXACT id match instead of substring, so '#1'
    doesn't also pull in #10-#19, #100+, etc."""
    raw = (query or "").strip()
    q = raw.lower()
    if not q:
        return []
    m = _re.fullmatch(r"#?(\d+)", raw)
    if m:
        target = int(m.group(1))
        return [o for o in orders if isinstance(o, dict) and int(o.get("id", 0) or 0) == target]
    import Restocker_db as _db
    _ic = {}
    def _ign(uid):
        uid = str(uid or "")
        if uid and uid not in _ic:
            try:
                _ic[uid] = _db.get_ign(uid) or ""
            except Exception:
                _ic[uid] = ""
        return _ic.get(uid, "")
    matches = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        hay = [str(o.get("item", "")), str(o.get("id", "")), "#" + str(o.get("id", "")),
               str(o.get("status", "")), str(o.get("claimed_by", ""))]
        for c in (o.get("claims") or []):
            hay += [str(c.get("user_tag", "")), str(c.get("user_id", "")), _ign(c.get("user_id", ""))]
        if q in " ".join(hay).lower():
            matches.append(o)
    return matches


_ORDER_STATUS_BADGE = {"fulfilled": "✅ Fulfilled", "cancelled": "❌ Cancelled",
                        "claimed": "🟡 Claimed", "open": "⚪ Open"}


async def _order_search_autocomplete(interaction: discord.Interaction, current: str):
    """Live suggestions as you type — this is what a discord.ui.Modal text field CANNOT do
    (Discord's autocomplete only exists on slash-command options), which is why this search
    also exists as a real command and not just the 🔎 button's popup form."""
    current = (current or "").strip()
    if not current:
        return []
    try:
        data = load_orders()
        matches = _order_search_matches(current, data.get("orders", []) or [])
    except Exception:
        return []
    matches.sort(key=lambda o: int(o.get("id", 0) or 0), reverse=True)
    out = []
    for o in matches[:25]:
        st = str(o.get("status", "open")).lower()
        label = f"#{o.get('id')} {o.get('item', '')} · {_ORDER_STATUS_BADGE.get(st, st.capitalize())}"[:100]
        out.append(app_commands.Choice(name=label, value=f"#{o.get('id')}"))
    return out


class OrdersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener("on_message")
    async def _payout_rundown_listener(self, message):
        # Reply with a work rundown under the bot's own "Coins Withdrawal Request" card so a
        # manager can see what the requester actually did before approving. In a loaded cog, so
        # it needs zero edits to Restocker_main.
        try:
            if not self.bot.user or getattr(message.author, "id", 0) != self.bot.user.id:
                return
            content = message.content or ""
            if "Coins Withdrawal Request" not in content:
                return
            mt = _PAYOUT_REQ_RE.search(content)
            if not mt:
                return
            rundown = _earnings_rundown(mt.group(1))
            if rundown:
                await message.channel.send(rundown[:1990], allowed_mentions=discord.AllowedMentions.none())
        except Exception as _e:
            try:
                core.log.warning("[payout_rundown] %s", _e)
            except Exception:
                pass

    @commands.Cog.listener("on_message")
    async def _payout_proof_listener(self, message):
        """When a manager uploads an in-game payment screenshot inside a payout ticket,
        DM it to the employee and file it in the payment-proof archive channel. Runs in
        a loaded cog, so it needs zero edits to Restocker_main. The employee + amount are
        read from the ticket's own 'Coins Withdrawal Request' card."""
        try:
            if message.author.bot or message.guild is None:
                return
            ch = message.channel
            if not str(getattr(ch, "name", "")).startswith("payout-"):
                return
            imgs = [a for a in (message.attachments or [])
                    if (a.content_type or "").lower().startswith("image/")
                    or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))]
            if not imgs:
                return
            # Only a manager's upload counts as the payment proof.
            member = message.author if isinstance(message.author, discord.Member) else \
                (message.guild.get_member(message.author.id) if message.guild else None)
            is_mgr = False
            try:
                is_mgr = bool(member and core._ai_is_manager(member)) or \
                    int(getattr(message.author, "id", 0)) in getattr(core, "MANAGER_DM_IDS", set())
            except Exception:
                is_mgr = False
            if not is_mgr:
                return
            # Resolve the employee (+ amount) from the withdrawal card in this ticket.
            uid, amount = 0, 0
            try:
                async for m in ch.history(limit=30, oldest_first=True):
                    if self.bot.user and m.author.id == self.bot.user.id and "Coins Withdrawal Request" in (m.content or ""):
                        mu = _PAYOUT_REQ_RE.search(m.content or "")
                        ma = _re.search(r"Amount:\s*\*\*([\d,]+)\s*coins", m.content or "")
                        if mu:
                            uid = int(mu.group(1))
                        if ma:
                            amount = int(ma.group(1).replace(",", ""))
                        break
            except Exception:
                pass
            if not uid:
                await message.reply("⚠️ Couldn't tell who this payout is for (no requester card found), "
                                    "so I didn't forward the screenshot.",
                                    allowed_mentions=discord.AllowedMentions.none())
                return

            import io as _io
            import Restocker_db as _db
            amt_txt = f"{amount:,} coins" if amount else "coins"
            try:
                ign = _db.get_ign(str(uid)) or ""
            except Exception:
                ign = ""
            ign_txt = f" · IGN `{ign}`" if ign else ""
            when = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            caption = (f"💸 **Payment proof** — {amt_txt}{ign_txt}\n"
                       f"Paid to <@{uid}> · by {message.author.mention} · {when}")

            # Read each image once, fan out to the employee DM + the archive.
            dm_ok = False
            archived = 0
            arch_ch = None
            try:
                _cfg = _db.get_config("PAYMENT_PROOF_CHANNEL_ID")
                if _cfg:
                    arch_ch = self.bot.get_channel(int(_cfg))
            except Exception:
                arch_ch = None
            try:
                employee = await self.bot.fetch_user(uid)
            except Exception:
                employee = None
            for att in imgs:
                try:
                    raw = await att.read()
                except Exception:
                    continue
                if employee is not None:
                    try:
                        await employee.send(
                            content=(f"💸 Proof of your payout of **{amt_txt}** has been recorded. "
                                     f"Screenshot attached."),
                            file=discord.File(_io.BytesIO(raw), filename=att.filename))
                        dm_ok = True
                    except Exception:
                        pass
                if arch_ch is not None:
                    try:
                        await arch_ch.send(content=caption,
                                           file=discord.File(_io.BytesIO(raw), filename=att.filename),
                                           allowed_mentions=discord.AllowedMentions.none())
                        archived += 1
                    except Exception:
                        pass
            try:
                await message.add_reaction("📸")
            except Exception:
                pass
            bits = []
            bits.append("✅ DM'd to the employee" if dm_ok else "⚠️ couldn't DM the employee (DMs closed?)")
            if arch_ch is not None:
                bits.append(f"archived {archived} shot(s) in {arch_ch.mention}")
            else:
                bits.append("no archive channel set (`/config set_channel` → *Payment-proof archive channel*)")
            await message.reply("📸 Payment screenshot: " + "; ".join(bits) + ". "
                                "Click **🗑 Close ticket** when done.",
                                allowed_mentions=discord.AllowedMentions.none())
        except Exception as _e:
            try:
                core.log.warning("[payout_proof] %s", _e)
            except Exception:
                pass

    @app_commands.command(name="orders", description="Show open production requests")
    async def orders(self, interaction: discord.Interaction):
        return await orders_cmd(interaction)

    @app_commands.command(
        name="search_orders",
        description="Search orders by item, IGN, Discord name/ID, or order # — shows live matches as you type",
    )
    @app_commands.describe(query="Item name, IGN, Discord name/ID, or order # (e.g. shovel · jzlr · #17)")
    @app_commands.autocomplete(query=_order_search_autocomplete)
    async def search_orders(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(**ephemeral_kwargs(interaction))
        try:
            data = load_orders()
            matches = _order_search_matches(query, data.get("orders", []) or [])
        except Exception as e:
            return await interaction.followup.send(
                f"❌ Search failed: `{type(e).__name__}: {e}`", **ephemeral_kwargs(interaction)
            )
        if not matches:
            return await interaction.followup.send(f"🔎 No orders match **{query}**.", **ephemeral_kwargs(interaction))
        matches.sort(key=lambda o: int(o.get("id", 0) or 0), reverse=True)
        import Restocker_db as _db
        _ic = {}
        def _ign(uid):
            uid = str(uid or "")
            if uid and uid not in _ic:
                try:
                    _ic[uid] = _db.get_ign(uid) or ""
                except Exception:
                    _ic[uid] = ""
            return _ic.get(uid, "")
        lines = []
        for o in matches[:25]:
            st = str(o.get("status", "open")).lower()
            cl = o.get("claims") or []
            who = ""
            if cl:
                who = " · " + ", ".join(
                    f"{(_ign(c.get('user_id', '')) or c.get('user_tag', '') or '?')} ({int(c.get('qty', 0) or 0)})"
                    for c in cl[:3]
                )
            lines.append(f"• **#{o.get('id')}** {o.get('item', '')} · {_ORDER_STATUS_BADGE.get(st, st.capitalize())}{who}")
        head = f"🔎 **{len(matches)} order(s) matching \"{query}\"**" + (" — showing 25" if len(matches) > 25 else "")
        await interaction.followup.send((head + "\n" + "\n".join(lines))[:1990], **ephemeral_kwargs(interaction))

    @app_commands.command(name="cancel_order", description="(Managers) Cancel an existing restock order by ID")


    @app_commands.describe(order_id="The ID of the order to cancel")


    @app_commands.autocomplete(order_id=order_id_autocomplete)
    async def cancel_order(self, interaction: discord.Interaction, order_id: int):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ You need the @Managers role to cancel orders.", **ephemeral_kwargs(interaction))

        # Defer up front: update_order_messages() makes several Discord API calls
        # (edit/delete order cards) that can exceed the 3-second interaction window,
        # which caused "404 Not Found (10062): Unknown interaction" when we replied
        # afterwards. Deferring gives us up to 15 min; all replies use followup.
        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)

        data = load_orders()
        order = next((o for o in data["orders"] if o["id"] == order_id), None)
        if not order:
            return await interaction.followup.send(f"❌ Order #{order_id} not found.", **ephemeral_kwargs(interaction))
        if order["status"] == "fulfilled":
            return await interaction.followup.send(
                f"⚠️ Order #{order_id} is already fulfilled and cannot be cancelled.", **ephemeral_kwargs(interaction)
            )
        if order["status"] == "cancelled":
            return await interaction.followup.send(f"⚠️ Order #{order_id} is already cancelled.", **ephemeral_kwargs(interaction))

        order["status"] = "cancelled"
        save_orders(data)
        await update_order_messages(interaction.client, order)
        await interaction.followup.send(f"❌ Order #{order_id} has been cancelled.", **ephemeral_kwargs(interaction))

    @app_commands.command(
        name="order",
        description="(Managers / market owners) Order an item from workers — everyone, or DM one specific worker"
    )
    @app_commands.describe(
        item_key="Pick an existing catalog item (type to search)",
        amount="How many (in the unit you choose)",
        unit_type="Choose pieces, stacks, or barrels",
        worker="Optional: assign directly to ONE worker (DMs only them, no mass ping). Blank = ask all workers."
    )
    @app_commands.choices(unit_type=[
        app_commands.Choice(name="Pieces", value="pieces"),
        app_commands.Choice(name="Stacks", value="stacks"),
        app_commands.Choice(name="Barrels", value="barrels"),
    ])
    @app_commands.autocomplete(item_key=_order_item_autocomplete)
    async def order(self,
        interaction: discord.Interaction,
        item_key: str,
        amount: int,
        unit_type: str,
        worker: Optional[discord.Member] = None,
    ):
        # Managers can order anything; a market owner/leader can order for their own
        # market(s) too (no more manager bottleneck). We know the final permission only
        # after resolving the item's market below — here we just reject users who are
        # neither a manager nor any market's owner.
        _is_mgr = is_manager(interaction)
        _owned_markets = _markets_owned_by(interaction.user.id)
        if not _is_mgr and not _owned_markets:
            return await interaction.response.send_message(
                "⛔ You need the @Managers role, or to be a market owner, to create orders.",
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
        if _is_future_item(item):
            return await interaction.response.send_message(
                f"❌ **{item}** is a futures item — use `/futures_order` for that. "
                f"`/order` is for normal (in-stock) items.",
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
            # Resolve enchanted-gear name variants (order / level-I / lore synonyms) to
            # the real catalog key so a canonicalized pick still matches an item stored
            # under a different spelling instead of failing with "no longer exists".
            _cn = _canon_gear_name(item)
            _match = _cn if _cn in items else {_canon_gear_name(k): k for k in items}.get(_cn)
            if _match:
                item = _match
            else:
                return await interaction.followup.send(
                    f"❌ Item **{item}** no longer exists.",
                    **ephemeral_kwargs(interaction)
                )

        info = items.get(item) or {}
        if not isinstance(info, dict):
            info = {}

        # Market scope: a non-manager market owner may only order items in a market they
        # own. Managers may order anything. Tag the order with the item's market so
        # per-market loyalty rewards can key off it.
        item_mid = str(info.get("market_id", "main") or "main")
        if not _is_mgr and item_mid not in _owned_markets:
            _own_str = ", ".join(f"`{m}`" for m in sorted(_owned_markets)) or "—"
            return await interaction.followup.send(
                f"⛔ **{item}** belongs to market `{item_mid}`, which you don't own. "
                f"You can only order items in your market(s): {_own_str}.",
                **ephemeral_kwargs(interaction)
            )

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

        # Stackability is auto-detected per item — managers never set it. Uses the catalog
        # flag if present, else infers from the name (tools/armor/sets don't stack).
        stackable, stack_size = _item_stackable(item, info)

        unit = str(unit_type).lower().strip()
        if unit not in ("pieces", "stacks", "barrels"):
            unit = "pieces"

        requested_pieces = unit_to_pieces(int(amount), unit, stackable=stackable, stack_size=stack_size)

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
                "market_id": item_mid,
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
                "market_id": item_mid,
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
        description="(Managers / market owners) Create many orders at once from a pasted list")
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
        _is_mgr = is_manager(interaction)
        _owned_markets = _markets_owned_by(interaction.user.id)
        if not _is_mgr and not _owned_markets:
            return await interaction.response.send_message(
                "⛔ You need the @Managers role, or to be a market owner, to create orders.",
                **ephemeral_kwargs(interaction))
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
        created, unpriced, failed, skipped_market = [], [], [], []
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
            if _is_future_item(name):
                failed.append(f"{name[:48]} → use /futures_order")
                continue
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
                stackable, stack_size = _item_stackable(name, info)
                item_mid = str(info.get("market_id", "main") or "main")
            else:
                price, stackable, stack_size = 0, False, 1   # lenient: unknown item still posts (price 0)
                item_mid = "main"
                unpriced.append(name)
            # A non-manager market owner can only bulk-order items in a market they own.
            if not _is_mgr and item_mid not in _owned_markets:
                skipped_market.append(f"{name} (`{item_mid}`)")
                continue
            requested_pieces = unit_to_pieces(int(qty), unit, stackable=stackable, stack_size=stack_size)
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
                "market_id": item_mid,
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
        if skipped_market:
            msg += (f"\n\n⛔ Skipped {len(skipped_market)} item(s) not in your market(s): "
                    + ", ".join(skipped_market[:8]))
        msg += f"\n\n⏱️ Cards post to the worker channel in ~{ANNOUNCE_DELAY_MINUTES} min."
        await interaction.followup.send(msg[:1950], **ephemeral_kwargs(interaction))

    # /order_from_stock removed 2026-07-15 — superseded by the website "My Market" order
    # builder (Stage 2) and /inventory restock_deficit. _build_stock_refill_plan /
    # _StockRefillConfirmView remain defined but unused; restore from git history if wanted.

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
        description="(Managers) Post all open order cards straight to the worker channel — no mass ping."
    )
    @app_commands.default_permissions(manage_guild=True)
    async def orders_resend(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))
        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)

        # Post the cards DIRECTLY, bypassing the background announce loop (which is
        # silently swallowing its post errors). Also surfaces the real error if any.
        channel = interaction.client.get_channel(WORKER_CHANNEL_ID)
        if channel is None:
            return await interaction.followup.send(
                f"❌ `get_channel({WORKER_CHANNEL_ID})` returned **nothing** — the bot can't see the worker "
                f"channel even though the ID is right and it has Administrator. That's the actual bug "
                f"(missing Guilds intent, or the channel isn't cached).",
                **ephemeral_kwargs(interaction)
            )

        data = load_orders()
        # Resend re-posts every order that is NOT closed (open OR claimed-but-unfulfilled),
        # so a manager can always force the cards back into the channel — the old
        # "and not o.get('claims')" wrongly skipped anything already claimed.
        open_orders = [
            o for o in data.get("orders", [])
            if isinstance(o, dict) and not _order_is_claimed_closed(o)
        ]
        open_orders.sort(key=lambda o: int(o.get("id", 0) or 0))

        posted, errors = 0, []
        for o in open_orders:
            # Post the CARD directly now (worker side done -> loop won't double-post it),
            # but leave the employee-DM side OPEN and due now, so the (now-fixed) employee
            # batch-DM loop sends the DM digest to every @Employee.
            o["worker_announced"] = True
            o["employee_announced"] = False
            o["employee_announce_at"] = utcnow_iso()
            try:
                await update_order_messages(interaction.client, o, allow_post=True)
                posted += 1
            except Exception as e:
                errors.append(f"#{o.get('id')}: {type(e).__name__}: {e}")
        # Do NOT save the whole pre-loop snapshot: the posting loop awaits Discord per order,
        # and a claim made meanwhile would be clobbered back to unclaimed (save_orders upserts
        # every row). Re-load fresh state and merge ONLY the fields this command changed.
        fresh = load_orders()
        by_id = {int(x.get("id", 0) or 0): x for x in (fresh.get("orders") or []) if isinstance(x, dict)}
        for o in open_orders:
            f = by_id.get(int(o.get("id", 0) or 0))
            if f is not None:
                f["worker_announced"] = o.get("worker_announced", True)
                f["employee_announced"] = o.get("employee_announced", False)
                f["employee_announce_at"] = o.get("employee_announce_at")
                f["messages"] = o.get("messages") or f.get("messages")
        save_orders(fresh)

        msg = (f"📮 Posted **{posted}/{len(open_orders)}** order card(s) to <#{WORKER_CHANNEL_ID}>, "
               f"and queued the **@Employee DM digest** — it goes out within ~1 min.")
        if errors:
            msg += "\n\n⚠️ Real errors (this is what the loop was hiding):\n" + "\n".join(f"`{e}`" for e in errors[:8])
        elif posted == 0:
            msg += "\n\n(No open orders to post.)"
        await interaction.followup.send(msg[:1900], **ephemeral_kwargs(interaction))

    @app_commands.command(name="manager_panel", description="Open the Manager control panel")
    @app_commands.default_permissions(manage_guild=True)
    async def manager_panel(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        embed = discord.Embed(
            title="🛠️ Manager Panel",
            description=(
                "Use the buttons below:\n"
                "• **View Orders** → full order list (same as `/orders`)\n"
                "• **Escalate order…** → repost/bump an order to workers\n"
                "• **Prune Cancelled** → deletes cancelled orders (fulfilled kept as history)"
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
        deleted_dms = 0

        import asyncio as _aio
        client = interaction.client

        for o in orders:

            # Delete the employee DMs this order sent (messages.dms = {user_id: message_id}).
            # A bot can delete its own DMs; do this BEFORE the records are wiped, since the
            # message IDs live inside the order record. Throttled to respect rate limits.
            try:
                dms = ((o.get("messages") or {}).get("dms") or {})
                for uid_str, mid in list(dms.items()):
                    try:
                        user = client.get_user(int(uid_str)) or await client.fetch_user(int(uid_str))
                        if not user:
                            continue
                        dm = user.dm_channel or await user.create_dm()
                        msg = await dm.fetch_message(int(mid))
                        await msg.delete()
                        deleted_dms += 1
                        await _aio.sleep(0.35)
                    except Exception:
                        pass
            except Exception:
                pass

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
        # prune=True is REQUIRED: since the SQLite migration, save_orders only upserts the
        # rows present in the list — saving an empty list without prune deletes NOTHING,
        # leaving every order alive while their Discord messages/tickets are already gone.
        save_orders(data, prune=True)

        # Refresh/delete the interactive "New Production Requests (batch)" digest DMs — a
        # separate per-employee message (tracked in the UI store), NOT in each order's
        # messages.dms. With no orders left, every digest is deleted.
        try:
            await cleanup_batch_dms_for_closed_order(interaction.client, 0)
        except Exception:
            pass
        # And the plain-text "🔔 New restock requests:" pings (channel + DMs), which are sent
        # un-tracked — removed by history scan. No id filter: the board is empty now.
        try:
            _pc, _pd = await _purge_worker_ping_messages(interaction.client, None)
            deleted_msgs += _pc
            deleted_dms += _pd
        except Exception:
            pass

        await interaction.followup.send(
            f"🧨 **ALL ORDERS DELETED**\n\n"
            f"• Orders removed: **{total}**\n"
            f"• Public messages deleted: **{deleted_msgs}**\n"
            f"• Employee DMs deleted: **{deleted_dms}**\n"
            f"• Verification channels deleted: **{deleted_channels}**\n\n"
            f"Ready for fresh testing ✅",
            **ephemeral_kwargs(interaction)
        )

    @app_commands.command(
        name="orders_purge",
        description="(Managers) Delete only a scoped batch of orders — by age / ID range / market. Keeps the rest."
    )
    @app_commands.describe(
        confirm="Type YES to actually delete. Anything else = preview only (shows counts, deletes nothing).",
        since_minutes="Only orders created within the last N minutes (e.g. 60 = the last hour).",
        market_id="Only orders tagged this market (optional).",
        min_id="Only orders with ID ≥ this (optional).",
        max_id="Only orders with ID ≤ this (optional).",
        clear_dms="Also sweep leftover 'New restock requests' digests + pings from the worker channel and DMs.",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def orders_purge(self, interaction: discord.Interaction, confirm: str = "no",
                           since_minutes: Optional[int] = None,
                           market_id: Optional[str] = None,
                           min_id: Optional[int] = None,
                           max_id: Optional[int] = None,
                           clear_dms: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))
        has_filter = not (since_minutes is None and market_id is None and min_id is None and max_id is None)
        if not has_filter and not clear_dms:
            return await interaction.response.send_message(
                "❌ Give at least one filter (`since_minutes` / `market_id` / `min_id` / `max_id`), "
                "or set `clear_dms:True` to just sweep leftover announcement DMs. "
                "To wipe the whole board, that's `/orders_clear_all`.", **ephemeral_kwargs(interaction))
        await interaction.response.defer(**ephemeral_kwargs(interaction), thinking=True)
        client = interaction.client
        data = load_orders()
        orders = list(data.get("orders", []) or [])
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=int(since_minutes))) if since_minutes else None
        mid_f = str(market_id).strip() if market_id else None

        def _match(o):
            try:
                oid = int(o.get("id", 0) or 0)
            except Exception:
                oid = 0
            if min_id is not None and oid < int(min_id):
                return False
            if max_id is not None and oid > int(max_id):
                return False
            if mid_f is not None and str(o.get("market_id") or "") != mid_f:
                return False
            if cutoff is not None:
                try:
                    ts = parse_iso(o.get("created_at"))
                except Exception:
                    ts = None
                if ts is None:
                    return False
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    return False
            return True

        matched = [o for o in orders if _match(o)] if has_filter else []
        ids = sorted(int(o.get("id", 0) or 0) for o in matched)
        id_span = (f"#{ids[0]}–#{ids[-1]}" if len(ids) > 1 else (f"#{ids[0]}" if ids else "—"))

        if not matched and not clear_dms:
            return await interaction.followup.send("No orders match that filter.", **ephemeral_kwargs(interaction))

        # Anything destructive is gated behind confirm:YES — preview otherwise.
        if confirm.strip().upper() != "YES":
            bits = []
            if matched:
                sample = ", ".join(f"#{i}" for i in ids[:25]) + (" …" if len(ids) > 25 else "")
                bits.append(f"**{len(matched)}** order(s) ({id_span}) — DMs, posts, tickets, records:\n{sample}")
            if clear_dms:
                bits.append("sweep leftover **restock-request digests + pings** from the worker channel and every employee DM")
            return await interaction.followup.send(
                "🔍 **Preview** — would " + "; and ".join(bits)
                + f"\n\nRe-run with **`confirm:YES`** to do it. Your other **{len(orders) - len(matched)}** order(s) stay.",
                **ephemeral_kwargs(interaction))

        import asyncio as _aio
        deleted_dms = deleted_msgs = deleted_channels = 0
        for o in matched:
            try:
                dms = ((o.get("messages") or {}).get("dms") or {})
                for uid_str, m_id in list(dms.items()):
                    try:
                        user = client.get_user(int(uid_str)) or await client.fetch_user(int(uid_str))
                        if not user:
                            continue
                        dm = user.dm_channel or await user.create_dm()
                        msg = await dm.fetch_message(int(m_id))
                        await msg.delete()
                        deleted_dms += 1
                        await _aio.sleep(0.35)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                mm = o.get("messages") or {}
                ch_id, msg_id = mm.get("channel_id"), mm.get("message_id")
                if ch_id and msg_id:
                    ch = client.get_channel(int(ch_id))
                    if ch:
                        msg = await ch.fetch_message(int(msg_id))
                        await msg.delete()
                        deleted_msgs += 1
            except Exception:
                pass
            try:
                vid = o.get("verification_ticket_id")
                if vid:
                    ch = client.get_channel(int(vid))
                    if ch:
                        await ch.delete(reason="Order purge (scoped)")
                        deleted_channels += 1
            except Exception:
                pass

        kept = len(orders)
        if matched:
            match_ids = {int(o.get("id", 0) or 0) for o in matched}
            data["orders"] = [o for o in orders if int(o.get("id", 0) or 0) not in match_ids]
            save_orders(data, prune=True)
            kept = len(data["orders"])

        # Sweep the batch digests + plain-text pings. When clear_dms is set we sweep ALL restock
        # pings (no id filter) — needed to catch a stale ping whose order is already gone;
        # otherwise only pings referencing the purged ids.
        try:
            await cleanup_batch_dms_for_closed_order(client, 0)
        except Exception:
            pass
        try:
            ping_ids = None if clear_dms else {int(o.get("id", 0) or 0) for o in matched}
            _pc, _pd = await _purge_worker_ping_messages(client, ping_ids)
            deleted_msgs += _pc
            deleted_dms += _pd
        except Exception:
            pass

        head = (f"🧹 **Purged {len(matched)} order(s)** ({id_span})."
                if matched else "🧹 **Swept leftover announcement DMs/pings.**")
        await interaction.followup.send(
            head + "\n"
            f"• Posts/pings deleted: **{deleted_msgs}**\n"
            f"• Employee DMs deleted: **{deleted_dms}**\n"
            f"• Verification channels deleted: **{deleted_channels}**\n"
            f"• Kept: **{kept}** other order(s).",
            **ephemeral_kwargs(interaction))


async def setup(bot):
    await bot.add_cog(OrdersCog(bot))
