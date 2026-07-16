#!/usr/bin/env python3
"""One-shot V Tech / GEX MERGER go-live. Everything is baked into this file — the two
Crimson cap-table exports (July 4, 2026), the liquidation list, and the merger terms —
so nothing needs pasting into Discord.

Merger terms
    V Tech + GEX -> V Tech, valued at 100,000,000 coins = 100,000 shares @ 1,000.
    V Tech side (hives):  65%  -> 65,000 new shares to the company owner (vaicos)
    GEX side:             35%  -> GEX's 39,500 common convert pro-rata into 35,000
                                  (1 GEX share = 0.886076 V Tech shares)

Liquidation (gone-for-good holders — equity returns to the company first)
    Kovač & Luxemburg    83 common          (left)
    Daffiest_Owl        400 common          (no longer playing)
    Frangoooo            39 common + 20 pref (perma banned)
    JonasBubble7455              40 pref    (left server)
    Common shares reroute to vaicos BEFORE conversion. Preferred holders are dropped
    from the register but pcts stay derived from the full 500 — the company keeps
    their 12% payout slice; the remaining 5 investors' pcts (summing 88%) don't inflate.

What the script does (idempotent — a second run changes nothing):
    1. Investor register  <- GEX.PR minus liquidated, pct over the full 500.
    2. Liquidation list   <- saved to bot config so future /investor sync and
                             /stock import_captable reroute these people automatically.
    3. Exchange listing   <- market public, 100,000 shares @ 1,000 (cap = 100M).
    4. Shareholder book   <- converted GEX holdings + the 65,000 V Tech-side shares.
       Set to MATCH exactly; stale holders zeroed; cost basis 0.
    5. Holder names       <- data/state/stock_names.yml for the website cap table.

How to run — pick one:
    • Discord (no console access needed): /stock golive market_id — previews by
      default, add apply:True to write it. The cog imports run() from this module.
    • CLI fallback (needs shell access to the live restocker.db):
          python3 golive_gex.py <market_id> --dry-run
          python3 golive_gex.py <market_id>

Afterwards, in Discord:
    /investor apply_roles          (Investor role, 5 active preferred holders)
    /stock apply_roles <market_id> (Shareholder role, all common holders)

This module does NOT touch coins, orders, loyalty, or anything else. Once the
merger has been applied on the live bot, this file and /stock golive can be
deleted — they're one-shot.
"""
import os
import re
import sys

# ── merger terms ─────────────────────────────────────────────────────────────
VALUATION = 100_000_000.0     # coins
TOTAL_SHARES = 100_000.0      # -> share price 1,000
VTECH_SIDE_SHARES = 65_000.0  # 65% (hives) — issued to COMPANY_UID
GEX_SIDE_SHARES = 35_000.0    # 35% — GEX common converts into these pro-rata
COMPANY_UID = "1203738126850461738"   # vaicos — receives V Tech side + liquidated stock

# ── liquidation list (equity back to the company; saved to bot config too) ──
LIQUIDATED = {
    "444443005630414848": "left",                # Kovač & Luxemburg — 83 common
    "868682069054726204": "no longer playing",   # Daffiest_Owl — 400 common
    "1105210916284407922": "perma banned",       # Frangoooo — 39 common + 20 pref
    "971388531157131294": "left server",         # JonasBubble7455 — 40 pref
}

# ── Crimson cap-table exports, as posted by Crimson BankingAPP 2026-07-04 ────
GEX_COMMON = """
10001,429708337039278101,Crimson Vault,93,
10771,1203738126850461738,vaicos,36000,
10537,558683020471697408,Fikcraft,799,
10638,1088884636932907129,potenjoyer1975,302,
10778,1001928279218995230,VanFon_Dood,14,
10767,661028063290851332,Civic5,10,
10834,1141712452552249425,Explifyim,10,
10804,425771067412578325,Krasnoi,788,
10729,346427660265717771,Dr_Plague,199,
10847,1125953712955850842,Sargent1010,38,
10069,427577426978275354,Trollerio,20,
10868,557221599305269249,HAMOD_DAD,100,
10096,429708337039278101,PixDeVl,61,
10870,546749635180625950,Hatchet_Doctor,1,
10869,180182849381466113,Maestro Inc.,3,
10858,1361129957505499157,yellowavocado15,3,
10599,1060724136822984774,Aetorox,23,
10431,444443005630414848,Kovač & Luxemburg,83,
10675,1105210916284407922,Frangoooo,39,
10874,323621854205968385,Hamburger___Man,81,
10919,868682069054726204,Daffiest_Owl,400,
10972,180182849381466113,Maestro Master Fund,61,
10588,415939818569334794,Typhon332,1,
11019,793202922707222559,__Duster,24,
11052,706234265951535105,FerdiBerdi,100,
10698,517082724104929330,FNLonely / Personal,30,
10950,1291112370206150777,Unclepabloo,197,
11015,1291112370206150777,Dutch republic investment fund,20,
"""

GEX_PREFERRED = """
10001,429708337039278101,Crimson Vault,221,
10638,1088884636932907129,potenjoyer1975,39,
10798,971388531157131294,JonasBubble7455,40,
10858,1361129957505499157,yellowavocado15,4,
10108,267804753319297035,jJoshuaTheGreat,10,
10869,180182849381466113,Maestro Inc.,111,
10675,1105210916284407922,Frangoooo,20,
10972,180182849381466113,Maestro Master Fund,55,
"""


def parse_captable(text):
    """`account,discord_id,name,shares,` lines -> [(discord_id, name, shares)],
    aggregated per Discord id, keeping the larger holding's entity name.
    (Copy of Restocker_main._parse_crimson_captable — this script must not import
    Restocker_main, which would start the web server.)"""
    agg = {}
    for raw in (text or "").splitlines():
        ln = raw.strip().strip("`").strip()
        if not ln:
            continue
        m = re.match(r'^\d+\s*,\s*(\d{17,20})\s*,\s*(.+?)\s*,\s*([\d.]+)\s*,?\s*$', ln)
        if not m:
            continue
        uid, name, shares = m.group(1), m.group(2), float(m.group(3))
        if uid in agg:
            prev_name, prev_sh = agg[uid]
            agg[uid] = (prev_name if prev_sh >= shares else name, prev_sh + shares)
        else:
            agg[uid] = (name, shares)
    return [(uid, nm, sh) for uid, (nm, sh) in agg.items()]


def _names_file():
    """Same routing as Restocker_main._resolve_data_file for stock_names.yml."""
    organized = os.path.join("data", "state", "stock_names.yml")
    if os.path.exists(organized):
        return organized
    if os.path.exists("stock_names.yml"):
        return "stock_names.yml"
    return organized


def run(market_id, dry=False):
    """Execute (or preview, dry=True) the merger import against the CURRENT process's
    database via Restocker_db. Returns the report as a list of lines. Raises ValueError
    for an unknown market. Idempotent — a second apply changes nothing."""
    import Restocker_db as db
    lines = []
    out = lines.append

    with db.db() as conn:
        known = [r["market_id"] for r in conn.execute("SELECT market_id FROM markets")]
    if market_id not in known:
        raise ValueError(f"market `{market_id}` not found. Known markets: {', '.join(known) or '(none)'}")

    mode = "DRY RUN — nothing will be written" if dry else "APPLYING"
    price = VALUATION / TOTAL_SHARES
    out(f"=== V Tech / GEX merger go-live on `{market_id}` ({mode}) ===")
    out(f"    valuation {VALUATION:,.0f} = {TOTAL_SHARES:,.0f} shares @ {price:,.0f}"
        f"  ·  V Tech side {VTECH_SIDE_SHARES:,.0f} (65%)  ·  GEX side {GEX_SIDE_SHARES:,.0f} (35%)\n")

    # 1) investor register (GEX.PR minus liquidated, pct over the full total) --
    pref_all = parse_captable(GEX_PREFERRED)
    pref_total = sum(sh for _, _, sh in pref_all)
    pref_kept = [(u, n, s) for u, n, s in pref_all if u not in LIQUIDATED]
    pref_liq = [(u, n, s) for u, n, s in pref_all if u in LIQUIDATED]
    out(f"[1/5] Investor register — GEX.PR: {len(pref_kept)} active investor(s) "
          f"of {pref_total:,.0f} preferred")
    for uid, name, sh in sorted(pref_kept, key=lambda r: -r[2]):
        out(f"      {name:<28} {sh:>6,.0f}  ({100.0 * sh / pref_total:5.1f}%)  <@{uid}>")
    for uid, name, sh in pref_liq:
        out(f"      {name:<28} {sh:>6,.0f}  LIQUIDATED ({LIQUIDATED[uid]}) — company keeps this slice")
    kept_pct = 100.0 * sum(s for _, _, s in pref_kept) / pref_total
    out(f"      -> payouts distribute {kept_pct:.0f}% of each pool; V Tech keeps {100 - kept_pct:.0f}%")
    if not dry:
        db.replace_investors(pref_kept, total_shares=pref_total)

    # 2) liquidation list -> bot config (future syncs/imports reroute them) ----
    out(f"\n[2/5] Liquidation list — saving {len(LIQUIDATED)} marked holder(s) to bot config")
    if not dry:
        import json as _json
        try:
            cur = _json.loads(db.get_config("liquidated_holders") or "{}") or {}
        except Exception:
            cur = {}
        cur.update(LIQUIDATED)
        db.set_config("liquidated_holders", _json.dumps(cur, ensure_ascii=False))

    # 3) exchange listing -------------------------------------------------------
    out(f"\n[3/5] Listing — `{market_id}` public: {TOTAL_SHARES:,.0f} shares @ {price:,.0f} "
        f"(market cap {VALUATION:,.0f})")
    if not dry:
        db.upsert_market_shares(market_id, active=1,
                                shares_outstanding=float(TOTAL_SHARES),
                                share_price=float(price))
        db.log_stock_price(market_id, float(price), "vtech_gex_merger")

    # 4) shareholder book: liquidate -> convert -> add V Tech side -------------
    common = parse_captable(GEX_COMMON)
    gex_total = sum(sh for _, _, sh in common)
    reclaimed = sum(sh for uid, _, sh in common if uid in LIQUIDATED)
    pre = {}
    names = {}
    for uid, name, sh in common:
        tgt = COMPANY_UID if uid in LIQUIDATED else uid
        pre[tgt] = pre.get(tgt, 0.0) + float(sh)
        names.setdefault(uid, name)
    out(f"\n[4/5] Holdings — GEX {gex_total:,.0f} common: {reclaimed:,.0f} liquidated -> company, "
        f"then converted x{GEX_SIDE_SHARES / gex_total:.6f}, + {VTECH_SIDE_SHARES:,.0f} V Tech side")
    ratio = GEX_SIDE_SHARES / gex_total
    target = {}
    for uid, sh in pre.items():
        if uid == COMPANY_UID:
            continue
        target[uid] = round(sh * ratio, 2)
    target[COMPANY_UID] = round(GEX_SIDE_SHARES - sum(target.values()), 2) + VTECH_SIDE_SHARES
    if abs(sum(target.values()) - TOTAL_SHARES) >= 0.01:
        raise RuntimeError("share total drifted — aborting before any write")

    current = {str(h.get("user_id")): float(h.get("shares") or 0)
               for h in (db.get_holders(market_id) or [])}
    changed = same = 0
    disp = dict(names)
    disp[COMPANY_UID] = "vaicos"
    for uid, sh in sorted(target.items(), key=lambda kv: -kv[1]):
        gex_was = pre.get(uid, 0.0)
        cur = current.pop(uid, 0.0)
        label = disp.get(uid, uid)
        if abs(sh - cur) > 1e-9:
            note = f"(GEX {gex_was:,.0f} + V Tech side)" if uid == COMPANY_UID else f"(GEX {gex_was:,.0f})"
            out(f"      {label:<28} {cur:>10,.2f} -> {sh:>10,.2f}  {note}")
            if not dry:
                db.adjust_holding(uid, market_id, delta_shares=sh - cur, delta_cost_basis=0.0)
            changed += 1
        else:
            same += 1
    zeroed = 0
    for uid, cur in current.items():
        if cur > 0:
            out(f"      <@{uid}>  {cur:,.2f} -> 0  (not on the export — zeroed)")
            if not dry:
                db.adjust_holding(uid, market_id, delta_shares=-cur, delta_cost_basis=0.0)
            zeroed += 1
    out(f"      adjusted {changed}, unchanged {same}, zeroed {zeroed} "
        f"| owner {target[COMPANY_UID]:,.2f} shares = {100.0 * target[COMPANY_UID] / TOTAL_SHARES:.2f}%")

    # 5) holder display names for the website -----------------------------------
    for uid, name, _sh in pref_all:
        names.setdefault(uid, name)
    names_path = _names_file()
    out(f"\n[5/5] Names — writing {len(names)} display name(s) to {names_path}")
    if not dry:
        try:
            import yaml
            existing = {}
            if os.path.exists(names_path):
                with open(names_path, "r", encoding="utf-8") as f:
                    existing = yaml.safe_load(f) or {}
            for uid, name in names.items():
                existing.setdefault(str(uid), name)
            d = os.path.dirname(names_path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = names_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(existing, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
            os.replace(tmp, names_path)
        except Exception as e:
            out(f"      WARNING: couldn't write names ({e}) — cap table will show IDs until trades happen")

    out("\nDone." + ("" if dry else f"""
Next (in Discord):
    /investor apply_roles            — Investor role for the {len(pref_kept)} active preferred holders
    /stock apply_roles {market_id}   — Shareholder role for all common holders
Verify on the website: Exchange tab shows the market at {price:,.0f}/share, cap {VALUATION / 1e6:,.0f}M."""))
    return lines


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = any(a in ("--dry-run", "-n") for a in sys.argv[1:])
    if not args:
        print(__doc__)
        print("ERROR: pass the market id, e.g.  python3 golive_gex.py main")
        return 2
    if not os.path.exists("restocker.db"):
        print("ERROR: restocker.db not found in the current folder.")
        print("Run this from the bot's folder (the one with Restocker_main.py) on the")
        print("machine that hosts the LIVE database — running it anywhere else would")
        print("silently create a new, empty database.")
        return 2
    try:
        print("\n".join(run(args[0], dry=dry)))
    except (ValueError, RuntimeError) as e:
        print(f"ERROR: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
