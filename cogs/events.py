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
_process_csn_profiles = core._process_csn_profiles
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

        # A human dropping a CSN report CSV (csn_monthly/export/stock) should get it LOGGED — the
        # AI mention handler can't see attachments, and the webhook-only path below skips humans, so
        # a manager uploading the file used to be ignored. AUDIT FIX (critical): this path had NO
        # auth at all — any guild member's csn_*.csv went straight to the importer, which could
        # book forged earnings, re-price shares and trigger harvest payouts. Now the uploader must
        # be a manager, OR the file itself must carry a VERIFYING "# MARKET,<id>,<code>" header.
        # txn_only is passed exactly like the webhook path: when a monthly file accompanies the
        # export in the same message, only the monthly one books earnings (no double-count).
        if (not message.author.bot) and message.guild is not None and message.attachments:
            _human_csn = [a for a in message.attachments
                          if a.filename.lower().endswith(".csv")
                          and any(k in a.filename.lower() for k in ("csn_monthly", "csn_export", "csn_stock"))]
            if _human_csn:
                _roles = {r.name for r in message.author.roles} if hasattr(message.author, "roles") else set()
                _is_mgr_up = (MANAGER_ROLE_NAME in _roles or MANAGER_ROLE_ALT in _roles
                              or message.author.id in MANAGER_DM_IDS)
                try:
                    await message.add_reaction("📥")
                except Exception:
                    pass
                _has_monthly_h = any("csn_monthly" in a.filename.lower() for a in _human_csn)
                for _att in _human_csn:
                    try:
                        if not _is_mgr_up:
                            _txt = (await _att.read()).decode("utf-8", errors="replace")
                            _mid, _code = core._extract_market_info(_txt)
                            if not (_mid and _code and core._verify_market_code(_mid, _code)):
                                log.warning("[csn] REJECTED human upload %s from %s — not a "
                                            "manager and no valid market code in the file.",
                                            _att.filename, message.author.id)
                                try:
                                    await message.reply(
                                        f"⛔ `{_att.filename}` not imported: uploads need a manager, "
                                        f"or the file must carry its market's valid "
                                        f"`# MARKET,<id>,<code>` header (the mod writes it when "
                                        f"market_id/market_code are set in CSN settings).",
                                        allowed_mentions=discord.AllowedMentions.none())
                                except Exception:
                                    pass
                                continue
                        await _process_csn_attachment(
                            _att, message.channel, source_channel_id=message.channel.id,
                            txn_only=("csn_export" in _att.filename.lower() and _has_monthly_h))
                    except Exception as _e:
                        log.error("CSN human upload failed: %s", _e)
                        try:
                            await message.reply(f"⚠️ Couldn't import that CSN report: {_e}",
                                                allowed_mentions=discord.AllowedMentions.none())
                        except Exception:
                            pass
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
                # AUDIT FIX (high): an IGN with unpaid harvest coins waiting could be
                # squatted by whoever DM'd it first — every future payout for that
                # player would flow to the squatter. Money-bearing IGNs need a manager.
                try:
                    _pend_val = _db_ign2.ign_unpaid_value(ign)
                except Exception:
                    _pend_val = 0
                if _pend_val > 0:
                    await message.channel.send(
                        f"⚠️ IGN `{ign}` has **{int(_pend_val):,}** coins of unpaid harvests "
                        f"waiting, so it can't be self-claimed. Ask a manager to link it to "
                        f"you (they'll verify it's really your account).")
                    return
                _db_ign2.set_ign(str(message.author.id), ign)
                _db_ign2.delete_ign_pending(str(message.author.id))
                await message.channel.send(
                    f"✅ IGN **{ign}** registered! You're all set.\n"
                    f"⭐ Your activity will now count toward loyalty points."
                )
                log.info("[ign] Registered IGN '%s' for user %s", ign, message.author.id)
                return

        # CSN reports arrive from an automated poster: either a Discord WEBHOOK
        # (message.webhook_id set) or a BOT APPLICATION posting with a bot token
        # (webhook_id is None but message.author.bot is True). Accept BOTH — the CSN
        # relay may run as either. Never process our own uploads (loop guard) or plain
        # human messages.
        _self_id = bot.user.id if bot.user else None
        _is_webhook = message.webhook_id is not None
        _is_bot_app = bool(getattr(message.author, "bot", False)) and message.author.id != _self_id
        if not (_is_webhook or _is_bot_app):
            return
        # AUDIT FIX (high): the allowlist used to default to accept-ANY-webhook, so a
        # forged webhook could post fake earnings that re-anchor the stock and trigger
        # payouts. Now: env CSN_WEBHOOK_IDS ∪ config csn_allowed_posters, with TRUST ON
        # FIRST USE — the first webhook/bot that delivers a CSN CSV gets locked in
        # automatically (your existing relay keeps working untouched); every different
        # poster after that is rejected and logged until a manager adds its id to the
        # csn_allowed_posters config (comma-separated).
        _poster_id = message.webhook_id if _is_webhook else message.author.id
        _has_csn_csv = any(
            _a.filename.lower().endswith(".csv")
            and any(k in _a.filename.lower() for k in ("csn_monthly", "csn_export", "csn_stock"))
            for _a in message.attachments)
        _has_csn_json = any(
            _a.filename.lower().endswith(".json") and "csn_profiles" in _a.filename.lower()
            for _a in message.attachments)
        # AUDIT FIX (high): the old gate was GLOBAL trust-on-first-use — the first poster
        # ever was locked in with no code check at all, and any poster that once carried
        # one valid code was trusted for ALL markets forever. Now:
        #   • the env allowlist (CSN_WEBHOOK_IDS) + legacy global config still work;
        #   • otherwise trust is PER MARKET (csn_allowed_posters:<mid>), earned only by
        #     delivering a file whose declared market code verifies — and it vouches the
        #     poster for THAT market only;
        #   • a channel bound to a market accepts that market's files (posting there
        #     already required guild-controlled channel access);
        #   • there is no blind first-poster lock-in any more.
        _poster_trusted = False
        _bound_mid = None
        if _has_csn_csv or _has_csn_json:
            import Restocker_db as _dbw
            _allowed = set(_CSN_ALLOWED_WEBHOOK_IDS)
            try:
                _cfg = str(_dbw.get_config("csn_allowed_posters") or "")
                _allowed |= {int(x) for x in _cfg.replace(" ", "").split(",") if x.strip().isdigit()}
            except Exception:
                pass
            _poster_trusted = _poster_id in _allowed
            try:
                _bm = _dbw.get_market_by_channel(message.channel.id)
                _bound_mid = _bm.get("market_id") if _bm else None
            except Exception:
                _bound_mid = None

            async def _csn_attachment_allowed(_a) -> bool:
                """Gate ONE attachment: trusted poster, bound-channel match, per-market
                vouch, or a verifying market code inside the file (which then vouches
                this poster for that one market)."""
                if _poster_trusted:
                    return True
                try:
                    _txt = (await _a.read()).decode("utf-8", errors="replace")
                    _mid, _code = core._extract_market_info(_txt)
                except Exception as _ve:
                    log.warning("[csn] poster vouch check failed for %s: %s", _a.filename, _ve)
                    return False
                if _bound_mid and (not _mid or _mid == _bound_mid):
                    return True
                if _mid:
                    try:
                        _per = str(_dbw.get_config(f"csn_allowed_posters:{_mid}") or "")
                        if str(_poster_id) in {x.strip() for x in _per.split(",") if x.strip()}:
                            return True
                    except Exception:
                        pass
                    if _code and core._verify_market_code(_mid, _code):
                        try:
                            _per = str(_dbw.get_config(f"csn_allowed_posters:{_mid}") or "")
                            _ids = {x.strip() for x in _per.split(",") if x.strip()}
                            _ids.add(str(_poster_id))
                            _dbw.set_config(f"csn_allowed_posters:{_mid}", ",".join(sorted(_ids)))
                            log.info("[csn] poster %s vouched for market %s (valid code).",
                                     _poster_id, _mid)
                        except Exception:
                            pass
                        return True
                log.warning("[csn] REJECTED %s from poster %s — no binding/vouch/valid code "
                            "for its market. Add the id to CSN_WEBHOOK_IDS or "
                            "csn_allowed_posters if this is a legitimate relay.",
                            _a.filename, _poster_id)
                return False
        else:
            async def _csn_attachment_allowed(_a) -> bool:
                return True
        _processed_any = False
        _all_transport = bool(message.attachments)
        # The mod posts the monthly aggregate AND the per-transaction period file in one
        # message. Both describe the same sales, so only the monthly one may book earnings
        # — otherwise every coin is counted twice. The period file still gets ingested for
        # its per-customer / per-day detail.
        _has_monthly = any(a.filename.lower().endswith(".csv") and "csn_monthly" in a.filename.lower()
                           for a in message.attachments)
        for att in message.attachments:
            name = att.filename.lower()
            report_channel = (
                bot.get_channel(CSN_REPORT_CHANNEL_ID) if CSN_REPORT_CHANNEL_ID else message.channel
            )
            if report_channel is None:
                report_channel = message.channel
            # csn_profiles.json — the mod's captured item lore. Auto-learn readable brew
            # names (potion effects) from it so reports stop showing raw #codes.
            # AUDIT FIX: this used to bypass the poster gate entirely (it sat outside the
            # .csv allowlist check) and wrote display_names verbatim into the GLOBAL alias
            # store. Now only trusted posters / bound channels feed it (and the learner
            # sanitizes every name it stores).
            if name.endswith(".json") and "csn_profiles" in name:
                if not (_poster_trusted or _bound_mid):
                    log.warning("[csn] REJECTED csn_profiles.json from unvouched poster %s "
                                "in unbound channel #%s.", _poster_id,
                                getattr(message.channel, "name", message.channel.id))
                    _all_transport = False
                    continue
                try:
                    await _process_csn_profiles(att, report_channel)
                    _processed_any = True
                except Exception as e:
                    log.error("CSN profiles processing failed: %s", e)
                    _all_transport = False
                continue
            if (not name.endswith(".csv")
                    or ("csn_monthly" not in name and "csn_export" not in name
                        and "csn_stock" not in name)):
                _all_transport = False        # carries something that isn't CSN transport — keep
                continue
            if not await _csn_attachment_allowed(att):
                _all_transport = False        # rejected — keep the raw file visible for review
                continue
            try:
                await _process_csn_attachment(
                    att, report_channel, source_channel_id=message.channel.id,
                    txn_only=("csn_export" in name and _has_monthly))
                _processed_any = True
            except Exception as e:
                log.error("CSN on_message processing failed: %s", e)
                _all_transport = False        # failed ingest — keep the file for retry/debug

        # De-clutter: the raw CSV/JSON webhook upload is machine transport, not something
        # owners should read — once every attachment ingested cleanly, delete the upload so
        # only the bot's report card remains. Human uploads are handled (and kept) above.
        # Opt out with config csn_keep_uploads=1. Needs Manage Messages in the channel.
        if _processed_any and _all_transport:
            try:
                import Restocker_db as _db_keep
                if str(_db_keep.get_config("csn_keep_uploads") or "") != "1":
                    await message.delete()
            except discord.Forbidden:
                # Was debug-level, so a missing permission looked identical to "working
                # fine" — the raw CSV just stayed in the channel with no explanation.
                log.warning("[csn] can't delete the raw upload in #%s — I need Manage "
                            "Messages there. The CSV was ingested fine, it just stays visible.",
                            getattr(message.channel, "name", message.channel.id))
            except Exception as e:
                log.warning("[csn] upload cleanup skipped: %s", e)
        elif message.attachments and _has_csn_csv:
            log.info("[csn] kept the raw upload in #%s (processed=%s, all_transport=%s)",
                     getattr(message.channel, "name", message.channel.id),
                     _processed_any, _all_transport)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Re-invited to a server → push the command tree immediately. Without this the
        startup signature guard sees an unchanged tree and skips the sync, so a freshly
        re-added bot sits there with no slash commands until the tree happens to change."""
        try:
            await bot.tree.sync()
            log.info("[sync] joined %s — command tree pushed", guild.name)
        except Exception as e:
            log.warning("[sync] on_guild_join sync failed: %s", e)

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
    # Nested-load the dedicated AI-brain cog so main.py's hardcoded cog tuple
    # never needs editing. Failure here must not take down the events cog.
    try:
        await bot.load_extension("cogs.ai")
    except commands.ExtensionAlreadyLoaded:
        pass
    except Exception as _e:
        log.error("[events] failed to load cogs.ai: %s", _e)
    # (cogs.enchant / the /enchant_area roster was retired 2026-07-28 — ask the AI to note
    # who runs which enchant area instead of maintaining a command group for it.)
