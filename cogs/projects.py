"""Team projects — fixed-budget tasking. A funder hands a manager a budget to "make
something happen"; the manager pays out their team from it and keeps whatever's left.
No escrow / approval / shares — the manager has full discretion (that's the point).
`/project pay` distributes to team members with loyalty points + leaderboard credit;
every coin move is recorded to the coin ledger."""
import sys

import discord
from discord import app_commands
from discord.ext import commands

import Restocker_db as db

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
add_coins = core.add_coins
deduct_coins = core.deduct_coins
_award_loyalty_points = core._award_loyalty_points
bot = core.bot


class ProjectsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    project = app_commands.Group(name="project",
                                 description="Hand a manager a budget to build something; they pay their team and keep the rest")

    @project.command(name="create", description="Fund a manager with a budget to make something happen")
    @app_commands.describe(manager="The manager who'll run it", budget="Coins to hand them", title="What to build")
    async def create(self, interaction: discord.Interaction, manager: discord.Member,
                     budget: app_commands.Range[int, 1, 1_000_000_000], title: str):
        funder = interaction.user
        if manager.bot or manager.id == funder.id:
            # Self-funding is a free round-trip (deduct N, add N back) that would mint
            # budget//1000 loyalty points per run — and points drive weekly interest (real
            # coins). Same guard class as futures' no-self-approval.
            return await interaction.response.send_message(
                "Pick a real manager (not yourself or a bot).", ephemeral=True)
        bal = int(db.get_balance(str(funder.id)).get("coins") or 0)
        if bal < budget:
            return await interaction.response.send_message(
                f"You need `{budget:,}` coins but have `{bal:,}`.", ephemeral=True)
        title = title.strip()[:100]
        deduct_coins(funder.id, int(budget), reason=f"project fund: {title}")
        add_coins(manager.id, int(budget), counts_as_principal=True, reason=f"project budget: {title}")
        pid = db.create_project(title, str(funder.id), str(manager.id), int(budget))
        db.set_project_status(pid, "funded")
        pts = max(1, int(budget) // 1000)
        try:
            _award_loyalty_points(manager.id, pts, reason=f"project#{pid}")
        except Exception:
            pass
        await interaction.response.send_message(
            f"✅ Funded {manager.mention} **{budget:,}** coins for project **#{pid} · {title}**. "
            f"They pay their team with `/project pay` and keep whatever's left.", ephemeral=True)
        try:
            await manager.send(
                f"📋 {funder.mention} tasked you with **#{pid} {title}** — budget **{budget:,}** coins, now in your "
                f"balance. Pay your team with `/project pay`, and keep the rest. (+{pts} pts)")
        except Exception:
            pass

    @project.command(name="pay", description="(Manager) Pay a team member from your project budget")
    @app_commands.describe(worker="A member of your team", amount="Coins to pay them", note="Optional note")
    async def pay(self, interaction: discord.Interaction, worker: discord.Member,
                  amount: app_commands.Range[int, 1, 1_000_000_000], note: str = ""):
        payer = interaction.user
        if worker.bot or worker.id == payer.id:
            return await interaction.response.send_message("Pick a real team member (not yourself).", ephemeral=True)
        if str(db.get_manager_of(str(worker.id)) or "") != str(payer.id):
            return await interaction.response.send_message(
                f"{worker.mention} isn't on your team. Add them with `/team add` first.", ephemeral=True)
        bal = int(db.get_balance(str(payer.id)).get("coins") or 0)
        if bal < amount:
            return await interaction.response.send_message(
                f"You only have `{bal:,}` coins.", ephemeral=True)
        deduct_coins(payer.id, int(amount), reason=f"project pay -> {worker.id}")
        add_coins(worker.id, int(amount), counts_as_principal=True,
                  reason=f"project pay from {payer.id}" + (f": {note}" if note else ""))
        # AUDIT FIX (high): coin ping-pong — two accounts managing EACH OTHER bounced
        # the same coins back and forth, minting unlimited loyalty points and
        # leaderboard credit. Coins still move freely (they're conserved); it's the
        # REWARDS that were mintable. Rules now: (1) circular manager pairs earn no
        # points/credit; (2) points + leaderboard credit are capped per payer→worker
        # per day (PROJECT_PAY_POINTS_DAILY_CAP, default 500).
        from datetime import datetime as _dt, timezone as _tz
        _circular = str(db.get_manager_of(str(payer.id)) or "") == str(worker.id)
        _cap = int(getattr(core, "PROJECT_PAY_POINTS_DAILY_CAP", 500) or 500)
        _day = _dt.now(_tz.utc).strftime("%Y-%m-%d")
        _key = f"projpts:{payer.id}:{worker.id}:{_day}"
        try:
            _used = int(float(db.get_config(_key) or 0))
        except Exception:
            _used = 0
        pts = 0 if _circular else max(0, min(max(1, int(amount) // 100), _cap - _used))
        if pts > 0:
            try:
                db.set_config(_key, str(_used + pts))
            except Exception:
                pass
            try:
                _award_loyalty_points(worker.id, pts, reason="project pay")
            except Exception:
                pass
            try:
                db.record_team_perf(str(payer.id), str(worker.id), "project",
                                    coins=int(amount), points=pts)
            except Exception:
                pass
        await interaction.response.send_message(
            f"💸 Paid {worker.mention} **{amount:,}** coins (+{pts} pts)." + (f" — {note}" if note else ""),
            ephemeral=True)
        try:
            await worker.send(
                f"💸 {payer.mention} paid you **{amount:,}** coins from a team project (+{pts} loyalty pts)."
                + (f"\nNote: {note}" if note else ""))
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(ProjectsCog(bot))
