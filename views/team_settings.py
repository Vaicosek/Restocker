"""TeamSettings — one panel replacing /team add · remove · name · list · mine · leaderboard.

`/me → Join a team` deliberately stays a command: it is the one WORKERS run, and making people
hunt for a panel to register their in-game name is exactly how IGNs end up unset — which
silently breaks CSN attribution for their sales.

Every action calls the same `Restocker_db` helpers the commands did, so team membership,
IGN linking and the manager override behave identically.
"""
import re
import sys

import discord

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log
MANAGER_OVERRIDE_ORDER_PCT = getattr(core, "MANAGER_OVERRIDE_ORDER_PCT", 0)
_IGN_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def _db():
    import Restocker_db as _d
    return _d


def _team_name(uid) -> str:
    try:
        return str(_db().get_config(f"team_name:{uid}") or "").strip()
    except Exception:
        return ""


def build_embed(manager_id: int) -> discord.Embed:
    d = _db()
    members = d.get_team(str(manager_id)) or []
    tn = _team_name(manager_id)
    e = discord.Embed(
        title=f"👥 TeamSettings — {tn or 'your team'}",
        description=(f"**{len(members)}** member(s) · you earn "
                     f"**{MANAGER_OVERRIDE_ORDER_PCT:g}%** on their order payouts."),
        color=0x22FF7A)
    if members:
        rows, missing = [], 0
        for w in members[:25]:
            ign = d.get_ign(w)
            if not ign:
                missing += 1
            rows.append(f"<@{w}> — " + (f"`{ign}`" if ign else "⚠️ no IGN"))
        e.add_field(name="Roster", value="\n".join(rows), inline=False)
        if missing:
            e.add_field(
                name="⚠️ Attribution gap",
                value=(f"{missing} member(s) have no in-game name linked, so their CSN sales "
                       f"and harvests can't be credited. Use **Add / link IGN**, or have them "
                       f"run `/me → Join a team`."),
                inline=False)
    else:
        e.add_field(name="Roster",
                    value="Empty — add someone, or have them run `/me → Join a team manager:@you ign:<name>`.",
                    inline=False)
    e.set_footer(text="IGNs are what link CSN sales back to a Discord account.")
    return e


class _PickMemberView(discord.ui.View):
    """Step 1: pick the person with Discord's own user picker — type-to-search.

    Modals may only contain TEXT inputs, so "Add member" as a single modal forced managers
    to hunt for a raw Discord id (Developer Mode → right-click → Copy User ID). That is a
    dead end for anyone who doesn't already know the trick, and it stalled real onboarding.
    A view can hold a UserSelect; a modal cannot. Hence two steps.
    """

    def __init__(self, panel, user_id: int):
        super().__init__(timeout=300)
        self.panel = panel
        self.user_id = int(user_id)
        sel = discord.ui.UserSelect(placeholder="Search for the member…", max_values=1)

        async def pick(i: discord.Interaction):
            await i.response.send_modal(_AddModal(self.panel, sel.values[0]))
        sel.callback = pick
        self.add_item(sel)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True


class _AddModal(discord.ui.Modal, title="Add member / link IGN"):
    def __init__(self, panel, member):
        super().__init__(timeout=300)
        self.panel, self.member = panel, member
        self.ign = discord.ui.TextInput(
            label=f"IGN for {getattr(member, 'display_name', member)}"[:45],
            placeholder="Exact in-game name (optional)", required=False)
        self.add_item(self.ign)

    async def on_submit(self, interaction: discord.Interaction):
        d = _db()
        raw = str(self.member.id)
        if int(raw) == int(self.panel.manager_id):
            return await interaction.response.send_message("❌ You can't add yourself.", ephemeral=True)
        existing = d.get_manager_of(raw)
        if existing and str(existing) != str(self.panel.manager_id):
            return await interaction.response.send_message(
                f"❌ <@{raw}> is already on <@{existing}>'s team.", ephemeral=True)
        note = ""
        ign = str(self.ign.value or "").strip()
        if ign:
            if not _IGN_RE.match(ign):
                return await interaction.response.send_message(
                    "❌ IGN must be 3–16 characters: letters, numbers, underscores.", ephemeral=True)
            owner = d.get_user_id_by_ign(ign)
            if owner and str(owner) != raw:
                return await interaction.response.send_message(
                    f"❌ `{ign}` is already linked to <@{owner}>.", ephemeral=True)
            d.set_ign(raw, ign)
            d.delete_ign_pending(raw)          # registered now → cancel any pending deadline
            note = f" · linked `{ign}`"
        d.set_team_member(raw, str(self.panel.manager_id))
        await self.panel.refresh(interaction, f"✅ Added <@{raw}>{note}.")


class _NameModal(discord.ui.Modal, title="Rename team"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.name = discord.ui.TextInput(label="Team name (blank clears it)",
                                         required=False, max_length=40)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        val = str(self.name.value or "").strip()[:40]
        _db().set_config(f"team_name:{self.panel.manager_id}", val)
        await self.panel.refresh(
            interaction, f"✅ Team renamed to **{val}**." if val else "✅ Team name cleared.")


class TeamSettingsView(discord.ui.View):
    def __init__(self, manager_id: int):
        super().__init__(timeout=300)
        self.manager_id = int(manager_id)
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.manager_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    def _build(self):
        self.clear_items()
        members = _db().get_team(str(self.manager_id)) or []
        if members:
            opts = []
            for w in members[:25]:
                ign = _db().get_ign(w)
                opts.append(discord.SelectOption(label=(ign or str(w))[:80], value=str(w),
                                                 description="remove from team"))
            sel = discord.ui.Select(placeholder="Remove a member…", options=opts, row=0)

            async def _remove(interaction: discord.Interaction):
                wid = sel.values[0]
                if str(_db().get_manager_of(wid)) != str(self.manager_id):
                    return await self.refresh(interaction, "That worker isn't on your team.")
                _db().remove_team_member(wid)
                await self.refresh(interaction, f"Removed <@{wid}>.")
            sel.callback = _remove
            self.add_item(sel)

        for label, style, cb, row in (
            ("Add / link IGN", discord.ButtonStyle.primary, self._add, 1),
            ("Rename team", discord.ButtonStyle.secondary, self._rename, 1),
            ("Leaderboard", discord.ButtonStyle.secondary, self._board, 1),
        ):
            b = discord.ui.Button(label=label, style=style, row=row)
            b.callback = cb
            self.add_item(b)

    async def refresh(self, interaction: discord.Interaction, note: str = ""):
        self._build()
        e = build_embed(self.manager_id)
        if note:
            e.description = note + "\n" + (e.description or "")
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=e, view=self)
            else:
                await interaction.response.edit_message(embed=e, view=self)
        except Exception as ex:
            log.debug("[team panel] refresh failed: %s", ex)

    async def _add(self, interaction: discord.Interaction):
        await interaction.response.send_message("Pick the member — start typing their name:",
            view=_PickMemberView(self, interaction.user.id), ephemeral=True)

    async def _rename(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_NameModal(self))

    async def _board(self, interaction: discord.Interaction):
        board = []
        try:
            # lives in Restocker_main; the team cog only aliases it
            board = core._all_teams_leaderboard(7) or []
        except Exception as ex:
            log.debug("[team panel] leaderboard failed: %s", ex)
        if not board:
            return await self.refresh(interaction, "No team activity in the last 7 days.")
        lines = []
        for i, tm in enumerate(board[:10], 1):
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            tn = _team_name(tm["manager_id"])
            label = f"**{tn}**" if tn else f"<@{tm['manager_id']}>'s team"
            lines.append(f"{medal} {label} — **{int(tm['total']):,}c** "
                         f"({tm['orders']} orders, sales {int(tm['sales_coins']):,}c)")
        e = discord.Embed(title="🏆 Team leaderboard — last 7d",
                          description="\n".join(lines), color=0x22FF7A)
        await interaction.response.send_message(embed=e, ephemeral=True)
