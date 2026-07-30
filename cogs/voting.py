"""Shareholder voting — real voting power for real owners.

Voting weight is HARD DATA like everything else on this exchange:
    weight = common shares held (the company's stock)
           + GEX.PR register share % × shares outstanding (preferred investors
             vote their slice of the company even without common shares)

A manager opens a proposal from /investor -> New proposal (posts to #investor-chat);
holders vote from /investor -> Vote or on the dashboard Investor page (re-voting just
moves your weight); the vote loop
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


def _results_embed(p: dict, final: bool = False) -> discord.Embed:
    """Standings for a proposal.

    PRE-EXISTING BUG: this was called by vote_close_loop and by /vote results but was
    never defined anywhere. The loop's `except Exception` swallowed the NameError, so
    proposals closed correctly and their FINAL RESULTS were never posted to
    #investor-chat — silently, for as long as voting has existed.
    """
    import Restocker_db as _db
    opts = p.get("options") or []
    votes = _db.get_votes(p["id"]) or []
    tally = [0.0] * len(opts)
    for v in votes:
        try:
            idx = int(v.get("choice_idx", 0))
        except Exception:
            continue
        if 0 <= idx < len(tally):
            tally[idx] += float(v.get("weight") or 0)
    total = sum(tally) or 1.0
    lines = []
    top = max(range(len(tally)), key=lambda i: tally[i]) if tally else None
    for i, o in enumerate(opts):
        pct = tally[i] / total * 100.0
        bar = "█" * int(round(pct / 5)) or "·"
        mark = " 🏆" if (final and i == top and tally[i] > 0) else ""
        lines.append(f"**{o}**{mark}\n`{bar:<20}` {pct:5.1f}%  ({tally[i]:,.0f})")
    e = discord.Embed(
        title=("🏁 Final — " if final else "🗳️ Standings — ") + f"#{p['id']} {p.get('question','')}",
        description="\n".join(lines) or "*no options*",
        color=discord.Color.green() if final else discord.Color.blurple())
    e.add_field(name="Turnout",
                value=f"**{len(votes)}** voter(s) · total weight `{sum(tally):,.0f}`", inline=True)
    e.set_footer(text=f"{p.get('market_id','?')} holders · "
                      + ("closed" if final or p.get("status") != "open"
                         else f"closes {p.get('closes_at','?')} UTC"))
    return e


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





    @app_commands.command(
        name="investor",
        description="Investor hub — holdings, shareholder votes, and the dashboard link")
    async def investor_hub(self, interaction: discord.Interaction):
        """Replaces /vote create|cast|results. Portfolio detail, dividend history and
        company backing live on the dashboard — embeds render tables badly — so this
        panel keeps voting and links out for the rest."""
        from views.investor_hub import InvestorHubView, build_embed
        await interaction.response.send_message(
            embed=build_embed(interaction.user),
            view=InvestorHubView(interaction.user.id, is_manager(interaction)),
            ephemeral=True)

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


async def setup(bot):
    await bot.add_cog(VotingCog(bot))
    await bot.add_cog(SuggestCog(bot))
