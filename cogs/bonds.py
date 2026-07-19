"""Corporate bonds — item-collateralized debt for V Tech's exchange.

HOUSE RULE: every bond must be backed at least 80% (BOND_MIN_ITEM_COVER) by ITEMS —
market inventory valued at shop prices plus assets listed for sale. Coins don't
count as bond collateral (coins walk away; chests full of stock don't). Coverage is
enforced at issuance AND at every purchase, and shows live on /bond info.

Life cycle: /bond issue (manager) → /bond buy (anyone, proceeds go to the market
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

    bond = app_commands.Group(name="bond", description="Item-collateralized corporate bonds")

    @bond.command(name="issue",
                  description="(Manager) Issue a COMPANY bond — ≥80% backed by the company's items across all its markets")
    @app_commands.describe(
        market_id="The company's listed stock (its treasury pays coupons; collateral = all its markets' items)",
        amount="Total face value to raise, in coins",
        coupon_pct="Monthly coupon, % of face (e.g. 1.5)",
        term_months="Months until principal is repaid",
        name="Optional series name, e.g. 'GEX 26-B'",
        unit_price="Coins per bond unit (default 100)")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def bond_issue(self, interaction: discord.Interaction, market_id: str,
                         amount: app_commands.Range[int, 1000, 1_000_000_000],
                         coupon_pct: app_commands.Range[float, 0.0, 25.0],
                         term_months: app_commands.Range[int, 1, 60],
                         name: Optional[str] = None,
                         unit_price: app_commands.Range[int, 1, 1_000_000] = 100):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        listing = _db.get_market_shares(market_id)
        if not listing or not listing.get("active"):
            return await interaction.response.send_message(
                f"❌ `{market_id}` isn't a listed company stock.", ephemeral=True)
        # Bonds are issued by COMPANIES — a market that rolls up into a parent
        # listing can't issue its own debt; the parent (the company) does.
        try:
            _parent = core._market_rollup_parent(market_id)
        except Exception:
            _parent = None
        if _parent:
            return await interaction.response.send_message(
                f"❌ `{market_id}` rolls up into **{_parent}** — bonds are issued by the "
                f"company. Issue from `{_parent}` instead.", ephemeral=True)
        pct, col, face = core._bond_coverage(market_id, extra_face=float(amount))
        need = core.BOND_MIN_ITEM_COVER
        if pct < need:
            return await interaction.response.send_message(
                f"❌ Under-collateralized: items on record `{int(col):,}` 🪙 cover only "
                f"**{pct:.1f}%** of `{int(face):,}` 🪙 total bond face (rule: ≥{need:g}%).\n"
                f"Add inventory / for-sale assets (or issue less — max issuable now: "
                f"`{max(0, int(col / (need / 100.0) - (face - amount))):,}` 🪙).",
                ephemeral=True)
        matures = (datetime.now(timezone.utc) + timedelta(days=30 * int(term_months))).strftime("%Y-%m-%d")
        bid = _db.create_bond(market_id, name or f"{market_id.upper()} {datetime.now(timezone.utc):%y-%m}",
                              float(amount), float(unit_price), float(coupon_pct),
                              int(term_months), matures)
        b = _db.get_bond(bid)
        core._queue_dividend_post({
            "type": "bond_event", "market_id": market_id,
            "title": f"🪙 New bond issue — {b['name']} (#{bid})",
            "lines": [f"Raising `{int(amount):,}` 🪙 · **{coupon_pct:g}%/mo** coupon · "
                      f"matures {matures}",
                      f"Item coverage **{pct:.0f}%** (`{int(col):,}` 🪙 of items on record)",
                      f"Buy in with `/bond buy` — units of `{int(unit_price):,}` 🪙"]})
        await interaction.response.send_message(
            f"✅ Bond **{b['name']}** (#{bid}) issued: `{int(amount):,}` 🪙 face · "
            f"{b['units_total']:,} units @ `{int(unit_price):,}` 🪙 · {coupon_pct:g}%/mo · "
            f"matures {matures}. Item coverage **{pct:.0f}%** ✅", ephemeral=True)

    @bond.command(name="buy", description="Buy bond units — coins go to the issuer's treasury, coupons come back monthly")
    @app_commands.describe(bond_id="Which bond series", units="How many units")
    @app_commands.autocomplete(bond_id=_bond_autocomplete)
    async def bond_buy(self, interaction: discord.Interaction, bond_id: str,
                       units: app_commands.Range[int, 1, 10_000_000]):
        import Restocker_db as _db
        try:
            b = _db.get_bond(int(bond_id))
        except (TypeError, ValueError):
            b = None
        if not b or b.get("status") != "open":
            return await interaction.response.send_message("❌ That bond isn't open for sale.", ephemeral=True)
        left = int(float(b["units_total"]) - float(b["units_sold"] or 0))
        if units > left:
            return await interaction.response.send_message(
                f"❌ Only `{left:,}` unit(s) left in this series.", ephemeral=True)
        cost = int(units * float(b["unit_price"]))
        uid = str(interaction.user.id)
        bal = int(_db.get_balance(uid).get("coins") or 0)
        if bal < cost:
            return await interaction.response.send_message(
                f"❌ Costs `{cost:,}` 🪙 — you have `{bal:,}`.", ephemeral=True)
        # coverage check includes THIS purchase so late buyers are protected too
        pct, col, face = core._bond_coverage(b["market_id"],
                                             extra_face=units * float(b["unit_price"]))
        if pct < core.BOND_MIN_ITEM_COVER:
            return await interaction.response.send_message(
                f"⛔ Sale paused: item coverage would drop to **{pct:.1f}%** "
                f"(rule: ≥{core.BOND_MIN_ITEM_COVER:g}%). The issuer must add collateral.",
                ephemeral=True)
        core.deduct_coins(uid, cost, reduce_principal=True)
        _db.adjust_treasury(b["market_id"], cost)
        _db.adjust_bond_holding(b["id"], uid, float(units), float(cost),
                                name=getattr(interaction.user, "display_name", None))
        if units >= left:
            _db.update_bond(b["id"], status="active")
        monthly = units * float(b["unit_price"]) * float(b["coupon_pct"]) / 100.0
        await interaction.response.send_message(
            f"✅ Bought `{units:,}` unit(s) of **{b['name']}** for `{cost:,}` 🪙.\n"
            f"Coupon ≈ `{int(monthly):,}` 🪙/month · principal back {str(b.get('matures_at') or '')[:10]} · "
            f"item coverage **{pct:.0f}%**.", ephemeral=True)

    @bond.command(name="info", description="A bond's coverage, coupon, holders and status")
    @app_commands.describe(bond_id="Which bond series")
    @app_commands.autocomplete(bond_id=_bond_autocomplete)
    async def bond_info(self, interaction: discord.Interaction, bond_id: str):
        import Restocker_db as _db
        try:
            b = _db.get_bond(int(bond_id))
        except (TypeError, ValueError):
            b = None
        if not b:
            return await interaction.response.send_message("❌ Unknown bond.", ephemeral=True)
        pct, col, face = core._bond_coverage(b["market_id"])
        holders = _db.get_bond_holders(b["id"])
        sold_face = core._bond_sold_face(b)
        emb = discord.Embed(title=f"🪙 {b['name']} — bond #{b['id']} ({b['market_id']})",
                            color=discord.Color.red() if b["status"] == "defaulted" else discord.Color.teal())
        emb.add_field(name="Status", value=f"`{b['status']}`" + (
            f" · ⚠ {b['missed_coupons']} missed coupon(s)" if b.get("missed_coupons") else ""), inline=True)
        emb.add_field(name="Coupon", value=f"`{float(b['coupon_pct']):g}%`/mo", inline=True)
        emb.add_field(name="Matures", value=str(b.get("matures_at") or "—")[:10], inline=True)
        emb.add_field(name="Sold", value=f"`{float(b['units_sold']):,.0f}`/`{b['units_total']:,}` units "
                                         f"(face `{int(sold_face):,}` 🪙)", inline=True)
        emb.add_field(name="Item coverage",
                      value=f"**{pct:.0f}%** — items `{int(col):,}` 🪙 vs total face `{int(face):,}` 🪙 "
                            f"(rule ≥{core.BOND_MIN_ITEM_COVER:g}%)"
                            + (" ✅" if pct >= core.BOND_MIN_ITEM_COVER else " ⚠ UNDER"), inline=False)
        if holders:
            emb.add_field(name=f"Holders ({len(holders)})",
                          value="\n".join(f"• <@{h['user_id']}> — `{float(h['units']):,.0f}` units"
                                          for h in holders[:12])[:1000], inline=False)
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @bond.command(name="list", description="All bond series (optionally one market's)")
    @app_commands.describe(market_id="Filter to one market")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def bond_list(self, interaction: discord.Interaction, market_id: Optional[str] = None):
        import Restocker_db as _db
        rows = _db.list_bonds(market_id)
        if not rows:
            return await interaction.response.send_message("No bonds issued yet.", ephemeral=True)
        emb = discord.Embed(title="🪙 Bond board", color=discord.Color.teal())
        for b in rows[:15]:
            left = int(float(b["units_total"]) - float(b["units_sold"] or 0))
            pct, col, _f = core._bond_coverage(b["market_id"])
            emb.add_field(
                name=f"#{b['id']} {b['name']} ({b['market_id']}) — `{b['status']}`",
                value=(f"{float(b['coupon_pct']):g}%/mo · matures {str(b.get('matures_at') or '—')[:10]} · "
                       f"{left:,} units left @ `{int(b['unit_price']):,}` 🪙 · coverage **{pct:.0f}%**"),
                inline=False)
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @bond.command(name="my", description="Your bond holdings and what they pay")
    async def bond_my(self, interaction: discord.Interaction):
        import Restocker_db as _db
        rows = _db.get_user_bonds(str(interaction.user.id))
        if not rows:
            return await interaction.response.send_message("You hold no bonds.", ephemeral=True)
        lines, monthly = [], 0.0
        for h in rows:
            m = float(h["units"]) * float(h["unit_price"]) * float(h["coupon_pct"]) / 100.0
            if h["status"] in ("open", "active"):
                monthly += m
            lines.append(f"• **{h['bond_name']}** ({h['market_id']}) — `{float(h['units']):,.0f}` units · "
                         f"`{int(m):,}` 🪙/mo · `{h['status']}` · matures {str(h.get('matures_at') or '—')[:10]}")
        emb = discord.Embed(title="🪙 Your bonds",
                            description="\n".join(lines)[:4000], color=discord.Color.teal())
        emb.set_footer(text=f"Total coupons ≈ {int(monthly):,} coins/month")
        await interaction.response.send_message(embed=emb, ephemeral=True)


async def _escrow_autocomplete(interaction: discord.Interaction, current: str):
    import Restocker_db as _db
    out = []
    for e in (_db.list_escrows() or [])[:50]:
        label = f"#{e['id']} [{e['status']}] {e['party']} — {int(e['value']):,}¢ ({e['kind']})"
        if current and current.lower() not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=str(e["id"])))
    return out[:25]


class EscrowCog(commands.Cog):
    """Listing escrow — V Tech as the clearing house. Outside companies that want
    to list on the exchange deposit collateral (coins or items at an agreed
    valuation) with V Tech first. Rug your investors → forfeit the deposit."""

    def __init__(self, bot):
        self.bot = bot

    escrow = app_commands.Group(name="escrow",
                                description="Listing collateral held by V Tech as clearing house")

    @escrow.command(name="hold", description="(Manager) Record a collateral deposit from a listing company")
    @app_commands.describe(party="Who deposited (company/player)",
                           value="Coin value (items at the agreed valuation)",
                           kind="What was deposited",
                           note="Terms, e.g. 'lists NetherCo stock; held 90 days'")
    @app_commands.choices(kind=[
        app_commands.Choice(name="coins", value="coins"),
        app_commands.Choice(name="items", value="items")])
    async def escrow_hold(self, interaction: discord.Interaction, party: str,
                          value: app_commands.Range[int, 1, 1_000_000_000],
                          kind: str = "coins", note: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        eid = _db.create_escrow(party.strip(), kind, float(value), note)
        core._queue_dividend_post({"type": "bond_event",
            "title": f"🏦 Escrow deposit #{eid} — {party.strip()}",
            "lines": [f"`{int(value):,}` 🪙 in {kind} now held by V Tech as listing collateral."
                      + (f"\nTerms: {note}" if note else "")]})
        await interaction.response.send_message(
            f"🏦 Escrow **#{eid}** recorded: {party.strip()} · `{int(value):,}` 🪙 ({kind}) — "
            f"announced in reports.", ephemeral=True)

    @escrow.command(name="resolve", description="(Manager) Release or forfeit a held deposit")
    @app_commands.describe(deposit="Which deposit", outcome="Release back or forfeit to V Tech",
                           note="Why (public)")
    @app_commands.choices(outcome=[
        app_commands.Choice(name="release — returned to the depositor", value="released"),
        app_commands.Choice(name="forfeit — kept (they rugged)", value="forfeited")])
    @app_commands.autocomplete(deposit=_escrow_autocomplete)
    async def escrow_resolve(self, interaction: discord.Interaction, deposit: str,
                             outcome: str, note: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        try:
            e = _db.get_escrow(int(deposit))
        except (TypeError, ValueError):
            e = None
        if not e or e["status"] != "held":
            return await interaction.response.send_message("❌ Not a held deposit.", ephemeral=True)
        _db.update_escrow(e["id"], outcome, note)
        icon = "✅" if outcome == "released" else "⚔️"
        core._queue_dividend_post({"type": "bond_event", "bad": outcome == "forfeited",
            "title": f"{icon} Escrow #{e['id']} {outcome} — {e['party']}",
            "lines": [f"`{int(e['value']):,}` 🪙 ({e['kind']}) "
                      + ("returned to the depositor." if outcome == "released"
                         else "FORFEITED to V Tech — investor claims come first.")
                      + (f"\n{note}" if note else "")]})
        await interaction.response.send_message(f"{icon} Escrow #{e['id']} → **{outcome}**.", ephemeral=True)

    @escrow.command(name="list", description="All escrow deposits and their status")
    async def escrow_list(self, interaction: discord.Interaction):
        import Restocker_db as _db
        rows = _db.list_escrows()
        if not rows:
            return await interaction.response.send_message("No escrow deposits on record.", ephemeral=True)
        icon = {"held": "🏦", "released": "✅", "forfeited": "⚔️"}
        emb = discord.Embed(title="🏦 Listing escrow ledger", color=discord.Color.dark_teal(),
                            description="\n".join(
                                f"{icon.get(e['status'],'•')} **#{e['id']}** {e['party']} — "
                                f"`{int(e['value']):,}` 🪙 ({e['kind']}) · `{e['status']}`"
                                + (f" — {e['note']}" if e.get("note") else "")
                                for e in rows[:20])[:4000])
        held = sum(float(e["value"]) for e in rows if e["status"] == "held")
        emb.set_footer(text=f"Currently held: {int(held):,} coins of collateral")
        await interaction.response.send_message(embed=emb, ephemeral=True)


async def setup(bot):
    await bot.add_cog(BondsCog(bot))
    await bot.add_cog(EscrowCog(bot))
