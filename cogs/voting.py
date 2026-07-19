"""Shareholder voting — real voting power for real owners.

Voting weight is HARD DATA like everything else on this exchange:
    weight = common shares held (the company's stock)
           + GEX.PR register share % × shares outstanding (preferred investors
             vote their slice of the company even without common shares)

/vote create (manager) opens a proposal and posts it to #investor-chat;
holders vote with /vote cast (re-voting just moves your weight); the vote loop
closes proposals at their deadline and posts weighted results. Weight is
snapshotted at cast time — buying shares after you voted? Cast again.
"""
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
_public_market_autocomplete = core._public_market_autocomplete
log = core.log


def _voting_weight(user_id: str, market_id: str) -> tuple:
    """(total_weight, common_shares, pref_equiv). Common shares count 1:1;
    GEX.PR register % converts to share-equivalents of the company."""
    import Restocker_db as _db
    uid = str(user_id)
    common = 0.0
    try:
        for h in (_db.get_portfolio(uid) or []):
            if str(h.get("market_id")) == str(market_id):
                common += float(h.get("shares") or 0)
    except Exception:
        pass
    pref = 0.0
    try:
        inv = (_db.get_investors() or {}).get(uid)
        if inv:
            so = float((_db.get_market_shares(market_id) or {}).get("shares_outstanding") or 0)
            pref = float(inv.get("share_pct") or 0) / 100.0 * so
    except Exception:
        pass
    return common + pref, common, pref


async def _proposal_autocomplete(interaction: discord.Interaction, current: str):
    import Restocker_db as _db
    out = []
    for p in (_db.list_proposals(status="open") or [])[:50]:
        label = f"#{p['id']} {p['question']}"
        if current and current.lower() not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=str(p["id"])))
    return out[:25]


async def _choice_autocomplete(interaction: discord.Interaction, current: str):
    import Restocker_db as _db
    pid = getattr(interaction.namespace, "proposal", None)
    try:
        p = _db.get_proposal(int(pid)) if pid else None
    except (TypeError, ValueError):
        p = None
    if not p:
        return []
    out = []
    for i, opt in enumerate(p.get("options") or []):
        if current and current.lower() not in str(opt).lower():
            continue
        out.append(app_commands.Choice(name=str(opt)[:100], value=str(i)))
    return out[:25]


class VotingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    vote = app_commands.Group(name="vote", description="Shareholder voting — weight = your stake")

    @vote.command(name="create", description="(Manager) Open a shareholder proposal — posts to #investor-chat")
    @app_commands.describe(
        question="What's being decided",
        options="Choices separated by | (e.g. 'Yes | No' or 'Build hives | Buy claims | Hold cash')",
        days="Voting window in days (default 3)",
        market_id="Which company's holders vote (default main)")
    @app_commands.autocomplete(market_id=_public_market_autocomplete)
    async def vote_create(self, interaction: discord.Interaction, question: str,
                          options: Optional[str] = None,
                          days: app_commands.Range[int, 1, 30] = 3,
                          market_id: str = "main"):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        opts = [o.strip() for o in (options or "Yes | No").split("|") if o.strip()]
        if len(opts) < 2:
            return await interaction.response.send_message(
                "❌ Give at least two options separated by `|`.", ephemeral=True)
        closes = (datetime.now(timezone.utc) + timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M")
        pid = _db.create_proposal(market_id, question.strip(), opts,
                                  str(interaction.user.id), closes)
        emb = discord.Embed(
            title=f"🗳️ Proposal #{pid} — {question.strip()}",
            description="\n".join(f"**{i+1}.** {o}" for i, o in enumerate(opts)),
            color=discord.Color.blurple())
        emb.add_field(name="How to vote",
                      value=f"`/vote cast` → pick proposal #{pid} · your weight = your shares "
                            f"+ GEX.PR stake · re-voting moves your weight", inline=False)
        emb.set_footer(text=f"{market_id} holders · closes {closes} UTC")
        ch = self.bot.get_channel(int(getattr(core, "INVESTOR_CHAT_CHANNEL_ID", 0) or 0))
        if ch:
            try:
                await ch.send(embed=emb)
            except Exception as e:
                log.warning("[vote] couldn't post proposal to investor-chat: %s", e)
        await interaction.response.send_message(
            f"✅ Proposal **#{pid}** open until {closes} UTC"
            + ("" if ch else " (couldn't post to #investor-chat — check channel access)"),
            ephemeral=True)

    @vote.command(name="cast", description="Vote on an open proposal — your weight is your stake")
    @app_commands.describe(proposal="Which proposal", choice="Your pick")
    @app_commands.autocomplete(proposal=_proposal_autocomplete, choice=_choice_autocomplete)
    async def vote_cast(self, interaction: discord.Interaction, proposal: str, choice: str):
        import Restocker_db as _db
        try:
            p = _db.get_proposal(int(proposal))
        except (TypeError, ValueError):
            p = None
        if not p or p.get("status") != "open":
            return await interaction.response.send_message("❌ That proposal isn't open.", ephemeral=True)
        try:
            idx = int(choice)
            label = (p["options"])[idx]
        except (TypeError, ValueError, IndexError):
            return await interaction.response.send_message("❌ Pick a choice from the list.", ephemeral=True)
        w, common, pref = _voting_weight(interaction.user.id, p["market_id"])
        if w <= 0:
            return await interaction.response.send_message(
                f"❌ No voting power — you hold no `{p['market_id']}` shares and aren't on "
                f"the GEX.PR register. Buy in with `/stock buy`.", ephemeral=True)
        _db.cast_vote(p["id"], str(interaction.user.id), idx, w,
                      name=getattr(interaction.user, "display_name", None))
        detail = f"`{common:,.0f}` shares" + (f" + `{pref:,.0f}` GEX.PR equivalent" if pref else "")
        await interaction.response.send_message(
            f"🗳️ Vote recorded: **{label}** with weight `{w:,.0f}` ({detail}). "
            f"Re-vote any time before it closes.", ephemeral=True)

    @vote.command(name="results", description="Standings of a proposal (live or final)")
    @app_commands.describe(proposal="Which proposal")
    @app_commands.autocomplete(proposal=_proposal_autocomplete)
    async def vote_results(self, interaction: discord.Interaction, proposal: str):
        import Restocker_db as _db
        try:
            p = _db.get_proposal(int(proposal))
        except (TypeError, ValueError):
            p = None
        if not p:
            return await interaction.response.send_message("❌ Unknown proposal.", ephemeral=True)
        await interaction.response.send_message(embed=_results_embed(p), ephemeral=True)

    @tasks.loop(minutes=10)
    async def vote_close_loop(self):
        """Close proposals past their deadline and post final results to #investor-chat."""
        try:
            import Restocker_db as _db
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            for p in _db.list_proposals(status="open"):
                if str(p.get("closes_at") or "") <= now:
                    _db.close_proposal(p["id"])
                    p["status"] = "closed"
                    ch = self.bot.get_channel(int(getattr(core, "INVESTOR_CHAT_CHANNEL_ID", 0) or 0))
                    if ch:
                        try:
                            await ch.send(embed=_results_embed(p, final=True))
                        except Exception as e:
                            log.warning("[vote] close post failed: %s", e)
        except Exception as e:
            log.warning("[vote] close loop error: %s", e)

    @vote_close_loop.before_loop
    async def _before_vote_close(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        if not self.vote_close_loop.is_running():
            self.vote_close_loop.start()

    async def cog_unload(self):
        self.vote_close_loop.cancel()


_SUGG_STATUS_ICON = {"new": "🆕", "planned": "🛠️", "done": "✅", "declined": "❌"}


async def _suggestion_autocomplete(interaction: discord.Interaction, current: str):
    import Restocker_db as _db
    out = []
    for s in (_db.list_suggestions(limit=50) or []):
        label = f"#{s['id']} [{s['status']}] {s['text'][:70]}"
        if current and current.lower() not in label.lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=str(s["id"])))
    return out[:25]


class SuggestCog(commands.Cog):
    """Investor request box — holders tell the company what they want to see."""

    def __init__(self, bot):
        self.bot = bot

    suggest = app_commands.Group(name="suggest",
                                 description="Tell V Tech what you want to see — requests go straight to management")

    @suggest.command(name="submit", description="Request a feature, product, market or change you want to see")
    @app_commands.describe(text="What do you want V Tech to build/change/offer?")
    async def suggest_submit(self, interaction: discord.Interaction,
                             text: app_commands.Range[str, 5, 500]):
        import Restocker_db as _db
        w, common, pref = _voting_weight(interaction.user.id, "main")
        sid = _db.create_suggestion(str(interaction.user.id),
                                    getattr(interaction.user, "display_name", None),
                                    w, text.strip())
        stake = (f"stake `{w:,.0f}`" if w > 0 else "no stake yet")
        emb = discord.Embed(
            title=f"💡 Investor request #{sid}",
            description=text.strip()[:2000],
            color=discord.Color.gold())
        emb.set_footer(text=f"from {getattr(interaction.user, 'display_name', 'holder')} · {stake} · "
                            f"management responds with /suggest respond")
        ch = self.bot.get_channel(int(getattr(core, "INVESTOR_CHAT_CHANNEL_ID", 0) or 0))
        if ch:
            try:
                await ch.send(embed=emb)
            except Exception as e:
                log.warning("[suggest] post failed: %s", e)
        await interaction.response.send_message(
            f"💡 Request **#{sid}** submitted" + (" and posted to investor chat." if ch else "."),
            ephemeral=True)

    @suggest.command(name="list", description="Browse investor requests and their status")
    @app_commands.describe(status="Filter by status")
    @app_commands.choices(status=[
        app_commands.Choice(name="new", value="new"),
        app_commands.Choice(name="planned", value="planned"),
        app_commands.Choice(name="done", value="done"),
        app_commands.Choice(name="declined", value="declined")])
    async def suggest_list(self, interaction: discord.Interaction, status: Optional[str] = None):
        import Restocker_db as _db
        rows = _db.list_suggestions(status=status, limit=15)
        if not rows:
            return await interaction.response.send_message("No requests yet — `/suggest submit`!", ephemeral=True)
        lines = []
        for s in rows:
            icon = _SUGG_STATUS_ICON.get(s["status"], "❔")
            line = f"{icon} **#{s['id']}** {s['text'][:120]} — *{s.get('name') or s['user_id']}*"
            if s.get("response"):
                line += f"\n    ↳ {s['response'][:150]}"
            lines.append(line)
        emb = discord.Embed(title="💡 Investor request box",
                            description="\n".join(lines)[:4000],
                            color=discord.Color.gold())
        emb.set_footer(text="🆕 new · 🛠️ planned · ✅ done · ❌ declined")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    @suggest.command(name="respond", description="(Manager) Answer a request — status + optional reply, DMs the submitter")
    @app_commands.describe(request="Which request", status="Verdict", response="Your reply to the investor")
    @app_commands.choices(status=[
        app_commands.Choice(name="planned — we'll build it", value="planned"),
        app_commands.Choice(name="done — it's live", value="done"),
        app_commands.Choice(name="declined", value="declined")])
    @app_commands.autocomplete(request=_suggestion_autocomplete)
    async def suggest_respond(self, interaction: discord.Interaction, request: str,
                              status: str, response: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        try:
            s = _db.get_suggestion(int(request))
        except (TypeError, ValueError):
            s = None
        if not s:
            return await interaction.response.send_message("❌ Unknown request.", ephemeral=True)
        _db.update_suggestion(s["id"], status, response)
        icon = _SUGG_STATUS_ICON.get(status, "❔")
        emb = discord.Embed(
            title=f"{icon} Request #{s['id']} → {status}",
            description=f"*{s['text'][:500]}*" + (f"\n\n**Management:** {response}" if response else ""),
            color=discord.Color.green() if status in ("planned", "done") else discord.Color.dark_grey())
        ch = self.bot.get_channel(int(getattr(core, "INVESTOR_CHAT_CHANNEL_ID", 0) or 0))
        if ch:
            try:
                await ch.send(embed=emb)
            except Exception:
                pass
        try:
            u = await self.bot.fetch_user(int(s["user_id"]))
            await u.send(embed=emb)
        except Exception:
            pass
        await interaction.response.send_message(
            f"{icon} Request #{s['id']} marked **{status}**" + (" · submitter DM'd." if s else ""),
            ephemeral=True)


def _results_embed(p, final: bool = False) -> discord.Embed:
    import Restocker_db as _db
    votes = _db.get_votes(p["id"])
    totals = [0.0] * len(p["options"])
    for v in votes:
        i = int(v["choice_idx"])
        if 0 <= i < len(totals):
            totals[i] += float(v["weight"] or 0)
    grand = sum(totals) or 1.0
    lines = []
    order = sorted(range(len(totals)), key=lambda i: -totals[i])
    for i in order:
        pct = 100.0 * totals[i] / grand
        bar = "█" * int(round(pct / 10)) or "▏"
        lines.append(f"{'🏆 ' if final and i == order[0] and totals[i] > 0 else ''}"
                     f"**{p['options'][i]}** — `{totals[i]:,.0f}` weight ({pct:.1f}%)\n{bar}")
    emb = discord.Embed(
        title=(f"🗳️ {'FINAL — ' if final else ''}Proposal #{p['id']} — {p['question']}"),
        description="\n".join(lines)[:4000],
        color=discord.Color.green() if final else discord.Color.blurple())
    emb.set_footer(text=f"{len(votes)} voter(s) · total weight {sum(totals):,.0f} · "
                        f"{'closed' if p['status'] == 'closed' else 'closes'} {p['closes_at']} UTC")
    return emb


async def setup(bot):
    await bot.add_cog(VotingCog(bot))
    await bot.add_cog(SuggestCog(bot))
