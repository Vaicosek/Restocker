"""Hive-pickup / harvester UI (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands
from discord.ui import View, Button, Select

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
HARVESTER_ROLE_NAME = core.HARVESTER_ROLE_NAME
HIVE_ACCESS_DM_TARGET_ID = core.HIVE_ACCESS_DM_TARGET_ID
_get_latest_batch = core._get_latest_batch
_load_hive_pickups = core._load_hive_pickups
_normalize_site = core._normalize_site
_save_hive_pickups = core._save_hive_pickups
utcnow_iso = core.utcnow_iso

class HiveAccessModal(discord.ui.Modal, title="Request Hive Pickup Access"):
    def __init__(self, sites: list[str]):
        super().__init__(timeout=300)
        self.sites = [ _normalize_site(s) for s in (sites or []) ]
        self.note = discord.ui.TextInput(
            label="Note for Managers (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
            placeholder="Why do you need access / any extra info…",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        target_id = int(HIVE_ACCESS_DM_TARGET_ID)
        note = (str(self.note.value) or "").strip()

        sites_txt = ", ".join(f"**{s}**" for s in (self.sites or ["(none)"]))
        msg = (
            f"🐝 **Hive pickup access request**\n"
            f"From: {interaction.user} (<@{interaction.user.id}>)\n"
            f"Sites: {sites_txt}\n"
        )
        if note:
            msg += f"\n📝 Note:\n{note}"

        sent = False

        try:
            u = interaction.client.get_user(target_id) or await interaction.client.fetch_user(target_id)
            if u:
                await u.send(msg)
                sent = True
        except Exception:
            sent = False

        if sent:
            return await interaction.response.send_message("✅ Sent your request to Managers.", ephemeral=True)


        return await interaction.response.send_message(
            "⚠️ I couldn’t DM the manager target (DMs off / blocked). Please contact a Manager directly.",
            ephemeral=True
        )


class JoinHarvesterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🐝 Join Hive Harvesters",
        style=discord.ButtonStyle.success,
        custom_id="join_hive_harvesters_btn"
    )
    async def join_harvesters(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "❌ This must be used in a server.",
                ephemeral=True
            )

        role = discord.utils.get(
            interaction.guild.roles,
            name=HARVESTER_ROLE_NAME
        )
        if not role:
            return await interaction.response.send_message(
                f"❌ Role `{HARVESTER_ROLE_NAME}` not found.",
                ephemeral=True
            )

        member = interaction.user
        if role in member.roles:
            return await interaction.response.send_message(
                "🐝 You are already a **Hive Harvester**.",
                ephemeral=True
            )

        try:
            await member.add_roles(role, reason="Joined Hive Harvesters via button")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don’t have permission to assign that role.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "🐝 Welcome! You are now a **Hive Harvester**.",
            ephemeral=True
        )


class HivePickupView(View):
    def __init__(self):
        super().__init__(timeout=None)

        options = [
            discord.SelectOption(label="Sapidorf", value="Sapidorf"),
            discord.SelectOption(label="Parasunt", value="Parasunt"),
            discord.SelectOption(label="Amazonia", value="Amazonia"),
            discord.SelectOption(label="All", value="All"),
        ]

        self.site_select = Select(
            placeholder="Pick site(s) for pickup…",
            min_values=1,
            max_values=4,
            options=options,
            custom_id="hp_site_select",
        )
        self.site_select.callback = self._on_select
        self.add_item(self.site_select)

    async def _on_select(self, interaction: discord.Interaction):
        vals = list(self.site_select.values or [])
        if "All" in vals:
            sites = ["Sapidorf", "Parasunt", "Amazonia"]
        else:
            sites = [_normalize_site(v) for v in vals]

        interaction.client._hive_last_select = getattr(interaction.client, "_hive_last_select", {})
        interaction.client._hive_last_select[interaction.user.id] = sites

        await interaction.response.defer()

    @discord.ui.button(label="✅ Claim selected", style=discord.ButtonStyle.success, custom_id="hp_claim_selected")
    async def claim_selected(self, interaction: discord.Interaction, button: Button):
        batch_id = getattr(interaction.client, "_active_hive_batch", None)
        if not batch_id:
            return await interaction.response.send_message("❌ No active hive pickup.", ephemeral=True)

        selected = getattr(interaction.client, "_hive_last_select", {}).get(interaction.user.id)
        if not selected:
            return await interaction.response.send_message("Pick site(s) first.", ephemeral=True)

        data = _load_hive_pickups()
        batch = (data.get("batches") or {}).get(str(batch_id))
        if not batch:
            return await interaction.response.send_message("❌ Batch not found.", ephemeral=True)

        claimed, blocked = [], []
        for s in selected:
            s = _normalize_site(s)
            if batch["sites"].get(s) is None:
                batch["sites"][s] = {
                    "user_id": interaction.user.id,
                    "user_tag": str(interaction.user),
                    "claimed_at": utcnow_iso(),
                }
                claimed.append(s)
            else:
                blocked.append(s)

        _save_hive_pickups(data)

        lines = []
        if claimed:
            lines.append("✅ Claimed: " + ", ".join(f"**{s}**" for s in claimed))
        if blocked:
            lines.append("⛔ Already claimed: " + ", ".join(f"**{s}**" for s in blocked))

        await interaction.response.send_message("\n".join(lines) or "Nothing changed.", ephemeral=True)

    @discord.ui.button(label="🧾 My claims", style=discord.ButtonStyle.secondary, custom_id="hp_my_claims")
    async def my_claims(self, interaction: discord.Interaction, button: Button):
        bid, batch = _get_latest_batch()
        if not batch:
            return await interaction.response.send_message("📭 No active hive pickup.", ephemeral=True)

        mine = [
            s for s, info in batch["sites"].items()
            if info and int(info.get("user_id", 0) or 0) == int(interaction.user.id)
        ]

        if not mine:
            return await interaction.response.send_message(
                f"📭 You have no claims in batch #{bid}.",
                ephemeral=True
            )

        await interaction.response.send_message(
            f"🐝 Your claims (Batch #{bid}): " + ", ".join(f"**{s}**" for s in mine),
            ephemeral=True
        )

    @discord.ui.button(label="🧑‍✈️ Request access", style=discord.ButtonStyle.primary, custom_id="hp_request_access")
    async def request_access(self, interaction: discord.Interaction, button: Button):
        selected = getattr(interaction.client, "_hive_last_select", {}).get(interaction.user.id)
        sites = selected[:] if selected else ["Sapidorf", "Parasunt", "Amazonia"]
        await interaction.response.send_modal(HiveAccessModal(sites))

