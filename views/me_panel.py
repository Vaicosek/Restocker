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


class _TeamModal(discord.ui.Modal, title="Join a team"):
    """Was /team join. Reproduces all three guards: IGN already owned by someone else,
    already on another manager's team, and money-bearing IGNs can't be self-claimed."""

    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.mgr = discord.ui.TextInput(label="Manager's Discord user id", required=True,
                                        placeholder="right-click them → Copy User ID")
        self.ign = discord.ui.TextInput(label="Your EXACT Minecraft name", required=True,
                                        max_length=16)
        self.add_item(self.mgr); self.add_item(self.ign)

    async def on_submit(self, i: discord.Interaction):
        d = _db()
        uid = str(i.user.id)
        raw = str(self.mgr.value or "").strip().strip("<@!>")
        ign = str(self.ign.value or "").strip()
        if not raw.isdigit():
            return await i.response.send_message("❌ That isn't a user id.", ephemeral=True)
        if raw == uid:
            return await i.response.send_message("❌ Pick a real manager, not yourself.", ephemeral=True)
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
        # Registering clears any pending role-strip deadline — every registration path
        # MUST do this or the deadline loop strips the role of someone who did register.
        try:
            d.delete_ign_pending(uid)
        except Exception as ex:
            log.warning("[me] delete_ign_pending failed for %s: %s", uid, ex)
        d.set_team_member(uid, raw)          # NOT set_manager_of — that does not exist
        try:
            mgr_user = core.bot.get_user(int(raw))
            if mgr_user:
                await mgr_user.send(f"<@{uid}> (IGN `{ign}`) joined your team.")
        except Exception:
            pass
        await self.panel.refresh(i, f"✅ Joined <@{raw}>'s team as `{ign}`.")


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
    async def _team(self, i): await i.response.send_modal(_TeamModal(self))

    async def _loyalty(self, i: discord.Interaction):
        from views.loyalty_settings import LoyaltyHubView, build_hub_embed
        is_mgr = False
        try:
            is_mgr = bool(core._ai_is_manager(i.user))
        except Exception:
            pass
        await i.response.send_message(embed=build_hub_embed(i.user, i.guild),
                                      view=LoyaltyHubView(i.user.id, is_mgr), ephemeral=True)
