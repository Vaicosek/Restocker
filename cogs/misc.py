"""Misc / admin commands (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
AUTOROLE_CREATE_IF_MISSING = core.AUTOROLE_CREATE_IF_MISSING
CSN_HISTORY_FILE = core.CSN_HISTORY_FILE
CUSTOMER_ROLE_NAME = core.CUSTOMER_ROLE_NAME
DEFAULT_MARKET_ID = core.DEFAULT_MARKET_ID
MANAGER_ROLE_NAME = core.MANAGER_ROLE_NAME
PLATFORM_FEE_PCT = core.PLATFORM_FEE_PCT
_load_markets = core._load_markets
_save_markets = core._save_markets
_market_autocomplete = core._market_autocomplete
is_manager = core.is_manager
load_yaml = core.load_yaml
save_yaml = core.save_yaml
utcnow_iso = core.utcnow_iso


async def _my_market_autocomplete(interaction: discord.Interaction, current: str):
    """For /market_code: Managers see every market; anyone else sees only the
    markets whose leader role they actually hold (so owners only pick their own)."""
    data = _load_markets()
    markets = data.get("markets", {}) or {}
    mgr = is_manager(interaction)
    member = interaction.user
    guild = interaction.guild
    cur = (current or "").lower()
    out = []
    for k, v in markets.items():
        if not isinstance(v, dict):
            continue
        if not mgr:
            role_name = (v.get("discord_role_name") or "").strip()
            if not role_name or guild is None:
                continue
            role = discord.utils.get(guild.roles, name=role_name)
            if not (role and role in getattr(member, "roles", [])):
                continue
        name = v.get("name", k)
        if cur in k.lower() or cur in str(name).lower():
            out.append(app_commands.Choice(name=f"{name} [{k}]", value=k))
    return out[:25]


class MiscCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="website_login", description="Get a one-time code to log in on the dashboard website")
    async def website_login(self, interaction: discord.Interaction):
        import secrets as _secrets
        import string as _string
        import time as _time
        code = "".join(_secrets.choice(_string.ascii_uppercase + _string.digits) for _ in range(6))
        now = _time.time()
        codes = load_yaml("web_login_codes.yml", {}) or {}
        codes = {k: v for k, v in codes.items()
                 if isinstance(v, dict) and float(v.get("expires", 0)) > now}
        codes[code] = {
            "user_id": str(interaction.user.id),
            "name": interaction.user.display_name,
            "expires": now + 600,
        }
        save_yaml("web_login_codes.yml", codes)
        await interaction.response.send_message(
            f"🔐 Your website login code is **`{code}`**  (valid 10 minutes, one-time).\n"
            f"Open the dashboard, click **Log in**, and paste it to link your account.",
            ephemeral=True,
        )

    @app_commands.command(
        name="market_code",
        description="Get your CSN mod verification code — proves you lead this market's shop",
    )
    @app_commands.describe(
        market_id="Market to get a code for (optional if you only lead one)",
        leader="(Managers) The market leader to generate the code for and DM directly",
    )
    @app_commands.autocomplete(market_id=_my_market_autocomplete)
    async def market_code_cmd(self,
        interaction: discord.Interaction,
        market_id: Optional[str] = None,
        leader: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        member = interaction.user
        if guild is None:
            return await interaction.followup.send("❌ Must be used inside the server.", ephemeral=True)

        markets_data = _load_markets()
        all_markets = markets_data.get("markets", {})

        mgr = is_manager(interaction)

        qualifying: list[str] = []
        for mid, m in all_markets.items():
            role_name = (m.get("discord_role_name") or "").strip()
            if not role_name:
                continue
            role = discord.utils.get(guild.roles, name=role_name)
            if role and role in getattr(member, "roles", []):
                qualifying.append(mid)

        if mgr:
            all_market_ids = list(all_markets.keys())
            if market_id:
                if market_id not in all_markets:
                    return await interaction.followup.send(
                        f"❌ Market `{market_id}` not found.",
                        ephemeral=True,
                    )
                chosen = market_id
            elif len(all_market_ids) == 1:
                chosen = all_market_ids[0]
            elif len(qualifying) == 1:
                chosen = qualifying[0]
            elif qualifying:
                ids = "`, `".join(qualifying)
                return await interaction.followup.send(
                    f"You lead multiple markets: `{ids}`\n"
                    f"Specify which one: `/market_code market_id:goldmart`",
                    ephemeral=True,
                )
            else:
                ids = "`, `".join(all_market_ids)
                return await interaction.followup.send(
                    f"Multiple markets exist: `{ids}`\n"
                    f"Specify which one: `/market_code market_id:amazonia`",
                    ephemeral=True,
                )
        else:
            if not qualifying:
                return await interaction.followup.send(
                    "❌ You don't have the leader role for any market.\n"
                    "Ask an admin to assign the appropriate role, then try again.",
                    ephemeral=True,
                )

            if market_id:
                if market_id not in qualifying:
                    return await interaction.followup.send(
                        f"❌ You don't have the leader role for market `{market_id}`.",
                        ephemeral=True,
                    )
                chosen = market_id
            elif len(qualifying) == 1:
                chosen = qualifying[0]
            else:
                ids = "`, `".join(qualifying)
                return await interaction.followup.send(
                    f"❌ You lead multiple markets: `{ids}`\n"
                    f"Specify which one: `/market_code market_id:goldmart`",
                    ephemeral=True,
                )

        import secrets, string as _string
        alphabet = _string.ascii_uppercase + _string.digits
        code = "".join(secrets.choice(alphabet) for _ in range(10))

        m_info = all_markets[chosen]
        market_name = m_info.get("name", chosen)
        csn_file = m_info.get("csn_history_file") or f"csn_history_{chosen}.yml"

        identified = True                      # did we positively identify the leader?
        if leader and mgr:
            code_owner = leader
        elif not mgr:
            code_owner = member
        else:
            role_name = (m_info.get("discord_role_name") or "").strip()
            role = discord.utils.get(guild.roles, name=role_name) if role_name else None
            role_holders = [m for m in role.members if not m.bot] if role else []
            if len(role_holders) == 1:
                code_owner = role_holders[0]
            else:
                code_owner = member            # ambiguous fallback: the invoking manager
                identified = False

        # AUDIT FIX (high): leader_discord_id gates market-OWNERSHIP rights
        # (_markets_owned_by → owner panel, restock orders, approvals). Previously any
        # leader-role holder could run /market_code and silently seize those rights
        # from the recorded leader. Rule now: once a leader is on record, ONLY a
        # manager explicitly naming `leader:` can transfer it — everyone else just
        # rotates the code without touching ownership.
        existing_leader = (m_info.get("leader_discord_id") or "").strip()
        _explicit_transfer = bool(leader and mgr)
        if not existing_leader:
            if identified:
                all_markets[chosen]["leader_discord_id"] = str(code_owner.id)
        elif _explicit_transfer and existing_leader != str(code_owner.id):
            all_markets[chosen]["leader_discord_id"] = str(code_owner.id)
            try:
                core.log.info("[market_code] leader of %s transferred %s -> %s by manager %s",
                              chosen, existing_leader, code_owner.id, member.id)
            except Exception:
                pass
        all_markets[chosen]["leader_code"] = code
        _save_markets(markets_data)

        code_msg = (
            f"🔑 **Market Verification Code — {market_name}**\n\n"
            f"**Market ID:** `{chosen}`\n"
            f"**Code:** `{code}`\n\n"
            "Enter both values in the **CSN Export Settings** screen in-game "
            "(open with your settings keybind, look for *Market ID* and *Market Code* fields).\n\n"
            f"📁 Sales data will be recorded to `{csn_file}`\n"
            "⚠️ **Keep this code private.** Running this command again generates a new code "
            "and immediately invalidates the old one."
        )

        dm_status = ""
        if code_owner.id != member.id:
            try:
                await code_owner.send(code_msg)
                dm_status = f"\n\n✅ Code sent to {code_owner.mention} via DM."
            except discord.Forbidden:
                dm_status = (
                    f"\n\n⚠️ Couldn't DM {code_owner.mention} (DMs closed). "
                    f"Send them the code manually:\n**Market ID:** `{chosen}`\n**Code:** `{code}`"
                )
            await interaction.followup.send(
                f"🔑 Generated new code for **{market_name}** (`{chosen}`).{dm_status}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(code_msg, ephemeral=True)

    @app_commands.command(
        name="create_market",
        description="(Managers) Create a new market with its own sales history",
    )
    @app_commands.describe(
        market_id="Unique ID, lowercase no spaces (e.g. goldmart)",
        name="Display name shown in embeds and the dashboard (e.g. Goldmart)",
        discord_role_name="Exact Discord role name that identifies the market leader (e.g. Goldmart Leader)",
    )
    @app_commands.checks.has_any_role(MANAGER_ROLE_NAME)
    @app_commands.default_permissions(manage_guild=True)
    async def create_market_cmd(self, 
        interaction: discord.Interaction,
        market_id: str,
        name: str,
        discord_role_name: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)

        market_id = market_id.strip().lower().replace(" ", "_")
        if not market_id.replace("_", "").isalnum():
            return await interaction.followup.send(
                "❌ Market ID must be lowercase letters, digits, and underscores only.", ephemeral=True
            )

        markets_data = _load_markets()
        if market_id in markets_data.get("markets", {}):
            return await interaction.followup.send(
                f"❌ Market `{market_id}` already exists. Use `/market list` to see all markets.",
                ephemeral=True,
            )

        csn_file = CSN_HISTORY_FILE if market_id == DEFAULT_MARKET_ID else f"csn_history_{market_id}.yml"
        markets_data.setdefault("markets", {})[market_id] = {
            "name": name,
            "discord_role_name": (discord_role_name or "").strip(),
            "leader_discord_id": None,
            "leader_code": None,
            "owner_id": None,
            "manager_ids": [],
            "platform_fee_pct": PLATFORM_FEE_PCT,
            "csn_history_file": csn_file,
            "active": True,
            "created_at": utcnow_iso(),
        }
        _save_markets(markets_data)

        role_line = f"\n🎭 Leader role: **{discord_role_name}**" if discord_role_name else \
                    "\n⚠️ No leader role set — use `/create_market` again or edit markets.yml to add one."

        await interaction.followup.send(
            f"✅ Created market **{name}** (`{market_id}`){role_line}\n"
            f"📁 History file: `{csn_file}`\n\n"
            f"Next: assign the **{discord_role_name or 'leader'}** role to the shop owner, "
            f"then they run `/market_code market_id:{market_id}` to get their CSN mod code.",
            ephemeral=True,
        )

    @app_commands.command(
        name="edit_dm",
        description="(Managers) Edit a previously sent DM by message ID",
    )
    @app_commands.describe(
        user_id="The Discord user ID whose DM channel contains the message",
        message_id="The ID of the DM message to edit",
        new_content="The new content to replace the message with",
    )
    @app_commands.checks.has_any_role(MANAGER_ROLE_NAME)
    @app_commands.default_permissions(manage_guild=True)
    async def edit_dm_cmd(
        self,
        interaction: discord.Interaction,
        user_id: str,
        message_id: str,
        new_content: str,
    ):
        await interaction.response.defer(ephemeral=True)

        # Validate and parse IDs
        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.followup.send(
                "❌ `user_id` must be a numeric Discord user ID.", ephemeral=True
            )

        try:
            mid = int(message_id.strip())
        except ValueError:
            return await interaction.followup.send(
                "❌ `message_id` must be a numeric Discord message ID.", ephemeral=True
            )

        # Fetch the user
        try:
            user = await self.bot.fetch_user(uid)
        except discord.NotFound:
            return await interaction.followup.send(
                f"❌ No user found with ID `{uid}`.", ephemeral=True
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"❌ Failed to fetch user: {e}", ephemeral=True
            )

        # Open / retrieve the DM channel
        try:
            dm_channel = user.dm_channel or await user.create_dm()
        except discord.Forbidden:
            return await interaction.followup.send(
                f"❌ Cannot open a DM channel with {user} (DMs may be closed).", ephemeral=True
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"❌ Failed to create DM channel: {e}", ephemeral=True
            )

        # Fetch the specific message
        try:
            message = await dm_channel.fetch_message(mid)
        except discord.NotFound:
            return await interaction.followup.send(
                f"❌ Message `{mid}` not found in {user}'s DM channel.", ephemeral=True
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ Missing permission to read that DM channel.", ephemeral=True
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"❌ Failed to fetch message: {e}", ephemeral=True
            )

        # Verify the message was sent by the bot
        if message.author.id != self.bot.user.id:
            return await interaction.followup.send(
                "❌ That message was not sent by this bot — only bot-authored messages can be edited.",
                ephemeral=True,
            )

        # Edit the message
        try:
            await message.edit(content=new_content)
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ Missing permission to edit that message.", ephemeral=True
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"❌ Failed to edit message: {e}", ephemeral=True
            )

        await interaction.followup.send(
            f"✅ Successfully edited message `{mid}` in {user.mention}'s DM channel.",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(MiscCog(bot))
