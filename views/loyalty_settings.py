"""LoyaltySettings — one panel replacing the 7 manager-side /loyalty subcommands.

Kept as commands on purpose: `stats`, `leaderboard`, `redeem` and `redemptions`. Those
are what MEMBERS use, and burying "spend my points" behind a manager panel is how a
reward system quietly stops being used.

Guards reproduced from the commands:
* link — IGN format, "already linked to someone else", and the MAX_IGNS_PER_USER cap.
* unlink — one alt or all of them, and it reports what's left.
* remind_unlinked — dry run by default; the deadline that STRIPS ROLES stays opt-in and
  is never enabled from a button.
"""
import re
import sys

import discord

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log


def _loy():
    """cogs.loyalty owns MAX_IGNS_PER_USER and TIER_EMOJIS — NOT core. Reading them off
    `core` silently fell back to wrong defaults (5 instead of 12, no emojis)."""
    return sys.modules.get("cogs.loyalty")


def _max_igns() -> int:
    return int(getattr(_loy(), "MAX_IGNS_PER_USER", 12) or 12)


def _tier_emojis() -> dict:
    return getattr(_loy(), "TIER_EMOJIS", {}) or {}


_IGN_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def _db():
    import Restocker_db as _d
    return _d


def _is_mgr(user) -> bool:
    try:
        return bool(core._ai_is_manager(user))
    except Exception:
        return False


def build_embed(guild) -> discord.Embed:
    d = _db()
    e = discord.Embed(title="🎖️ LoyaltySettings", color=0xF1C40F,
                      description="Points, IGN links, and chasing the people who haven't linked one.")
    unlinked = []
    try:
        roles = set(getattr(core, "LOYALTY_EMPLOYEE_ROLES", []) or [])
        if guild is not None and roles:
            for m in guild.members:
                if m.bot:
                    continue
                if any(r.name in roles for r in m.roles) and not d.get_ign(str(m.id)):
                    unlinked.append(m)
    except Exception as ex:
        log.debug("[loyalty panel] unlinked scan failed: %s", ex)
    if unlinked:
        e.add_field(
            name=f"⚠️ {len(unlinked)} employee(s) with no IGN",
            value=(", ".join(m.mention for m in unlinked[:15])
                   + (f" +{len(unlinked)-15} more" if len(unlinked) > 15 else "")
                   + "\nTheir sales and harvests credit nobody until they link one."),
            inline=False)
    else:
        e.add_field(name="IGN coverage", value="✅ everyone with an employee role is linked.",
                    inline=False)
    try:
        # _load_redemptions() lives in cogs.loyalty (not core, not the db layer).
        reds = ((_loy()._load_redemptions() if _loy() else {}) or {}).values()
        pend = [r for r in reds if r.get("status") == "pending"]
        if pend:
            e.add_field(name="Pending redemptions",
                        value=f"**{len(pend)}** waiting — approve/reject on each ticket.",
                        inline=False)
    except Exception as ex:
        log.debug("[loyalty panel] redemption count failed: %s", ex)
    e.set_footer(text="Members keep /loyalty stats · leaderboard · redeem.")
    return e


class _PointsModal(discord.ui.Modal):
    def __init__(self, panel, mode):
        super().__init__(title=("Add points" if mode == "add" else "Set points"), timeout=300)
        self.panel, self.mode = panel, mode
        self.uid = discord.ui.TextInput(label="Discord user id", required=True)
        self.pts = discord.ui.TextInput(label="Points", required=True)
        self.reason = discord.ui.TextInput(label="Reason", required=False, default="manual")
        self.add_item(self.uid); self.add_item(self.pts)
        if mode == "add":
            self.add_item(self.reason)

    async def on_submit(self, i: discord.Interaction):
        raw = str(self.uid.value).strip().strip("<@!>")
        if not raw.isdigit():
            return await i.response.send_message("❌ That isn't a user id.", ephemeral=True)
        try:
            pts = float(str(self.pts.value).strip())
        except Exception:
            return await i.response.send_message("❌ Points must be a number.", ephemeral=True)
        if self.mode == "add":
            new_total, old_t, new_t = core._award_loyalty_points(
                int(raw), int(pts), reason=str(self.reason.value or "manual"))
            up = " 🏆 tier up!" if new_t["tier"] > old_t["tier"] else ""
            return await self.panel.refresh(
                i, f"✅ Added {pts:,.0f} pts to <@{raw}> → {new_total:,.0f} total{up}")
        new = _db().set_loyalty_points(raw, pts)
        tier = core._loyalty_tier(new)
        await self.panel.refresh(
            i, f"✅ <@{raw}> set to {new:,.0f} pts — "
               f"{_tier_emojis().get(tier['tier'], '')} {tier['name']}")


class _LinkModal(discord.ui.Modal, title="Link an IGN"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.uid = discord.ui.TextInput(label="Discord user id", required=True)
        self.ign = discord.ui.TextInput(label="Exact in-game name", required=True)
        self.add_item(self.uid); self.add_item(self.ign)

    async def on_submit(self, i: discord.Interaction):
        d = _db()
        raw = str(self.uid.value).strip().strip("<@!>")
        ign = str(self.ign.value).strip()
        if not raw.isdigit():
            return await i.response.send_message("❌ That isn't a user id.", ephemeral=True)
        if not _IGN_RE.match(ign):
            return await i.response.send_message(
                "❌ IGN must be 3–16 characters: letters, numbers, underscores.", ephemeral=True)
        owner = d.get_user_id_by_ign(ign)
        if owner and str(owner) != raw:
            return await i.response.send_message(
                f"❌ `{ign}` is already linked to <@{owner}>. Unlink it from them first.",
                ephemeral=True)
        if str(owner) == raw:
            return await i.response.send_message(f"ℹ️ <@{raw}> already has `{ign}`.", ephemeral=True)
        if d.count_igns(raw) >= _max_igns():
            return await i.response.send_message(
                f"❌ <@{raw}> already has the max of {_max_igns()} IGNs. Unlink one first.",
                ephemeral=True)
        d.add_ign(raw, ign)
        d.delete_ign_pending(raw)          # linked now → cancel any role-strip deadline
        igns = d.get_igns(raw)
        extra = (" They now have: " + ", ".join(f"`{g}`" for g in igns)) if len(igns) > 1 else ""
        await self.panel.refresh(i, f"🔗 <@{raw}> → **{ign}**. Their CSN sales credit them now.{extra}")


class _UnlinkModal(discord.ui.Modal, title="Unlink an IGN"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.uid = discord.ui.TextInput(label="Discord user id", required=True)
        self.ign = discord.ui.TextInput(label="One IGN (blank = ALL of theirs)", required=False)
        self.add_item(self.uid); self.add_item(self.ign)

    async def on_submit(self, i: discord.Interaction):
        d = _db()
        raw = str(self.uid.value).strip().strip("<@!>")
        if not raw.isdigit():
            return await i.response.send_message("❌ That isn't a user id.", ephemeral=True)
        current = d.get_igns(raw)
        if not current:
            return await i.response.send_message(f"<@{raw}> has no IGN linked.", ephemeral=True)
        ign = str(self.ign.value or "").strip()
        if ign:
            if d.remove_ign(raw, ign):
                left = d.get_igns(raw)
                return await self.panel.refresh(
                    i, f"🔓 Removed `{ign}` from <@{raw}>. Remaining: "
                       + (", ".join(f"`{g}`" for g in left) if left else "*none*"))
            return await i.response.send_message(
                f"❌ <@{raw}> has no `{ign}`. They have: " + ", ".join(f"`{g}`" for g in current),
                ephemeral=True)
        d.delete_ign(raw)
        await self.panel.refresh(i, f"🔓 Removed all {len(current)} IGN(s) from <@{raw}>.")


class _WhoisModal(discord.ui.Modal, title="Who holds an IGN?"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.ign = discord.ui.TextInput(label="In-game name", required=True)
        self.add_item(self.ign)

    async def on_submit(self, i: discord.Interaction):
        d = _db()
        ign = str(self.ign.value).strip()
        uid = d.get_user_id_by_ign(ign)
        if not uid:
            try:
                pend = d.ign_unpaid_value(ign)
            except Exception:
                pend = 0
            extra = (f" It has **{int(pend):,}** coins of unpaid harvests waiting, so only a "
                     f"manager can link it." if pend else "")
            return await i.response.send_message(
                f"🔎 `{ign}` isn't registered to anyone.{extra}", ephemeral=True)
        others = [g for g in d.get_igns(uid) if g.lower() != ign.lower()]
        alts = ("\nOther IGNs: " + ", ".join(f"`{g}`" for g in others)) if others else ""
        await i.response.send_message(
            f"🔎 `{ign}` → <@{uid}>.{alts}\nTo move it: Unlink from them, then Link to the "
            f"rightful owner.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())


class LoyaltySettingsView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = int(user_id)
        for label, style, cb, row in (
            ("Add points", discord.ButtonStyle.primary, self._add, 0),
            ("Set points", discord.ButtonStyle.secondary, self._set, 0),
            ("Link IGN", discord.ButtonStyle.success, self._link, 0),
            ("Unlink IGN", discord.ButtonStyle.secondary, self._unlink, 0),
            ("Who holds…", discord.ButtonStyle.secondary, self._whois, 0),
            ("Remind unlinked (preview)", discord.ButtonStyle.secondary, self._remind, 1),
        ):
            b = discord.ui.Button(label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        if not _is_mgr(i.user):
            await i.response.send_message("⛔ Managers only.", ephemeral=True)
            return False
        return True

    async def refresh(self, i: discord.Interaction, note: str = ""):
        e = build_embed(i.guild)
        if note:
            e.description = note
        try:
            if i.response.is_done():
                await i.edit_original_response(embed=e, view=self)
            else:
                await i.response.edit_message(embed=e, view=self)
        except Exception as ex:
            log.debug("[loyalty panel] refresh failed: %s", ex)

    async def _add(self, i):    await i.response.send_modal(_PointsModal(self, "add"))
    async def _set(self, i):    await i.response.send_modal(_PointsModal(self, "set"))
    async def _link(self, i):   await i.response.send_modal(_LinkModal(self))
    async def _unlink(self, i): await i.response.send_modal(_UnlinkModal(self))
    async def _whois(self, i):  await i.response.send_modal(_WhoisModal(self))

    async def _remind(self, i: discord.Interaction):
        """Preview only. The command's `set_deadline` option starts a countdown that
        STRIPS the employee role of anyone who doesn't reply — that is not something a
        single button click should ever trigger, so the panel never offers it."""
        d = _db()
        roles = set(getattr(core, "LOYALTY_EMPLOYEE_ROLES", []) or [])
        guild = i.guild
        if guild is None or not roles:
            return await i.response.send_message("Run this in the server.", ephemeral=True)
        targets = [m for m in guild.members
                   if not m.bot and any(r.name in roles for r in m.roles)
                   and not d.get_ign(str(m.id)) and not d.get_ign_pending(str(m.id))]
        if not targets:
            return await i.response.send_message(
                "✅ Everyone is linked or already prompted.", ephemeral=True)
        await i.response.send_message(
            f"**{len(targets)}** employee(s) would be DM'd:\n"
            + ", ".join(m.mention for m in targets[:30])
            + (f" +{len(targets)-30} more" if len(targets) > 30 else "")
            + "\n\nThis panel only previews. To actually send, ask the bot to remind "
              "unlinked employees — and it will confirm before touching deadlines.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
