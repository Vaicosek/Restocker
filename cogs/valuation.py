"""AI valuation — `/valuate <market_id>`.

Auto-gathers a market's earnings (CSN trailing average of CLOSED months), hive
income, teleport-fee income, shop inventory, hive-fleet asset and cash, runs the
V Tech valuation model (earnings x quality-adjusted P/E, held to a conservative
cap; backing sets the credit grade), flags data anomalies, and has Claude write
the analyst report. The goal (owner's rule) is AI valuations with zero human
input once the per-market params are set.

Params are read from bot_config so a valuation is one command. Set them once with
the optional args on `/valuate` (they persist), or leave them and the model uses
what's on record. Everything the model touches is HARD DATA — CSN months, chest
scans, tp fees, config — never a human-typed profit number.
"""
import sys
import os
import json
import asyncio
import calendar
from datetime import datetime, timezone, timedelta, date

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = core.log
is_manager = core.is_manager
_market_autocomplete = core._market_autocomplete
_get_market = core._get_market
_load_csn_for_market = core._load_csn_for_market
_auto_pe = core._auto_pe

# model constants (mirror the pricing engine; overridable via bot_config)
DEF = dict(
    honey_worker_val=2.539063, comb_worker_val=1.953125,     # per-block, worker/production basis
    honey_asset_stack=520.0, comb_asset_stack=450.0, blocks_per_stack=64.0,  # market/asset basis
    wage_pct=0.17, wage_site_share=0.50, tp_fee=100.0, renders_per_day=1.0,
    hive_haircut=0.50, land_haircut=0.65, pe_min=4.0, pe_max=25.0, quality_swing=0.20,
    q_w_backing=0.35, q_w_traffic=0.25, q_w_orders=0.25, q_w_history=0.15,
    q_traffic_target=10000.0, q_history_target=12.0, backing_target_pct=50.0,
    outage_month_threshold=0.40,    # a month this fraction-covered by an outage is dropped from run-rate
    land_rate_per_chunk=10000.0,    # owner's anchor: 10,000 coins/chunk of RAW land
)

# Quality-multiplier presets for value_plot() — how much build/farms/market quality
# lifts a plot above raw land value. Amazonia is the owner's real anchor point:
# 200 chunks x 10k = 2M raw -> assessed 3.5M with build+farms = a 1.75x multiplier.
# Overridable per-tier via bot_config: valuate:land_quality_mult:<tier>.
_LAND_QUALITY_MULT = {
    "raw": 1.0, "modest": 1.25, "developed": 1.5, "premium": 1.75, "flagship": 2.0,
}


def _load_outages(_db) -> list:
    """Global server-outage windows: [{"start","end","reason"}] (ISO dates). A DDoS or
    downtime here must not drag any company's earnings — affected months are dropped."""
    raw = _db.get_config("outage_windows")
    if not raw:
        return []
    try:
        out = []
        for w in json.loads(raw):
            s, e = w.get("start"), w.get("end")
            if s and e:
                out.append({"start": str(s), "end": str(e), "reason": str(w.get("reason", ""))})
        return out
    except Exception:
        return []


def _save_outages(_db, windows: list) -> None:
    _db.set_config("outage_windows", json.dumps(windows))


def _month_outage_fraction(month_key: str, outages: list) -> float:
    """Fraction of a calendar month (e.g. '2026-06') covered by any outage window."""
    try:
        y, m = int(month_key[:4]), int(month_key[5:7])
    except Exception:
        return 0.0
    dim = calendar.monthrange(y, m)[1]
    mstart, mend = date(y, m, 1), date(y, m, dim)
    covered = set()
    for w in outages:
        try:
            sd, ed = date.fromisoformat(w["start"]), date.fromisoformat(w["end"])
        except Exception:
            continue
        a, b = max(mstart, sd), min(mend, ed)
        d = a
        while d <= b:
            covered.add(d)
            d += timedelta(days=1)
    return len(covered) / dim if dim else 0.0


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _gd(_db, key, fallback):
    """Global default from config, else the hard-coded DEF fallback."""
    v = _db.get_config(f"valuate:{key}")
    return _num(v, fallback) if v not in (None, "") else fallback


def _pm(_db, key, market_id, fallback=0.0):
    """Per-market param from config (valuate:<key>:<mid>)."""
    v = _db.get_config(f"valuate:{key}:{market_id}")
    return _num(v, fallback) if v not in (None, "") else fallback


def _grade(pct):
    return ("AAA" if pct >= 80 else "AA" if pct >= 60 else "A" if pct >= 50
            else "BBB" if pct >= 30 else "BB" if pct >= 15 else "C")


def value_plot(chunks: float, quality: str = "raw", comps: "list[float] | None" = None) -> dict:
    """Standalone reserve/listing-price estimate for a plot of land — factored out of
    `gather_and_value`'s land math so the Land Exchange (cogs/land_exchange.py) can
    auto-price a listing without needing a market_id or the full valuation run.

    Same anchor as the land-backing math (owner's number): chunks x 10,000 coins/chunk
    raw, lifted by a quality multiplier for build/farms/market quality (see
    _LAND_QUALITY_MULT — Amazonia's real comp is a 1.75x "premium" plot). If `comps`
    (recent comparable sale prices, e.g. AllMart at 5M) are supplied, the estimate is
    averaged with them so a reserve price is never JUST the formula when real sales
    exist to anchor it.

    This is deliberately independent of DEF['land_haircut'] / the land_claim/backing
    pillar in gather_and_value — that's about how much of an ASSESSED value backs a
    stock's credit grade (65% rule), a different question from what a plot should
    list for. Selling a plot does not change how it backs any company."""
    import Restocker_db as _db
    chunks = max(0.0, _num(chunks))
    quality = (quality or "raw").strip().lower()
    rate = _gd(_db, "land_rate_per_chunk", DEF["land_rate_per_chunk"])
    mult = _gd(_db, f"land_quality_mult:{quality}", _LAND_QUALITY_MULT.get(quality, 1.0))
    raw_value = chunks * rate
    formula_value = raw_value * mult
    comps = [c for c in (comps or []) if _num(c) > 0]
    if comps:
        comp_avg = sum(_num(c) for c in comps) / len(comps)
        assessed_value = round((formula_value + comp_avg) / 2.0, 2)
    else:
        comp_avg = None
        assessed_value = round(formula_value, 2)
    return {
        "chunks": chunks, "quality": quality, "rate_per_chunk": rate, "quality_multiplier": mult,
        "raw_value": round(raw_value, 2), "formula_value": round(formula_value, 2),
        "comps_used": comps, "comp_avg": comp_avg, "assessed_value": assessed_value,
    }


def gather_and_value(market_id: str) -> dict:
    """Pull every input from the live DB/config and compute the full valuation.
    Returns a structured dict (inputs + computed + anomalies) — the same bundle
    that is handed to the AI narrator and returned to the caller."""
    import Restocker_db as _db
    market = _get_market(market_id) or {}
    g = lambda k: _gd(_db, k, DEF[k])
    pm = lambda k, d=0.0: _pm(_db, k, market_id, d)

    # ── earnings: CSN trailing average of CLOSED months ──────────────────────
    months = (_load_csn_for_market(market_id) or {}).get("months", {}) or {}
    cur_key = datetime.now(timezone.utc).strftime("%Y-%m")
    closed = sorted(k for k in months if isinstance(months.get(k), dict) and k < cur_key)
    # Months are dropped from the run-rate when (a) a global server-OUTAGE window covers
    # most of them — downtime must not hurt companies — or (b) the owner excludes them
    # manually: valuate:exclude_months:<mid> = "2026-06,2026-04".
    outages = _load_outages(_db)
    outage_thresh = g("outage_month_threshold")
    excl_raw = _db.get_config(f"valuate:exclude_months:{market_id}") or ""
    manual_excl = {m.strip() for m in excl_raw.split(",") if m.strip()}

    anomalies = []
    nets = []
    excluded_months = []
    for k in closed:
        row_net = _num(months[k].get("net"))
        frac = _month_outage_fraction(k, outages)
        if frac >= outage_thresh:
            excluded_months.append(k)
            anomalies.append(f"Month {k} dropped from the run-rate — {frac*100:.0f}% of it fell in a "
                             f"server-outage window (net {row_net:,.0f}). Downtime doesn't count against "
                             f"the company.")
            continue
        if k in manual_excl:
            excluded_months.append(k)
            anomalies.append(f"Month {k} excluded by the owner (net {row_net:,.0f}) — non-representative.")
            continue
        nets.append(row_net)      # the real recorded net — never fabricated
    window = nets[-3:] if nets else []
    avg_net = sum(window) / len(window) if window else 0.0

    # ── hive income (config-driven production; worker valuation) ─────────────
    honey_r = pm("honey_per_render")
    comb_r = pm("comb_per_render")
    per_render = honey_r * g("honey_worker_val") + comb_r * g("comb_worker_val")
    rpd = pm("renders_per_day", g("renders_per_day"))
    prod_mo = per_render * rpd * 30.0
    site_share = pm("site_share", 0.40)
    wage = g("wage_pct")
    wage_site = g("wage_site_share")
    hive_site_net = prod_mo * site_share - prod_mo * wage * wage_site
    hive_op_net = prod_mo * (1.0 - site_share) - prod_mo * wage * (1.0 - wage_site)

    # ── teleport-fee income ──────────────────────────────────────────────────
    visits_wk = pm("tp_visits_wk")
    tp_fee = g("tp_fee")
    tp_mo = visits_wk * tp_fee * 52.0 / 12.0
    visitors_month = visits_wk * 52.0 / 12.0

    # ── assets & backing components ──────────────────────────────────────────
    # Inventory computed HERE (not via core._market_asset_value) so a NULL-qty legacy row —
    # a per-STACK price that inflates inventory ~64x (the "99M inventory / 383% backed" bug) —
    # is skipped regardless of whether main.py carries the fix. Only rows the scanner captured
    # on a per-UNIT basis (qty present) count; the rest self-heal on the next fresh scan.
    inventory = 0.0
    try:
        for _it, _x in (_db.get_market_stock(market_id) or {}).items():
            if float(_x.get("stock") or 0) <= 0:
                continue
            if _x.get("sell_qty") is not None and _x.get("sell_price") is not None:
                inventory += float(_x["stock"]) * float(_x["sell_price"])
            elif _x.get("buy_qty") is not None and _x.get("buy_price") is not None:
                inventory += float(_x["stock"]) * float(_x["buy_price"])
    except Exception:
        inventory = 0.0
    hive_count = pm("hive_count")
    hive_build_cost = pm("hive_build_cost")     # cumulative build cost per hive at its level
    hive_asset = hive_count * hive_build_cost
    try:
        cash = float(_db.get_treasury(market_id) or 0.0)
    except Exception:
        cash = 0.0
    cash = pm("cash", cash)                     # explicit override wins (bank + land not yet bound)
    barrel_honey = pm("barrel_honey_blocks")
    barrel_comb = pm("barrel_comb_blocks")
    bps = g("blocks_per_stack")
    barrel_val = (barrel_honey * g("honey_asset_stack") / bps
                  + barrel_comb * g("comb_asset_stack") / bps)
    haircut = g("hive_haircut")
    # Land claim: the owner's assessed value of the plot + build/farms (e.g. Amazonia:
    # 200 chunks × 10k = 2M raw, assessed 3.5M with build/farms; AllMart sold at 5M as a comp).
    # Counted at the LAND haircut (0.65) — the owner's stated conservative "65% rule" —
    # so only 65% of the assessment backs the stock. It's an asset, not liquid, so it does
    # NOT feed `sellable`/liquid-backing in list_public; it only lifts the credit grade.
    land_claim = pm("land_claim")
    land_backing = land_claim * g("land_haircut")
    backing = cash + hive_asset * haircut + inventory + barrel_val * haircut + land_backing

    # ── earnings total ───────────────────────────────────────────────────────
    earnings = avg_net + hive_site_net + tp_mo

    # ── P/E: growth base x quality factor (backing + traffic + history) ──────
    pe_base = _auto_pe(nets) if nets else g("pe_min")
    hist_months = len(closed)
    hist_score = min(1.0, hist_months / g("q_history_target")) if g("q_history_target") else 0.0
    # Teleport fees are counted as INCOME (in `earnings`) per the V Tech model, so the traffic
    # quality pillar is held at 0 to avoid double-counting the same tp-fee stream in the multiple.
    traffic_score = 0.0
    wsum = (g("q_w_backing") + g("q_w_traffic") + g("q_w_orders") + g("q_w_history")) or 1.0
    swing = g("quality_swing")
    # `cap` is the owner's stated valuation. When set, the market cap IS that number
    # (their informed call — e.g. normal run-rate despite a one-off outage month) and the
    # earnings-based figure is shown alongside for transparency. When unset, the valuation
    # is purely earnings-driven. Either way the grade is measured against the market cap.
    cap_ceiling = pm("cap", 0.0)   # owner valuation / ceiling; 0 = earnings-driven
    cap = cap_ceiling if cap_ceiling > 0 else max(1.0, earnings * pe_base)
    pe_q = pe_base
    for _ in range(40):
        b_pct = 100.0 * backing / cap if cap > 0 else 0.0
        b_score = min(1.0, b_pct / g("backing_target_pct")) if g("backing_target_pct") else 0.0
        q = (g("q_w_backing") * b_score + g("q_w_traffic") * traffic_score
             + g("q_w_orders") * 0.0 + g("q_w_history") * hist_score) / wsum
        fac = 1.0 - swing + 2.0 * swing * q
        pe_q = round(max(g("pe_min"), min(g("pe_max"), pe_base * fac)), 2)
        uncapped = earnings * pe_q
        cap = cap_ceiling if cap_ceiling > 0 else uncapped
    uncapped = earnings * pe_q
    market_cap = cap_ceiling if cap_ceiling > 0 else uncapped

    listing = None
    try:
        listing = _db.get_market_shares(market_id)
    except Exception:
        pass
    shares = float((listing or {}).get("shares_outstanding") or 1000.0)
    backing_pct = 100.0 * backing / market_cap if market_cap > 0 else 0.0
    grade = _grade(backing_pct)

    # ── remaining anomaly flags ──────────────────────────────────────────────
    if not (market or {}).get("owner_id"):
        anomalies.append("No owner assigned (owner_id is empty) — link the market to its owner.")
    bound = any(str(v) == market_id for k, v in (_db.get_config_prefix("land_map:") or {}).items())
    if not bound and visits_wk <= 0:
        anomalies.append("No land bound and no teleport data — traffic pillar reads 0, capping the "
                         "quality multiple. Bind the land for the traffic upside.")
    if hive_count and not (honey_r or comb_r):
        anomalies.append("Hive fleet recorded but no per-render production set — hive income reads 0.")
    if cap_ceiling and abs(uncapped - cap_ceiling) > 0.05 * cap_ceiling:
        rel = "above" if cap_ceiling > uncapped else "below"
        anomalies.append(f"Owner valuation {cap_ceiling:,.0f} is {rel} the earnings-based figure "
                         f"{uncapped:,.0f} — headline uses the owner valuation. If that gap is a "
                         f"one-off outage month, exclude it so the run-rate agrees.")
    now_partial = months.get(cur_key)
    if isinstance(now_partial, dict):
        anomalies.append(f"Current month {cur_key} is in-progress (net {_num(now_partial.get('net')):,.0f}) "
                         f"— excluded from pricing until it closes.")

    return {
        "market_id": market_id,
        "market_name": market.get("name", market_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "earnings": {
            "closed_months": closed, "monthly_nets": {k: _num(months[k].get("net")) for k in closed},
            "excluded_months": excluded_months, "outage_windows": outages,
            "csn_trailing_avg": round(avg_net, 2),
            "hive_income_site_net": round(hive_site_net, 2),
            "tp_fee_income": round(tp_mo, 2),
            "total_monthly": round(earnings, 2),
        },
        "hive": {
            "count": hive_count, "build_cost_each": hive_build_cost, "fleet_asset": round(hive_asset, 2),
            "per_render_value": round(per_render, 2), "production_monthly": round(prod_mo, 2),
            "site_share": site_share, "site_net_monthly": round(hive_site_net, 2),
            "operator_net_monthly": round(hive_op_net, 2),
            "barrel_honey_blocks": barrel_honey, "barrel_comb_blocks": barrel_comb,
            "barrel_asset_value": round(barrel_val, 2),
        },
        "traffic": {"visits_per_week": visits_wk, "visitors_per_month": round(visitors_month),
                    "tp_fee_income_monthly": round(tp_mo, 2), "land_bound": bound},
        "valuation": {
            "pe_base_growth": pe_base, "pe_quality_adjusted": pe_q, "quality_factor": round(fac, 4),
            "uncapped_value": round(uncapped, 2), "cap_ceiling": cap_ceiling,
            "market_cap": round(market_cap, 2), "shares": shares,
            "share_price": round(market_cap / shares, 2) if shares else None,
        },
        "backing": {
            "cash": round(cash, 2), "hive_fleet_at_haircut": round(hive_asset * haircut, 2),
            "inventory": round(inventory, 2), "barrels_at_haircut": round(barrel_val * haircut, 2),
            "land_claim_assessed": round(land_claim, 2), "land_claim_at_haircut": round(land_backing, 2),
            "land_haircut": g("land_haircut"),
            "total": round(backing, 2), "pct_of_cap": round(backing_pct, 1),
            "target_pct": g("backing_target_pct"), "grade": grade,
        },
        "anomalies": anomalies,
    }


_AI_SYSTEM = (
    "You are V Tech's equity analyst valuing a company on a scam-heavy Minecraft economy server, "
    "where trust is the product and every number must be HARD DATA. You are given a JSON bundle with "
    "a market's computed valuation: CSN trailing earnings, hive income, teleport-fee income, the "
    "quality-adjusted P/E, a conservatively capped market cap, the backing stack and credit grade, and "
    "a list of detected anomalies. Write a concise analyst report in Discord markdown (<1800 chars). "
    "Lead with the headline: market cap, share price, and grade. Then briefly justify the earnings "
    "basis and the backing/grade. Then list the anomalies as blockers with a one-line fix each — treat "
    "any CRITICAL anomaly (e.g. a corrupted/overwritten month) as a hard stop before going public. End "
    "with a one-line recommendation. Do not invent numbers; use only the bundle. Be direct, not fluffy."
)


class ValuationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="valuate",
        description="AI valuation — auto-gathers earnings, hives, traffic & backing, grades the stock")
    @app_commands.describe(
        market_id="Market to value",
        visits_per_week="(optional) weekly teleport visits — persists",
        cash="(optional) cash/treasury backing — persists",
        honey_per_render="(optional) hive honey blocks per render — persists",
        comb_per_render="(optional) hive comb blocks per render — persists",
        hive_count="(optional) number of hives on site — persists",
        hive_build_cost="(optional) build cost per hive at its level — persists",
        barrel_honey="(optional) honey blocks stockpiled in barrels — persists",
        barrel_comb="(optional) comb blocks stockpiled in barrels — persists",
        land_claim="(optional) assessed land/build value — backs the stock at the 65% rule — persists",
        cap="(optional) conservative valuation cap — persists",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def valuate(self, interaction: discord.Interaction, market_id: str,
                      visits_per_week: Optional[float] = None,
                      cash: Optional[float] = None,
                      honey_per_render: Optional[float] = None,
                      comb_per_render: Optional[float] = None,
                      hive_count: Optional[float] = None,
                      hive_build_cost: Optional[float] = None,
                      barrel_honey: Optional[float] = None,
                      barrel_comb: Optional[float] = None,
                      land_claim: Optional[float] = None,
                      cap: Optional[float] = None):
        if not is_manager(interaction) and not core._is_market_manager(interaction, market_id):
            return await interaction.response.send_message(
                "⛔ Managers or this market's owner/managers only.", ephemeral=True)
        market_id = (market_id or "").strip()
        if not _get_market(market_id):
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)

        import Restocker_db as _db
        # optional args persist to config so future runs are zero-input
        for key, val in (("tp_visits_wk", visits_per_week), ("cash", cash),
                         ("honey_per_render", honey_per_render), ("comb_per_render", comb_per_render),
                         ("hive_count", hive_count), ("hive_build_cost", hive_build_cost),
                         ("barrel_honey_blocks", barrel_honey), ("barrel_comb_blocks", barrel_comb),
                         ("land_claim", land_claim), ("cap", cap)):
            if val is not None:
                _db.set_config(f"valuate:{key}:{market_id}", str(float(val)))

        await interaction.response.defer(thinking=True)
        try:
            data = await asyncio.to_thread(gather_and_value, market_id)
        except Exception as e:
            log.exception("valuate: gather failed")
            return await interaction.followup.send(f"⚠️ Valuation failed: `{type(e).__name__}: {e}`")

        v, b = data["valuation"], data["backing"]
        header = (f"📊 **{data['market_name']} — valuation**\n"
                  f"**Market cap `{v['market_cap']:,.0f}` 🪙**"
                  + (f" · `{v['share_price']:,.0f}`/share" if v.get('share_price') else "")
                  + f" · **Grade {b['grade']}** ({b['pct_of_cap']:.0f}% backed)\n"
                  f"Earnings `{data['earnings']['total_monthly']:,.0f}`/mo × P/E `{v['pe_quality_adjusted']}` "
                  + (f"(capped from `{v['uncapped_value']:,.0f}`)" if v['cap_ceiling'] and v['uncapped_value'] > v['market_cap'] else "")
                  + f"\nBacking: cash `{b['cash']:,.0f}` + hive50% `{b['hive_fleet_at_haircut']:,.0f}` + "
                  + f"inv `{b['inventory']:,.0f}` + barrels50% `{b['barrels_at_haircut']:,.0f}`"
                  + (f" + land65% `{b['land_claim_at_haircut']:,.0f}`" if b.get('land_claim_at_haircut') else "")
                  + f" = `{b['total']:,.0f}`")

        # AI narrative (falls back to the deterministic header + anomaly list)
        narrative = None
        client = core._get_anthropic_client()
        if client is not None:
            try:
                def _call():
                    return client.messages.create(
                        model=os.getenv("VALUATE_AI_MODEL", "claude-sonnet-4-6"),
                        max_tokens=1400, system=_AI_SYSTEM,
                        messages=[{"role": "user", "content": json.dumps(data, default=str)}])
                msg = await asyncio.to_thread(_call)
                narrative = "".join(getattr(bl, "text", "") for bl in msg.content).strip()
            except Exception as e:
                log.warning("valuate: AI narrate failed: %s", e)

        if narrative:
            body = f"{header}\n\n{narrative}"
        else:
            flags = "\n".join(f"• {a}" for a in data["anomalies"]) or "• none"
            body = f"{header}\n\n**Flags**\n{flags}"

        await interaction.followup.send(body[:1990])
        # attach the full JSON bundle for the record
        try:
            import io
            buf = io.BytesIO(json.dumps(data, indent=2, default=str).encode("utf-8"))
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            await interaction.followup.send(
                content="Full valuation bundle:",
                file=discord.File(buf, filename=f"valuation_{market_id}_{stamp}.json"))
        except Exception:
            pass


    @app_commands.command(
        name="list_public",
        description="(Manager) Value a market, set its params from the model, and list it on the exchange")
    @app_commands.describe(
        market_id="Market to take public",
        force="Re-run even if it's already listed (re-sets params & price)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def list_public(self, interaction: discord.Interaction, market_id: str, force: bool = False):
        # Listing is consequential on a scam-heavy server — full server managers only.
        if not is_manager(interaction):
            return await interaction.response.send_message(
                "⛔ Server managers only — listing a company is consequential.", ephemeral=True)
        market_id = (market_id or "").strip()
        if not _get_market(market_id):
            return await interaction.response.send_message(f"❌ Market `{market_id}` not found.", ephemeral=True)

        import Restocker_db as _db
        await interaction.response.defer(thinking=True)
        try:
            data = await asyncio.to_thread(gather_and_value, market_id)
        except Exception as e:
            log.exception("list_public: gather failed")
            return await interaction.followup.send(f"⚠️ Valuation failed: `{type(e).__name__}: {e}`")

        try:
            listing = _db.get_market_shares(market_id)
        except Exception:
            listing = None
        if listing and listing.get("active") and not force:
            return await interaction.followup.send(
                f"❌ `{market_id}` is already public at `{float(listing.get('share_price') or 0):,.2f}`/share. "
                f"Pass `force:true` to re-set its params and price from the model.")

        v, b, h = data["valuation"], data["backing"], data["hive"]
        shares = float(v.get("shares") or 1000.0)
        price = v.get("share_price") or (v["market_cap"] / shares if shares else 0.0)
        sellable = float(b["hive_fleet_at_haircut"]) + float(b["barrels_at_haircut"])  # liquid backing
        try:
            # PIN the price: set the book-value floor to the model's (capped) market cap, so the
            # engine's own rule (price >= asset_value / shares) holds the quote at the valuation
            # even though the live engine can't see the hive/TP layers or the corrupt-June fix.
            # sellable = liquid backing (for the grade); treasury = model cash.
            _db.set_config(f"asset_value:{market_id}", str(float(v["market_cap"])))
            _db.set_config(f"sellable_assets:{market_id}", str(sellable))
            _db.upsert_market_shares(
                market_id, active=1, shares_outstanding=shares, pe_multiplier=12.0,
                treasury_coins=float(b["cash"]), share_price=round(float(price), 2),
                last_priced_at=core.utcnow_iso())
            _db.log_stock_price(market_id, round(float(price), 2), "ipo_model")
        except Exception as e:
            log.exception("list_public: write failed")
            return await interaction.followup.send(f"⚠️ Listing writes failed: `{type(e).__name__}: {e}`")

        embed = discord.Embed(
            title=f"📈 {data['market_name']} — now on the exchange",
            description=(f"Listed at **`{float(price):,.0f}`**/share · {shares:,.0f} shares · "
                        f"cap **`{v['market_cap']:,.0f}`** 🪙 · **Grade {b['grade']}** "
                        f"({b['pct_of_cap']:.0f}% backed)"),
            color=0x2ECC71)
        embed.add_field(name="Set from the model",
                        value=(f"asset_value `{v['market_cap']:,.0f}` (pins price)\n"
                               f"sellable `{sellable:,.0f}`\n"
                               f"treasury `{b['cash']:,.0f}`"), inline=True)
        embed.add_field(name="Earnings /mo",
                        value=(f"CSN `{data['earnings']['csn_trailing_avg']:,.0f}`\n"
                               f"hive `{data['earnings']['hive_income_site_net']:,.0f}`\n"
                               f"tp `{data['earnings']['tp_fee_income']:,.0f}`"), inline=True)
        crit = [a for a in data["anomalies"] if a.startswith("CRITICAL")]
        if crit:
            embed.add_field(name="⚠️ Must fix (or the quote drifts)",
                            value="\n".join(f"• {a[len('CRITICAL: '):]}" for a in crit)[:1000], inline=False)
        embed.set_footer(text=f"/stock buy market_id:{market_id} · /market go_private to delist")
        await interaction.followup.send(embed=embed)

    # ── server-outage windows (global; a DDoS/downtime must not hurt companies) ──
    outage = app_commands.Group(
        name="outage", description="(Manager) Server-outage windows excluded from every valuation")

    @outage.command(name="add", description="Record a server-outage window — excluded from all valuations")
    @app_commands.describe(start="Start date (YYYY-MM-DD)", end="End date (YYYY-MM-DD)", reason="What happened (e.g. DDoS)")
    async def outage_add(self, interaction: discord.Interaction, start: str, end: str,
                         reason: Optional[str] = ""):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Server managers only.", ephemeral=True)
        try:
            sd = date.fromisoformat(start.strip())
            ed = date.fromisoformat(end.strip())
        except Exception:
            return await interaction.response.send_message("❌ Dates must be `YYYY-MM-DD`.", ephemeral=True)
        if ed < sd:
            return await interaction.response.send_message("❌ End date is before the start date.", ephemeral=True)
        import Restocker_db as _db
        wins = _load_outages(_db)
        wins.append({"start": sd.isoformat(), "end": ed.isoformat(), "reason": (reason or "").strip()})
        wins.sort(key=lambda w: w["start"])
        _save_outages(_db, wins)
        days = (ed - sd).days + 1
        thr = int(_gd(_db, "outage_month_threshold", DEF["outage_month_threshold"]) * 100)
        await interaction.response.send_message(
            f"✅ Outage recorded: **{sd} → {ed}** ({days}d)"
            + (f" · {reason}" if reason else "")
            + f"\nAny month ≥{thr}% inside an outage now drops out of every company's run-rate.",
            ephemeral=True)

    @outage.command(name="list", description="Show recorded server-outage windows")
    async def outage_list(self, interaction: discord.Interaction):
        import Restocker_db as _db
        wins = _load_outages(_db)
        if not wins:
            return await interaction.response.send_message("No outage windows recorded.", ephemeral=True)
        lines = []
        for i, w in enumerate(wins):
            try:
                d = f"{(date.fromisoformat(w['end']) - date.fromisoformat(w['start'])).days + 1}d"
            except Exception:
                d = "?"
            lines.append(f"`{i}` **{w['start']} → {w['end']}** ({d})"
                         + (f" · {w['reason']}" if w.get("reason") else ""))
        await interaction.response.send_message("🛑 **Server-outage windows**\n" + "\n".join(lines), ephemeral=True)

    @outage.command(name="remove", description="Remove an outage window by index (see /outage list)")
    @app_commands.describe(index="Index shown by /outage list")
    async def outage_remove(self, interaction: discord.Interaction, index: int):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Server managers only.", ephemeral=True)
        import Restocker_db as _db
        wins = _load_outages(_db)
        if index < 0 or index >= len(wins):
            return await interaction.response.send_message(f"❌ No outage window at index {index}.", ephemeral=True)
        removed = wins.pop(index)
        _save_outages(_db, wins)
        await interaction.response.send_message(
            f"🗑️ Removed outage **{removed['start']} → {removed['end']}**.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(ValuationCog(bot))
