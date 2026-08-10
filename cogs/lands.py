"""Lands (claims) tracking — consumes the CSN mod's LANDS FEED webhook posts.

The mod forwards two line types (see LandTracker.java):
    LANDS-BAL|<land>|<balance>|<iso timestamp>
    LANDS-ENTRY|<land>|#<n>|<MM/DD/YYYY HH:MM>|<entry text ... New balance: $X>

What this cog does with them:
  1. Stores every entry (idempotent — the mod already dedups, we dedup again by PK).
  2. TREASURY SYNC: a bound land's latest balance auto-updates its market's treasury
     (the exchange's TOTAL TREASURY and the backing rating stay live).
  3. TELEPORT FEES BY MATH (the owner's spec): fees never appear as inbox entries, so
     they are inferred as the unexplained gap between consecutive known balances:
         expected_prev = new_balance(entry N) − amount(entry N)
         fees between N−1 and N = expected_prev − new_balance(entry N−1)
     plus the gap between the newest entry and the live LANDS-BAL snapshot. Recomputed
     from scratch on every ingest (idempotent), bucketed per YYYY-MM.
"""
import re
import sys

import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
is_manager = core.is_manager
_market_autocomplete = core._market_autocomplete
log = core.log

_BAL_RX = re.compile(r"^LANDS-BAL\|([^|]+)\|([\d.]+)\|")
_ENTRY_RX = re.compile(r"^LANDS-ENTRY\|([^|]+)\|#(\d+)\|([\d/]+\s+[\d:]+)\|(.+)$")
_MONEY_RX = re.compile(r"\$([\d,]+(?:\.\d+)?)")
_NEWBAL_RX = re.compile(r"(?i)new balance:\s*\$([\d,]+(?:\.\d+)?)")


def _money(s) -> float:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _classify(body: str):
    """(kind, signed_amount) of an inbox entry's effect on the land balance."""
    low = body.lower()
    m = _MONEY_RX.search(body)
    amt = _money(m.group(1)) if m else 0.0
    if "deposited" in low:
        return "deposit", amt
    if "withdrew" in low or "withdrawn" in low:
        return "withdraw", -amt
    if "taxes" in low and ("received" in low or "total" in low):
        return "taxes", amt
    return "other", 0.0


def _month_of(ts: str) -> str:
    """'07/15/2026 11:00' → '2026-07'."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", ts.strip())
    return f"{m.group(3)}-{m.group(1)}" if m else "unknown"


def _lands_feed_channels() -> set:
    """The channel ids LANDS FEED posts are accepted from, as a set.

    Config `lands_feed_channel` holds a comma-separated list. A bare single id still
    parses (it is just a one-element list), so existing setups keep working untouched.
    An empty value means UNLOCKED — any webhook anywhere is accepted, which is flagged
    loudly at the call site because land balances drive market treasuries.
    """
    import Restocker_db as _db
    out = set()
    try:
        raw = str(_db.get_config("lands_feed_channel") or "")
    except Exception:
        return out
    for part in raw.replace(";", ",").split(","):
        part = part.strip().strip("<#>")
        if part.isdigit():
            out.add(int(part))
    return out


def _land_market(land: str) -> str:
    import Restocker_db as _db
    return str(_db.get_config(f"land_map:{land.lower()}") or "").strip()


def _recompute_fees(land: str) -> dict:
    """Rebuild the land's inferred monthly fees from the stored chain. Returns {month: fees}."""
    import Restocker_db as _db
    entries = [e for e in _db.get_land_entries(land)]
    fees: dict = {}
    prev_bal = None
    prev_seen = False
    for e in entries:
        nb = e.get("new_balance")
        if nb is None:
            continue                       # membership entries carry no balance
        nb = float(nb)
        if prev_seen:
            expected_prev = nb - float(e.get("amount") or 0.0)
            gap = expected_prev - prev_bal
            if gap > 0.005:                # positive unexplained income = fees
                mk = _month_of(e.get("ts") or "")
                fees[mk] = fees.get(mk, 0.0) + gap
        prev_bal = nb
        prev_seen = True
    # tail: live balance snapshot vs the newest entry's balance
    snap = _db.get_land_balance(land)
    if snap is not None and prev_seen:
        gap = float(snap.get("balance") or 0.0) - prev_bal
        if gap > 0.005:
            from datetime import datetime, timezone
            mk = datetime.now(timezone.utc).strftime("%Y-%m")
            fees[mk] = fees.get(mk, 0.0) + gap
    _db.replace_land_fees(land, fees)
    return fees


class LandsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── feed listener: AUTHENTICATED LANDS FEED ingest ───────────────────────
    # House rule: treasuries are hard data. A forged LANDS-BAL line would set a
    # market's treasury (and thus dividends) to whatever an attacker types, so
    # feed posts are only accepted from a WEBHOOK (regular members can't post as
    # one without Manage Webhooks) and, when config `lands_feed_channel` is set,
    # only in that channel. Lock it down by asking the bot (set_lands_feed_channel).
    @commands.Cog.listener()
    async def on_message(self, message):
        try:
            content = message.content or ""
            if "LANDS FEED" not in content.split("\n", 1)[0]:
                return
            if message.author and self.bot.user and message.author.id == self.bot.user.id:
                return
            if message.webhook_id is None:
                log.warning("[lands] REJECTED non-webhook LANDS FEED from user %s in #%s",
                            getattr(message.author, "id", "?"),
                            getattr(message.channel, "name", "?"))
                return
            import Restocker_db as _db
            # One lock value, but a SET of channels. Each market's owner runs their own
            # copy of the mod and posts into their own CSN channel, so a single-channel
            # lock meant exactly one market could ever be tracked — every other feed was
            # rejected, left undeleted (nothing ingests it, so nothing cleans it up), and
            # that market's treasury silently never updated. Stored comma-separated.
            _allowed = _lands_feed_channels()
            if _allowed and message.channel.id not in _allowed:
                log.warning("[lands] REJECTED LANDS FEED in unauthorized channel %s "
                            "(allowed: %s)", message.channel.id,
                            ",".join(str(c) for c in sorted(_allowed)) or "none")
                return
            # SECURITY: no channel lock configured means ANY webhook in ANY channel can
            # post LANDS-BAL/LANDS-ENTRY lines that overwrite a bound market's treasury —
            # this is exactly how another market's CSN-mod client (misconfigured land name)
            # can corrupt YOUR treasury unnoticed. Still ingest (don't break a currently
            # working setup) but flag it loudly every time so it can't go unnoticed.
            unlocked = not _allowed
            await self._ingest(message, content, unlocked=unlocked)
        except Exception as e:
            log.warning("[lands] feed ingest failed: %s", e)

    async def _ingest(self, message, content: str, unlocked: bool = False):
        import Restocker_db as _db
        touched = set()
        balances = {}
        new_entries = 0
        batch = {}          # land → kind → [count, signed_total] for THIS ingest's new rows
        for line in content.split("\n"):
            line = line.strip()
            mb = _BAL_RX.match(line)
            if mb:
                land = mb.group(1).strip()
                balances[land] = _money(mb.group(2))
                touched.add(land)
                continue
            me = _ENTRY_RX.match(line)
            if me:
                land, no, ts, body = (me.group(1).strip(), int(me.group(2)),
                                      me.group(3).strip(), me.group(4).strip())
                kind, amt = _classify(body)
                nb_m = _NEWBAL_RX.search(body)
                nb = _money(nb_m.group(1)) if nb_m else None
                if _db.add_land_entry(land, no, ts, kind, amt, nb, body):
                    new_entries += 1
                    b = batch.setdefault(land, {}).setdefault(kind, [0, 0.0])
                    b[0] += 1
                    b[1] += amt
                touched.add(land)
        if not touched:
            return

        report = []
        for land, bal in balances.items():
            _db.set_land_balance(land, bal)
        for land in sorted(touched):
            fees = _recompute_fees(land)
            mid = _land_market(land)
            line = f"🏦 **{land}**"
            snap = _db.get_land_balance(land)
            if snap:
                line += f" — balance `{float(snap['balance']):,.0f}`"
            if fees:
                total = sum(fees.values())
                line += f" · inferred teleport fees `{total:,.0f}` ({len(fees)} month(s))"
            if mid:
                # Treasury sync: the land IS the market's treasury.
                if snap:
                    _db.upsert_market_shares(mid, treasury_coins=float(snap["balance"]))
                    core._recompute_share_price(mid, reason="land_treasury")
                    line += f" → treasury of `{mid}` updated"
            else:
                line += " · *(unbound — `/my market` → Edit links it)*"
            report.append(line)
            # Human digest of what this batch actually contained — the raw pipe lines
            # are deleted below, so the story ("18 withdrawals −1,644,550") lives here.
            b = batch.get(land) or {}
            bits = []
            _KINDS = (("deposit", "deposit(s)"), ("withdraw", "withdrawal(s)"),
                      ("taxes", "tax collection(s)"), ("other", "member/other event(s)"))
            for kind, label in _KINDS:
                if kind in b:
                    cnt, tot = b[kind]
                    if kind == "other" or abs(tot) < 0.005:
                        bits.append(f"{cnt} {label}")
                    else:
                        bits.append(f"{cnt} {label} `{tot:+,.0f}`")
            if bits:
                report.append("   ↳ " + " · ".join(bits))
        # SILENT BY DEFAULT: the feed is machine transport — the bot ingests it, the
        # treasury/fee numbers update, the raw dump is deleted below, and NOTHING is
        # posted. The channel stays clean; the data lives on the dashboard and in
        # /my market. Set config lands_feed_verbose=1 if you ever want the
        # per-ingest summary card back.
        verbose = False
        try:
            verbose = str(_db.get_config("lands_feed_verbose") or "") == "1"
        except Exception:
            pass
        if unlocked:
            # Still worth a log line even when mute — an unlocked feed means any
            # webhook anywhere can write treasuries.
            log.warning("[lands] feed ingested from an UNLOCKED channel (%s) — ask the "
                        "bot to lock the lands feed channel.", message.channel.id)
        if new_entries and verbose:
            try:
                warn = ("⚠️ **No lands-feed channel is locked** — this was accepted from "
                         "**any** webhook, including ones belonging to other markets. "
                         "Ask the bot to lock the feed channel to restrict ingestion to your "
                         "official feed channel and close this gap.") if unlocked else ""
                emb = discord.Embed(
                    title="🏦 Lands feed",
                    description="\n".join(report)[:3900],
                    color=0xC9A227)
                emb.set_footer(text=f"{new_entries} new ledger entrie(s) · "
                                    f"{len(balances)} balance snapshot(s)")
                await message.channel.send(content=(warn or None), embed=emb,
                                           allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                pass
        if new_entries or balances:
            # De-clutter: the raw LANDS-ENTRY|…|… pipe dump is machine transport, not
            # something anyone should read — once ingested, delete it so only the card
            # above remains (exactly like CSN CSV uploads). Opt out with config
            # lands_keep_uploads=1. Needs Manage Messages in the feed channel.
            try:
                if str(_db.get_config("lands_keep_uploads") or "") != "1":
                    await message.delete()
            except discord.Forbidden:
                log.warning("[lands] can't delete the raw feed post in #%s — I need Manage "
                            "Messages there. The feed was ingested fine, it just stays visible.",
                            getattr(message.channel, "name", message.channel.id))
            except Exception as e:
                log.warning("[lands] raw-feed cleanup skipped: %s", e)

    # ── commands ─────────────────────────────────────────────────────────────





async def setup(bot):
    await bot.add_cog(LandsCog(bot))
