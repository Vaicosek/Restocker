"""Live inventory: barrel fullness, capacity, deficit restock, and owner stock alarms.
Split out of /market because Discord caps a command group at 25 subcommands."""
import sys

import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
_is_market_owner = core._is_market_owner
_get_market = core._get_market
_market_autocomplete = core._market_autocomplete
_fullness_bar = core._fullness_bar
_create_restock_orders = core._create_restock_orders
_load_items = core._load_items
STOCK_LOW_PCT = core.STOCK_LOW_PCT


class InventoryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    inventory = app_commands.Group(name="inventory",
                                   description="Live barrel stock: fullness, capacity, deficit restock, low-stock alarms")

    @inventory.command(name="stock", description="Live shop stock / barrel fullness for a market (lowest first)")
    @app_commands.describe(market_id="Which market", low_only="Show only low-stock items")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def stock(self, interaction: discord.Interaction, market_id: str, low_only: bool = False):
        import Restocker_db as _db
        st = _db.get_market_stock(market_id)
        if not st:
            return await interaction.response.send_message(
                f"No live stock for `{market_id}` yet — scan shops in-game (stock keybind) and upload `csn_stock_*.csv`.",
                ephemeral=True)

        def _pct(x):
            cap = int(x.get("capacity") or 0)
            return (100.0 * int(x.get("stock") or 0) / cap) if cap > 0 else 100.0
        items = sorted(st.values(), key=_pct)
        if low_only:
            items = [x for x in items if _pct(x) <= STOCK_LOW_PCT]
            if not items:
                return await interaction.response.send_message(
                    f"Nothing at/under {STOCK_LOW_PCT:g}% for `{market_id}`. ", ephemeral=True)
        m = _get_market(market_id) or {}
        lines = []
        for x in items[:25]:
            cap = int(x.get("capacity") or 0) or int(x.get("stock") or 0) or 1
            cur = int(x.get("stock") or 0)
            pct = 100.0 * cur / cap if cap else 0.0
            lines.append(f"`{_fullness_bar(pct)}` **{x['item']}** {cur:,}/{cap:,} ({pct:.0f}%)")
        embed = discord.Embed(title=f"\U0001F4E6 Stock — {m.get('name', market_id)}",
                              description="\n".join(lines), color=0x22FF7A)
        low_n = sum(1 for x in st.values() if _pct(x) <= STOCK_LOW_PCT and int(x.get("capacity") or 0) > 0)
        embed.set_footer(text=f"{len(st)} item(s) · {low_n} low (<= {STOCK_LOW_PCT:g}%) · capacity = highest stock seen")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # /inventory set_capacity removed 2026-07-15 (capacity is managed on the website).
    # _db.set_stock_capacity() stays for the scan path.

    @inventory.command(name="restock_deficit",
                    description="(Manager) Create restock orders from the real shortfall (capacity - current stock)")
    @app_commands.describe(market_id="Market", min_deficit="Only items short by at least this many pieces")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def restock_deficit(self, interaction: discord.Interaction, market_id: str, min_deficit: int = 1):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        import Restocker_db as _db
        st = _db.get_market_stock(market_id)
        if not st:
            return await interaction.response.send_message(f"No live stock for `{market_id}`.", ephemeral=True)
        known = (_load_items().get("items") or {})
        to_order = []
        skipped = 0
        for it, x in st.items():
            deficit = int(x.get("capacity") or 0) - int(x.get("stock") or 0)
            if deficit < max(1, int(min_deficit)):
                continue
            if it not in known:
                skipped += 1
                continue
            to_order.append((it, deficit, known[it]))
        if not to_order:
            return await interaction.response.send_message(
                f"Nothing short by >= {min_deficit} for `{market_id}`"
                + (f" ({skipped} not in catalog)." if skipped else "."), ephemeral=True)
        created = _create_restock_orders(to_order)
        top = ", ".join(f"{it} ({d:,})" for it, d, _ in sorted(to_order, key=lambda r: -r[1])[:8])
        await interaction.response.send_message(
            f"Created **{created}** restock order(s) from real deficit for `{market_id}`."
            + (f" {skipped} item(s) skipped (not in catalog)." if skipped else "")
            + f"\nTop shortfalls: {top}", ephemeral=True)


    @inventory.command(name="clear_stock",
                    description="(Manager) Delete a market's live stock rows (flush stale / mis-routed scans)")
    @app_commands.describe(
        market_id="Market to clear",
        confirm="Type the market_id again to confirm the deletion",
        since_minutes="Only delete rows updated within the last N minutes (0 = ALL rows for the market)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def clear_stock(self, interaction: discord.Interaction, market_id: str, confirm: str,
                          since_minutes: int = 0):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        if confirm.strip() != market_id.strip():
            return await interaction.response.send_message(
                f"⚠️ Confirmation failed. Re-run with `confirm:{market_id}` (exact) to delete its live stock.",
                ephemeral=True)
        from datetime import datetime, timezone, timedelta
        since_iso = None
        if since_minutes and since_minutes > 0:
            since_iso = (datetime.now(timezone.utc) - timedelta(minutes=int(since_minutes))).isoformat()
        import Restocker_db as _db
        n = _db.clear_market_stock(market_id, since_iso)
        window = f"updated in the last {since_minutes} min" if since_iso else "ALL rows"
        await interaction.response.send_message(
            f"🧹 Cleared **{n}** live-stock row(s) from `{market_id}` ({window}).", ephemeral=True)

    # /inventory set_alarm, alarms, clear_alarm removed 2026-07-15 — stock alarms are managed
    # on the website now. The DB layer (set_stock_alarm / get_stock_alarms / delete_stock_alarm)
    # and the alarm-checking loop are untouched, so existing alarms still fire; restore the
    # Discord commands from git history if wanted.


async def setup(bot):
    await bot.add_cog(InventoryCog(bot))
