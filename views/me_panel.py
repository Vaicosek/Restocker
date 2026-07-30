"""MePanel — one `/me` for everything personal: balance, IGNs, team, loyalty.

Replaces four separate picker rows (/balance, /register_ign, /team join, /loyalty hub).
None of them were manager tools; they were the things an ordinary worker needs, scattered.

Nothing here is gated: every action is about the caller's OWN account. The guards that
DO matter are the anti-squatting ones carried over from /team join — an IGN holding
unpaid wages cannot be self-claimed, and the per-user IGN cap still applies.
"""
import re
import sys

import discord

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log
_IGN_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def _db():
    import Restocker_db as _d
    return _d


def _loy():
    return sys.modules.get("cogs.loyalty")


def _max_igns() -> int:
    return int(getattr(_loy(), "MAX_IGNS_PER_USER", 12) or 12)


def build_embed(user) -> discord.Embed:
    d = _db()
    uid = str(user.id)
    e = discord.Embed(title=f"👤 {getattr(user, 'display_name', user)}", color=0x3498DB)

    # ── coins (was /balance) ──
    try:
        bal = core._get_user_bal(core._load_balances()["users"], user.id)
        e.add_field(name="💰 Coins", value=f"**{bal['coins']:,}**", inline=True)
        e.add_field(name="Principal", value=f"`{bal.get('principal', bal['coins']):,}`", inline=True)
    except Exception as ex:
        log.debug("[me] balance failed: %s", ex)

    # ── loyalty (was /loyalty hub stats) ──
    try:
        pts = float((d.get_loyalty(uid) or {}).get("points", 0))
        tier = core._loyalty_tier(pts)
        em = (getattr(_loy(), "TIER_EMOJIS", {}) or {}).get(tier["tier"], "")
        e.add_field(name="🎖️ Loyalty", value=f"**{pts:,.0f}** pts · {em} {tier['name']}", inline=True)
    except Exception as ex:
        log.debug("[me] loyalty failed: %s", ex)

    # ── IGNs (was /register_ign) ──
    try:
        igns = d.get_igns(uid) or []
        e.add_field(name="🎮 In-game names",
                    value=(", ".join(f"`{g}`" for g in igns) if igns else
                           "*none linked — your wages can't reach you*"),
                    inline=False)
    except Exception as ex:
        log.debug("[me] igns failed: %s", ex)

    # ── team (was /team join) ──
    try:
        mgr = d.get_manager_of(uid)
        e.add_field(name="👥 Team", value=(f"<@{mgr}>'s team" if mgr else "*not on a team*"),
                    inline=False)
    except Exception as ex:
        log.debug("[me] team failed: %s", ex)

    e.set_footer(text="Only you can see this.")
    return e


class _IgnModal(discord.ui.Modal, title="Link an in-game name"):
    """Was /register_ign. Alts pool into one account, so this ADDS rather than replaces."""

    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.ign = discord.ui.TextInput(label="Your EXACT Minecraft name", required=True,
                                        max_length=16)
        self.add_item(self.ign)

    async def on_submit(self, i: discord.Interaction):
        lc = _loy()
        if lc is None or not hasattr(lc, "LoyaltyCog"):
            return await i.response.send_message("⚠️ The loyalty cog isn't loaded.", ephemeral=True)
        cog = None
        try:
            cog = core.bot.get_cog("LoyaltyCog")
        except Exception:
            pass
        if cog is None or not hasattr(cog, "_register_ign_impl"):
            return await i.response.send_message("⚠️ IGN registration is unavailable.", ephemeral=True)
        # Reuse the command's own implementation so the DM-verification flow, the
        # anti-squat check and the alt-pooling rules stay in exactly one place.
        await cog._register_ign_impl(i, str(self.ign.value or "").strip())


class _TeamIgnModal(discord.ui.Modal, title="Join a team"):
    """Step 2: the IGN. The MANAGER was already chosen with a native user picker, so
    nobody has to find a Discord ID."""

    def __init__(self, panel, manager: discord.abc.User):
        super().__init__(timeout=300)
        self.panel, self.manager = panel, manager
        self.ign = discord.ui.TextInput(
            label="Your EXACT Minecraft name", required=True, max_length=16,
            placeholder="case-sensitive, e.g. DivineAxo")
        self.add_item(self.ign)

    async def on_submit(self, i: discord.Interaction):
        d = _db()
        uid = str(i.user.id)
        raw = str(self.manager.id)
        ign = str(self.ign.value or "").strip()
        if raw == uid:
            return await i.response.send_message("❌ Pick a manager, not yourself.", ephemeral=True)
        if not _IGN_RE.match(ign):
            return await i.response.send_message(
                "❌ IGN must be 3–16 characters: letters, numbers, underscores.", ephemeral=True)
        owner = d.get_user_id_by_ign(ign)
        if owner and str(owner) != uid:
            return await i.response.send_message(
                f"❌ `{ign}` is already registered to someone else. Use your own exact name.",
                ephemeral=True)
        existing = d.get_manager_of(uid)
        if existing and str(existing) != raw:
            return await i.response.send_message(
                f"❌ You're already on <@{existing}>'s team — ask them to remove you in "
                f"`/team settings` first.", ephemeral=True)
        try:
            pend = d.ign_unpaid_value(ign)
        except Exception:
            pend = 0
        if pend > 0:
            return await i.response.send_message(
                f"⚠️ `{ign}` has **{int(pend):,}** coins of unpaid harvests waiting, so it "
                f"can't be self-claimed. Ask a manager to link it (they'll verify it's yours).",
                ephemeral=True)
        try:
            if (d.count_igns(uid) >= _max_igns()
                    and ign not in (d.get_igns(uid) or [])):
                return await i.response.send_message(
                    f"❌ You've hit the max of **{_max_igns()}** in-game names. Ask a manager "
                    f"to unlink one you no longer use first.", ephemeral=True)
        except Exception:
            pass
        d.set_ign(uid, ign)
        try:
            d.delete_ign_pending(uid)
        except Exception as ex:
            log.warning("[me] delete_ign_pending failed for %s: %s", uid, ex)
        d.set_team_member(uid, raw)
        try:
            await self.manager.send(f"<@{uid}> (IGN `{ign}`) joined your team.")
        except Exception:
            pass
        await i.response.send_message(
            f"✅ Joined {self.manager.mention}'s team as `{ign}`. Your orders and tracked "
            f"sales now credit them.", ephemeral=True)


class _PickManagerView(discord.ui.View):
    """Step 1: choose a TEAM, not a person.

    A generic user picker asked the wrong question — a new worker is told "join Pollum
    sector", so they know the TEAM, not which Discord account manages it. This lists the
    teams that actually exist, named, with their size. The user picker stays as a fallback
    for a manager who has no roster yet and so does not appear in the list.
    """

    def __init__(self, panel, user_id: int):
        super().__init__(timeout=300)
        self.panel = panel
        self.user_id = int(user_id)
        d = _db()
        opts = []
        try:
            for mgr in (d.get_all_team_managers() or []):
                mid = str(mgr)
                if mid == str(user_id):
                    continue                      # can't join your own team
                try:
                    name = str(d.get_config(f"team_name:{mid}") or "").strip()
                except Exception:
                    name = ""
                size = len(d.get_team(mid) or [])
                who = None
                try:
                    who = core.bot.get_user(int(mid))
                except Exception:
                    pass
                label = name or (getattr(who, "display_name", None) or f"Team {mid[:6]}")
                desc = (f"led by {who.display_name}" if who and name else "")
                desc = (desc + (" · " if desc else "") + f"{size} member(s)")[:100]
                opts.append(discord.SelectOption(label=label[:100], value=mid, description=desc))
        except Exception as ex:
            log.debug("[me] team list failed: %s", ex)

        if opts:
            sel = discord.ui.Select(placeholder="Pick your team…", options=opts[:25], row=0)

            async def pick_team(i: discord.Interaction):
                mgr = core.bot.get_user(int(sel.values[0]))
                if mgr is None:
                    try:
                        mgr = await core.bot.fetch_user(int(sel.values[0]))
                    except Exception:
                        return await i.response.send_message(
                            "❌ Couldn't resolve that team's manager.", ephemeral=True)
                await i.response.send_modal(_TeamIgnModal(self.panel, mgr))
            sel.callback = pick_team
            self.add_item(sel)

        us = discord.ui.UserSelect(
            placeholder=("Or search for a manager by name…" if opts
                         else "Search for your manager…"),
            max_values=1, row=1)

        async def pick_user(i: discord.Interaction):
            await i.response.send_modal(_TeamIgnModal(self.panel, us.values[0]))
        us.callback = pick_user
        self.add_item(us)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True


class MePanelView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = int(user_id)
        for label, cb, style in (
            ("Link in-game name", self._ign, discord.ButtonStyle.success),
            ("Join a team", self._team, discord.ButtonStyle.secondary),
            ("Loyalty & rewards", self._loyalty, discord.ButtonStyle.primary),
        ):
            b = discord.ui.Button(label=label, style=style, row=0)
            b.callback = cb
            self.add_item(b)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    async def refresh(self, i: discord.Interaction, note: str = ""):
        e = build_embed(i.user)
        if note:
            e.description = note
        try:
            if i.response.is_done():
                await i.edit_original_response(embed=e, view=self)
            else:
                await i.response.edit_message(embed=e, view=self)
        except Exception as ex:
            log.debug("[me] refresh failed: %s", ex)

    async def _ign(self, i): await i.response.send_modal(_IgnModal(self))
    async def _team(self, i: discord.Interaction):
        await i.response.send_message(
            "Pick your team:",
            view=_PickManagerView(self, i.user.id), ephemeral=True)

    async def _loyalty(self, i: discord.Interaction):
        from views.loyalty_settings import LoyaltyHubView, build_hub_embed
        is_mgr = False
        try:
            is_mgr = bool(core._ai_is_manager(i.user))
        except Exception:
            pass
        await i.response.send_message(embed=build_hub_embed(i.user, i.guild),
                                      view=LoyaltyHubView(i.user.id, is_mgr), ephemeral=True)
