"""Hive engine (/hive) — the company's perpetual "Hive harvesting" project.

Harvesting is WORK employees do for the company: they sell honey/combs to the chest
shops (which buy at 0 coins), and the company owes them a wage. This cog closes that
loop automatically: the CSN Notifier webhook posts per-player lines ("X sold you
276xHoney Block …") into a bound channel; every line is recorded idempotently, and —
with autopay on — the harvester is IMMEDIATELY paid their % of the harvested value
(Honey Block 350 / Honeycomb Block 300 by default) to their coin balance, awarded
loyalty, and the wage is logged under the perpetual project (team_perf kind="project",
so the cost of hive harvesting is always visible). A partner owner's cut is paid where
configured, and V Tech's remainder books to the market's hive ledger — which the stock
roll-up prices off.

One-time setup: /hive bind (in the webhook channel) → /hive settle (write off the
pre-engine backlog you already paid by hand) → /hive autopay on. After that it runs
itself. /hive payout stays as a manual sweep (e.g. after someone registers their IGN).
"""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
bot = core.bot
log = core.log
is_manager = core.is_manager
_is_market_manager = core._is_market_manager
_market_autocomplete = core._market_autocomplete
_load_markets = core._load_markets
add_coins = core.add_coins
safe_dm = core.safe_dm
LOYALTY_POINTS_DIVISOR = core.LOYALTY_POINTS_DIVISOR
VTECH_SLICE_PCT = core.VTECH_SLICE_PCT
_is_vtech_market = core._is_vtech_market
_award_loyalty_points = core._award_loyalty_points
_award_market_loyalty_points = core._award_market_loyalty_points
_market_loyalty_cfg = core._market_loyalty_cfg
_market_owner_id = core._market_owner_id

PROJECT_DETAIL = "project:hive-harvesting"


def _fmt(n) -> str:
    return f"{int(round(float(n))):,}"


def _ingest_lines(market_id: str, msg_id: str, lines: list, start_line: int = 0) -> list:
    """Insert parsed (ign, qty, item) rows for one message; returns the NEW row ids.
    Values snapshot at ingest; unregistered IGNs stored with user_id NULL.

    AUDIT FIX (high): dedup is by CONTENT MULTISET per message, not line index.
    The old index-based scheme assumed cumulative feeds only APPEND — a webhook
    that prepends its newest sale shifted every old line to a new index (each one
    re-ingested and re-paid) while the actual new line hid below start_line.
    Now each already-stored (ign, qty, item) occurrence cancels one incoming
    occurrence, so append, prepend and mid-rewrite are all safe. (start_line is
    kept for call compatibility but content matching supersedes it.)"""
    import Restocker_db as _db
    from collections import Counter
    have = Counter()
    try:
        for t in _db.get_hive_msg_lines(msg_id):
            have[t] += 1
    except Exception:
        pass
    next_no = sum(have.values())
    new_ids = []
    for (ign, qty, item) in lines:
        key = (str(ign), int(qty), str(item))
        if have.get(key, 0) > 0:
            have[key] -= 1                    # already ingested from a prior version
            continue
        uid = None
        try:
            uid = _db.get_user_id_by_ign(ign)
        except Exception:
            pass
        val = core._hive_item_value(item)
        try:
            rid = _db.add_hive_harvest(market_id, ign, uid, item, qty, val, msg_id, next_no)
            if rid:
                new_ids.append(rid)
                next_no += 1
        except Exception as e:
            log.warning("[hive] ingest failed (%s line %d): %s", msg_id, next_no, e)
    return new_ids


def _group_rows(rows: list):
    """Split harvest rows into payable groups and holdbacks.
    Returns (groups {uid: {ign, ids, qty, value}}, unregistered {ign: value}, unvalued {item: qty})."""
    import Restocker_db as _db
    groups, unregistered, unvalued = {}, {}, {}
    for r in rows:
        uid = r.get("user_id")
        if not uid:  # late-registration self-heal: try resolving again now
            try:
                uid = _db.get_user_id_by_ign(r.get("ign") or "")
                if uid:
                    _db.set_hive_harvest_user(r.get("ign"), uid)
            except Exception:
                uid = None
        val = float(r.get("unit_value") or 0) or core._hive_item_value(r.get("item"))
        if val <= 0:
            unvalued[str(r.get("item"))] = unvalued.get(str(r.get("item")), 0) + int(r.get("qty") or 0)
            continue
        if not uid:
            v = int(round(int(r.get("qty") or 0) * val))
            unregistered[str(r.get("ign"))] = unregistered.get(str(r.get("ign")), 0) + v
            continue
        g = groups.setdefault(str(uid), {"ign": r.get("ign"), "ids": [], "qty": 0, "value": 0.0})
        g["ids"].append(int(r["id"]))
        g["qty"] += int(r.get("qty") or 0)
        g["value"] += int(r.get("qty") or 0) * val
    return groups, unregistered, unvalued


class HiveIngestModal(discord.ui.Modal, title="Paste hive feed lines"):
    """Manual fallback / back-history: paste 'X sold you Nx Item' lines."""

    def __init__(self, market_id: str):
        super().__init__(timeout=600)
        self.market_id = str(market_id)
        self.blob = discord.ui.TextInput(
            label=f"Feed lines for {market_id}",
            style=discord.TextStyle.paragraph, required=True, max_length=3900,
            placeholder="JesseNapoleon sold you 276xHoney Block 3d10h45m ago (-0 Coins)")
        self.add_item(self.blob)

    async def on_submit(self, interaction: discord.Interaction):
        lines = core._parse_hive_feed(str(self.blob.value or ""))
        if not lines:
            return await interaction.response.send_message(
                "❌ No 'X sold you Nx Item' lines found in that paste.", ephemeral=True)
        new_ids = _ingest_lines(self.market_id, f"manual:{interaction.id}", lines)
        await interaction.response.send_message(
            f"🐝 Recorded **{len(new_ids)}** harvest line(s) for `{self.market_id}`. "
            f"`/hive status` to review — with autopay ON they'd have paid instantly; "
            f"manual ingests settle via `/hive payout`.", ephemeral=True)


class HiveCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    hive = app_commands.Group(
        name="hive",
        description="(Managers) Hive harvesting — the company's perpetual harvest project",
        default_permissions=discord.Permissions(manage_guild=True))

    # ── the payment core (shared by autopay and /hive payout) ────────────────
    async def _settle_groups(self, market_id: str, groups: dict, batch: str) -> dict:
        """Pay every group: harvester % to coins, loyalty, project team-credit; then the
        owner cut and the hive-ledger booking (which reprices the stock). Returns a
        summary dict. Rows are marked paid per-user right after their payment lands."""
        import Restocker_db as _db
        import asyncio as _aio
        pct = core._hive_harvester_pct()
        opct = core._hive_owner_pct(market_id)
        mkt_mult, _mb, _mp = _market_loyalty_cfg(market_id)
        value_total = sum(g["value"] for g in groups.values())

        paid_lines, harv_total = [], 0
        settled_value = 0
        for uid, g in sorted(groups.items(), key=lambda kv: -kv[1]["value"]):
            pay = int(round(g["value"] * pct / 100.0))
            # AUDIT FIX (high): CLAIM FIRST, pay after. Two concurrent settle runs
            # (the autopay listener mid-batch + a manager's /hive payout) used to
            # snapshot the same unpaid rows and BOTH paid them. The claim is one
            # atomic UPDATE ... WHERE paid=0; whoever claims, pays. A payment that
            # fails releases the claim so the rows stay payable — and value is only
            # BOOKED for rows settled in this run, so a later retry can't book the
            # same production twice.
            claimed = _db.mark_hive_harvests_paid(g["ids"])
            if claimed <= 0:
                continue                       # another settle run owns these rows
            if claimed < len(g["ids"]):
                # ambiguous overlap with another run's snapshot — release and let the
                # next run recompute cleanly; no coins move on ambiguity.
                try:
                    _db.unmark_hive_harvests_paid(g["ids"])
                except Exception:
                    pass
                continue
            if pay <= 0:
                settled_value += g["value"]    # produced value with a 0-coin wage still books
                continue
            try:
                new_bal, _p = add_coins(int(uid), pay, counts_as_principal=True,
                                        reason=f"hive:{market_id}:{batch}")
            except Exception as e:
                try:
                    _db.unmark_hive_harvests_paid(g["ids"])   # release for retry
                except Exception:
                    pass
                paid_lines.append(f"• <@{uid}> — ❌ pay failed: {e}")
                continue
            harv_total += pay
            settled_value += g["value"]
            # Loyalty — order-payout convention: points from VALUE; market ledger full,
            # shared V Tech pool full-or-slice.
            lp = max(1, int(g["value"] // LOYALTY_POINTS_DIVISOR))
            if mkt_mult != 1.0:
                lp = max(1, int(lp * mkt_mult))
            try:
                _award_market_loyalty_points(int(uid), market_id, lp, reason=f"hive:{batch}")
            except Exception:
                pass
            vtech_pts = lp if _is_vtech_market(market_id) else max(1, int(lp * VTECH_SLICE_PCT / 100.0))
            try:
                _award_loyalty_points(int(uid), vtech_pts, reason=f"hive:{batch}")
            except Exception:
                pass
            # The wage is PROJECT work — logged under the perpetual hive-harvesting
            # project so the company always sees what harvesting costs.
            try:
                mgr = _db.get_manager_of(uid)
                mgr_id = str(mgr) if mgr else (uid if _db.get_team(uid) else None)
                if mgr_id:
                    _db.record_team_perf(mgr_id, uid, "project", coins=float(pay),
                                         points=float(lp), qty=int(g["qty"]),
                                         detail=f"{PROJECT_DETAIL}:{market_id}:{batch}")
            except Exception:
                pass
            paid_lines.append(f"• <@{uid}> ({g['ign']}) +**{_fmt(pay)}** for {_fmt(g['qty'])} pcs · +{lp} pts")
            try:
                user = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                if user:
                    await safe_dm(user, f"🐝 Harvest wage: **{_fmt(pay)}** coins for "
                                        f"{_fmt(g['qty'])} pcs (`{market_id}`, hive-harvesting project). "
                                        f"+{lp} loyalty pts.")
            except Exception:
                pass
            await _aio.sleep(0.35)

        # Partner-site share ("rent"): the site keeps this slice of the honey IN KIND —
        # V Tech owes nobody coins for it. Computed on SETTLED value only, so failed
        # or contested groups book nothing until they actually settle.
        owner_pay = int(round(settled_value * opct / 100.0)) if opct > 0 else 0
        owner_line = ""
        if owner_pay > 0:
            owner_line = (f"🏠 Site share ({opct:g}%): **{_fmt(owner_pay)}** kept by the site "
                          f"in kind — no coins paid")

        booked = core._book_hive_month(market_id, settled_value, harv_total, owner_pay)
        return {"paid_lines": paid_lines, "value_total": settled_value,
                "harv_total": harv_total, "owner_line": owner_line,
                "net": settled_value - harv_total - owner_pay,
                "month": booked.get("month", "current")}

    # ── feed listeners: record, and (autopay) pay on the spot ────────────────
    async def _handle_feed_message(self, message, start_line: int = 0):
        try:
            if message.author and self.bot.user and message.author.id == self.bot.user.id:
                return
            import Restocker_db as _db
            mid = _db.get_config(f"hive_feed:{message.channel.id}")
            if not mid:
                return                                 # not a hive-feed channel — ignore silently
            text = message.content or ""
            for e in (message.embeds or []):
                if getattr(e, "description", None):
                    text += "\n" + e.description
            lines = core._parse_hive_feed(text)
            if not lines:
                return                                 # normal chat, nothing harvest-shaped — ignore
            # AUDIT FIX (critical): only the notifier may feed harvest lines — a plain
            # member typing "TheirIGN sold you 64000xHoney Block" in a bound channel
            # was minting instant wages via autopay. Webhook posts and bot posts pass;
            # human-authored messages never do. This check now runs ONLY after we know
            # the message is a real harvest line in a real feed channel, so ordinary
            # human chatter no longer floods the log with false REJECTED warnings — the
            # warning now marks a genuine injection attempt worth seeing.
            if message.webhook_id is None and not getattr(message.author, "bot", False):
                log.warning("[hive] REJECTED harvest line from human user %s in bound channel #%s",
                            getattr(message.author, "id", "?"),
                            getattr(message.channel, "name", "?"))
                return
            new_ids = _ingest_lines(str(mid), str(message.id), lines, start_line=start_line)
            if not new_ids:
                return
            try:
                await message.add_reaction("🐝")
            except Exception:
                pass
            if str(_db.get_config(f"hive_autopay:{mid}") or "") != "1":
                return                                 # record-only until autopay is on
            rows = _db.get_hive_harvests_by_ids(new_ids)
            groups, unregistered, _unvalued = _group_rows(rows)
            if groups:
                res = await self._settle_groups(str(mid), groups, batch=str(message.id))
                receipt = ("🐝 **Harvest wages paid** (`hive-harvesting` project · "
                           f"value {_fmt(res['value_total'])}):\n" + "\n".join(res["paid_lines"]))
                if res["owner_line"]:
                    receipt += "\n" + res["owner_line"]
                if unregistered:
                    receipt += ("\n⚠ Not registered (need `/register_ign`): "
                                + ", ".join(unregistered))
                try:
                    await message.channel.send(receipt[:1900],
                                               allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass
            elif unregistered:
                try:
                    await message.channel.send(
                        "🐝 Harvest recorded, but these IGNs aren't linked to Discord yet — "
                        "they'll be paid automatically once they run `/register_ign`: "
                        + ", ".join(unregistered),
                        allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass
        except Exception as e:
            log.warning("[hive] feed listener failed: %s", e)

    @commands.Cog.listener()
    async def on_message(self, message):
        await self._handle_feed_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        # A cumulative-list feed edits its message in place — ingest only lines beyond
        # what this message already contributed (idempotent for pure re-edits).
        try:
            import Restocker_db as _db
            already = _db.hive_lines_for_msg(str(after.id))
        except Exception:
            already = 0
        await self._handle_feed_message(after, start_line=already)

    # ── config commands ───────────────────────────────────────────────────────
    @hive.command(name="bind", description="Bind THIS channel as a market's hive harvest feed")
    @app_commands.describe(market_id="The hive market these harvests belong to (e.g. vtech)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def hive_bind(self, interaction: discord.Interaction, market_id: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        markets = (_load_markets() or {}).get("markets") or {}
        if market_id not in markets:
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)
        import Restocker_db as _db
        _db.set_config(f"hive_feed:{interaction.channel_id}", str(market_id))
        await interaction.response.send_message(
            f"🐝 This channel now feeds **{markets[market_id].get('name', market_id)}**'s hive project. "
            f"Every \"X sold you …\" line here is recorded automatically.\n"
            f"Next: `/hive settle` once to write off the already-paid backlog, then "
            f"`/hive autopay market_id:{market_id} enabled:True` — from then on harvesters "
            f"are paid the moment their sale posts.", ephemeral=True)

    @hive.command(name="unbind", description="Stop treating THIS channel as a hive feed")
    async def hive_unbind(self, interaction: discord.Interaction):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        _db.delete_config(f"hive_feed:{interaction.channel_id}")
        await interaction.response.send_message("✅ Channel unbound — no longer a hive feed.", ephemeral=True)

    @hive.command(name="autopay", description="Pay harvesters INSTANTLY when their sale posts to the feed")
    @app_commands.describe(market_id="The hive market",
                           enabled="True = pay on ingest (run /hive settle FIRST). False = record only.")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def hive_autopay(self, interaction: discord.Interaction, market_id: str, enabled: bool):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        if enabled:
            backlog = len(_db.get_unpaid_hive_harvests(market_id))
            _db.set_config(f"hive_autopay:{market_id}", "1")
            warn = (f"\n⚠ **{backlog} unpaid row(s) are sitting in the backlog** — autopay only "
                    f"touches NEW lines, so settle or pay those explicitly (`/hive settle` for "
                    f"already-paid-by-hand, `/hive payout` to pay them)." if backlog else "")
            return await interaction.response.send_message(
                f"⚡ Autopay **ON** for `{market_id}` — harvesters are paid the moment their "
                f"sale posts, wages logged under the hive-harvesting project, value booked to "
                f"the stock automatically.{warn}", ephemeral=True)
        _db.set_config(f"hive_autopay:{market_id}", "0")
        await interaction.response.send_message(
            f"⏸ Autopay **off** for `{market_id}` — lines record only; settle with `/hive payout`.",
            ephemeral=True)

    @hive.command(name="set_value", description="Set the per-piece value of a hive product (default: Honey Block 350, Honeycomb 300)")
    @app_commands.describe(item="Item name as it appears in the feed (e.g. Honey Block)",
                           value="Coins per piece (0 removes it from hive valuation)")
    async def hive_set_value(self, interaction: discord.Interaction, item: str,
                             value: app_commands.Range[float, 0.0, 1_000_000.0]):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        key = " ".join(str(item).strip().lower().split())
        _db.set_config(f"hive_value:{key}", str(float(value)))
        await interaction.response.send_message(
            f"✅ **{item.strip()}** now valued at **{value:g}**/pc for harvests.", ephemeral=True)

    @hive.command(name="set_wage", description="Set the harvesters' wage — their % of harvested value (default 17)")
    @app_commands.describe(pct="0–100 — e.g. 17 means a harvester gets 17% of the value they bring in")
    async def hive_set_wage(self, interaction: discord.Interaction,
                            pct: app_commands.Range[float, 0.0, 100.0]):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        _db.set_config("hive_harvester_pct", str(float(pct)))
        await interaction.response.send_message(
            f"✅ Harvester wage set to **{pct:g}%** of harvested value. Applies to every payout "
            f"from now on (never retroactive).", ephemeral=True)

    @hive.command(name="set_split", description="Set a partner owner's cut of harvested value on a market (V Tech's own hives = 0)")
    @app_commands.describe(market_id="The hive market",
                           owner_pct="% of harvested VALUE the market owner gets (e.g. 32 for 60/40 after the harvester cut)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def hive_set_split(self, interaction: discord.Interaction, market_id: str,
                             owner_pct: app_commands.Range[float, 0.0, 80.0]):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        import Restocker_db as _db
        _db.set_config(f"hive_owner_pct:{market_id}", str(float(owner_pct)))
        hp = core._hive_harvester_pct()
        await interaction.response.send_message(
            f"✅ `{market_id}` hive split: harvesters **{hp:g}%** · owner **{owner_pct:g}%** · "
            f"V Tech **{100.0 - hp - float(owner_pct):g}%** of harvested value.", ephemeral=True)

    @hive.command(name="ingest", description="Manually paste feed lines (fallback / back-history)")
    @app_commands.describe(market_id="The hive market these harvests belong to")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def hive_ingest(self, interaction: discord.Interaction, market_id: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.send_modal(HiveIngestModal(market_id))

    # ── backlog + manual sweep ───────────────────────────────────────────────
    @hive.command(name="settle",
                  description="Mark unpaid harvests as ALREADY paid by hand — optionally still BOOK their value to the company")
    @app_commands.describe(
        market_id="The hive market",
        ign="Only this player's rows (blank = everyone's unpaid backlog)",
        book="True = no coins move, but the harvest VALUE + hand-paid wage cost are booked to the ledger/stock (the honey was real!)",
        apply="False (default) = preview. True = execute.")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def hive_settle(self, interaction: discord.Interaction, market_id: str,
                          ign: Optional[str] = None, book: bool = False, apply: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import Restocker_db as _db
        rows = _db.get_unpaid_hive_harvests(market_id)
        if ign:
            rows = [r for r in rows if str(r.get("ign") or "").lower() == ign.strip().lower()]
        if not rows:
            return await interaction.followup.send(
                f"Nothing unpaid to settle for `{market_id}`" + (f" / `{ign}`" if ign else "") + ".",
                ephemeral=True)
        per, total_v = {}, 0.0
        for r in rows:
            v = (float(r.get("unit_value") or 0) or core._hive_item_value(r.get("item"))) * int(r.get("qty") or 0)
            total_v += v
            p = per.setdefault(str(r.get("ign")), {"qty": 0, "value": 0.0})
            p["qty"] += int(r.get("qty") or 0)
            p["value"] += v
        pct = core._hive_harvester_pct()
        opct = core._hive_owner_pct(market_id)
        wage_cost = int(round(total_v * pct / 100.0))
        owner_cost = int(round(total_v * opct / 100.0)) if opct > 0 else 0
        net_gain = int(round(total_v)) - wage_cost - owner_cost
        lines = [f"• {i} — {_fmt(p['qty'])} pcs · value {_fmt(p['value'])}"
                 for i, p in sorted(per.items(), key=lambda kv: -kv[1]["value"])[:20]]
        book_line = (f"\n📒 With `book:True`: value **{_fmt(total_v)}** in · hand-paid wages "
                     f"**{_fmt(wage_cost)}** out"
                     + (f" · owner **{_fmt(owner_cost)}**" if owner_cost else "")
                     + f" → **{_fmt(net_gain)} net** booked to the ledger + stock.")
        if not apply:
            return await interaction.followup.send(
                f"🧾 **Settle preview — `{market_id}`**: {len(rows)} row(s), value {_fmt(total_v)}. "
                f"No coins would move (already paid by hand):\n" + "\n".join(lines)
                + (book_line if book else "\n(Not booking — add `book:True` to also record the "
                                          "economics to the ledger/stock.)")
                + "\n\nRe-run with `apply:True` to confirm.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        n = _db.mark_hive_harvests_paid([int(r["id"]) for r in rows])
        booked_note = ""
        if book and total_v > 0:
            booked = core._book_hive_month(market_id, float(total_v), float(wage_cost), float(owner_cost))
            booked_note = (f"\n📒 Booked to {booked.get('month', 'current')}: value {_fmt(total_v)}, "
                           f"costs {_fmt(wage_cost + owner_cost)} → **{_fmt(net_gain)} net** — "
                           f"ledger + stock updated.")
        await interaction.followup.send(
            f"🧾 Settled **{n}** row(s) ({_fmt(total_v)} value) — no coins paid.{booked_note}",
            ephemeral=True)

    @hive.command(name="status", description="Unpaid harvests for a market — who's owed what")
    @app_commands.describe(market_id="The hive market")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def hive_status(self, interaction: discord.Interaction, market_id: str):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import Restocker_db as _db
        groups, unregistered, unvalued = _group_rows(_db.get_unpaid_hive_harvests(market_id))
        pct = core._hive_harvester_pct()
        autopay = str(_db.get_config(f"hive_autopay:{market_id}") or "") == "1"
        if not groups and not unregistered and not unvalued:
            return await interaction.followup.send(
                f"🐝 No unpaid harvests for `{market_id}` (autopay {'⚡ ON' if autopay else 'off'}).",
                ephemeral=True)
        total_v = sum(g["value"] for g in groups.values())
        lines = [f"• <@{uid}> ({g['ign']}) — {_fmt(g['qty'])} pcs · value {_fmt(g['value'])} → "
                 f"pay **{_fmt(g['value'] * pct / 100)}**"
                 for uid, g in sorted(groups.items(), key=lambda kv: -kv[1]["value"])[:20]]
        msg = (f"🐝 **Unpaid harvests — `{market_id}`** (harvesters {pct:g}% · autopay "
               f"{'⚡ ON' if autopay else 'off'})\n" + "\n".join(lines)
               + f"\n**Total value {_fmt(total_v)}** → payouts {_fmt(total_v * pct / 100)}")
        if unregistered:
            msg += ("\n⚠ Unregistered IGNs (need `/register_ign`): "
                    + ", ".join(f"{i} ({_fmt(v)} value)" for i, v in unregistered.items()))
        if unvalued:
            msg += ("\n⚠ No hive value set (`/hive set_value`): "
                    + ", ".join(f"{it} ×{q}" for it, q in unvalued.items()))
        await interaction.followup.send(msg[:1950], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @hive.command(name="payout", description="Manual sweep: pay everything unpaid (autopay handles new lines by itself)")
    @app_commands.describe(market_id="The hive market",
                           apply="False (default) = preview only. True = pay + book.")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def hive_payout(self, interaction: discord.Interaction, market_id: str,
                          apply: bool = False):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        import Restocker_db as _db
        groups, unregistered, unvalued = _group_rows(_db.get_unpaid_hive_harvests(market_id))
        if not groups:
            extra = " (only unregistered IGNs are waiting — they need `/register_ign`)" if unregistered else ""
            return await interaction.followup.send(
                f"🐝 Nothing payable for `{market_id}`{extra}.", ephemeral=True)
        pct = core._hive_harvester_pct()
        value_total = sum(g["value"] for g in groups.values())
        warn = ""
        if unregistered:
            warn += "\n⚠ Held back (unregistered): " + ", ".join(
                f"{i} ({_fmt(v)})" for i, v in unregistered.items())
        if unvalued:
            warn += "\n⚠ Skipped (no value set): " + ", ".join(f"{it} ×{q}" for it, q in unvalued.items())
        if not apply:
            lines = [f"• <@{uid}> ({g['ign']}) — {_fmt(g['qty'])} pcs → **{_fmt(g['value'] * pct / 100)}**"
                     for uid, g in sorted(groups.items(), key=lambda kv: -kv[1]["value"])[:20]]
            return await interaction.followup.send(
                f"🐝 **Payout preview — `{market_id}`** (value {_fmt(value_total)}):\n"
                + "\n".join(lines) + warn
                + "\n\n🔍 Nothing paid. Re-run with `apply:True` to execute.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        res = await self._settle_groups(market_id, groups, batch=str(interaction.id))
        await interaction.followup.send(
            f"🐝 **Hive payout — `{market_id}`** · value {_fmt(res['value_total'])} · wages "
            f"{_fmt(res['harv_total'])} · V Tech keeps {_fmt(res['net'])}\n"
            + "\n".join(res["paid_lines"])
            + (("\n" + res["owner_line"]) if res["owner_line"] else "") + warn
            + f"\n\n✅ Paid & booked to `{market_id}`'s {res['month']} hive ledger — stock repriced.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(HiveCog(bot))
