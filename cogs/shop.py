"""Shop / catalog commands (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
BARREL_PIECES = core.BARREL_PIECES
MANAGER_ROLE_NAME = core.MANAGER_ROLE_NAME
_get_market = core._get_market
_load_items = core._load_items
_order_is_claimed_closed = core._order_is_claimed_closed
_save_items = core._save_items
_detect_stack_size = core._detect_stack_size
_is_future_item = core._is_future_item
_sync_twin_price = core._sync_twin_price
any_item_autocomplete = core.any_item_autocomplete
ephemeral_kwargs = core.ephemeral_kwargs
is_manager = core.is_manager
load_orders = core.load_orders
log = core.log
save_orders = core.save_orders
update_order_messages = core.update_order_messages

class ShopCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # /shop_rename_item was REMOVED (owner decision after the 2026-07 audit): renaming
    # an item key orphaned its references in consignment deals, stock scans, restock
    # targets and alarms. Delete + re-add under the new name instead.

    @app_commands.command(name="add_item", description="Create a new item and set its coin price")
    @app_commands.checks.has_any_role(MANAGER_ROLE_NAME)
    @app_commands.describe(
        item="Item name", coin="Coin price per piece (integer)",
        stackable="Override auto-detect: True = stacks to 64, False = single (potions, tools, jetpacks). Blank = auto.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def add_item(self, interaction: discord.Interaction, item: str, coin: int,
                       stackable: bool | None = None):
        item_name = item.strip()
        if not item_name:
            return await interaction.response.send_message("❌ Item name cannot be empty.", **ephemeral_kwargs(interaction))
        if coin < 0:
            return await interaction.response.send_message("❌ Coin must be ≥ 0.", **ephemeral_kwargs(interaction))

        shops = _load_items()
        items = shops.setdefault("items", {})

        if item_name in items:
            return await interaction.response.send_message(f"❌ Item `{item_name}` already exists. Use `/item_set_price` to update it.", **ephemeral_kwargs(interaction))

        # Auto-detect the real Minecraft stack size from the name (potions/brews, tools,
        # jetpacks etc. → 1) so barrels aren't sized 64× too big. An explicit `stackable`
        # override wins.
        detected = _detect_stack_size(item_name)
        if stackable is True:
            ss = 64
        elif stackable is False:
            ss = 1
        else:
            ss = detected
        items[item_name] = {"stock": 0, "coin": int(coin), "stackable": ss > 1, "stack_size": ss}
        _save_items(shops)

        kind = "stacks to 64" if ss >= 64 else (f"stacks to {ss}" if ss > 1 else "non-stackable (single)")
        barrel = int(coin) * BARREL_PIECES * ss
        return await interaction.response.send_message(
            f"✅ Created `{item_name}` for **{coin} coins/piece** — **{kind}** "
            f"(barrel = {barrel:,}¢). Stock starts at 0."
            + ("" if stackable is not None else "\n*(stackability auto-detected — pass `stackable:` to override)*"),
            **ephemeral_kwargs(interaction)
        )

    @app_commands.command(name="item_set_price", description="Set the coin price for an existing item (per piece or per stack of 64)")
    @app_commands.checks.has_any_role(MANAGER_ROLE_NAME)
    @app_commands.describe(
        item="Item name",
        coin="Price amount",
        per_stack="Set to True if the price is per stack of 64 (bot divides by 64 automatically)",
        worker_cost="(Optional) per-piece break-even cost for consignment futures. -1 = leave unchanged.",
    )
    @app_commands.autocomplete(item=any_item_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def set_price(self, interaction: discord.Interaction, item: str, coin: int,
                        per_stack: bool = False, worker_cost: int = -1):
        if coin < 0:
            return await interaction.response.send_message("❌ Coin must be ≥ 0.", **ephemeral_kwargs(interaction))

        per_val = "stack" if per_stack else "piece"
        stack_size = 64
        if per_val == "stack":
            coin_per_piece = round(coin / stack_size, 4)
        else:
            coin_per_piece = coin

        shops = _load_items()
        items = shops.setdefault("items", {})
        if item not in items:
            return await interaction.response.send_message(
                f"❌ Item `{item}` not found. Use `/add_item` to create it.", **ephemeral_kwargs(interaction))

        old_price = items[item].get("coin", 0)
        items[item]["coin"] = coin_per_piece
        if per_val == "stack":
            items[item]["stackable"] = True
            items[item]["stack_size"] = stack_size
        if worker_cost is not None and worker_cost >= 0:
            items[item]["worker_cost"] = int(worker_cost)
        _save_items(shops)
        if worker_cost is not None and worker_cost >= 0:
            try:
                import Restocker_db as _db_wc
                _db_wc.set_item_worker_cost(item, int(worker_cost))
            except Exception:
                pass

        try:
            import Restocker_db as _db_sp2
            with _db_sp2.db() as _conn:
                if per_val == "stack":
                    _conn.execute("UPDATE items SET coin=?, stackable=1, stack_size=64 WHERE name=?",
                                  (coin_per_piece, item))
                else:
                    _conn.execute("UPDATE items SET coin=? WHERE name=?", (coin_per_piece, item))
        except Exception:
            pass

        # Keep the normal ↔ Future twin at the same price so paired items don't drift.
        twin = _sync_twin_price(item, coin_per_piece)
        twin_note = f"\n↔️ Also synced its twin **{twin}** to the same price." if twin else ""

        if per_val == "stack":
            await interaction.response.send_message(
                f"✅ **{item}** price set: `{coin}¢/stack` → `{coin_per_piece}¢/piece` "
                f"(barrel = `{round(coin_per_piece * BARREL_PIECES * stack_size):,}¢`).{twin_note}",
                **ephemeral_kwargs(interaction)
            )
        else:
            await interaction.response.send_message(
                f"✅ **{item}** price updated: `{old_price}¢` → `{coin_per_piece}¢` per piece "
                f"(barrel = `{round(coin_per_piece * BARREL_PIECES * stack_size):,}¢`).{twin_note}",
                **ephemeral_kwargs(interaction)
            )

    @app_commands.command(name="item_edit", description="Edit an existing item's price and/or stackability")
    @app_commands.checks.has_any_role(MANAGER_ROLE_NAME)
    @app_commands.describe(
        item="Item to edit",
        coin="(Optional) new coin price per piece. Leave blank to keep the current price.",
        stackable="(Optional) True = stacks to 64, False = single piece (potions, tools, armor sets). Blank = keep.",
        stack_size="(Optional) exact stack size (advanced, e.g. 16). Leave -1 to derive from stackable.",
    )
    @app_commands.autocomplete(item=any_item_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def item_edit(self, interaction: discord.Interaction, item: str,
                        coin: int | None = None, stackable: bool | None = None,
                        stack_size: int = -1):
        if coin is not None and coin < 0:
            return await interaction.response.send_message("❌ Coin must be ≥ 0.", **ephemeral_kwargs(interaction))
        if coin is None and stackable is None and stack_size < 0:
            return await interaction.response.send_message(
                "❌ Nothing to change — pass `coin:` and/or `stackable:`.", **ephemeral_kwargs(interaction))

        shops = _load_items()
        items = shops.setdefault("items", {})
        if item not in items:
            return await interaction.response.send_message(
                f"❌ Item `{item}` not found. Use `/add_item` to create it.", **ephemeral_kwargs(interaction))
        entry = items[item]
        changes = []

        # ── stackability (explicit stack_size wins over the True/False toggle) ──
        new_ss = None
        if stack_size >= 1:
            new_ss = int(stack_size)
        elif stackable is True:
            new_ss = 64
        elif stackable is False:
            new_ss = 1
        if new_ss is not None:
            entry["stackable"] = new_ss > 1
            entry["stack_size"] = new_ss
            changes.append("stackable → **" + (f"yes, ×{new_ss}" if new_ss > 1 else "no (single)") + "**")

        # ── price ──
        new_coin = None
        if coin is not None:
            old_price = entry.get("coin", 0)
            new_coin = int(coin)
            entry["coin"] = new_coin
            changes.append(f"price `{old_price}¢` → `{new_coin}¢`/piece")

        _save_items(shops)

        # Mirror to the DB items table (the JSON store above is the catalog source of
        # truth; the items table is the twin the orders/stock paths read).
        try:
            import Restocker_db as _db_ie
            with _db_ie.db() as _conn:
                if new_coin is not None:
                    _conn.execute("UPDATE items SET coin=? WHERE name=?", (new_coin, item))
                if new_ss is not None:
                    _conn.execute("UPDATE items SET stackable=?, stack_size=? WHERE name=?",
                                  (1 if new_ss > 1 else 0, new_ss, item))
        except Exception:
            pass

        # Keep the normal ↔ Future twin at the same price when price changed.
        twin_note = ""
        if new_coin is not None:
            twin = _sync_twin_price(item, new_coin)
            if twin:
                twin_note = f"\n↔️ Synced its twin **{twin}** to the same price."

        eff_ss = int(entry.get("stack_size", 1) or 1)
        eff_coin = entry.get("coin", 0)
        barrel = round(eff_coin * BARREL_PIECES * eff_ss)
        return await interaction.response.send_message(
            f"✅ **{item}** updated: " + "; ".join(changes) +
            f".\nBarrel now = **{BARREL_PIECES} slots × {eff_ss} = {BARREL_PIECES * eff_ss} items** → `{barrel:,}¢`."
            + twin_note,
            **ephemeral_kwargs(interaction)
        )

    # /fix_stacks and /pair_items removed 2026-07-15 — one-time catalog cleanup tools.
    # _detect_stack_size and the twin-pairing logic (_sync_twin_price) still live in core for
    # the normal add/price paths; restore these two commands from git history if a bulk
    # re-scan is ever needed again.

    @app_commands.command(name="item_info", description="Look up the price and stock of an item")
    @app_commands.describe(item="Item name to look up")
    @app_commands.autocomplete(item=any_item_autocomplete)
    async def item_info(self, interaction: discord.Interaction, item: str):
        shops = _load_items()
        items = shops.get("items", {})
        if item not in items:
            return await interaction.response.send_message(f"❌ Item `{item}` not found.", **ephemeral_kwargs(interaction))
        info = items[item]
        coin = info.get("coin", 0)
        stock = info.get("stock", 0)
        market_id = info.get("market_id", "main")
        market = _get_market(market_id)
        market_name = (market or {}).get("name", market_id) if market else market_id

        stack_size = info.get("stack_size", 1 if not info.get("stackable", True) else 64)
        barrel_price = coin * BARREL_PIECES * stack_size
        is_stackable = stack_size > 1

        embed = discord.Embed(title=f"📦 {item}", color=0x3498DB)
        embed.add_field(name="Price/piece", value=f"`{coin}¢`", inline=True)
        embed.add_field(name="Price/barrel", value=f"`{barrel_price:,}¢`", inline=True)
        embed.add_field(name="Stock", value=f"`{stock}`", inline=True)
        embed.add_field(name="Market", value=f"`{market_name}`", inline=True)
        embed.add_field(name="Barrel size", value=f"`{BARREL_PIECES} slots × {stack_size} = {BARREL_PIECES * stack_size} items`", inline=True)
        embed.add_field(name="Stackable",
                        value=(f"`yes · ×{stack_size}`" if is_stackable else "`no · single`"), inline=True)
        embed.set_footer(text="Managers: /item_edit to change price or stackability")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(ShopCog(bot))
