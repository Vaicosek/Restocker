"""Dedicated AI-brain cog.

This is where the bot's @mention "brain" lives - the vision-enabled handler that reads
images people attach, runs the tool-use loop, and replies. It is split out of the cursed
Restocker_main.py (and out of events.py) so the model, system-prompt tuning, and behaviour
can be edited in ONE small file without touching anything else.

What stays in core (Restocker_main): the actual TOOLS (_AI_TOOLS / _AI_TOOL_MAP), the base
system prompt (_AI_SYSTEM), conversation history, cooldown state, audit, and the allow-list.
This cog only owns *how the mention is handled* and *which model / extra prompt* is used.

Loaded from cogs/events.py's setup() via bot.load_extension("cogs.ai"), so main.py's
hardcoded cog tuple never has to change.
"""
import sys
import os
import base64 as _b64
import asyncio as _aio
from datetime import datetime, timezone

import discord
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]

MANAGER_DM_IDS = core.MANAGER_DM_IDS
_is_ai_allowed = core._is_ai_allowed
log = core.log

# -----------------------------------------------------------------------------
#  EDITABLE KNOBS - tune the brain here.
# -----------------------------------------------------------------------------
# Model used for @mention replies. Override at runtime with the MENTION_AI_MODEL env
# var. Falls back to core._AI_MODEL (the cheaper model the rest of the bot uses) if
# this model errors - so a bad/limited model name never takes the brain fully offline.
AI_MODEL = os.getenv("MENTION_AI_MODEL") or "claude-sonnet-4-5"

# Max tokens per reply turn.
AI_MAX_TOKENS = 1400

# How many tool-use round-trips to allow before giving up on a single question.
AI_MAX_STEPS = 10

# Largest image (bytes) we'll base64 into the API. Discord attachments over this are
# skipped so we don't blow the request size / token budget.
AI_MAX_IMAGE_BYTES = 5_000_000

# Extra behavioural instructions appended to core._AI_SYSTEM. This is the part you'll
# tweak most - tone, what NOT to say, how eager to act. Keep it short and imperative.
AI_SYSTEM_EXTRA = (
    "You CAN see images the user attaches - read them directly and answer from what is "
    "shown. NEVER say you cannot see files, images, or attachments. Be direct and concise: "
    "do not ask permission-style clarifying questions or over-scope a simple request - just "
    "do the useful thing. If someone sends a CSN report CSV, the bot auto-imports it; do not "
    "ask them to paste it."
)


def _looks_like_csn_csv(message: discord.Message) -> bool:
    """A human dropping a csn_monthly/export/stock CSV is handled by events.py (it routes to
    the validated importer). The AI brain must ignore those so we don't double-handle a
    report as a chat message."""
    for a in (message.attachments or []):
        n = (a.filename or "").lower()
        if n.endswith(".csv") and any(k in n for k in ("csn_monthly", "csn_export", "csn_stock")):
            return True
    return False


async def _ai_mention_vision(message: discord.Message):
    """Vision-enabled @mention handler. Passes attached IMAGES (and text) to Claude so the bot
    can actually READ pictures people send instead of claiming it can't see files, and runs a
    stronger model. Reuses core's tools / system / history / cooldown / audit."""
    bot = core.bot
    client = core._get_anthropic_client()
    if client is None:
        try:
            await message.reply("AI features are not configured (missing ANTHROPIC_API_KEY).",
                                allowed_mentions=core._NO_MASS_MENTIONS)
        except Exception:
            pass
        return

    loop = _aio.get_event_loop()
    _now = loop.time()
    member = message.guild.get_member(message.author.id) if message.guild else None
    exempt = (int(getattr(message.author, "id", 0)) in MANAGER_DM_IDS
              or (member is not None and core._ai_is_manager(member)))
    cd = getattr(core, "AI_COOLDOWN_SEC", 0) or 0
    last = core._AI_COOLDOWN.get(message.author.id, 0)
    if (not exempt) and cd > 0 and (_now - last) < cd:
        try:
            await message.reply(f"One moment - wait {int(cd - (_now - last))}s before asking again.",
                                allowed_mentions=core._NO_MASS_MENTIONS)
        except Exception:
            pass
        return
    core._AI_COOLDOWN[message.author.id] = _now

    guild = message.guild
    user = message.author
    channel = message.channel
    roles = [r.name for r in getattr(member, "roles", [])]
    is_mgr = core._ai_is_manager(member) if member else False
    text = message.content or ""
    if guild and guild.me:
        text = text.replace(guild.me.mention, "").strip()

    blocks = []
    for att in (message.attachments or []):
        ct = (att.content_type or "").lower()
        is_img = ct.startswith("image/") or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
        if not is_img:
            continue
        try:
            raw = await att.read()
            if len(raw) <= AI_MAX_IMAGE_BYTES:
                media = ct if ct.startswith("image/") else "image/png"
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media,
                               "data": _b64.standard_b64encode(raw).decode("ascii")}})
        except Exception as e:
            log.warning("[ai] image read failed: %s", e)
    if text:
        blocks.append({"type": "text", "text": text})
    if not blocks:
        try:
            await message.reply("Mention me with a question, command, or an image.",
                                allowed_mentions=core._NO_MASS_MENTIONS)
        except Exception:
            pass
        return

    now_utc = datetime.now(timezone.utc)
    system = core._AI_SYSTEM + f"""

Current context:
- User: {user.display_name} (ID: {user.id})
- Roles: {', '.join(roles) if roles else 'none'}
- Manager access: {is_mgr}
- Channel: #{getattr(channel, 'name', '?')} (ID: {channel.id})
- Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}

{AI_SYSTEM_EXTRA}
"""

    hist = core._AI_CONVERSATION_HISTORY.get(channel.id, [])
    messages = hist + [{"role": "user", "content": blocks}]
    base_model = core._AI_MODEL
    model = AI_MODEL
    tools = core._AI_TOOLS

    def _create(msgs):
        nonlocal model
        try:
            return client.messages.create(model=model, max_tokens=AI_MAX_TOKENS, system=system,
                                           tools=tools, messages=msgs)
        except Exception:
            if model != base_model:
                model = base_model
                return client.messages.create(model=model, max_tokens=AI_MAX_TOKENS, system=system,
                                               tools=tools, messages=msgs)
            raise

    try:
        async with channel.typing():
            for _ in range(AI_MAX_STEPS):
                response = await loop.run_in_executor(None, lambda: _create(messages))
                if response.stop_reason == "tool_use":
                    tool_results = []
                    assistant = response.content
                    for b in response.content:
                        if getattr(b, "type", None) != "tool_use":
                            continue
                        h = core._AI_TOOL_MAP.get(b.name)
                        try:
                            res = await h(guild, channel, member, b.input) if h else f"Unknown tool: {b.name}"
                        except Exception as e:
                            res = f"Tool error: {e}"
                        try:
                            core._ai_audit_record(member, b.name, b.input, res)
                        except Exception:
                            pass
                        tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(res)})
                    messages.append({"role": "assistant", "content": assistant})
                    messages.append({"role": "user", "content": tool_results})
                else:
                    reply = "".join(bl.text for bl in response.content if hasattr(bl, "text")).strip()
                    if reply:
                        if len(reply) > 1990:
                            reply = reply[:1987] + "..."
                        try:
                            await core._safe_reply(message, reply, allowed_mentions=core._NO_MASS_MENTIONS)
                        except Exception:
                            pass
                        h2 = core._AI_CONVERSATION_HISTORY.get(channel.id, [])
                        h2.append({"role": "user", "content": text or "[image]"})
                        h2.append({"role": "assistant", "content": reply})
                        core._AI_CONVERSATION_HISTORY[channel.id] = h2[-(2 * core._AI_HISTORY_MAX):]
                    return
    except Exception as e:
        log.error("[ai vision] %s", e)
        try:
            await core._safe_reply(message, f"Error: {e}", allowed_mentions=core._NO_MASS_MENTIONS)
        except Exception:
            pass


# -----------------------------------------------------------------------------
#  NOTES TOOL FIXES  (rebound into core._AI_TOOL_MAP at cog load)
# -----------------------------------------------------------------------------
# The originals in Restocker_main.py had two bugs that made the whole note
# workflow silently useless:
#   * list_notes called _db.get_notes(...) -> that function does not exist
#     (it's list_notes), so the tool always errored: "get_notes is broken".
#   * note_to_self called save_note(user_id, name, text) but the real signature
#     is save_note(text, author_id, author_name) -> args were scrambled, so the
#     stored note text was actually the user id, filed under the display name.
# These corrected versions are rebound over the broken ones without touching the
# cursed main.py.

async def _ai_tool_note_to_self_fixed(guild, channel, user, args):
    text = (args.get("text", "") or "").strip()
    if not text:
        return "No text provided."
    try:
        import Restocker_db as _db
        _db.save_note(
            text=text,
            author_id=str(user.id),
            author_name=getattr(user, "display_name", str(user.id)),
        )
        return "Note saved."
    except Exception as e:
        return f"Error saving note: {e}"


async def _ai_tool_list_notes_fixed(guild, channel, user, args):
    try:
        limit = int(args.get("limit", 5) or 5)
    except Exception:
        limit = 5
    try:
        import Restocker_db as _db
        notes = _db.list_notes(str(user.id), limit=limit)
        if not notes:
            return "No notes found."
        lines = []
        for n in notes:
            ts = (n.get("created_at") or "")[:16]
            lines.append(f"[#{n.get('id')} {ts}] {n.get('text')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving notes: {e}"


def _rebind_notes_tools():
    """Overwrite the broken note tool handlers in core's registry with the fixed
    ones. Safe to call repeatedly (idempotent)."""
    try:
        core._AI_TOOL_MAP["note_to_self"] = _ai_tool_note_to_self_fixed
        core._AI_TOOL_MAP["list_notes"] = _ai_tool_list_notes_fixed
        log.info("[ai] rebound fixed note_to_self / list_notes handlers")
    except Exception as e:
        log.error("[ai] could not rebind notes tools: %s", e)


class AICog(commands.Cog):
    """Owns the @mention -> AI brain path. Its on_message listener fires independently of
    events.py's; it acts ONLY when the bot is mentioned in a normal message that isn't a
    manager CLEAR command or a CSN-CSV upload (both handled in events.py), so nothing is
    double-handled."""

    def __init__(self, bot):
        self.bot = bot
        _rebind_notes_tools()

    @commands.Cog.listener("on_message")
    async def ai_mention(self, message: discord.Message):
        bot = core.bot
        if (message.author.bot
                or message.guild is None
                or bot.user is None
                or not bot.user.mentioned_in(message)
                or message.mention_everyone):
            return
        # Manager CLEAR flow (mention + "clear") is owned by events.py.
        if "clear" in (message.content or "").lower():
            return
        # CSN report CSVs are owned by events.py's importer.
        if _looks_like_csn_csv(message):
            return
        if not _is_ai_allowed(message.author.id):
            return
        await _ai_mention_vision(message)


async def setup(bot):
    await bot.add_cog(AICog(bot))
