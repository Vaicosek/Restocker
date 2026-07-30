"""Corporate bonds — item-collateralized debt for V Tech's exchange.

HOUSE RULE: every bond must be backed at least 80% (BOND_MIN_ITEM_COVER) by ITEMS —
market inventory valued at shop prices plus assets listed for sale. Coins don't
count as bond collateral (coins walk away; chests full of stock don't). Coverage is
enforced at issuance AND at every purchase, and shows live on the website bond board.

Life cycle: issued on the dashboard (Issue a bond panel, owners of listed companies)
→ bought on the dashboard exchange (proceeds go to the market
treasury) → monthly coupon auto-paid from the treasury (bond loop) → at maturity
principal repaid, or the bond DEFAULTS and #dividend-reports announces the
bondholders' first claim on the item collateral.
"""
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
_public_market_autocomplete = core._public_market_autocomplete
log = core.log


async def _bond_autocomplete(interaction: discord.Interaction, current: str):
    import Restocker_db as _db
    out = []
    for b in (_db.list_bonds(status="open") or [])[:60]:
        left = int(float(b["units_total"]) - float(b["units_sold"] or 0))
        if left <= 0:
            continue
        label = (f"#{b['id']} {b.get('name') or b['market_id']} — "
                 f"{float(b['coupon_pct']):g}%/mo · {left:,} units left "
                 f"@ {int(b['unit_price']):,}¢")
        if current and current.lower() not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=str(b["id"])))
    return out[:25]


class BondsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # The /bond group is gone: issuance moved to the dashboard (/api/bond/issue) and
    # buying already lived on the exchange bond board. An EMPTY app_commands.Group
    # is rejected by Discord at sync, so it must not be left behind.








class VaultCog(commands.Cog):
    """Vault state now lives on the /market settings panel (Vault button + embed line);
    this cog is kept only as the home for the vault constants and future loops.

    V Tech vault — mandatory 10% retained-earnings deposits + item pledges at a
    70% liquidation haircut. Both count as backing; arrears cap the grade at BBB."""

    def __init__(self, bot):
        self.bot = bot






async def setup(bot):
    await bot.add_cog(BondsCog(bot))
    await bot.add_cog(VaultCog(bot))
