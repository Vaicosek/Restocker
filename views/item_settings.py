"""ItemPanel — one `/item` command instead of three picker rows.

Grouping /add_item, /item_info and /item_edit under an `/item` GROUP did not help: Discord
renders every subcommand as its own row, so the picker still showed three. A single
command opening a panel is the only shape that actually collapses them to one.

Look-up is open to everyone — workers price things constantly and most of them are not on
the AI allow-list, so putting it behind the bot would have locked them out. Add and Edit
are manager-gated and their buttons are not rendered for anyone else.
"""
import sys

import discord

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log
BARREL_PIECES = getattr(core, "BARREL_PIECES", 54)


def _items() -> dict:
    return (core._load_items().get("items") or {})


def _is_mgr(user) -> bool:
    try:
        return bool(core._ai_is_manager(user))
    except Exception:
        return False


def _resolve(name: str):
    """Exact, then case-insensitive, then unambiguous partial — same order the AI tool uses."""
    items = _items()
    if name in items:
        return name, None
    low = next((k for k in items if k.lower() == name.lower()), None)
    if low:
        return low, None
    hits = [k for k in items if name.lower() in k.lower()]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, ("❓ Multiple items match: " + ", ".join(f"`{h}`" for h in hits[:8])
                      + ("…" if len(hits) > 8 else "") + "\nBe more specific.")
    return None, f"❌ No item named `{name}`."


def info_embed(key: str) -> discord.Embed:
    info = _items().get(key) or {}
    coin = info.get("coin", 0)
    ss = int(info.get("stack_size", 1 if not info.get("stackable", True) else 64) or 1)
    mid = info.get("market_id", "main")
    try:
        mname = (core._get_market(mid) or {}).get("name", mid)
    except Exception:
        mname = mid
    e = discord.Embed(title=f"📦 {key}", color=0x3498DB)
    e.add_field(name="Price/piece", value=f"`{coin}¢`", inline=True)
    e.add_field(name="Price/barrel", value=f"`{coin * BARREL_PIECES * ss:,}¢`", inline=True)
    e.add_field(name="Stock", value=f"`{info.get('stock', 0)}`", inline=True)
    e.add_field(name="Market", value=f"`{mname}`", inline=True)
    e.add_field(name="Barrel size",
                value=f"`{BARREL_PIECES} slots × {ss} = {BARREL_PIECES * ss} items`", inline=True)
    e.add_field(name="Stackable", value=(f"`yes · ×{ss}`" if ss > 1 else "`no · single`"), inline=True)
    if info.get("worker_cost"):
        e.add_field(name="Worker cost", value=f"`{int(info['worker_cost'])}¢`/piece", inline=True)
    return e


def build_embed(user) -> discord.Embed:
    items = _items()
    e = discord.Embed(
        title="📦 Items",
        description=f"**{len(items):,}** items in the catalog.\nUse the buttons below.",
        color=0x3498DB)
    if not _is_mgr(user):
        e.set_footer(text="Look-up is open to everyone. Adding and editing are manager-only.")
    return e


class _SearchModal(discord.ui.Modal, title="Find an item"):
    """Step 1 of 2. Modals cannot autocomplete — that is a hard Discord limit — so the
    only way to keep type-ahead inside a panel is: type a fragment here, then pick the
    real name from a Select. With 554 catalog items, asking anyone to recall an exact
    name like 'Diamond Sword - Fire Aspect II, Sharpness V, Unbreaking III' is hopeless.
    """

    def __init__(self, panel, mode: str):
        super().__init__(timeout=300)
        self.panel, self.mode = panel, mode
        self.q = discord.ui.TextInput(
            label="Search (part of the name is enough)", required=True,
            placeholder="e.g. sword, fortune, honey")
        self.add_item(self.q)

    async def on_submit(self, i: discord.Interaction):
        q = str(self.q.value or "").strip().lower()
        hits = [k for k in _items() if q in k.lower()]
        if not hits:
            return await i.response.send_message(
                f"❌ Nothing matches `{q}`.", ephemeral=True)
        if len(hits) == 1:
            return await _open(i, self.panel, hits[0], self.mode)
        hits.sort(key=lambda k: (len(k), k))
        more = ("\n\n…and %d more — narrow the search." % (len(hits) - 25)) if len(hits) > 25 else ""
        await i.response.send_message(
            f"**{len(hits)}** match `{q}` — pick one:{more}",
            view=_PickItemView(self.panel, i.user.id, hits[:25], self.mode), ephemeral=True)


class _PickItemView(discord.ui.View):
    def __init__(self, panel, user_id: int, names: list, mode: str):
        super().__init__(timeout=300)
        self.panel, self.user_id, self.mode = panel, int(user_id), mode
        sel = discord.ui.Select(
            placeholder="Pick the item…",
            options=[discord.SelectOption(label=n[:100], value=str(n_i))
                     for n_i, n in enumerate(names)])
        self._names = names

        async def pick(i: discord.Interaction):
            await _open(i, self.panel, self._names[int(sel.values[0])], self.mode)
        sel.callback = pick
        self.add_item(sel)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True


async def _open(i: discord.Interaction, panel, key: str, mode: str):
    """Show info, or open the edit modal prefilled for the chosen item."""
    if mode == "info":
        return await i.response.send_message(embed=info_embed(key), ephemeral=True)
    if not _is_mgr(i.user):
        return await i.response.send_message("⛔ Managers only.", ephemeral=True)
    await i.response.send_modal(_EditModal(panel, key))

class _LookupModal(discord.ui.Modal, title="Look up an item"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.name = discord.ui.TextInput(label="Item name (partial is fine)", required=True)
        self.add_item(self.name)

    async def on_submit(self, i: discord.Interaction):
        key, err = _resolve(str(self.name.value or "").strip())
        if err:
            return await i.response.send_message(err, ephemeral=True)
        await i.response.send_message(embed=info_embed(key), ephemeral=True)


class _EditModal(discord.ui.Modal, title="Edit an item"):
    """Was /item edit. The item is already CHOSEN by the time this opens — the name field
    is gone, and current values are prefilled so nothing has to be remembered. Blank means
    "leave unchanged"."""

    def __init__(self, panel, key: str):
        super().__init__(timeout=300)
        self.panel, self.key = panel, key
        self.title = f"Edit — {key}"[:45]
        info = _items().get(key) or {}
        self.coin = discord.ui.TextInput(
            label="Price per piece (blank = keep)", required=False,
            default=str(info.get("coin", "") or ""))
        self.per_stack = discord.ui.TextInput(
            label="Price is per stack of 64? yes/no", required=False, default="no")
        self.stackable = discord.ui.TextInput(
            label="Stackable? yes / no / a number (blank=keep)", required=False)
        self.worker_cost = discord.ui.TextInput(label="Worker cost per piece (blank = keep)",
                                                required=False)
        for f in (self.coin, self.per_stack, self.stackable, self.worker_cost):
            self.add_item(f)

    async def on_submit(self, i: discord.Interaction):
        if not _is_mgr(i.user):
            return await i.response.send_message("⛔ Managers only.", ephemeral=True)
        key = self.key
        raw_coin = str(self.coin.value or "").strip().replace(",", "")
        per_stack = str(self.per_stack.value or "").strip().lower() in ("yes", "y", "true", "1")
        raw_stk = str(self.stackable.value or "").strip().lower()
        raw_wc = str(self.worker_cost.value or "").strip().replace(",", "")
        if not raw_coin and not raw_stk and not raw_wc:
            return await i.response.send_message(
                "❌ Nothing to change — fill in at least one field.", ephemeral=True)
        if per_stack and not raw_coin:
            return await i.response.send_message(
                "❌ `per stack` only means something alongside a price.", ephemeral=True)

        # Route through the AI tool's handler so the two paths can never diverge — it
        # already does the per-stack division, the twin sync and the YAML+DB mirror.
        payload = {"name": key}
        if raw_coin:
            try:
                payload["price"] = float(raw_coin)
            except Exception:
                return await i.response.send_message("❌ Price must be a number.", ephemeral=True)
            payload["per_stack"] = per_stack
        else:
            payload["price"] = float((_items().get(key) or {}).get("coin", 0) or 0)
        if raw_stk:
            if raw_stk in ("yes", "y", "true"):
                payload["stackable"] = True
            elif raw_stk in ("no", "n", "false"):
                payload["stackable"] = False
            elif raw_stk.isdigit():
                payload["stack_size"] = int(raw_stk)
            else:
                return await i.response.send_message(
                    "❌ Stackable must be yes, no, or a number.", ephemeral=True)
        if raw_wc:
            if not raw_wc.isdigit():
                return await i.response.send_message("❌ Worker cost must be a whole number.",
                                                     ephemeral=True)
            payload["worker_cost"] = int(raw_wc)

        msg = await core._ai_tool_set_item_price(i.guild, i.channel, i.user, payload)
        await i.response.send_message(msg, ephemeral=True)


class _AddModal(discord.ui.Modal, title="Add a catalog item"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.name = discord.ui.TextInput(label="Exact item name", required=True)
        self.coin = discord.ui.TextInput(label="Price per piece", required=True)
        self.market = discord.ui.TextInput(label="Market id (blank = main)", required=False)
        self.stackable = discord.ui.TextInput(
            label="Stackable? yes / no / a number", required=False, default="yes")
        self.add_item(self.name); self.add_item(self.coin)
        self.add_item(self.market); self.add_item(self.stackable)

    async def on_submit(self, i: discord.Interaction):
        if not _is_mgr(i.user):
            return await i.response.send_message("⛔ Managers only.", ephemeral=True)
        name = str(self.name.value or "").strip()
        if name in _items():
            return await i.response.send_message(
                f"❌ `{name}` already exists — use **Edit** instead.", ephemeral=True)
        try:
            coin = int(float(str(self.coin.value or "0").strip().replace(",", "")))
        except Exception:
            return await i.response.send_message("❌ Price must be a number.", ephemeral=True)
        raw = str(self.stackable.value or "yes").strip().lower()
        ss = 64 if raw in ("yes", "y", "true", "") else (1 if raw in ("no", "n", "false")
                                                         else (int(raw) if raw.isdigit() else None))
        if ss is None:
            return await i.response.send_message(
                "❌ Stackable must be yes, no, or a number.", ephemeral=True)
        mid = str(self.market.value or "").strip() or "main"
        msg = await core._ai_tool_add_item(i.guild, i.channel, i.user, {
            "name": name, "price": coin, "market_id": mid,
            "stackable": ss > 1, "stack_size": ss})
        await i.response.send_message(msg, ephemeral=True)


class ItemPanelView(discord.ui.View):
    def __init__(self, user_id: int, is_manager: bool = False, key: str = None):
        super().__init__(timeout=300)
        self.user_id = int(user_id)
        # Set when /item was called WITH an autocompleted name: Edit then skips the
        # search step entirely and opens straight on that item.
        self.key = key
        b = discord.ui.Button(label="Look up", style=discord.ButtonStyle.primary, row=0)
        b.callback = self._lookup
        self.add_item(b)
        if is_manager:
            for label, cb, style in (("Add item", self._add, discord.ButtonStyle.success),
                                     ("Edit item", self._edit, discord.ButtonStyle.secondary)):
                x = discord.ui.Button(label=label, style=style, row=0)
                x.callback = cb
                self.add_item(x)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    async def _lookup(self, i): await i.response.send_modal(_SearchModal(self, "info"))

    async def _add(self, i):
        if not _is_mgr(i.user):
            return await i.response.send_message("⛔ Managers only.", ephemeral=True)
        await i.response.send_modal(_AddModal(self))

    async def _edit(self, i):
        if not _is_mgr(i.user):
            return await i.response.send_message("⛔ Managers only.", ephemeral=True)
        if self.key:
            return await i.response.send_modal(_EditModal(self, self.key))
        await i.response.send_modal(_SearchModal(self, "edit"))
