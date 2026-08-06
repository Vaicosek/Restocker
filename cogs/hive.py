"""Hive engine (/hive) — the company's perpetual "Hive harvesting" project.

Harvesting is WORK employees do for the company: they sell honey/combs to the chest
shops (which buy at 0 coins), and the company owes them a wage. This cog closes that
loop automatically: the CSN Notifier webhook posts per-player lines ("X sold you
276xHoney Block …") into a bound channel; every line is recorded idempotently, and —
with autopay on — the harvester is IMMEDIATELY paid their % of the harvested value
to their coin balance, awarded
loyalty, and the wage is logged under the perpetual project (team_perf kind="project",
so the cost of hive harvesting is always visible). A partner owner's cut is paid where
configured, and V Tech's remainder books to the market's hive ledger — which the stock
roll-up prices off.

Everything is driven from ONE panel: `/hive settings` (HiveSettings). Bind a feed
channel, set the wage / partner split / item values, toggle autopay, and pay the
backlog — all from there. Seven subcommands used to do this; the panel replaced them.
"""
import sys
import discord
from discord import app_commands
from discord.ext import commands, tasks

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


# ── central hive-project report ──────────────────────────────────────────────
# One place that always answers "how is the hive project actually doing" without
# anyone running a command: posted when the bot comes up, and again after every
# 6h autopay sweep. Config key `hive_report_channel` overrides the default, and
# setting it to "0" turns the whole thing off.
HIVE_REPORT_CHANNEL_DEFAULT = 1525241251967012874


def _hive_report_channel_id() -> int:
    try:
        import Restocker_db as _db
        raw = str(_db.get_config("hive_report_channel") or "").strip()
        if raw:
            return int(raw)
    except Exception:
        pass
    return HIVE_REPORT_CHANNEL_DEFAULT


def _hive_report_markets() -> list:
    """Every hive site worth reporting on: those with a bound feed channel, those
    holding unpaid rows, and those with any harvest history at all."""
    import Restocker_db as _db
    mids = set()
    try:
        mids |= {str(v) for v in (_db.get_config_prefix("hive_feed:") or {}).values()}
    except Exception:
        pass
    try:
        mids |= {str(m) for m in (_db.hive_markets_with_unpaid() or [])}
    except Exception:
        pass
    try:
        with _db.db() as conn:
            mids |= {str(r[0]) for r in conn.execute(
                "SELECT DISTINCT market_id FROM hive_harvests").fetchall()}
    except Exception:
        pass
    return sorted(m for m in mids if m)


def build_hive_project_report(trigger: str = "startup") -> list:
    """The full hive-project picture as a list of Discord-sized message strings.

    Per site: value harvested, wages paid, what is still owed, every harvester with
    their quantity/value/wage, the honey-vs-comb split, and coins parked on IGNs that
    were never linked to a Discord account. Read-only — it never moves a coin."""
    import Restocker_db as _db
    pct = core._hive_harvester_pct()
    mids = _hive_report_markets()
    if not mids:
        return []

    out = [f"🐝 **Hive project report** · {trigger} · harvester wage {pct:g}%"]
    g_value = g_paid_value = g_unpaid_value = g_unlinked = 0.0
    g_qty = 0

    for mid in mids:
        try:
            summary = _db.get_hive_harvest_summary(mid) or {}
        except Exception as e:
            out.append(f"\n**`{mid}`** — could not read summary: {e}")
            continue
        if not summary:
            continue

        qty = sum(m.get("qty", 0) for m in summary.values())
        value = sum(m.get("value", 0.0) for m in summary.values())
        paid_value = sum(m.get("paid_value", 0.0) for m in summary.values())
        unpaid_value = max(0.0, value - paid_value)
        g_qty += qty
        g_value += value
        g_paid_value += paid_value
        g_unpaid_value += unpaid_value

        owner_pct = core._hive_owner_pct(mid)
        autopay = "on" if core.hive_autopay_on(mid) else "**OFF**"
        mname = (core._get_market(mid) or {}).get("name", mid)

        out.append(
            f"\n**{mname}** (`{mid}`) · autopay {autopay}"
            f"\n{_fmt(qty)} pcs harvested · worth **{_fmt(value)}** · "
            f"wages paid {_fmt(paid_value * pct / 100.0)}"
            + (f" · owner cut {owner_pct:g}%" if owner_pct else "")
            + (f"\n⚠️ **{_fmt(unpaid_value)}** of value still unpaid "
               f"(≈{_fmt(unpaid_value * pct / 100.0)} in wages owed)" if unpaid_value > 0 else ""))

        # who harvested, and what they earned
        by_ign, by_item = {}, {}
        for mo in summary.values():
            for ign, v in (mo.get("by_ign") or {}).items():
                a = by_ign.setdefault(ign, {"qty": 0, "value": 0.0})
                a["qty"] += v.get("qty", 0); a["value"] += v.get("value", 0.0)
            for item, v in (mo.get("by_item") or {}).items():
                a = by_item.setdefault(item, {"qty": 0, "value": 0.0})
                a["qty"] += v.get("qty", 0); a["value"] += v.get("value", 0.0)

        if by_item:
            out.append("· " + " · ".join(
                f"{_fmt(v['qty'])}× {item} ({_fmt(v['value'])})"
                for item, v in sorted(by_item.items(), key=lambda kv: -kv[1]["value"])))

        for ign, v in sorted(by_ign.items(), key=lambda kv: -kv[1]["value"]):
            wage = v["value"] * pct / 100.0
            linked = None
            try:
                linked = _db.get_user_id_by_ign(ign)
            except Exception:
                pass
            tag = f"<@{linked}>" if linked else f"`{ign}` ⚠️ not linked"
            out.append(f"  • {tag} — {_fmt(v['qty'])} pcs · "
                       f"{_fmt(v['value'])} value · {_fmt(wage)} earned"
                       + ("" if linked else " (cannot be paid)"))
            if not linked:
                g_unlinked += wage

    out.append(
        f"\n**Total** · {_fmt(g_qty)} pcs · {_fmt(g_value)} value · "
        f"{_fmt(g_paid_value * pct / 100.0)} wages paid")
    if g_unpaid_value > 0:
        out.append(f"Unpaid backlog: {_fmt(g_unpaid_value)} value "
                   f"(≈{_fmt(g_unpaid_value * pct / 100.0)} in wages)")
    if g_unlinked > 0:
        out.append(f"Stuck on unlinked IGNs: **{_fmt(g_unlinked)}** — link them, or clear "
                   f"the ledger with the `settle_unlinked_harvests` tool.")

    # chunk to Discord's 2000-char limit, never splitting a line
    msgs, cur = [], ""
    for line in out:
        piece = line if line.startswith("\n") else "\n" + line
        if len(cur) + len(piece) > 1900:
            msgs.append(cur.strip("\n"))
            cur = ""
        cur += piece
    if cur.strip():
        msgs.append(cur.strip("\n"))
    return msgs


def build_harvester_statements() -> dict:
    """{user_id: message} — one personal statement per LINKED harvester: how much
    honey and comb they have delivered, what it was worth, what they have already
    been paid, and anything still owed. Read-only."""
    import Restocker_db as _db
    pct = core._hive_harvester_pct()
    people = {}
    for mid in _hive_report_markets():
        try:
            summary = _db.get_hive_harvest_summary(mid) or {}
        except Exception:
            continue
        for mo in summary.values():
            for ign, v in (mo.get("by_ign") or {}).items():
                a = people.setdefault(str(ign), {"qty": 0, "value": 0.0, "sites": set(),
                                                 "items": {}, "uid": None})
                a["qty"] += v.get("qty", 0)
                a["value"] += v.get("value", 0.0)
                a["sites"].add(mid)
        # per-item detail is only available market-wide, so attribute it per site
        # by re-reading the raw rows (cheap: this table is small).
        try:
            with _db.db() as conn:
                for r in conn.execute(
                        "SELECT ign, item, SUM(qty) q, SUM(qty*unit_value) v, "
                        "SUM(CASE WHEN paid=1 THEN qty*unit_value ELSE 0 END) pv, "
                        "MAX(user_id) uid "
                        "FROM hive_harvests WHERE market_id=? GROUP BY ign, item",
                        (str(mid),)).fetchall():
                    a = people.setdefault(str(r[0]), {"qty": 0, "value": 0.0,
                                                      "sites": set(), "items": {},
                                                      "uid": None})
                    it = a["items"].setdefault(str(r[1]), {"qty": 0, "value": 0.0, "paid": 0.0})
                    it["qty"] += int(r[2] or 0)
                    it["value"] += float(r[3] or 0)
                    it["paid"] += float(r[4] or 0)
                    # The harvest rows already carry the account they were attributed to;
                    # trust that first and only fall back to the IGN registry, so someone
                    # who was linked at payout time still gets their statement even if the
                    # registry entry was later renamed or removed.
                    if r[5] and not a.get("uid"):
                        a["uid"] = str(r[5])
        except Exception:
            pass

    out = {}
    for ign, a in people.items():
        uid = a.get("uid")
        if not uid:
            try:
                uid = _db.get_user_id_by_ign(ign)
            except Exception:
                uid = None
        if not uid:
            continue                          # nobody to DM; the report flags these
        value = sum(i["value"] for i in a["items"].values()) or a["value"]
        paid_value = sum(i["paid"] for i in a["items"].values())
        qty = sum(i["qty"] for i in a["items"].values()) or a["qty"]
        earned = value * pct / 100.0
        already = paid_value * pct / 100.0
        owed = max(0.0, earned - already)
        breakdown = " · ".join(
            f"{_fmt(i['qty'])}× {item}"
            for item, i in sorted(a["items"].items(), key=lambda kv: -kv[1]["qty"]))
        msg = (f"🐝 **Your harvest so far**\n"
               f"{breakdown or _fmt(qty) + ' pcs'}\n"
               f"That's **{_fmt(qty)}** pieces worth **{_fmt(value)}** coins at shop value.\n"
               f"Your cut is {pct:g}% — **{_fmt(earned)}** coins earned in total, "
               f"of which **{_fmt(already)}** has already been paid to you.")
        if owed >= 1:
            msg += f"\n**{_fmt(owed)}** is still to come — it pays out automatically."
        else:
            msg += "\nYou're fully paid up. Thanks for keeping the hives running."
        out[str(uid)] = msg
    return out


async def dm_harvester_statements() -> int:
    """DM every linked harvester their personal statement. Returns how many were sent.
    Deliberately NOT run on the 6h sweep — that would be four DMs a day per person."""
    import asyncio as _aio
    sent = 0
    try:
        statements = build_harvester_statements()
    except Exception as e:
        log.warning("[hive report] could not build harvester statements: %s", e)
        return 0
    for uid, msg in statements.items():
        try:
            user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
            if user is None:
                continue
            await safe_dm(user, msg)
            sent += 1
        except Exception as e:
            log.info("[hive report] DM to %s failed: %s", uid, e)
        await _aio.sleep(0.4)                 # gentle on the DM rate limit
    log.info("[hive report] sent %d harvester statement(s)", sent)
    return sent


async def post_hive_project_report(trigger: str = "startup") -> bool:
    """Post the report to the central hive channel. Never raises: a reporting
    problem must not take the sweep — or the bot's startup — down with it."""
    try:
        cid = _hive_report_channel_id()
        if not cid:
            return False                      # hive_report_channel = 0 disables it
        chan = bot.get_channel(cid)
        if chan is None:
            try:
                chan = await bot.fetch_channel(cid)
            except Exception as e:
                log.warning("[hive report] channel %s unreachable: %s", cid, e)
                return False
        msgs = build_hive_project_report(trigger)
        if not msgs:
            log.info("[hive report] nothing to report (no hive sites with history)")
            return False
        import asyncio as _aio
        for i, m in enumerate(msgs):
            await chan.send(m)
            if i + 1 < len(msgs):
                await _aio.sleep(0.4)         # stay clear of the channel rate limit
        log.info("[hive report] posted %d message(s) to #%s (%s)", len(msgs), cid, trigger)
        return True
    except Exception as e:
        log.warning("[hive report] failed: %s", e)
        return False




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
    for row in lines:
        ign, qty, item = row[0], row[1], row[2]
        sale_ts = row[3] if len(row) > 3 else None
        # Timed lines dedup on real sale identity in the DB (uq_hive_sale), so skip the
        # per-message content counter for them — it would wrongly cancel two distinct sales
        # of the same qty. Untimed (legacy) lines keep the content-multiset dedup.
        if sale_ts is None:
            key = (str(ign), int(qty), str(item))
            if have.get(key, 0) > 0:
                have[key] -= 1                # already ingested from a prior version
                continue
        uid = None
        try:
            uid = _db.get_user_id_by_ign(ign)
        except Exception:
            pass
        val = core._hive_item_value(item)
        try:
            rid = _db.add_hive_harvest(market_id, ign, uid, item, qty, val, msg_id, next_no,
                                       sale_ts=sale_ts)
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
        g = groups.setdefault(str(uid), {"ign": r.get("ign"), "ids": [], "qty": 0,
                                         "value": 0.0, "items": {}, "by_id": {}})
        g["ids"].append(int(r["id"]))
        g["qty"] += int(r.get("qty") or 0)
        g["value"] += int(r.get("qty") or 0) * val
        # AUDIT FIX (high): keep each row's own qty/value so a settle run that only
        # wins PART of its snapshot can pay for exactly what it claimed instead of
        # releasing the whole group (which used to un-pay rows another run had
        # already moved coins for, and the next sweep paid them twice).
        g["by_id"][int(r["id"])] = {"item": str(r.get("item")),
                                    "qty": int(r.get("qty") or 0),
                                    "value": int(r.get("qty") or 0) * val}
        # Per-item tally so the harvester's DM can say WHAT they delivered, not just a
        # lump "N pcs" — they sell honey and comb at different values and ask why.
        it = g["items"].setdefault(str(r.get("item")), {"qty": 0, "value": 0.0})
        it["qty"] += int(r.get("qty") or 0)
        it["value"] += int(r.get("qty") or 0) * val
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
            f"Open `/hive settings` to review — with autopay ON they'd have paid "
            f"instantly; otherwise hit **Pay now** there.", ephemeral=True)


class HiveCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._reported_this_boot = False
        self.autopay_sweep_loop.start()

    async def cog_unload(self):
        self.autopay_sweep_loop.cancel()

    # ── periodic autopay sweep ───────────────────────────────────────────────
    # Autopay fires on ingest, but a harvest can sit unpaid for other reasons: the
    # harvester wasn't registered yet, the item had no value at the time, or a
    # payment failed and released its claim. Those only cleared when someone
    # remembered to hit Pay now. This sweeps every 6h so balances can't go
    # stale between CSN runs.
    @tasks.loop(hours=6)
    async def autopay_sweep_loop(self):
        import Restocker_db as _db
        try:
            feeds = _db.get_config_prefix("hive_feed:") or {}
        except Exception as e:
            log.warning("[hive sweep] can't read feeds: %s", e)
            feeds = {}
        # Markets to consider = those with a bound feed channel PLUS any market that
        # simply has unpaid rows. Harvests now also arrive via the CSN export path,
        # which needs no feed channel — discovering markets from `hive_feed:` alone
        # meant those markets were never swept and their stragglers never paid.
        mids = {str(v) for v in feeds.values()}
        try:
            mids |= {str(m) for m in (_db.hive_markets_with_unpaid() or [])}
        except Exception as e:
            log.warning("[hive sweep] unpaid-market scan failed: %s", e)
        for mid in sorted(mids):
            try:
                # AUDIT FIX (medium, 2026-08-06): one shared definition — unset means ON,
                # matching the export path that has actually been paying people. This
                # used to read "== '1'", so an unconfigured market was paid on export but
                # skipped by the sweep, and its unpaid rows piled up forever.
                if not core.hive_autopay_on(mid):
                    continue                      # respect autopay being off
                rows = _db.get_unpaid_hive_harvests(mid)
                if not rows:
                    continue
                groups, unregistered, unvalued = _group_rows(rows)
                if not groups:
                    log.info("[hive sweep] %s: %d unpaid row(s) but none payable "
                             "(unregistered=%s, unvalued=%s)",
                             mid, len(rows), list(unregistered)[:5], list(unvalued)[:5])
                    continue
                res = await self._settle_groups(mid, groups, batch=f"sweep-{mid}")
                log.info("[hive sweep] %s: paid %s in wages on %s value to %d harvester(s)",
                         mid, f"{res['harv_total']:,.0f}", f"{res['value_total']:,.0f}", len(groups))
            except Exception as e:
                log.warning("[hive sweep] %s failed: %s", mid, e)

        # The central hive-project report. tasks.loop runs its first iteration as soon
        # as the bot is ready, so this doubles as the "bot just started" report; every
        # later iteration is the 6-hourly one. Personal DMs go out on the FIRST run
        # only — four statements a day per harvester would be spam.
        try:
            first = not self._reported_this_boot
            self._reported_this_boot = True
            await post_hive_project_report("bot started" if first else "6h sweep")
            if first:
                await dm_harvester_statements()
        except Exception as e:
            log.warning("[hive report] post-sweep reporting failed: %s", e)

    @autopay_sweep_loop.before_loop
    async def _before_autopay_sweep(self):
        await self.bot.wait_until_ready()

    hive = app_commands.Group(
        name="hive",
        description="(Managers) Hive harvesting — the company's perpetual harvest project",
        default_permissions=discord.Permissions(manage_guild=True))

    # ── the payment core (shared by autopay, the sweep and the panel) ────────
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
            # AUDIT FIX (high): CLAIM FIRST, pay after. Two concurrent settle runs
            # (the autopay listener mid-batch + a manager's manual payout) used to
            # snapshot the same unpaid rows and BOTH paid them. The claim is one
            # atomic UPDATE ... WHERE paid=0; whoever claims, pays. A payment that
            # fails releases the claim so the rows stay payable — and value is only
            # BOOKED for rows settled in this run, so a later retry can't book the
            # same production twice.
            #
            # AUDIT FIX (high, 2026-08-06): claim_hive_harvests returns the ids THIS
            # run actually flipped. On a partial claim we now pay for exactly those
            # rows and leave the rest to whoever owns them. The old code released the
            # WHOLE id list on a partial claim, which reset rows the other run had
            # already paid coins for back to unpaid — the next sweep paid them again.
            claimed_ids = _db.claim_hive_harvests(g["ids"])
            if not claimed_ids:
                continue                       # another settle run owns these rows
            if len(claimed_ids) < len(g["ids"]):
                # Recompute this run's share from the rows we actually won. Never
                # touch the rows we lost — they belong to the run that claimed them.
                by_id = g.get("by_id") or {}
                won = [by_id.get(i) for i in claimed_ids if by_id.get(i)]
                g = dict(g)
                g["ids"] = list(claimed_ids)
                g["qty"] = sum(w["qty"] for w in won)
                g["value"] = sum(w["value"] for w in won)
                items = {}
                for w in won:
                    it = items.setdefault(w["item"], {"qty": 0, "value": 0.0})
                    it["qty"] += w["qty"]
                    it["value"] += w["value"]
                g["items"] = items
            pay = int(round(g["value"] * pct / 100.0))
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
                    # Itemised, and it states the rate — "why did I get this much?" is
                    # the question people actually ask, so answer it in the message.
                    breakdown = " · ".join(
                        f"{_fmt(v['qty'])}× {item}"
                        for item, v in sorted(g.get("items", {}).items(),
                                              key=lambda kv: -kv[1]["qty"]))
                    # new_bal comes straight back from add_coins above — no second read,
                    # so the figure quoted is exactly the balance this payment produced.
                    try:
                        bal_line = f"\nYour balance is now **{_fmt(new_bal)}** coins."
                    except Exception:
                        bal_line = ""
                    await safe_dm(user,
                        f"🐝 **Harvest paid — {_fmt(pay)} coins**\n"
                        f"{breakdown or _fmt(g['qty']) + ' pcs'}\n"
                        f"Worth {_fmt(g['value'])} at shop value · your cut {pct:g}% · "
                        f"+{lp} loyalty pts · market `{market_id}`.{bal_line}")
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
            if not core.hive_autopay_on(mid):          # unset = ON; "0" = record-only
                return
            rows = _db.get_hive_harvests_by_ids(new_ids)
            groups, unregistered, _unvalued = _group_rows(rows)
            if groups:
                res = await self._settle_groups(str(mid), groups, batch=str(message.id))
                # ONE LINE. The per-harvester breakdown used to be dumped in full on
                # every single ingest — with several markets exporting on a loop that
                # buried the channel. Who got what is already in each person's coin
                # history, the team-project ledger and /hive settings; the channel only
                # needs to know the run happened and what it was worth.
                receipt = (f"🐝 Harvest run · {len(groups)} harvester(s) · "
                           f"value {_fmt(res['value_total'])} · wages {_fmt(res['harv_total'])}")
                if unregistered:
                    receipt += f" · {len(unregistered)} unlinked IGN(s) held"
                try:
                    await message.channel.send(receipt[:1900],
                                               allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass
            elif unregistered:
                try:
                    await message.channel.send(
                        "🐝 Harvest recorded, but these IGNs aren't linked to Discord yet — "
                        "they'll be paid automatically once they run `/me → Link in-game name`: "
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
    @hive.command(name="settings",
                  description="(Managers) HiveSettings — one panel: feeds, wage, split, values, autopay, payout")
    @app_commands.describe(market_id="Hive site to open on (blank = this channel's site, else the first bound one)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def hive_settings(self, interaction: discord.Interaction, market_id: str = None):
        """One panel replacing bind/unbind/info/payout/set_value/set_wage/set_split."""
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        from views.hive_settings import HiveSettingsView, build_embed, _feeds
        mid = (market_id or "").strip()
        if not mid:
            here = [m for c, m in _feeds() if c == interaction.channel_id]
            bound = sorted({m for _c, m in _feeds()})
            mid = here[0] if here else (bound[0] if bound else core.DEFAULT_MARKET_ID)
        view = HiveSettingsView(self, mid, interaction.user.id)
        await interaction.response.send_message(embed=build_embed(mid), view=view, ephemeral=True)










async def setup(bot):
    await bot.add_cog(HiveCog(bot))
