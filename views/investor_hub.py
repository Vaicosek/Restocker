"""InvestorHub — the Discord half of the investor surface.

Deliberately THIN. The dashboard's /investor page owns anything that wants a table or a
series (portfolio P/L, dividend history, per-company backing and bond coverage) because
Discord embeds render those badly. What stays here is the one thing a shareholder should
not have to open a browser for: casting a vote.

Replaces /vote create, /vote cast and /vote results.
"""
import sys
from datetime import datetime, timedelta, timezone

import discord

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log


def _db():
    import Restocker_db as _d
    return _d


def _vc():
    """cogs.voting owns _voting_weight and _results_embed — NOT core."""
    return sys.modules.get("cogs.voting")


def _is_mgr(user) -> bool:
    try:
        return bool(core._ai_is_manager(user))
    except Exception:
        return False


def _weight(uid, mid) -> tuple:
    vc = _vc()
    if not vc:
        return (0.0, 0.0, 0.0)
    try:
        return vc._voting_weight(str(uid), mid)
    except Exception:
        return (0.0, 0.0, 0.0)


def build_embed(user) -> discord.Embed:
    d = _db()
    url = getattr(core, "DASHBOARD_URL", "")
    e = discord.Embed(
        title="📈 Investor hub",
        description=(f"Portfolio, dividends, company detail and backing live on the "
                     f"dashboard: {url}/investor\nVoting is here."),
        color=0x3498DB)
    try:
        holds = d.get_portfolio(str(user.id)) or []
        if holds:
            lines = []
            for h in holds[:10]:
                mid = h.get("market_id")
                price = float((d.get_market_shares(mid) or {}).get("share_price") or 0)
                sh = float(h.get("shares") or 0)
                lines.append(f"`{mid}` — **{sh:,.0f}** sh · `{sh*price:,.0f}`c")
            e.add_field(name="Your holdings", value="\n".join(lines), inline=False)
        else:
            e.add_field(name="Your holdings", value="*none — buy on the dashboard exchange*",
                        inline=False)
    except Exception as ex:
        log.debug("[investor hub] holdings failed: %s", ex)
    try:
        openp = [p for p in (d.list_proposals(status="open") or [])]
        e.add_field(name="Open proposals",
                    value=(f"**{len(openp)}** — use the buttons below" if openp else "*none*"),
                    inline=False)
    except Exception:
        pass
    e.set_footer(text="Vote weight = your shares + GEX.PR equivalent. Re-voting moves your weight.")
    return e


class _CreateModal(discord.ui.Modal, title="Open a shareholder proposal"):
    """Was /vote create. Managers only — checked again on submit, not just at render."""

    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.q = discord.ui.TextInput(label="Question", required=True, max_length=200)
        self.opts = discord.ui.TextInput(
            label="Options separated by |", required=False, default="Yes | No")
        self.days = discord.ui.TextInput(label="Days open (default 3)", required=False, default="3")
        self.mid = discord.ui.TextInput(label="Company id", required=False, default="main")
        self.add_item(self.q); self.add_item(self.opts); self.add_item(self.days); self.add_item(self.mid)

    async def on_submit(self, i: discord.Interaction):
        if not _is_mgr(i.user):
            return await i.response.send_message("⛔ Managers only.", ephemeral=True)
        d = _db()
        opts = [o.strip() for o in str(self.opts.value or "Yes | No").split("|") if o.strip()]
        if len(opts) < 2:
            return await i.response.send_message(
                "❌ Give at least two options separated by `|`.", ephemeral=True)
        try:
            days = max(1, min(int(str(self.days.value or "3").strip() or 3), 30))
        except Exception:
            return await i.response.send_message("❌ Days must be a number.", ephemeral=True)
        mid = str(self.mid.value or "main").strip() or "main"
        closes = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        pid = d.create_proposal(mid, str(self.q.value).strip(), opts, str(i.user.id), closes)

        emb = discord.Embed(
            title=f"🗳️ Proposal #{pid} — {str(self.q.value).strip()}",
            description="\n".join(f"**{n+1}.** {o}" for n, o in enumerate(opts)),
            color=discord.Color.blurple())
        emb.add_field(name="How to vote",
                      value=f"`/investor` → **Vote** · or on the dashboard: "
                            f"{getattr(core, 'DASHBOARD_URL', '')}/investor\n"
                            f"Weight = your shares + GEX.PR stake; re-voting moves your weight.",
                      inline=False)
        emb.set_footer(text=f"{mid} holders · closes {closes} UTC")
        ch = core.bot.get_channel(int(getattr(core, "INVESTOR_CHAT_CHANNEL_ID", 0) or 0))
        posted = False
        if ch:
            try:
                await ch.send(embed=emb)
                posted = True
            except Exception as ex:
                log.warning("[investor hub] couldn't post proposal: %s", ex)
        await self.panel.refresh(
            i, f"✅ Proposal **#{pid}** open until {closes} UTC"
               + ("" if posted else " (couldn't post to #investor-chat — check channel access)"))


class _VoteView(discord.ui.View):
    """One proposal's choices as buttons. Weight is recomputed server-side on press."""

    def __init__(self, proposal, user_id: int):
        super().__init__(timeout=300)
        self.p = proposal
        self.user_id = int(user_id)
        for idx, label in enumerate((proposal.get("options") or [])[:20]):
            b = discord.ui.Button(label=str(label)[:80], style=discord.ButtonStyle.secondary,
                                  row=min(idx // 5, 4))
            b.callback = self._make(idx, str(label))
            self.add_item(b)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This ballot isn't yours.", ephemeral=True)
            return False
        return True

    def _make(self, idx, label):
        async def cb(i: discord.Interaction):
            d = _db()
            p = d.get_proposal(self.p["id"])
            # Re-read: the proposal may have closed since the panel was opened.
            if not p or p.get("status") != "open":
                return await i.response.send_message("❌ That proposal isn't open.", ephemeral=True)
            w, common, pref = _weight(i.user.id, p["market_id"])
            if w <= 0:
                return await i.response.send_message(
                    f"❌ No voting power — you hold no `{p['market_id']}` shares and aren't on "
                    f"the GEX.PR register. Buy in on the dashboard exchange.", ephemeral=True)
            d.cast_vote(p["id"], str(i.user.id), idx, float(w),
                        name=getattr(i.user, "display_name", None))
            detail = f"`{common:,.0f}` shares" + (f" + `{pref:,.0f}` GEX.PR equivalent" if pref else "")
            await i.response.send_message(
                f"🗳️ Vote recorded: **{label}** with weight `{w:,.0f}` ({detail}). "
                f"Re-vote any time before it closes.", ephemeral=True)
        return cb


class _PickView(discord.ui.View):
    """Choose which open proposal to vote on, then hand off to _VoteView."""

    def __init__(self, proposals, user_id: int, results: bool = False):
        super().__init__(timeout=300)
        self.user_id = int(user_id)
        self.results = results
        opts = [discord.SelectOption(label=f"#{p['id']} {str(p.get('question') or '')[:70]}",
                                     value=str(p["id"]))
                for p in proposals[:25]]
        sel = discord.ui.Select(placeholder="Proposal…", options=opts)

        async def pick(i: discord.Interaction):
            d = _db()
            p = d.get_proposal(int(sel.values[0]))
            if not p:
                return await i.response.send_message("❌ Unknown proposal.", ephemeral=True)
            if self.results:
                vc = _vc()
                if not vc:
                    return await i.response.send_message("⚠️ Voting cog isn't loaded.", ephemeral=True)
                return await i.response.send_message(embed=vc._results_embed(p), ephemeral=True)
            if p.get("status") != "open":
                return await i.response.send_message("❌ That proposal isn't open.", ephemeral=True)
            w = _weight(i.user.id, p["market_id"])[0]
            e = discord.Embed(
                title=f"🗳️ #{p['id']} — {p.get('question')}",
                description=f"Your weight: **{w:,.0f}**\nCloses {p.get('closes_at')} UTC",
                color=discord.Color.blurple())
            await i.response.send_message(embed=e, view=_VoteView(p, i.user.id), ephemeral=True)
        sel.callback = pick
        self.add_item(sel)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if int(i.user.id) != self.user_id:
            await i.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True


class InvestorHubView(discord.ui.View):
    def __init__(self, user_id: int, is_manager: bool = False):
        super().__init__(timeout=300)
        self.user_id = int(user_id)
        url = getattr(core, "DASHBOARD_URL", "")
        for label, cb in (("Vote", self._vote), ("Results", self._results)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, row=0)
            b.callback = cb
            self.add_item(b)
        if is_manager:
            b = discord.ui.Button(label="New proposal", style=discord.ButtonStyle.success, row=0)
            b.callback = self._create
            self.add_item(b)
        if url:
            self.add_item(discord.ui.Button(label="Open dashboard", style=discord.ButtonStyle.link,
                                            url=f"{url}/investor", row=1))

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
            log.debug("[investor hub] refresh failed: %s", ex)

    async def _vote(self, i: discord.Interaction):
        props = _db().list_proposals(status="open") or []
        if not props:
            return await i.response.send_message("No open proposals.", ephemeral=True)
        await i.response.send_message("Pick a proposal:", view=_PickView(props, i.user.id),
                                      ephemeral=True)

    async def _results(self, i: discord.Interaction):
        props = _db().list_proposals() or []
        if not props:
            return await i.response.send_message("No proposals yet.", ephemeral=True)
        await i.response.send_message("Pick a proposal:",
                                      view=_PickView(props, i.user.id, results=True), ephemeral=True)

    async def _create(self, i: discord.Interaction):
        if not _is_mgr(i.user):
            return await i.response.send_message("⛔ Managers only.", ephemeral=True)
        await i.response.send_modal(_CreateModal(self))
