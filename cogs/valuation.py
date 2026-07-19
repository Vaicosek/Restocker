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
from datetime import datetime, timezone

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
    hive_haircut=0.50, pe_min=4.0, pe_max=25.0, quality_swing=0.20,
    q_w_backing=0.35, q_w_traffic=0.25, q_w_orders=0.25, q_w_history=0.15,
    q_traffic_target=10000.0, q_history_target=12.0, backing_target_pct=50.0,
)


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
    nets = [_num(months[k].get("net")) for k in closed]
    window = nets[-3:] if nets else []
    avg_net = sum(window) / len(window) if window else 0.0

    anomalies = []
    # anomaly: a stored month whose net disagrees with its csn_meta fingerprint
    for k in closed:
        raw = _db.get_config(f"csn_meta:{market_id}:{k}")
        if not raw:
            continue
        try:
            meta_net = _num(json.loads(raw).get("net"))
        except Exception:
            continue
        row_net = _num(months[k].get("net"))
        if meta_net and abs(meta_net - row_net) > max(10000.0, 0.10 * abs(meta_net)):
            anomalies.append(
                f"CRITICAL: month {k} stored net {row_net:,.0f} disagrees with its "
                f"csn_meta fingerprint {meta_net:,.0f} — the month was likely overwritten "
                f"by a partial/stub re-import. Re-import the real month before pricing.")

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
    try:
        inventory = float(core._market_asset_value(market_id) or 0.0)
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
    backing = cash + hive_asset * haircut + inventory + barrel_val * haircut

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
    cap_ceiling = pm("cap", 0.0)   # 0 = uncapped

    # backing_score depends on cap, cap depends on quality -> iterate to a fixed point
    cap = max(1.0, earnings * pe_base)
    pe_q = pe_base
    for _ in range(40):
        b_pct = 100.0 * backing / cap if cap > 0 else 0.0
        b_score = min(1.0, b_pct / g("backing_target_pct")) if g("backing_target_pct") else 0.0
        q = (g("q_w_backing") * b_score + g("q_w_traffic") * traffic_score
             + g("q_w_orders") * 0.0 + g("q_w_history") * hist_score) / wsum
        fac = 1.0 - swing + 2.0 * swing * q
        pe_q = round(max(g("pe_min"), min(g("pe_max"), pe_base * fac)), 2)
        uncapped = earnings * pe_q
        cap = min(uncapped, cap_ceiling) if cap_ceiling > 0 else uncapped
    uncapped = earnings * pe_q
    market_cap = min(uncapped, cap_ceiling) if cap_ceiling > 0 else uncapped

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
    if cap_ceiling and uncapped > cap_ceiling:
        anomalies.append(f"Uncapped value {uncapped:,.0f} exceeds the conservative cap "
                         f"{cap_ceiling:,.0f}; headline held at the cap.")
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
        visits_per_week="(optional) set weekly teleport visits — persists",
        cash="(optional) set cash/treasury backing — persists",
        honey_per_render="(optional) set hive honey blocks per render — persists",
        comb_per_render="(optional) set hive comb blocks per render — persists",
        cap="(optional) set a conservative valuation cap — persists",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def valuate(self, interaction: discord.Interaction, market_id: str,
                      visits_per_week: Optional[float] = None,
                      cash: Optional[float] = None,
                      honey_per_render: Optional[float] = None,
                      comb_per_render: Optional[float] = None,
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
                         ("cap", cap)):
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
                  f"inv `{b['inventory']:,.0f}` + barrels50% `{b['barrels_at_haircut']:,.0f}` = `{b['total']:,.0f}`")

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


async def setup(bot):
    await bot.add_cog(ValuationCog(bot))
