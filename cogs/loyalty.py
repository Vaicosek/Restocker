"""
Loyalty cog — points, tiers, leaderboard, IGN registration + link audit.

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


def _mention_list(members: list, cap: int = 30) -> str:
    shown = ", ".join(m.mention for m in members[:cap])
    extra = len(members) - cap
    return shown + (f" … +{extra} more" if extra > 0 else "")


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

    @loyalty.command(name="register_ign",
                     description="Register a Minecraft in-game name — run again to add alt accounts")
    @app_commands.describe(ign="Your Minecraft username (a main or an alt — alts pool into your one account)")
    async def loyalty_register_ign(self, interaction: discord.Interaction, ign: str):
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
                f"Ask a manager to `/loyalty unlink` one you no longer use first.", ephemeral=True)
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

    @loyalty.command(name="unlink_ign", description="Remove a previously registered Minecraft IGN from your loyalty account")
    @app_commands.describe(ign="The Minecraft IGN to remove from your account")
    async def loyalty_unlink_ign(self, interaction: discord.Interaction, ign: str):
        import re as _re_ui, Restocker_db as _db_ui
        ign = ign.strip()
        if not _re_ui.match(_IGN_RE, ign):
            return await interaction.response.send_message(
                "❌ Invalid IGN. Must be 3-16 characters: letters, numbers, underscores.", ephemeral=True)
        uid = str(interaction.user.id)
        current = _db_ui.get_igns(uid)
        if not current:
            return await interaction.response.send_message(
                "❌ You have no IGNs registered on your account.", ephemeral=True)
        if ign not in current:
            return await interaction.response.send_message(
                f"❌ `{ign}` is not registered to your account. Your IGNs: "
                + ", ".join(f"`{g}`" for g in current), ephemeral=True)
        if _db_ui.remove_ign(uid, ign):
            remaining = _db_ui.get_igns(uid)
            if remaining:
                left_txt = ", ".join(f"`{g}`" for g in remaining)
                await interaction.response.send_message(
                    f"🔓 `{ign}` has been unlinked from your account. "
                    f"Remaining IGN(s): {left_txt}.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"🔓 `{ign}` has been unlinked from your account. "
                    f"You now have no IGNs registered.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"❌ Something went wrong trying to unlink `{ign}`. Please try again or contact a manager.",
                ephemeral=True)

    @loyalty.command(name="unlinked", description="(Manager) List employees who haven't linked their Minecraft IGN")
    async def loyalty_unlinked(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Run this in a server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        # Make sure the full member list is cached (members intent is on).
        members = guild.members
        if not guild.chunked:
            try:
                members = await guild.chunk()
            except Exception:
                members = guild.members

        import Restocker_db as _db_ul
        employees = [m for m in members if not m.bot
                     and any(r.name in LOYALTY_EMPLOYEE_ROLES for r in m.roles)]
        linked, pending, unlinked = [], [], []
        for m in employees:
            if _db_ul.get_ign(str(m.id)):
                linked.append(m)
            elif _db_ul.get_ign_pending(str(m.id)):
                pending.append(m)
            else:
                unlinked.append(m)

        embed = discord.Embed(
            title="🔗 IGN Link Status — all employees",
            description=(f"Scanned **{len(employees)}** members holding an employee role "
                         f"({', '.join(sorted(LOYALTY_EMPLOYEE_ROLES))})."),
            color=0xE74C3C if unlinked else 0x2ECC71,
        )
        embed.add_field(name=f"✅ Linked ({len(linked)})",
                        value=_mention_list(linked) if linked else "*none*", inline=False)
        if pending:
            embed.add_field(name=f"⏳ Prompted, awaiting reply ({len(pending)})",
                            value=_mention_list(pending), inline=False)
        embed.add_field(name=f"❌ Not linked ({len(unlinked)})",
                        value=_mention_list(unlinked) if unlinked else "*none — everyone is linked!* 🎉",
                        inline=False)
        if unlinked:
            embed.set_footer(text="Unlinked employees' CSN sales credit NO ONE. "
                                  "Link them with /loyalty link <user> <ign>.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @loyalty.command(
        name="remind_unlinked",
        description="(Manager) DM every employee who hasn't linked their IGN, asking them to")
    @app_commands.describe(
        apply="false = dry-run preview (default); true = actually send the DMs",
        set_deadline="⚠️ true = also start the 3-day countdown; their role is REMOVED if they "
                     "don't reply. Default false = reminder only, nobody loses anything.")
    async def loyalty_remind_unlinked(self, interaction: discord.Interaction,
                                      apply: bool = False, set_deadline: bool = False):
        """Nudges existing unlinked employees. The on-role-gain prompt in events.py only fires
        when someone RECEIVES an employee role, so anyone who already had it never got asked —
        this catches them up.

        set_deadline is off by default on purpose: turning it on writes an ign_pending row, and
        the deadline loop STRIPS THE ROLE of anyone who doesn't reply in time. Fine for a couple
        of new hires; a bad idea to fire at 60+ existing staff at once."""
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Run this in a server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)

        members = guild.members
        if not guild.chunked:
            try:
                members = await guild.chunk()
            except Exception:
                members = guild.members

        import Restocker_db as _db_r
        employees = [m for m in members if not m.bot
                     and any(r.name in LOYALTY_EMPLOYEE_ROLES for r in m.roles)]
        targets = [m for m in employees
                   if not _db_r.get_ign(str(m.id)) and not _db_r.get_ign_pending(str(m.id))]

        if not targets:
            return await interaction.followup.send(
                "✅ Everyone with an employee role is either linked or already prompted.",
                ephemeral=True)

        if not apply:
            warn = ("\n\n⚠️ `set_deadline:true` would ALSO start the "
                    f"{LOYALTY_IGN_DEADLINE_DAYS}-day countdown — anyone who doesn't reply gets "
                    f"their **role removed**. With {len(targets)} people, think hard before doing that."
                    if set_deadline else
                    "\n\nThis sends a reminder only — no deadline, nobody loses their role.")
            return await interaction.followup.send(
                f"**Dry run** — would DM **{len(targets)}** unlinked employee(s):\n"
                + ", ".join(m.mention for m in targets[:30])
                + (f" … +{len(targets)-30} more" if len(targets) > 30 else "")
                + warn + "\n\nRe-run with `apply:true` to send.", ephemeral=True)

        sent, blocked = 0, []
        deadline = (datetime.now(timezone.utc)
                    + timedelta(days=LOYALTY_IGN_DEADLINE_DAYS)).isoformat()
        for m in targets:
            try:
                dm = await m.create_dm()
                body = (
                    f"👋 Hi! You have an employee role in **{guild.name}**, but you haven't linked "
                    f"your **Minecraft in-game username (IGN)** yet.\n\n"
                    f"Right now your in-game shop sales **credit nobody** — you're missing out on "
                    f"loyalty points and payouts.\n\n"
                    f"Just reply here with your IGN (exactly as it appears in-game), or run "
                    f"`/loyalty register_ign` in the server."
                )
                if set_deadline:
                    body += (f"\n\n⏰ Please do it within **{LOYALTY_IGN_DEADLINE_DAYS} days** — "
                             f"after that your role is removed automatically.")
                await dm.send(body)
                sent += 1
                if set_deadline:
                    role = next((r for r in m.roles if r.name in LOYALTY_EMPLOYEE_ROLES), None)
                    _db_r.set_ign_pending(str(m.id), str(dm.id),
                                          str(role.id) if role else "0",
                                          str(guild.id), deadline)
            except discord.Forbidden:
                blocked.append(m)
            except Exception as e:
                log.warning("[ign] reminder to %s failed: %s", m.id, e)
                blocked.append(m)
            # Throttle — 60+ DMs in a burst is exactly what gets a bot rate-limited or flagged.
            await asyncio.sleep(1.2)

        msg = f"✅ DM'd **{sent}**/{len(targets)} unlinked employee(s)."
        if set_deadline:
            msg += (f"\n⏰ Deadline set — they lose their role in {LOYALTY_IGN_DEADLINE_DAYS} days "
                    f"if they don't reply.")
        else:
            msg += "\n(Reminder only — no deadline set, nobody loses their role.)"
        if blocked:
            msg += (f"\n\n🚫 Couldn't DM **{len(blocked)}** (DMs closed): "
                    + ", ".join(m.mention for m in blocked[:15])
                    + (f" … +{len(blocked)-15} more" if len(blocked) > 15 else "")
                    + "\nThose need `/loyalty link <user> <ign>` manually.")
        await interaction.followup.send(msg[:1900], ephemeral=True)

    @loyalty.command(name="link", description="(Manager) Link a member to a Minecraft IGN — run again to add their alts")
    @app_commands.describe(user="The Discord member", ign="An EXACT Minecraft username of theirs (main or alt)")
    async def loyalty_link(self, interaction: discord.Interaction, user: discord.Member, ign: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import re as _re3, Restocker_db as _db_lk
        ign = ign.strip()
        if not _re3.match(_IGN_RE, ign):
            return await interaction.response.send_message(
                "❌ Invalid IGN. Must be 3-16 characters: letters, numbers, underscores.", ephemeral=True)
        owner = _db_lk.get_user_id_by_ign(ign)
        if owner and owner != str(user.id):
            return await interaction.response.send_message(
                f"❌ `{ign}` is already linked to <@{owner}>. Unlink it from them first if this is a mistake.",
                ephemeral=True)
        if owner == str(user.id):
            return await interaction.response.send_message(
                f"ℹ️ {user.mention} already has `{ign}` linked.", ephemeral=True)
        if _db_lk.count_igns(str(user.id)) >= MAX_IGNS_PER_USER:
            return await interaction.response.send_message(
                f"❌ {user.mention} already has the max of **{MAX_IGNS_PER_USER}** IGNs. "
                f"Unlink one first.", ephemeral=True)
        _db_lk.add_ign(str(user.id), ign)
        _db_lk.delete_ign_pending(str(user.id))
        igns = _db_lk.get_igns(str(user.id))
        extra = (f" They now have **{len(igns)}**: " + ", ".join(f"`{g}`" for g in igns)) if len(igns) > 1 else ""
        await interaction.response.send_message(
            f"🔗 Linked {user.mention} → **{ign}**. Their CSN sales now credit them.{extra}")

    @loyalty.command(name="unlink", description="(Manager) Remove a member's IGN — one specific alt, or all of them")
    @app_commands.describe(user="The Discord member to unlink",
                           ign="(Optional) remove just this one IGN. Leave blank to remove ALL of theirs.")
    async def loyalty_unlink(self, interaction: discord.Interaction, user: discord.Member,
                             ign: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db_ulk
        current = _db_ulk.get_igns(str(user.id))
        if not current:
            return await interaction.response.send_message(
                f"{user.mention} has no IGN linked.", ephemeral=True)
        if ign:
            ign = ign.strip()
            if _db_ulk.remove_ign(str(user.id), ign):
                left = _db_ulk.get_igns(str(user.id))
                left_txt = ", ".join(f"`{g}`" for g in left) if left else "*none left*"
                return await interaction.response.send_message(
                    f"🔓 Removed `{ign}` from {user.mention}. Remaining: {left_txt}.")
            return await interaction.response.send_message(
                f"❌ {user.mention} has no IGN `{ign}`. They have: "
                + ", ".join(f"`{g}`" for g in current), ephemeral=True)
        _db_ulk.delete_ign(str(user.id))
        await interaction.response.send_message(
            f"🔓 Unlinked {user.mention} — removed all **{len(current)}** IGN(s) "
            f"(was: {', '.join(f'`{g}`' for g in current)}).")

    @loyalty.command(name="set_points", description="(Manager) Manually set a user's loyalty points")
    @app_commands.describe(user="The user", points="New point total")
    async def loyalty_set_points(self, interaction: discord.Interaction, user: discord.Member, points: float):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db_sp
        new = _db_sp.set_loyalty_points(str(user.id), points)
        tier = _loyalty_tier(new)
        await interaction.response.send_message(
            f"✅ Set **{user.display_name}**'s loyalty to `{new:,.0f}` pts — {TIER_EMOJIS.get(tier['tier'],'')} **{tier['name']}**"
        )

    @loyalty.command(name="add_points", description="(Manager) Add loyalty points to a user")
    @app_commands.describe(user="The user", points="Points to add", reason="Reason")
    async def loyalty_add_points(self, interaction: discord.Interaction, user: discord.Member, points: float, reason: str = "manual"):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        new_total, old_tier, new_tier = _award_loyalty_points(user.id, int(points), reason=reason)
        tier_up = f" 🏆 Tier up to **{new_tier['name']}**!" if new_tier["tier"] > old_tier["tier"] else ""
        await interaction.response.send_message(
            f"✅ Added **{points:,.0f}** pts to {user.mention} → `{new_total:,.0f}` total{tier_up}"
        )


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
                f"Pay them, then run `/loyalty approve id:{rid}` (or `/loyalty deny id:{rid}`).")
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
            "\n\nApprove with `/loyalty approve id:<#>` (deducts points) or `/loyalty deny id:<#>`.",
            ephemeral=True)

    @loyalty.command(name="approve", description="(Manager/market owner) Approve a redemption — deducts the points")
    @app_commands.describe(id="Redemption ID from /loyalty redemptions")
    async def loyalty_approve(self, interaction: discord.Interaction, id: int):
        import Restocker_db as _db
        reds = _load_redemptions()
        r = reds.get(str(id))
        if not r:
            return await interaction.response.send_message(f"❌ No redemption #{id}.", ephemeral=True)
        if not self._can_action_redemption(interaction, r):
            return await interaction.response.send_message(
                "⛔ Managers only, or the owner of the market this redemption is scoped to.", ephemeral=True)
        if r.get("status") != "pending":
            return await interaction.response.send_message(
                f"⚠️ Redemption #{id} is already **{r.get('status')}**.", ephemeral=True)
        uid = str(r["user_id"]); pts = int(r["points"]); mid = r.get("market_id")
        pool_name = (_get_market(mid) or {}).get("name", mid) if mid else "V Tech pool"
        have = (float(_db.get_market_loyalty(uid, mid).get("points", 0) or 0) if mid
                else float(_db.get_loyalty(uid).get("points", 0) or 0))
        if have < pts:
            return await interaction.response.send_message(
                f"❌ <@{uid}> now only has **{have:,.0f}** pts in **{pool_name}** — can't deduct **{pts:,}**. "
                f"Deny it or ask them to re-submit.", ephemeral=True)
        new_total = (_db.add_market_loyalty_points(uid, mid, -pts, update_activity=False) if mid
                    else _db.add_loyalty_points(uid, -pts, update_activity=False))
        r["status"] = "approved"; r["approved_by"] = str(interaction.user.id)
        _save_redemptions(reds)
        await interaction.response.send_message(
            f"✅ Approved **#{id}** — deducted **{pts:,}** pts from <@{uid}>'s **{pool_name}** balance "
            f"(now `{new_total:,.0f}`).", ephemeral=True)
        try:
            u = await interaction.client.fetch_user(int(uid))
            await u.send(f"✅ Your redemption **#{id}** (*{r['reward']}*) was approved — "
                         f"**{pts:,}** points deducted from **{pool_name}**. Enjoy your reward!")
        except Exception:
            pass

    @loyalty.command(name="deny", description="(Manager/market owner) Deny a redemption (no points deducted)")
    @app_commands.describe(id="Redemption ID", reason="Optional reason shown to the user")
    async def loyalty_deny(self, interaction: discord.Interaction, id: int, reason: str = ""):
        reds = _load_redemptions()
        r = reds.get(str(id))
        if not r:
            return await interaction.response.send_message(f"❌ No redemption #{id}.", ephemeral=True)
        if not self._can_action_redemption(interaction, r):
            return await interaction.response.send_message(
                "⛔ Managers only, or the owner of the market this redemption is scoped to.", ephemeral=True)
        if r.get("status") != "pending":
            return await interaction.response.send_message(
                f"⚠️ Redemption #{id} is already **{r.get('status')}**.", ephemeral=True)
        r["status"] = "denied"; r["denied_by"] = str(interaction.user.id); r["deny_reason"] = reason.strip()
        _save_redemptions(reds)
        await interaction.response.send_message(f"❌ Denied redemption **#{id}**. No points deducted.", ephemeral=True)
        try:
            u = await interaction.client.fetch_user(int(r["user_id"]))
            await u.send(f"❌ Your redemption **#{id}** (*{r['reward']}*) was denied."
                         + (f"\nReason: {reason.strip()}" if reason.strip() else ""))
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(LoyaltyCog(bot))
