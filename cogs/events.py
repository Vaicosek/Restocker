"""Discord gateway event handlers (member update/join, messages).
on_ready stays in Restocker_main (startup orchestration: view registration + tree sync)."""
import sys
import discord
from discord.ext import commands

from datetime import datetime

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
CSN_REPORT_CHANNEL_ID = core.CSN_REPORT_CHANNEL_ID
LOYALTY_EMPLOYEE_ROLES = core.LOYALTY_EMPLOYEE_ROLES
LOYALTY_IGN_DEADLINE_DAYS = core.LOYALTY_IGN_DEADLINE_DAYS
MANAGER_DM_IDS = core.MANAGER_DM_IDS
MANAGER_ROLE_ALT = core.MANAGER_ROLE_ALT
MANAGER_ROLE_NAME = core.MANAGER_ROLE_NAME
WELCOME_CHANNEL_ID = core.WELCOME_CHANNEL_ID
WorkerView = core.WorkerView
_AI_ALLOWED_USER_IDS = core._AI_ALLOWED_USER_IDS
_is_ai_allowed = core._is_ai_allowed
_CSN_ALLOWED_WEBHOOK_IDS = core._CSN_ALLOWED_WEBHOOK_IDS
_assign_customer_role = core._assign_customer_role
_process_csn_attachment = core._process_csn_attachment
bot = core.bot
handle_ai_mention = core.handle_ai_mention
log = core.log
timedelta = core.timedelta
timezone = core.timezone

class EventsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Detect when a user gains an employee role → trigger IGN registration DM."""
        before_roles = {r.name for r in before.roles}
        after_roles  = {r.name for r in after.roles}
        new_roles = after_roles - before_roles
        triggered = new_roles & LOYALTY_EMPLOYEE_ROLES
        if not triggered:
            return

        import Restocker_db as _db_ign
        if _db_ign.get_ign(str(after.id)):
            return
        if _db_ign.get_ign_pending(str(after.id)):
            return

        role_name = next(iter(triggered))
        role_obj = discord.utils.find(lambda r: r.name == role_name, after.guild.roles)
        role_id = str(role_obj.id) if role_obj else "0"
        deadline = (datetime.now(timezone.utc) + timedelta(days=LOYALTY_IGN_DEADLINE_DAYS)).isoformat()

        try:
            dm = await after.create_dm()
            await dm.send(
                f"👋 Welcome to **{after.guild.name}**! You've been given the **{role_name}** role.\n\n"
                f"To complete your setup and start earning loyalty points, please reply with your "
                f"**Minecraft in-game username (IGN)**.\n\n"
                f"⏰ You have **{LOYALTY_IGN_DEADLINE_DAYS} days** to register. "
                f"If you don't, your role will be removed."
            )
            _db_ign.set_ign_pending(str(after.id), str(dm.id), role_id, str(after.guild.id), deadline)
            log.info("[ign] Sent registration DM to %s (%s)", after.display_name, after.id)
        except discord.Forbidden:
            log.warning("[ign] Could not DM %s — DMs closed", after.display_name)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (
            not message.author.bot
            and message.guild is not None
            and bot.user is not None
            and bot.user.mentioned_in(message)
            and not message.mention_everyone
            and "clear" in message.content.lower()
        ):
            author_role_names = {r.name for r in message.author.roles} if hasattr(message.author, "roles") else set()
            is_mgr = (MANAGER_ROLE_NAME in author_role_names or MANAGER_ROLE_ALT in author_role_names or message.author.id in MANAGER_DM_IDS)
            if not is_mgr:
                await message.channel.send("⛔ Only managers can use CLEAR.", delete_after=5)
                return
            confirm_msg = await message.channel.send(
                f"⚠️ **Are you sure?** This will delete ALL messages in **#{message.channel.name}**.\n"
                f"React with ✅ to confirm or ❌ to cancel."
            )
            await confirm_msg.add_reaction("✅")
            await confirm_msg.add_reaction("❌")

            def check(reaction, user):
                return user == message.author and str(reaction.emoji) in ("✅", "❌") and reaction.message.id == confirm_msg.id

            try:
                import asyncio as _asyncio
                reaction, _ = await bot.wait_for("reaction_add", timeout=30.0, check=check)
            except _asyncio.TimeoutError:
                await confirm_msg.edit(content="⏱️ CLEAR cancelled — timed out.")
                return

            if str(reaction.emoji) == "❌":
                await confirm_msg.edit(content="❌ CLEAR cancelled.")
                return

            await confirm_msg.edit(content="🗑️ Clearing channel...")
            deleted = 0
            async for msg in message.channel.history(limit=None):
                try:
                    await msg.delete()
                    deleted += 1
                except Exception:
                    pass
            await message.channel.send(f"✅ Cleared **{deleted}** messages.")
            return

        if (
            not message.author.bot
            and message.guild is not None
            and bot.user is not None
            and bot.user.mentioned_in(message)
            and not message.mention_everyone
        ):
            if not _is_ai_allowed(message.author.id):
                return
            await handle_ai_mention(message)
            return

        if (not message.author.bot and message.guild is None and message.content.strip()):
            import Restocker_db as _db_ign2
            pending = _db_ign2.get_ign_pending(str(message.author.id))
            if pending:
                ign = message.content.strip()
                import re as _re
                if not _re.match(r"^[A-Za-z0-9_]{3,16}$", ign):
                    await message.channel.send(
                        "❌ That doesn't look like a valid Minecraft username (3-16 chars, letters/numbers/underscore). "
                        "Please try again."
                    )
                    return
                existing_owner = _db_ign2.get_user_id_by_ign(ign)
                if existing_owner and existing_owner != str(message.author.id):
                    await message.channel.send(
                        f"❌ IGN `{ign}` is already registered to another user. "
                        f"If this is a mistake, contact a manager."
                    )
                    return
                _db_ign2.set_ign(str(message.author.id), ign)
                _db_ign2.delete_ign_pending(str(message.author.id))
                await message.channel.send(
                    f"✅ IGN **{ign}** registered! You're all set.\n"
                    f"⭐ Your activity will now count toward loyalty points."
                )
                log.info("[ign] Registered IGN '%s' for user %s", ign, message.author.id)
                return

        if not message.webhook_id:
            return
        if _CSN_ALLOWED_WEBHOOK_IDS and message.webhook_id not in _CSN_ALLOWED_WEBHOOK_IDS:
            return
        for att in message.attachments:
            name = att.filename.lower()
            if not name.endswith(".csv"):
                continue
            if "csn_monthly" not in name and "csn_export" not in name and "csn_stock" not in name:
                continue
            report_channel = (
                bot.get_channel(CSN_REPORT_CHANNEL_ID) if CSN_REPORT_CHANNEL_ID else message.channel
            )
            if report_channel is None:
                report_channel = message.channel
            try:
                await _process_csn_attachment(att, report_channel, source_channel_id=message.channel.id)
            except Exception as e:
                log.error("CSN on_message processing failed: %s", e)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:

            if member.bot:
                return

            await _assign_customer_role(member)

            embed = discord.Embed(
                title="👋 Welcome!",
                description=(
                    "Thanks for joining! Want to help with brewing and restocking?\n"
                    "Click **Join Workers** below to receive order notifications."
                ),
                color=discord.Color.blurple(),
            )

            try:
                welcome_channel = (
                    bot.get_channel(WELCOME_CHANNEL_ID) if WELCOME_CHANNEL_ID else None
                )
                if welcome_channel is None and WELCOME_CHANNEL_ID:
                    welcome_channel = await bot.fetch_channel(WELCOME_CHANNEL_ID)
                if welcome_channel is not None:
                    await welcome_channel.send(
                        content=f"Welcome {member.mention}! 🎉",
                        embed=embed,
                        view=WorkerView(),
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                else:
                    print(f"[on_member_join] WELCOME_CHANNEL_ID={WELCOME_CHANNEL_ID} not found")
            except Exception as e:
                print(f"[on_member_join] channel welcome failed: {e}")

            try:
                await member.send(embed=embed, view=WorkerView())
            except discord.Forbidden:
                pass
            except discord.HTTPException as e:
                if getattr(e, "code", None) != 50007:
                    print(f"[on_member_join] DM failed: {e}")

        except Exception as e:
            print(f"[on_member_join] error: {e}")


async def setup(bot):
    await bot.add_cog(EventsCog(bot))
