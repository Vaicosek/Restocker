"""Stock-exchange UI (extracted from Restocker_main)."""
import re
import sys
import discord
from discord import app_commands, Embed
from discord.ui import View, Button, Select

from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
_build_stock_panel_embed = core._build_stock_panel_embed
_exec_stock_buy = core._exec_stock_buy
_exec_stock_sell = core._exec_stock_sell
_panel_market_from_message = core._panel_market_from_message
_re_alarm = re.compile(r"mkt:(\S+)")
_alarm_triggered_items = core._alarm_triggered_items
_create_restock_orders = core._create_restock_orders
_load_items = core._load_items
is_manager = core.is_manager
_markets_owned_by = core._markets_owned_by

class StockTradeModal(discord.ui.Modal):
    """Popup for buying/selling an arbitrary number of shares."""

    def __init__(self, market_id: str, side: str, panel_message=None):
        super().__init__(title=f"{'Buy' if side == 'buy' else 'Sell'} shares")
        self.market_id = market_id
        self.side = side
        self.panel_message = panel_message
        self.amount = discord.ui.TextInput(
            label="How many shares?",
            placeholder="e.g. 250",
            required=True, max_length=9,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.amount.value or "").strip().replace(",", "").replace(" ", "")
        try:
            qty = int(raw)
        except ValueError:
            return await interaction.response.send_message("❌ Enter a whole number of shares.", ephemeral=True)
        if qty <= 0:
            return await interaction.response.send_message("❌ Shares must be a positive number.", ephemeral=True)
        fn = _exec_stock_buy if self.side == "buy" else _exec_stock_sell
        ok, msg = fn(interaction.user.id, self.market_id, qty, interaction.user.display_name)
        await interaction.response.send_message(msg, ephemeral=True)
        if self.panel_message is not None:
            try:
                await self.panel_message.edit(embed=_build_stock_panel_embed(self.market_id))
            except Exception:
                pass


class StockPanelView(discord.ui.View):
    """Interactive, restart-persistent buy/sell panel. Trades execute for the
    clicking user; the public embed updates with the new price; the per-user
    result is sent privately. Constructed with no market_id for persistent
    registration — each callback recovers the market from the panel message."""

    def __init__(self, market_id: str | None = None):
        super().__init__(timeout=None)
        self.market_id = market_id

    def _mid(self, interaction: discord.Interaction) -> Optional[str]:
        return self.market_id or _panel_market_from_message(interaction)

    async def _trade(self, interaction: discord.Interaction, side: str, qty: int):
        mid = self._mid(interaction)
        if not mid:
            return await interaction.response.send_message("❌ Couldn't identify this market.", ephemeral=True)
        fn = _exec_stock_buy if side == "buy" else _exec_stock_sell
        ok, msg = fn(interaction.user.id, mid, qty, interaction.user.display_name)
        try:
            await interaction.response.edit_message(embed=_build_stock_panel_embed(mid), view=self)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            try:
                await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

    async def _open_modal(self, interaction: discord.Interaction, side: str):
        mid = self._mid(interaction)
        if not mid:
            return await interaction.response.send_message("❌ Couldn't identify this market.", ephemeral=True)
        await interaction.response.send_modal(StockTradeModal(mid, side, interaction.message))

    @discord.ui.button(label="Buy 1", style=discord.ButtonStyle.success, row=0, custom_id="stk:buy:1")
    async def buy1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trade(interaction, "buy", 1)

    @discord.ui.button(label="Buy 10", style=discord.ButtonStyle.success, row=0, custom_id="stk:buy:10")
    async def buy10(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trade(interaction, "buy", 10)

    @discord.ui.button(label="Buy 100", style=discord.ButtonStyle.success, row=0, custom_id="stk:buy:100")
    async def buy100(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trade(interaction, "buy", 100)

    @discord.ui.button(label="Buy…", style=discord.ButtonStyle.secondary, row=0, custom_id="stk:buy:x")
    async def buyx(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_modal(interaction, "buy")

    @discord.ui.button(label="Sell 1", style=discord.ButtonStyle.danger, row=1, custom_id="stk:sell:1")
    async def sell1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trade(interaction, "sell", 1)

    @discord.ui.button(label="Sell 10", style=discord.ButtonStyle.danger, row=1, custom_id="stk:sell:10")
    async def sell10(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trade(interaction, "sell", 10)

    @discord.ui.button(label="Sell 100", style=discord.ButtonStyle.danger, row=1, custom_id="stk:sell:100")
    async def sell100(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._trade(interaction, "sell", 100)

    @discord.ui.button(label="Sell…", style=discord.ButtonStyle.secondary, row=1, custom_id="stk:sell:x")
    async def sellx(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_modal(interaction, "sell")

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2, custom_id="stk:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = self._mid(interaction)
        try:
            await interaction.response.edit_message(embed=_build_stock_panel_embed(mid), view=self)
        except Exception:
            await interaction.response.defer()


class StockAlarmView(discord.ui.View):
    """Owner-facing stock alarm: 'Create restock orders' builds the /order set from
    the items currently past the owner's alarm (recomputed at click -> persistent-safe);
    'Acknowledge' just dismisses. Registered persistently; recovers the market from the
    embed footer marker `mkt:<id>` when no market_id is set."""

    def __init__(self, market_id: str = None):
        super().__init__(timeout=None)
        self.market_id = market_id

    def _mid(self, interaction: discord.Interaction):
        if self.market_id:
            return self.market_id
        try:
            for e in (interaction.message.embeds or []):
                txt = (e.footer.text if e.footer else "") or ""
                m = _re_alarm.search(txt)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    @discord.ui.button(label="🛒 Create restock orders", style=discord.ButtonStyle.success,
                       custom_id="stockalarm_create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = self._mid(interaction)
        if not mid:
            return await interaction.response.send_message("Couldn't determine the market.", ephemeral=True)
        # The alarm can land in a public channel (owner-DM fallback) and this view is
        # persistent — without a gate ANY member could click and spawn payable restock
        # orders for someone else's market. Owner/manager of THIS market only.
        if not (is_manager(interaction) or mid in _markets_owned_by(interaction.user.id)):
            return await interaction.response.send_message(
                "⛔ Only this market's owner or a manager can create these orders.", ephemeral=True)
        trig = _alarm_triggered_items(mid)
        known = (_load_items().get("items") or {})
        to_order = [(t["item"], t["deficit"], known[t["item"]])
                    for t in trig if t["deficit"] > 0 and t["item"] in known]
        if not to_order:
            return await interaction.response.send_message(
                "Nothing to order right now (stock recovered, or items not in catalog).", ephemeral=True)
        created = _create_restock_orders(to_order)
        top = ", ".join(f"{it} ({d:,})" for it, d, _ in sorted(to_order, key=lambda r: -r[1])[:8])
        try:
            await interaction.response.edit_message(
                content=f"🛒 Created **{created}** restock order(s): {top}", view=None)
        except Exception:
            await interaction.response.send_message(f"🛒 Created **{created}** restock order(s).", ephemeral=True)

    @discord.ui.button(label="✅ Acknowledge", style=discord.ButtonStyle.secondary,
                       custom_id="stockalarm_dismiss")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = self._mid(interaction)
        if mid and not (is_manager(interaction) or mid in _markets_owned_by(interaction.user.id)):
            return await interaction.response.send_message(
                "⛔ Only this market's owner or a manager can dismiss its alarm.", ephemeral=True)
        try:
            await interaction.response.edit_message(content="🔕 Acknowledged — no orders created.", view=None)
        except Exception:
            await interaction.response.send_message("Noted.", ephemeral=True)


class CaptableImportModal(discord.ui.Modal, title="Import cap table → holdings"):
    """Paste a Crimson Banking cap-table export (`account,discord_id,name,shares,`) and set
    this market's bot-exchange holders to MATCH it exactly: entities aggregate per Discord
    user, holders missing from the export are zeroed, shares_outstanding is set to the
    export total, and holder display names are remembered for the website cap table.
    Cost basis for imported shares is 0 (they were acquired on Crimson, not via the bot)."""

    def __init__(self, market_id: str):
        super().__init__(timeout=600)
        self.market_id = str(market_id)
        self.blob = discord.ui.TextInput(
            label=f"Cap-table export for {market_id}",
            style=discord.TextStyle.paragraph, required=True, max_length=3900,
            placeholder="10771,1203738126850461738,vaicos,36000,\n10537,558683020471697408,Fikcraft,799,")
        self.add_item(self.blob)

    async def on_submit(self, interaction: discord.Interaction):
        import Restocker_db as _db
        rows = core._parse_crimson_captable(str(self.blob.value or ""))
        if not rows:
            return await interaction.response.send_message(
                "❌ Couldn't parse any holder lines — paste the raw CSV block from the "
                "Crimson cap-table report.", ephemeral=True)
        listing = _db.get_market_shares(self.market_id)
        if not listing:
            return await interaction.response.send_message(
                f"❌ `{self.market_id}` isn't a listed stock — take it public first.", ephemeral=True)
        # Liquidated holders (left for good — /investor liquidate) don't get their lines
        # back: their shares reroute to the market owner (stock returns to the company).
        liq = core._liquidated_holders()
        owner_id = None
        try:
            owner_id = str(core._market_owner_id(self.market_id) or "") or None
        except Exception:
            pass
        reclaimed, reclaimed_names = 0.0, []
        target = {}
        for uid, name, sh in rows:
            uid = str(uid)
            if uid in liq and owner_id and uid != owner_id:
                reclaimed += float(sh)
                reclaimed_names.append(f"{name} ({sh:,.0f})")
                uid = owner_id
            else:
                try:
                    core._remember_holder_name(int(uid), name)
                except Exception:
                    pass
            target[uid] = target.get(uid, 0.0) + float(sh)
        current = {str(h.get("user_id")): float(h.get("shares") or 0)
                   for h in (_db.get_holders(self.market_id) or [])}
        total = sum(target.values())
        changed = 0
        for uid, sh in target.items():
            cur = current.pop(uid, 0.0)
            delta = float(sh) - cur
            if abs(delta) > 1e-9:
                _db.adjust_holding(uid, self.market_id, delta_shares=delta, delta_cost_basis=0.0)
                changed += 1
        zeroed = 0
        for uid, cur in current.items():
            if cur > 0:
                _db.adjust_holding(uid, self.market_id, delta_shares=-cur, delta_cost_basis=0.0)
                zeroed += 1
        _db.upsert_market_shares(self.market_id, shares_outstanding=float(total))
        msg = (f"📥 Imported cap table for `{self.market_id}`: **{len(target)}** holder(s) "
               f"({total:,.0f} shares, entities merged per Discord account). "
               f"Adjusted {changed}, zeroed {zeroed} stale holder(s), shares outstanding set to "
               f"{total:,.0f}.")
        if reclaimed_names:
            msg += (f"\n🧹 Liquidated → market owner: {reclaimed:,.0f} shares from "
                    + ", ".join(reclaimed_names))
        msg += ("\nThe website cap table now mirrors Crimson. Cost basis for imported "
                "shares is 0 — P/L shows as pure gain until they trade through the bot.")
        await interaction.response.send_message(msg[:1900], ephemeral=True)
