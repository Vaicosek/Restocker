"""Lands (claims) tracking — consumes the CSN mod's LANDS FEED webhook posts.

The mod forwards two line types (see LandTracker.java):
    LANDS-BAL|<land>|<balance>|<iso timestamp>
    LANDS-ENTRY|<land>|#<n>|<MM/DD/YYYY HH:MM>|<entry text ... New balance: $X>

What this cog does with them:
  1. Stores every entry (idempotent — the mod already dedups, we dedup again by PK).
  2. TREASURY SYNC: a bound land's latest balance auto-updates its market's treasury
     (the exchange's TOTAL TREASURY and the backing rating stay live).
  3. TELEPORT FEES BY MATH (the owner's spec): fees never appear as inbox entries, so
     they are inferred as the unexplained gap between consecutive known balances:
         expected_prev = new_balance(entry N) − amount(entry N)
         fees between N−1 and N = expected_prev − new_balance(entry N−1)
     plus the gap between the newest entry and the live LANDS-BAL snapshot. Recomputed
     from scratch on every ingest (idempotent), bucketed per YYYY-MM.
"""
import re
import sys

import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
_market_autocomplete = core._market_autocomplete
log = core.log

_BAL_RX = re.compile(r"^LANDS-BAL\|([^|]+)\|([\d.]+)\|")
_ENTRY_RX = re.compile(r"^LANDS-ENTRY\|([^|]+)\|#(\d+)\|([\d/]+\s+[\d:]+)\|(.+)$")
_MONEY_RX = re.compile(r"\$([\d,]+(?:\.\d+)?)")
_NEWBAL_RX = re.compile(r"(?i)new balance:\s*\$([\d,]+(?:\.\d+)?)")


def _money(s) -> float:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _classify(body: str):
    """(kind, signed_amount) of an inbox entry's effect on the land balance."""
    low = body.lower()
    m = _MONEY_RX.search(body)
    amt = _money(m.group(1)) if m else 0.0
    if "deposited" in low:
        return "deposit", amt
    if "withdrew" in low or "withdrawn" in low:
        return "withdraw", -amt
    if "taxes" in low and ("received" in low or "total" in low):
        return "taxes", amt
    return "other", 0.0


def _month_of(ts: str) -> str:
    """'07/15/2026 11:00' → '2026-07'."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", ts.strip())
    return f"{m.group(3)}-{m.group(1)}" if m else "unknown"


def _land_market(land: str) -> str:
    import Restocker_db as _db
    return str(_db.get_config(f"land_map:{land.lower()}") or "").strip()


def _recompute_fees(land: str) -> dict:
    """Rebuild the land's inferred monthly fees from the stored chain. Returns {month: fees}."""
    import Restocker_db as _db
    entries = [e for e in _db.get_land_entries(land)]
    fees: dict = {}
    prev_bal = None
    prev_seen = False
    for e in entries:
        nb = e.get("new_balance")
        if nb is None:
            continue                       # membership entries carry no balance
        nb = float(nb)
        if prev_seen:
            expected_prev = nb - float(e.get("amount") or 0.0)
            gap = expected_prev - prev_bal
            if gap > 0.005:                # positive unexplained income = fees
                mk = _month_of(e.get("ts") or "")
                fees[mk] = fees.get(mk, 0.0) + gap
        prev_bal = nb
        prev_seen = True
    # tail: live balance snapshot vs the newest entry's balance
    snap = _db.get_land_balance(land)
    if snap is not None and prev_seen:
        gap = float(snap.get("balance") or 0.0) - prev_bal
        if gap > 0.005:
            from datetime import datetime, timezone
            mk = datetime.now(timezone.utc).strftime("%Y-%m")
            fees[mk] = fees.get(mk, 0.0) + gap
    _db.replace_land_fees(land, fees)
    return fees


class LandsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── feed listener: AUTHENTICATED LANDS FEED ingest ───────────────────────
    # House rule: treasuries are hard data. A forged LANDS-BAL line would set a
    # market's treasury (and thus dividends) to whatever an attacker types, so
    # feed posts are only accepted from a WEBHOOK (regular members can't post as
    # one without Manage Webhooks) and, when config `lands_feed_channel` is set,
    # only in that channel. Lock it down with /land feed_channel.
    @commands.Cog.listener()
    async def on_message(self, message):
        try:
            content = message.content or ""
            if "LANDS FEED" not in content.split("\n", 1)[0]:
                return
            if message.author and self.bot.user and message.author.id == self.bot.user.id:
                return
            if message.webhook_id is None:
                log.warning("[lands] REJECTED non-webhook LANDS FEED from user %s in #%s",
                            getattr(message.author, "id", "?"),
                            getattr(message.channel, "name", "?"))
                return
            import Restocker_db as _db
            try:
                _ch = int(_db.get_config("lands_feed_channel") or 0)
            except Exception:
                _ch = 0
            if _ch and message.channel.id != _ch:
                log.warning("[lands] REJECTED LANDS FEED in unauthorized channel %s",
                            message.channel.id)
                return
            await self._ingest(message, content)
        except Exception as e:
            log.warning("[lands] feed ingest failed: %s", e)

    async def _ingest(self, message, content: str):
        import Restocker_db as _db
        touched = set()
        balances = {}
        new_entries = 0
        for line in content.split("\n"):
            line = line.strip()
            mb = _BAL_RX.match(line)
            if mb:
                land = mb.group(1).strip()
                balances[land] = _money(mb.group(2))
                touched.add(land)
                continue
            me = _ENTRY_RX.match(line)
            if me:
                land, no, ts, body = (me.group(1).strip(), int(me.group(2)),
                                      me.group(3).strip(), me.group(4).strip())
                kind, amt = _classify(body)
                nb_m = _NEWBAL_RX.search(body)
                nb = _money(nb_m.group(1)) if nb_m else None
                if _db.add_land_entry(land, no, ts, kind, amt, nb, body):
                    new_entries += 1
                touched.add(land)
        if not touched:
            return

        report = []
        for land, bal in balances.items():
            _db.set_land_balance(land, bal)
        for land in sorted(touched):
            fees = _recompute_fees(land)
            mid = _land_market(land)
            line = f"🏦 **{land}**"
            snap = _db.get_land_balance(land)
            if snap:
                line += f" — balance `{float(snap['balance']):,.0f}`"
            if fees:
                total = sum(fees.values())
                line += f" · inferred teleport fees `{total:,.0f}` ({len(fees)} month(s))"
            if mid:
                # Treasury sync: the land IS the market's treasury.
                if snap:
                    _db.upsert_market_shares(mid, treasury_coins=float(snap["balance"]))
                    core._recompute_share_price(mid, reason="land_treasury")
                    line += f" → treasury of `{mid}` updated"
            else:
                line += " · *(unbound — `/land bind` to link a market)*"
            report.append(line)
        if new_entries or balances:
            try:
                await message.channel.send(
                    f"✅ Lands feed ingested — {new_entries} new entrie(s), "
                    f"{len(balances)} balance(s).\n" + "\n".join(report)[:1700])
            except Exception:
                pass

    # ── commands ─────────────────────────────────────────────────────────────
    land = app_commands.Group(
        name="land",
        description="(Managers) Lands/claims tracking — treasuries and teleport-fee income",
        default_permissions=discord.Permissions(manage_guild=True))

    @land.command(name="bind", description="Link a land (claim) to a market — its balance becomes that market's treasury")
    @app_commands.describe(land_name="Land name exactly as in-game (e.g. MardURAK)",
                           market_id="Market it belongs to (blank to unbind)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def land_bind(self, interaction: discord.Interaction,
                        land_name: str, market_id: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        key = f"land_map:{land_name.strip().lower()}"
        if not (market_id or "").strip():
            _db.delete_config(key)
            return await interaction.response.send_message(
                f"✅ `{land_name}` unbound — its feed is stored but no longer syncs a treasury.",
                ephemeral=True)
        _db.set_config(key, market_id.strip())
        msg = f"✅ Land **{land_name}** → market `{market_id}`."
        snap = _db.get_land_balance(land_name.strip())
        if snap:
            _db.upsert_market_shares(market_id.strip(), treasury_coins=float(snap["balance"]))
            core._recompute_share_price(market_id.strip(), reason="land_treasury")
            msg += f"\nTreasury synced now: `{float(snap['balance']):,.0f}` 🪙."
        msg += "\nEvery future LANDS FEED post keeps the treasury live and re-infers teleport fees."
        await interaction.response.send_message(msg, ephemeral=True)

    @land.command(name="feed_channel",
                  description="(Manager) Lock LANDS FEED ingest to one channel — spoof protection")
    @app_commands.describe(channel="The only channel the mod's webhook posts land data in")
    async def land_feed_channel(self, interaction: discord.Interaction,
                                channel: discord.TextChannel):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        _db.set_config("lands_feed_channel", str(channel.id))
        await interaction.response.send_message(
            f"🔒 LANDS FEED now only accepted from **webhook posts in {channel.mention}**. "
            f"Everything else is rejected and logged.", ephemeral=True)

    @land.command(name="status", description="Balances, bindings and inferred teleport fees per land")
    async def land_status(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        rows = _db.get_all_land_fees()
        lands = {}
        for r in rows:
            lands.setdefault(r["land"], {})[r["month"]] = float(r["fees"])
        # also show lands that have balances but no fees yet
        embed = discord.Embed(title="🏦 Lands — treasuries & teleport fees", color=0x3498DB)
        seen = set()
        for land in sorted(set(list(lands.keys()))):
            seen.add(land)
            snap = _db.get_land_balance(land)
            mid = _land_market(land)
            months = lands.get(land) or {}
            val = []
            if snap:
                val.append(f"balance `{float(snap['balance']):,.0f}` 🪙")
            val.append(f"→ `{mid}`" if mid else "*unbound*")
            if months:
                recent = sorted(months.items())[-3:]
                val.append("fees: " + " · ".join(f"`{m}` **{f:,.0f}**" for m, f in recent))
            else:
                val.append("fees: none inferred yet")
            embed.add_field(name=land, value="\n".join(val), inline=False)
        if not seen:
            embed.description = ("No land data yet — run the mod's lands sweep or open a "
                                 "land inbox in-game, then the feed posts land here.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(LandsCog(bot))
