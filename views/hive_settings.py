"""HiveSettings — one interactive panel replacing the seven /hive subcommands.

`/hive` used to be bind / unbind / info / payout / set_value / set_wage / set_split:
seven picker entries for what is really one settings screen. This is that screen.

Nothing here reimplements hive logic — every action calls the same helpers the commands
called (`cogs.hive._group_rows`, `HiveCog._settle_groups`, `core._hive_item_value`, …),
so payouts and valuations behave identically to before.

Ephemeral and short-lived on purpose: it can move real coins, so it is NOT a persistent
view and dies with its timeout.
"""
import sys

import discord

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log


def _db():
    import Restocker_db as _d
    return _d


def _feeds() -> list:
    """[(channel_id, market_id)] for every channel bound as a hive feed."""
    out = []
    for k, v in (_db().get_config_prefix("hive_feed:") or {}).items():
        try:
            out.append((int(k.split(":", 1)[1]), str(v)))
        except Exception:
            continue
    return out


def build_embed(market_id: str) -> discord.Embed:
    """Everything the seven commands used to report, on one card."""
    d = _db()
    markets = (core._load_markets().get("markets", {}) or {})
    name = (markets.get(market_id) or {}).get("name", market_id)
    autopay = str(d.get_config(f"hive_autopay:{market_id}") or "") == "1"
    wage = core._hive_harvester_pct()
    owner_pct = core._hive_owner_pct(market_id)
    rows = d.get_unpaid_hive_harvests(market_id) or []
    value = sum(int(r.get("qty") or 0) * float(r.get("unit_value") or 0) for r in rows)

    e = discord.Embed(
        title=f"🐝 HiveSettings — {name}",
        description=(("✅ Autopay ON" if autopay else "⏸️ Autopay off")
                     + f" · harvesters **{wage:g}%**"
                     + (f" · partner owner **{owner_pct:g}%**" if owner_pct else "")
                     + f" · V Tech **{max(0.0, 100.0 - wage - owner_pct):g}%**"),
        color=0x3FB950 if autopay else 0xE3B341)

    chans = [c for c, m in _feeds() if m == market_id]
    e.add_field(name="Feed channels",
                value=(" ".join(f"<#{c}>" for c in chans) if chans else "*none bound*"),
                inline=False)

    vals = " · ".join(f"{i.title()} **{core._hive_item_value(i):,.4g}**/pc"
                      for i in ("honey block", "honeycomb block"))
    e.add_field(name="Item values (per PIECE)",
                value=vals + "\n*Shop prices are per stack of 64 — a value of 0 pays nothing.*",
                inline=False)

    if rows:
        try:
            groups, unreg, unvalued = sys.modules["cogs.hive"]._group_rows(rows)
        except Exception:
            groups, unreg, unvalued = {}, {}, {}
        bits = [f"**{len(rows)}** unpaid line(s) · value **{value:,.0f}** "
                f"→ wages **{value * wage / 100:,.0f}**"]
        if unreg:
            bits.append("⏳ held (needs /me → Link in-game name): "
                        + ", ".join(f"{i} {v:,.0f}" for i, v in list(unreg.items())[:4]))
        if unvalued:
            bits.append("⚠️ skipped (no value set): "
                        + ", ".join(f"{i} ×{q}" for i, q in list(unvalued.items())[:4]))
        e.add_field(name="Outstanding", value="\n".join(bits), inline=False)
    else:
        e.add_field(name="Outstanding", value="✅ nothing unpaid", inline=False)
    e.set_footer(text="Settings apply to FUTURE payouts only — never retroactive.")
    return e


class _ValueModal(discord.ui.Modal, title="Set hive item value"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.item = discord.ui.TextInput(label="Item", placeholder="Honeycomb Block", required=True)
        self.val = discord.ui.TextInput(label="Coins per PIECE (not per stack of 64)",
                                        placeholder="4.6875", required=True)
        self.add_item(self.item)
        self.add_item(self.val)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            v = float(str(self.val.value).strip())
        except Exception:
            return await interaction.response.send_message("❌ Value must be a number.", ephemeral=True)
        if not (0 <= v <= 1_000_000):
            return await interaction.response.send_message("❌ Value out of range.", ephemeral=True)
        key = " ".join(str(self.item.value).strip().lower().split())
        _db().set_config(f"hive_value:{key}", str(v))
        await self.panel.refresh(interaction, f"✅ {str(self.item.value).strip()} → {v:g}/piece.")


class _WageModal(discord.ui.Modal, title="Set harvester wage"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.pct = discord.ui.TextInput(label="Harvester % of harvested value",
                                        placeholder="15", required=True)
        self.add_item(self.pct)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            p = float(str(self.pct.value).strip())
        except Exception:
            return await interaction.response.send_message("❌ Must be a number.", ephemeral=True)
        if not (0 <= p <= 100):
            return await interaction.response.send_message("❌ Must be 0-100.", ephemeral=True)
        _db().set_config("hive_harvester_pct", str(p))
        await self.panel.refresh(interaction, f"✅ Harvester wage → {p:g}%.")


class _SplitModal(discord.ui.Modal, title="Set partner owner cut"):
    def __init__(self, panel):
        super().__init__(timeout=300)
        self.panel = panel
        self.pct = discord.ui.TextInput(label="Partner owner % (V Tech's own hives = 0)",
                                        placeholder="0", required=True)
        self.add_item(self.pct)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            p = float(str(self.pct.value).strip())
        except Exception:
            return await interaction.response.send_message("❌ Must be a number.", ephemeral=True)
        if not (0 <= p <= 80):
            return await interaction.response.send_message("❌ Must be 0-80.", ephemeral=True)
        _db().set_config(f"hive_owner_pct:{self.panel.market_id}", str(p))
        await self.panel.refresh(interaction, f"✅ Partner owner cut → {p:g}%.")


class HiveSettingsView(discord.ui.View):
    """One panel for every hive setting. `cog` is the HiveCog (for _settle_groups)."""

    def __init__(self, cog, market_id: str, user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.market_id = str(market_id)
        self.user_id = int(user_id)
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.user_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    def _build(self):
        self.clear_items()
        markets = (core._load_markets().get("markets", {}) or {})
        bound = sorted({m for _c, m in _feeds()})
        pool = bound or [m for m, v in markets.items() if isinstance(v, dict) and v.get("active", True)]
        if self.market_id not in pool:
            pool = [self.market_id] + pool
        opts = [discord.SelectOption(label=str((markets.get(m) or {}).get("name", m))[:80],
                                     value=m, default=(m == self.market_id))
                for m in pool[:25]]
        if opts:
            sel = discord.ui.Select(placeholder="Hive site…", options=opts, row=0)

            async def _pick(interaction: discord.Interaction):
                self.market_id = sel.values[0]
                await self.refresh(interaction)
            sel.callback = _pick
            self.add_item(sel)

        autopay = str(_db().get_config(f"hive_autopay:{self.market_id}") or "") == "1"
        self.add_item(self._btn("Autopay: ON" if autopay else "Autopay: off",
                                discord.ButtonStyle.success if autopay else discord.ButtonStyle.secondary,
                                self._toggle_autopay, 1))
        self.add_item(self._btn("Pay now", discord.ButtonStyle.primary, self._payout, 1))
        self.add_item(self._btn("Bind this channel", discord.ButtonStyle.secondary, self._bind, 1))
        self.add_item(self._btn("Unbind", discord.ButtonStyle.secondary, self._unbind, 1))
        self.add_item(self._btn("Item value", discord.ButtonStyle.secondary, self._set_value, 2))
        self.add_item(self._btn("Wage %", discord.ButtonStyle.secondary, self._set_wage, 2))
        self.add_item(self._btn("Owner split", discord.ButtonStyle.secondary, self._set_split, 2))

    def _btn(self, label, style, cb, row):
        b = discord.ui.Button(label=label, style=style, row=row)
        b.callback = cb
        return b

    async def refresh(self, interaction: discord.Interaction, note: str = ""):
        self._build()
        e = build_embed(self.market_id)
        if note:
            e.description = note + "\n" + (e.description or "")
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=e, view=self)
            else:
                await interaction.response.edit_message(embed=e, view=self)
        except Exception as ex:
            log.debug("[hive panel] refresh failed: %s", ex)

    # ── actions ──────────────────────────────────────────────────────────────
    async def _toggle_autopay(self, interaction: discord.Interaction):
        d = _db()
        on = str(d.get_config(f"hive_autopay:{self.market_id}") or "") == "1"
        d.set_config(f"hive_autopay:{self.market_id}", "0" if on else "1")
        if on:
            note = "⏸️ Autopay off — lines record only."
        else:
            note = "✅ Autopay on."
            backlog = len(d.get_unpaid_hive_harvests(self.market_id) or [])
            if backlog:
                note += (f" Note: {backlog} line(s) are ALREADY unpaid — autopay only touches "
                         f"new lines, so use **Pay now** for those.")
        await self.refresh(interaction, note)

    async def _payout(self, interaction: discord.Interaction):
        await interaction.response.defer()
        hive_mod = sys.modules.get("cogs.hive")
        rows = _db().get_unpaid_hive_harvests(self.market_id) or []
        if not rows or hive_mod is None:
            return await self.refresh(interaction, "Nothing unpaid.")
        groups, unreg, unvalued = hive_mod._group_rows(rows)
        if not groups:
            held = ", ".join(f"{i} ({v:,.0f})" for i, v in list(unreg.items())[:5])
            return await self.refresh(
                interaction,
                "Nothing payable." + (f" Held for unregistered: {held}" if held else ""))
        try:
            res = await self.cog._settle_groups(self.market_id, groups,
                                                batch=f"panel-{interaction.id}")
        except Exception as e:
            log.warning("[hive panel] payout failed: %s", e)
            return await self.refresh(interaction, f"❌ Payout failed: {e}")
        await self.refresh(interaction,
                           f"💸 Paid **{res['harv_total']:,.0f}** in wages on "
                           f"**{res['value_total']:,.0f}** value to {len(groups)} harvester(s).")

    async def _bind(self, interaction: discord.Interaction):
        _db().set_config(f"hive_feed:{interaction.channel_id}", str(self.market_id))
        await self.refresh(interaction, f"🔗 This channel now feeds **{self.market_id}**.")

    async def _unbind(self, interaction: discord.Interaction):
        _db().delete_config(f"hive_feed:{interaction.channel_id}")
        await self.refresh(interaction, "🔌 This channel is no longer a hive feed.")

    async def _set_value(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ValueModal(self))

    async def _set_wage(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_WageModal(self))

    async def _set_split(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_SplitModal(self))
