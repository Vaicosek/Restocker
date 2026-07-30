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

    @app_commands.command(name="item", description="Items — look up a price, or add/edit one")
    @app_commands.describe(item="Start typing to search the catalog (optional)")
    @app_commands.autocomplete(item=any_item_autocomplete)
    async def item_panel(self, interaction: discord.Interaction, item: str = None):
        """ONE picker row. An app_commands.Group would not have helped: Discord renders
        every subcommand as its own row, so /item add|info|edit still showed three.

        The optional `item` argument keeps REAL type-ahead: modals cannot autocomplete,
        so the fastest path to one item is the command's own autocomplete. Pass nothing
        and you get the panel, which searches instead."""
        from views.item_settings import ItemPanelView, build_embed, info_embed, _resolve
        q = (item or "").strip()
        if q:
            key, err = _resolve(q)
            if err:
                return await interaction.response.send_message(err, ephemeral=True)
            return await interaction.response.send_message(
                embed=info_embed(key),
                view=ItemPanelView(interaction.user.id, is_manager(interaction), key),
                ephemeral=True)
        await interaction.response.send_message(
            embed=build_embed(interaction.user),
            view=ItemPanelView(interaction.user.id, is_manager(interaction)),
            ephemeral=True)



    # /shop_rename_item was REMOVED (owner decision after the 2026-07 audit): renaming
    # an item key orphaned its references in consignment deals, stock scans, restock
    # targets and alarms. Delete + re-add under the new name instead.




    # /fix_stacks and /pair_items removed 2026-07-15 — one-time catalog cleanup tools.
    # _detect_stack_size and the twin-pairing logic (_sync_twin_price) still live in core for
    # the normal add/price paths; restore these two commands from git history if a bulk
    # re-scan is ever needed again.



async def setup(bot):
    await bot.add_cog(ShopCog(bot))
