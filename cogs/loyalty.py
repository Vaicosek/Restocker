"""Loyalty cog — points, tiers, leaderboard, IGN registration + link audit.

First extracted cog (pilot for the module split). Shared helpers/config are bound
from the running core module via sys.modules, so this works whether the bot is
launched as `python Restocker_main.py` (module __main__) or imported under its
own name — no double-import, no startup-command change required.
"""
import sys
import asyncio
from typing import Optional
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

# Bind to the already-loaded core module (the running Restocker_main).
core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager             = core.is_manager
_loyalty_tier          = core._loyalty_tier
LOYALTY_TIERS          = core.LOYALTY_TIERS
_award_loyalty_points  = core._award_loyalty_points
LOYALTY_EMPLOYEE_ROLES = core.LOYALTY_EMPLOYEE_ROLES
LOYALTY_IGN_DEADLINE_DAYS = getattr(core, "LOYALTY_IGN_DEADLINE_DAYS", 3)
MANAGER_DM_IDS         = getattr(core, "MANAGER_DM_IDS", set())
log                    = getattr(core, "log", None)
_market_autocomplete   = core._market_autocomplete
_markets_owned_by      = core._markets_owned_by
_get_market            = core._get_market
_award_market_loyalty_points = core._award_market_loyalty_points

# How many in-game names (main + alts) one Discord user may register. Generous by design —
# several owners run 8+ alts. Env-overridable via core if ever needed.
MAX_IGNS_PER_USER = int(getattr(core, "MAX_IGNS_PER_USER", 12))


# ── Loyalty reward redemptions (points → real reward) ─────────────────────────
# State lives in bot_config as JSON so it survives restarts. A worker opens a
# redemption; a manager/owner pays out-of-band, then approves it here, which
# deducts the points. Kept intentionally simple (no button views to persist).
def _load_redemptions() -> dict:
    import json as _json, Restocker_db as _db
    try:
        raw = _db.get_config("loyalty_redemptions")
        return _json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_redemptions(d: dict) -> None:
    import json as _json, Restocker_db as _db
    _db.set_config("loyalty_redemptions", _json.dumps(d))


def _next_redemption_id(d: dict) -> int:
    ids = [int(k) for k in d.keys() if str(k).isdigit()]
    return (max(ids) + 1) if ids else 1

TIER_EMOJIS = {1: "🪨", 2: "🔨", 3: "⚔️", 4: "💎", 5: "👑"}

_IGN_RE = r"^[A-Za-z0-9_]{3,16}$"




class LoyaltyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    loyalty = app_commands.Group(name="loyalty", description="Loyalty points, tiers, and rewards")

    @loyalty.command(name="stats", description="View your loyalty stats and tier")
    @app_commands.describe(user="View another user's stats (managers only)")
    async def loyalty_stats(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user
        if user and user != interaction.user and not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only for other users.", ephemeral=True)
        import Restocker_db as _db_ls
        rec = _db_ls.get_loyalty(str(target.id))
        pts = float(rec.get("points", 0))
        total = float(rec.get("total_earned", 0))
        tier = _loyalty_tier(pts)
        next_tier = next((t for t in LOYALTY_TIERS if t["min_pts"] > pts), None)
        igns = _db_ls.get_igns(str(target.id))
        if igns:
            # Primary (earliest) first with a ★; alts after. All pool into this one account.
            ign_val = ", ".join((f"`{g}` ★" if i == 0 else f"`{g}`") for i, g in enumerate(igns))
        else:
            ign_val = "*Not registered*"

        embed = discord.Embed(
            title=f"{TIER_EMOJIS.get(tier['tier'], '⭐')} {target.display_name} — {tier['name']}",
            color=0xF1C40F
        )
        embed.add_field(name="Points", value=f"`{pts:,.0f}`", inline=True)
        embed.add_field(name="All-time Earned", value=f"`{total:,.0f}`", inline=True)
        embed.add_field(name=(f"IGNs ({len(igns)})" if len(igns) > 1 else "IGN"),
                        value=ign_val, inline=(len(igns) <= 1))
        embed.add_field(name="Interest Rate", value=f"`{tier['interest_weekly_pct']}%/week`", inline=True)
        embed.add_field(name="Payout Bonus", value=f"`+{tier['payout_bonus_pct']}%`", inline=True)
        if next_tier:
            needed = next_tier["min_pts"] - pts
            embed.add_field(name=f"Next: {TIER_EMOJIS.get(next_tier['tier'],'')} {next_tier['name']}",
                            value=f"`{needed:,.0f}` pts away", inline=True)
        else:
            embed.add_field(name="Tier", value="🏆 Max tier reached!", inline=True)

        tiers_str = "\n".join(
            f"{'→' if t['tier'] == tier['tier'] else '  '} {TIER_EMOJIS.get(t['tier'],'')} **{t['name']}** — "
            f"{t['min_pts']:,} pts · {t['interest_weekly_pct']}%/wk · +{t['payout_bonus_pct']}% payout"
            for t in LOYALTY_TIERS
        )
        embed.add_field(name="All Tiers", value=tiers_str, inline=False)

        # Per-market ledgers (Stage 4) — each market's OWN reward currency, separate from
        # the shared V Tech pool above.
        mkt_rows = _db_ls.get_all_market_loyalty_for_user(str(target.id))
        if mkt_rows:
            lines = []
            for r in mkt_rows[:8]:
                mname = (_get_market(r["market_id"]) or {}).get("name", r["market_id"])
                lines.append(f"• **{mname}** — `{float(r.get('points', 0) or 0):,.0f}` pts")
            if len(mkt_rows) > 8:
                lines.append(f"… and {len(mkt_rows) - 8} more")
            embed.add_field(name="🏪 Market Points", value="\n".join(lines), inline=False)

        await interaction.response.send_message(embed=embed)

    @loyalty.command(name="leaderboard", description="Top loyalty point holders")
    async def loyalty_leaderboard(self, interaction: discord.Interaction):
        import Restocker_db as _db_lb
        rows = _db_lb.get_loyalty_leaderboard(15)
        if not rows:
            return await interaction.response.send_message("No loyalty data yet.", ephemeral=True)
        lines = []
        for i, row in enumerate(rows, 1):
            uid = row["user_id"]
            pts = float(row.get("points", 0))
            tier = _loyalty_tier(pts)
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i}.`")
            ign = _db_lb.get_ign(uid) or "—"
            lines.append(f"{medal} <@{uid}> (`{ign}`) — **{pts:,.0f}** pts {TIER_EMOJIS.get(tier['tier'],'')} {tier['name']}")
        embed = discord.Embed(title="🏆 Loyalty Leaderboard", description="\n".join(lines), color=0xF1C40F)
        await interaction.response.send_message(embed=embed)

    # Top-level alias — every bot message says "run /register_ign", so that exact command
    # must exist (the /loyalty subcommand kept for compatibility). Same logic, one path.
    @app_commands.command(name="register_ign",
                          description="Register YOUR Minecraft in-game name so your wages reach you — run again to add alts")
    @app_commands.describe(ign="Your Minecraft username (a main or an alt — alts pool into your one account)")
    async def register_ign_toplevel(self, interaction: discord.Interaction, ign: str):
        await self._register_ign_impl(interaction, ign)

    # NOTE: the old /loyalty register_ign subcommand was removed 2026-07-28 — every bot
    # message points people at the top-level /register_ign, so having both was just a
    # confusing duplicate (and it frees a slot in the 25-subcommand /loyalty group).
    async def _register_ign_impl(self, interaction: discord.Interaction, ign: str):
        import re as _re2, Restocker_db as _db_ri
        ign = ign.strip()
        if not _re2.match(r"^[A-Za-z0-9_]{3,16}$", ign):
            return await interaction.response.send_message(
                "❌ Invalid IGN. Must be 3-16 characters: letters, numbers, underscores.", ephemeral=True)
        uid = str(interaction.user.id)
        existing = _db_ri.get_user_id_by_ign(ign)
        if existing and existing != uid:
            return await interaction.response.send_message(
                f"❌ `{ign}` is already registered to someone else.", ephemeral=True)
        if existing == uid:
            have = _db_ri.get_igns(uid)
            return await interaction.response.send_message(
                f"ℹ️ You've already got `{ign}` registered. Your IGNs: "
                + ", ".join(f"`{g}`" for g in have), ephemeral=True)
        if _db_ri.count_igns(uid) >= MAX_IGNS_PER_USER:
            return await interaction.response.send_message(
                f"❌ You've hit the max of **{MAX_IGNS_PER_USER}** in-game names. "
                f"Ask a manager to unlink one you no longer use (`/loyalty settings`).", ephemeral=True)
        # AUDIT FIX (high): money-bearing IGNs can't be self-claimed (anti-squatting) —
        # unpaid harvest coins would flow to whoever registered the name first.
        try:
            _pend_val = _db_ri.ign_unpaid_value(ign)
        except Exception:
            _pend_val = 0
        if _pend_val > 0 and not is_manager(interaction):
            return await interaction.response.send_message(
                f"⚠️ `{ign}` has **{int(_pend_val):,}** coins of unpaid harvests waiting, so it "
                f"can't be self-claimed. Ask a manager to link it (they'll verify it's yours).",
                ephemeral=True)
        _db_ri.add_ign(uid, ign)
        _db_ri.delete_ign_pending(uid)
        igns = _db_ri.get_igns(uid)
        if len(igns) == 1:
            msg = f"✅ IGN **{ign}** registered! You're all set."
        else:
            msg = (f"✅ Added alt **{ign}**. You now have **{len(igns)}** in-game names, all "
                   f"pooling into this one account:\n" + ", ".join(f"`{g}`" for g in igns))
        await interaction.response.send_message(msg, ephemeral=True)

    @loyalty.command(name="settings",
                     description="(Manager) LoyaltySettings — points, IGN links, unlinked employees")
    async def loyalty_settings(self, interaction: discord.Interaction):
        """One panel replacing add_points/set_points/link/unlink/unlinked/remind_unlinked/whois.
        Members keep stats, leaderboard, redeem and redemptions as commands."""
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        from views.loyalty_settings import LoyaltySettingsView, build_embed
        await interaction.response.send_message(
            embed=build_embed(interaction.guild),
            view=LoyaltySettingsView(interaction.user.id), ephemeral=True)









    @loyalty.command(name="redeem", description="Redeem your loyalty points for a reward (a manager or market owner pays it out)")
    @app_commands.describe(points="How many points to redeem", reward="What you want (e.g. '5000 coins', 'a diamond block')",
                           market="Redeem from a specific market's own points instead of the shared V Tech pool")
    @app_commands.autocomplete(market=_market_autocomplete)
    async def loyalty_redeem(self, interaction: discord.Interaction, points: int, reward: str,
                             market: Optional[str] = None):
        import Restocker_db as _db
        from datetime import datetime, timezone
        if points <= 0:
            return await interaction.response.send_message("❌ Redeem a positive number of points.", ephemeral=True)
        reward = (reward or "").strip()
        if not reward:
            return await interaction.response.send_message("❌ Say what you'd like to redeem for.", ephemeral=True)
        if market and not _get_market(market):
            return await interaction.response.send_message(
                f"❌ Market `{market}` not found. See `/market list`.", ephemeral=True)
        if market:
            have = float(_db.get_market_loyalty(str(interaction.user.id), market).get("points", 0) or 0)
        else:
            have = float(_db.get_loyalty(str(interaction.user.id)).get("points", 0) or 0)
        pool_name = (_get_market(market) or {}).get("name", market) if market else "V Tech pool"
        if have < points:
            return await interaction.response.send_message(
                f"❌ You only have **{have:,.0f}** points in **{pool_name}** — can't redeem **{points:,}**.", ephemeral=True)
        # Guard against stacking pending requests beyond your balance, per pool.
        reds = _load_redemptions()
        pending_pts = sum(int(r.get("points", 0)) for r in reds.values()
                          if str(r.get("user_id")) == str(interaction.user.id) and r.get("status") == "pending"
                          and str(r.get("market_id") or "") == str(market or ""))
        if pending_pts + points > have:
            return await interaction.response.send_message(
                f"❌ You already have **{pending_pts:,}** points in pending redemptions from **{pool_name}**. "
                f"That plus **{points:,}** exceeds your **{have:,.0f}**.", ephemeral=True)
        rid = _next_redemption_id(reds)
        reds[str(rid)] = {
            "id": rid, "user_id": str(interaction.user.id), "user_tag": str(interaction.user),
            "points": int(points), "reward": reward, "status": "pending",
            "market_id": market or None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_redemptions(reds)
        payer = "This market's owner" if market else "A manager"
        await interaction.response.send_message(
            f"🎟️ **Redemption #{rid}** submitted — **{points:,}** pts from **{pool_name}** for *{reward}*.\n"
            f"{payer} will pay it out and approve it here; your points are deducted on approval.",
            ephemeral=True)
        # Notify: the market's own owner if this is a market-scoped redemption (they're the
        # one who pays it — "each market owner ... handles their own loyalty rewards"),
        # otherwise every global manager as before.
        note = (f"🎟️ **New loyalty redemption #{rid}**{f' — {pool_name}' if market else ''}\n"
                f"{interaction.user.mention} wants **{points:,}** pts → *{reward}*\n"
                f"Pay them, then use the **Approve / Reject** buttons on the redemption ticket.")
        notify_ids = set(MANAGER_DM_IDS)
        if market:
            owner_id = (_get_market(market) or {}).get("owner_id")
            if owner_id:
                try:
                    notify_ids.add(int(owner_id))
                except (TypeError, ValueError):
                    pass
        for mid in notify_ids:
            try:
                u = await interaction.client.fetch_user(int(mid))
                await u.send(note)
            except Exception:
                pass
        try:
            if interaction.channel:
                await interaction.channel.send(note, delete_after=1800)
        except Exception:
            pass

    def _can_action_redemption(self, interaction: discord.Interaction, r: dict) -> bool:
        """A global manager can action any redemption. A market-scoped redemption can ALSO
        be actioned by that market's own owner/manager — "each market owner ... handles
        their own loyalty rewards" (Stage 4)."""
        if is_manager(interaction):
            return True
        mid = r.get("market_id")
        return bool(mid) and mid in _markets_owned_by(interaction.user.id)

    @loyalty.command(name="redemptions", description="List pending loyalty redemptions (managers see all; owners see their market's)")
    async def loyalty_redemptions(self, interaction: discord.Interaction):
        reds = _load_redemptions()
        pending = [r for r in reds.values() if r.get("status") == "pending"]
        if not is_manager(interaction):
            owned = _markets_owned_by(interaction.user.id)
            pending = [r for r in pending if r.get("market_id") and r["market_id"] in owned]
            if not pending:
                return await interaction.response.send_message(
                    "⛔ Managers only, or the owner of the market a redemption is scoped to.", ephemeral=True)
        pending.sort(key=lambda r: int(r.get("id", 0)))
        if not pending:
            return await interaction.response.send_message("✅ No pending redemptions.", ephemeral=True)
        lines = []
        for r in pending[:25]:
            mid = r.get("market_id")
            tag = f" · {(_get_market(mid) or {}).get('name', mid)}" if mid else " · V Tech pool"
            lines.append(f"**#{r['id']}** — <@{r['user_id']}> · **{int(r['points']):,}** pts → *{r['reward']}*{tag}")
        await interaction.response.send_message(
            "🎟️ **Pending redemptions**\n" + "\n".join(lines) +
            "\n\nUse the **Approve / Reject** buttons on each redemption ticket — approving deducts the points.",
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LoyaltyCog(bot))
