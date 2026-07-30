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




async def submit_redemption(interaction, points: int, reward: str, market=None) -> str:
    """Create a pending redemption and notify whoever pays it. Extracted from
    /loyalty redeem so the LoyaltyHub panel runs the SAME path — including the guard
    that stops someone stacking pending requests beyond their balance per pool.

    Returns a message for the caller to show; it does NOT reply to the interaction.
    """
    import Restocker_db as _db
    from datetime import datetime, timezone
    user = interaction.user
    if points <= 0:
        return "❌ Redeem a positive number of points."
    reward = (reward or "").strip()
    if not reward:
        return "❌ Say what you'd like to redeem for."
    if market and not _get_market(market):
        return f"❌ Market `{market}` not found."
    if market:
        have = float(_db.get_market_loyalty(str(user.id), market).get("points", 0) or 0)
    else:
        have = float(_db.get_loyalty(str(user.id)).get("points", 0) or 0)
    pool_name = (_get_market(market) or {}).get("name", market) if market else "V Tech pool"
    if have < points:
        return f"❌ You only have **{have:,.0f}** points in **{pool_name}**."
    reds = _load_redemptions()
    pending_pts = sum(int(r.get("points", 0)) for r in reds.values()
                      if str(r.get("user_id")) == str(user.id) and r.get("status") == "pending"
                      and str(r.get("market_id") or "") == str(market or ""))
    if pending_pts + points > have:
        return (f"❌ You already have **{pending_pts:,}** pts pending from **{pool_name}** — "
                f"that plus **{points:,}** exceeds your **{have:,.0f}**.")
    rid = _next_redemption_id(reds)
    reds[str(rid)] = {
        "id": rid, "user_id": str(user.id), "user_tag": str(user),
        "points": int(points), "reward": reward, "status": "pending",
        "market_id": market or None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_redemptions(reds)
    note = (f"🎟️ **New loyalty redemption #{rid}**{f' — {pool_name}' if market else ''}\n"
            f"{user.mention} wants **{points:,}** pts → *{reward}*\n"
            f"Pay them, then use the **Approve / Reject** buttons on the ticket.")
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
    payer = "This market's owner" if market else "A manager"
    return (f"🎟️ **Redemption #{rid}** submitted — **{points:,}** pts from **{pool_name}** "
            f"for *{reward}*.\n{payer} pays it out and approves here; points are deducted on approval.")


class LoyaltyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="me",
                          description="Your stuff — coins, in-game names, team, loyalty points")
    async def me_panel(self, interaction: discord.Interaction):
        """One picker row replacing /me, /me → Link in-game name, /me → Join a team and /me → Loyalty & rewards.
        None of those were manager tools — they were what an ordinary worker needs,
        scattered across four commands."""
        from views.me_panel import MePanelView, build_embed
        await interaction.response.send_message(
            embed=build_embed(interaction.user),
            view=MePanelView(interaction.user.id), ephemeral=True)





    # Top-level alias — every bot message says "run /me → Link in-game name", so that exact command
    # must exist (the /loyalty subcommand kept for compatibility). Same logic, one path.

    # NOTE: the old /loyalty register_ign subcommand was removed 2026-07-28 — every bot
    # message points people at the top-level /me → Link in-game name, so having both was just a
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
                f"Ask a manager to unlink one you no longer use (`/me → Loyalty & rewards` → Manager settings).", ephemeral=True)
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











    def _can_action_redemption(self, interaction: discord.Interaction, r: dict) -> bool:
        """A global manager can action any redemption. A market-scoped redemption can ALSO
        be actioned by that market's own owner/manager — "each market owner ... handles
        their own loyalty rewards" (Stage 4)."""
        if is_manager(interaction):
            return True
        mid = r.get("market_id")
        return bool(mid) and mid in _markets_owned_by(interaction.user.id)



async def setup(bot: commands.Bot):
    await bot.add_cog(LoyaltyCog(bot))
