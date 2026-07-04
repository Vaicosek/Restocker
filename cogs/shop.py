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
any_item_autocomplete = core.any_item_autocomplete
ephemeral_kwargs = core.ephemeral_kwargs
is_manager = core.is_manager
load_orders = core.load_orders
save_orders = core.save_orders
update_order_messages = core.update_order_messages

class ShopCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="shop_rename_item", description="Rename an item in items.yml (updates open orders).")


    @app_commands.describe(item_key="Pick the item to rename (type to search)", new_name="New item name")


    @app_commands.autocomplete(item_key=any_item_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def shop_rename_item(self, interaction: discord.Interaction, item_key: str, new_name: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", **ephemeral_kwargs(interaction))

        old_name = (item_key or "").strip()
        new_name = (new_name or "").strip()

        if not old_name:
            return await interaction.response.send_message("❌ Invalid item selection.", **ephemeral_kwargs(interaction))
        if not new_name:
            return await interaction.response.send_message("❌ New name can’t be empty.", **ephemeral_kwargs(interaction))


        try:
            data_items = _load_items()
            items = data_items.setdefault("items", {})

            if old_name not in items:
                return await interaction.response.send_message(f"❌ `{old_name}` not found.", **ephemeral_kwargs(interaction))
            if new_name in items and new_name != old_name:
                return await interaction.response.send_message(f"❌ `{new_name}` already exists.", **ephemeral_kwargs(interaction))

            items[new_name] = items.pop(old_name)
            _save_items(data_items)
        except Exception as e:
            return await interaction.response.send_message(f"❌ Failed to update items.yml: {e}", **ephemeral_kwargs(interaction))


        updated_orders = 0
        try:
            data = load_orders()

            for o in data.get("orders", []):
                if _order_is_claimed_closed(o):
                    continue
                if o.get("item") == old_name:
                    o["item"] = new_name
                    updated_orders += 1

            save_orders(data)

            for o in data.get("orders", []):
                if _order_is_claimed_closed(o):
                    continue
                if o.get("item") == new_name:
                    try:
                        await update_order_messages(interaction.client, o)
                    except Exception:
                        pass

        except Exception as e:
            return await interaction.response.send_message(
                f"✅ Renamed in **items.yml**.\n⚠️ But updating orders failed: {e}",
                **ephemeral_kwargs(interaction)
            )

        await interaction.response.send_message(
            f"✅ Renamed **{old_name}** → **{new_name}**.\n"
            f"🔁 Updated **{updated_orders}** open order(s).",
            **ephemeral_kwargs(interaction)
        )

    @app_commands.command(name="add_item", description="Create a new item and set its coin price")
    @app_commands.checks.has_any_role(MANAGER_ROLE_NAME)
    @app_commands.describe(item="Item name", coin="Coin price per piece (integer)")
    @app_commands.default_permissions(manage_guild=True)
    async def add_item(self, interaction: discord.Interaction, item: str, coin: int):
        item_name = item.strip()
        if not item_name:
            return await interaction.response.send_message("❌ Item name cannot be empty.", **ephemeral_kwargs(interaction))
        if coin < 0:
            return await interaction.response.send_message("❌ Coin must be ≥ 0.", **ephemeral_kwargs(interaction))

        shops = _load_items()
        items = shops.setdefault("items", {})

        if item_name in items:
            return await interaction.response.send_message(f"❌ Item `{item_name}` already exists. Use `/set_price` to update it.", **ephemeral_kwargs(interaction))

        items[item_name] = {"stock": 0, "coin": int(coin)}
        _save_items(shops)

        return await interaction.response.send_message(
            f"✅ Created `{item_name}` for **{coin} coins/piece**. (stock starts at 0)",
            **ephemeral_kwargs(interaction)
        )

    @app_commands.command(name="set_price", description="Set the coin price for an existing item (per piece or per stack of 64)")
    @app_commands.checks.has_any_role(MANAGER_ROLE_NAME)
    @app_commands.describe(
        item="Item name",
        coin="Price amount",
        per_stack="Set to True if the price is per stack of 64 (bot divides by 64 automatically)"
    )
    @app_commands.autocomplete(item=any_item_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def set_price(self, interaction: discord.Interaction, item: str, coin: int,
                        per_stack: bool = False):
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
        _save_items(shops)

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

        if per_val == "stack":
            await interaction.response.send_message(
                f"✅ **{item}** price set: `{coin}¢/stack` → `{coin_per_piece}¢/piece` "
                f"(barrel = `{round(coin_per_piece * BARREL_PIECES * stack_size):,}¢`).",
                **ephemeral_kwargs(interaction)
            )
        else:
            await interaction.response.send_message(
                f"✅ **{item}** price updated: `{old_price}¢` → `{coin_per_piece}¢` per piece "
                f"(barrel = `{round(coin_per_piece * BARREL_PIECES * stack_size):,}¢`).",
                **ephemeral_kwargs(interaction)
            )

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

        embed = discord.Embed(title=f"📦 {item}", color=0x3498DB)
        embed.add_field(name="Price/piece", value=f"`{coin}¢`", inline=True)
        embed.add_field(name="Price/barrel", value=f"`{barrel_price:,}¢`", inline=True)
        embed.add_field(name="Stock", value=f"`{stock}`", inline=True)
        embed.add_field(name="Market", value=f"`{market_name}`", inline=True)
        embed.add_field(name="Barrel size", value=f"`{BARREL_PIECES} slots × {stack_size} = {BARREL_PIECES * stack_size} items`", inline=True)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(ShopCog(bot))
