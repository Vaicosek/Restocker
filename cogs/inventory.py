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

    @inventory.command(name="set_capacity", description="(Manager/Owner) Set an item's barrel capacity (defines 'full')")
    @app_commands.describe(market_id="Market", item="Exact item name", capacity="Full capacity in pieces")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def set_capacity(self, interaction: discord.Interaction, market_id: str, item: str,
                           capacity: app_commands.Range[int, 1, 100_000_000]):
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)):
            return await interaction.response.send_message("Managers / market owner only.", ephemeral=True)
        import Restocker_db as _db
        _db.set_stock_capacity(market_id, item, int(capacity))
        await interaction.response.send_message(
            f"Set **{item}** capacity to `{capacity:,}` for `{market_id}`. Fullness is now measured against it.",
            ephemeral=True)

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

    @inventory.command(name="set_alarm",
                    description="(Owner/Manager) Alarm that pings you + preps a restock when an item runs low")
    @app_commands.describe(market_id="Market", item="Exact item name, or '*' for a market-wide default",
                           threshold="Trigger at/under this value", mode="'pct' (of capacity) or 'pieces'")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def set_alarm(self, interaction: discord.Interaction, market_id: str, item: str,
                        threshold: app_commands.Range[float, 0, 100_000_000], mode: str = "pct"):
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)):
            return await interaction.response.send_message("Managers / market owner only.", ephemeral=True)
        mode = (mode or "pct").lower().strip()
        if mode not in ("pct", "pieces"):
            return await interaction.response.send_message("mode must be `pct` or `pieces`.", ephemeral=True)
        if mode == "pct" and threshold > 100:
            return await interaction.response.send_message("For `pct` mode, threshold must be 0–100.", ephemeral=True)
        item = item.strip()
        import Restocker_db as _db
        _db.set_stock_alarm(market_id, item, float(threshold), mode)
        scope = "market-wide default" if item == "*" else f"**{item}**"
        unit = "%" if mode == "pct" else " pcs"
        await interaction.response.send_message(
            f"🔔 Alarm set for {scope} on `{market_id}`: pings you at/under `{threshold:g}{unit}` "
            f"and preps a restock you can create or dismiss.", ephemeral=True)

    @inventory.command(name="alarms", description="List a market's stock alarms")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def alarms(self, interaction: discord.Interaction, market_id: str):
        import Restocker_db as _db
        al = _db.get_stock_alarms(market_id)
        if not al:
            return await interaction.response.send_message(
                f"No alarms on `{market_id}`. Set one with `/market set_alarm`.", ephemeral=True)
        lines = []
        for it, a in sorted(al.items(), key=lambda kv: (kv[0] != "*", kv[0])):
            unit = "%" if a["mode"] == "pct" else " pcs"
            name = "★ default (all items)" if it == "*" else it
            lines.append(f"• {name} — at/under `{a['threshold']:g}{unit}`")
        m = _get_market(market_id) or {}
        embed = discord.Embed(title=f"🔔 Stock alarms — {m.get('name', market_id)}",
                              description="\n".join(lines), color=0xE5A13A)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @inventory.command(name="clear_alarm", description="(Owner/Manager) Remove a stock alarm")
    @app_commands.describe(market_id="Market", item="Item name, or '*' for the default")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def clear_alarm(self, interaction: discord.Interaction, market_id: str, item: str):
        if not (is_manager(interaction) or _is_market_owner(interaction, market_id)):
            return await interaction.response.send_message("Managers / market owner only.", ephemeral=True)
        import Restocker_db as _db
        _db.delete_stock_alarm(market_id, item.strip())
        await interaction.response.send_message(
            f"Cleared alarm for {'default' if item.strip()=='*' else item} on `{market_id}`.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(InventoryCog(bot))
